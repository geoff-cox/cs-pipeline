"""
Public ingest API for the PROTEUS pipeline.

The top-level entry point is `ingest_file`, which dispatches to the
right format-specific parser based on the file extension and writes
results to the database. The format-specific parsers (datashop,
runestone) are also importable directly for callers that want finer
control.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from proteus.ingest import datashop, runestone
from proteus.ingest.common import (
    Response,
    file_hash,
    register_import,
    write_responses,
)

log = logging.getLogger(__name__)

__all__ = ["ingest_file", "IngestResult", "Response"]

# Sentinel distinguishing "notes argument omitted" from an explicit value.
# Legacy callers (and the CLI's `ingest` subcommand) omit notes and expect
# the question_id-derived marker; an explicit None or "" means "store no
# note" and must override that fallback — otherwise a UI user who clears the
# notes field on a *filtered* ingest would still get the generated marker.
_NOTES_OMITTED: object = object()


@dataclass(frozen=True)
class IngestResult:
    """Summary of one ingest run."""

    import_id: int
    file_path: Path
    file_format: str
    inserted: int
    skipped: int
    total_seen: int


def ingest_file(
    conn: sqlite3.Connection,
    file_path: str | Path,
    question_id: str | None = None,
    file_format: str | None = None,
    notes: str | None = _NOTES_OMITTED,  # type: ignore[assignment]
) -> IngestResult:
    """
    Ingest one export file. Returns an IngestResult.

    file_format is auto-detected from the file extension if not given:
    .tab is DataShop, .csv is Runestone. Pass it explicitly to override.

    If question_id is given, only responses to that question are
    ingested. If None, every short-answer response in the file is
    ingested.

    notes is stored verbatim in raw_imports.notes. An explicit value
    (including None) is honored as-is; only when notes is *omitted* does
    the import note fall back to a question_id marker (the prior behavior),
    so existing callers and the CLI are unaffected.

    The whole import runs in a single transaction. If any step fails,
    the database is left unchanged. Re-running the same file is safe:
    duplicate responses are skipped, not re-inserted.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(path)

    if file_format is None:
        file_format = _detect_format(path)

    parser = _PARSERS.get(file_format)
    if parser is None:
        raise ValueError(
            f"Unknown file_format {file_format!r}; "
            f"known: {sorted(_PARSERS)}"
        )

    # Refuse re-importing the exact same file content (by hash). This is
    # cheap protection against accidental double-ingests; it does not
    # prevent re-importing a file that has been re-exported and now has
    # different content but overlapping rows (the per-row dedup in
    # write_responses handles that case).
    fhash = file_hash(path)
    existing = conn.execute(
        "SELECT import_id FROM raw_imports WHERE file_hash = ?", (fhash,)
    ).fetchone()
    if existing is not None:
        raise FileExistsError(
            f"{path}: file with this exact content already imported as "
            f"import_id={existing['import_id']}; refusing to re-import"
        )

    responses = list(parser.parse(path, question_id=question_id))
    log.info(
        "Parsed %d short-answer responses from %s (format=%s, question=%s)",
        len(responses), path.name, file_format, question_id or "ALL",
    )

    if notes is _NOTES_OMITTED:
        resolved_notes = f"question_id={question_id!r}" if question_id else None
    else:
        resolved_notes = notes
    import_id = register_import(
        conn,
        file_path=path,
        file_format=file_format,
        row_count=len(responses),
        notes=resolved_notes,
    )
    inserted, skipped = write_responses(conn, import_id, responses)

    return IngestResult(
        import_id=import_id,
        file_path=path,
        file_format=file_format,
        inserted=inserted,
        skipped=skipped,
        total_seen=len(responses),
    )


def _detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".tab":
        return datashop.FORMAT_NAME
    if suffix == ".csv":
        return runestone.FORMAT_NAME
    raise ValueError(
        f"Cannot auto-detect format for {path.name!r}; "
        f"pass file_format explicitly"
    )


_PARSERS = {
    datashop.FORMAT_NAME: datashop,
    runestone.FORMAT_NAME: runestone,
}
