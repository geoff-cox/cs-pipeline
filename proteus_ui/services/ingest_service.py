"""
Thin service layer over proteus.ingest for use by the Streamlit UI.

Design rule: no Streamlit imports may appear in this module. Anything
defined here must be callable from a notebook, a pytest test, or a
future FastAPI handler with no changes. The Streamlit page is a view
on top of these functions; if it needs something this module does not
expose, add the function here first.

The module adds one capability the existing CLI does not have: a
preview step that parses a file and reports what *would* happen on
commit, without writing to the database. This is the seam that makes
"preview then commit" possible in the UI.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from proteus import db
from proteus.ingest import ingest_file, IngestResult
from proteus.ingest import datashop, runestone
from proteus.ingest.common import Response, file_hash

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

# Single source of truth for known formats. Keep this in sync with the
# CLI's --format choices; both ultimately point at the same parser modules.
KNOWN_FORMATS: dict[str, str] = {
    datashop.FORMAT_NAME: "DataShop (.tab)",
    runestone.FORMAT_NAME: "Runestone useinfo (.csv)",
}

_PARSERS = {
    datashop.FORMAT_NAME: datashop,
    runestone.FORMAT_NAME: runestone,
}


def detect_format(path: Path) -> str | None:
    """
    Guess format from file extension. Returns None if no guess is possible;
    the caller must then prompt the user for an explicit choice.

    Mirrors proteus.ingest._detect_format but returns None instead of
    raising, because the UI wants to fall back to a dropdown rather than
    surface an exception.
    """
    suffix = path.suffix.lower()
    if suffix == ".tab":
        return datashop.FORMAT_NAME
    if suffix == ".csv":
        return runestone.FORMAT_NAME
    return None


# ---------------------------------------------------------------------------
# Preview (parse-without-commit)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreviewRow:
    """One parsed response, flattened for display. Mirrors Response but
    pre-computes the deterministic response_id and stringifies the
    timestamp so the UI does not need to import datetime."""
    response_id: str
    student_id: str
    question_id: str
    response_text: str
    submitted_at: str
    course_id: str | None


@dataclass(frozen=True)
class PreviewResult:
    """
    Everything the UI needs to render a 'before you commit' summary.

    rows                  Parsed responses (flattened for display).
    duplicate_response_ids response_ids already in the DB; would be skipped.
    duplicate_file_import  If the file's hash already exists in raw_imports,
                          the import_id of the prior import; else None.
    file_format           The format used to parse (after auto-detect or override).
    file_hash             SHA-256 of the file bytes.
    parse_errors          Formatted WARNING-level messages the parser emitted
                          while parsing this file (e.g. "Skipping malformed
                          row 142 ..."). Captured per-call via a temporary log
                          handler; empty when the parse was clean.
    """
    rows: list[PreviewRow]
    duplicate_response_ids: set[str]
    duplicate_file_import: int | None
    file_format: str
    file_hash: str
    parse_errors: list[str] = field(default_factory=list)

    @property
    def n_total(self) -> int:
        return len(self.rows)

    @property
    def n_duplicates(self) -> int:
        return len(self.duplicate_response_ids)

    @property
    def n_new(self) -> int:
        return self.n_total - self.n_duplicates

    @property
    def question_ids(self) -> list[str]:
        return sorted({r.question_id for r in self.rows})

    @property
    def student_count(self) -> int:
        return len({r.student_id for r in self.rows})


class _WarningCollector(logging.Handler):
    """Collects formatted WARNING+ records emitted from the owning thread.

    Used to capture parser warnings during a single preview_file call.
    Holds formatted strings (not LogRecord objects) so the result is a
    plain, serializable list the UI can render directly.

    The parser logger this attaches to is process-global, and Streamlit
    runs each session in its own ScriptRunner thread, so two previews can
    have their collectors attached simultaneously. logging.callHandlers
    would then copy every warning into *both* collectors. To keep one
    preview's warnings out of another's parse_errors, we record only
    records originating on the thread that created this collector
    (LogRecord.thread is the emitting thread's id). preview_file parses
    synchronously on its calling thread, so its own warnings always match.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.setFormatter(logging.Formatter("%(message)s"))
        self.messages: list[str] = []
        self._owner_thread = threading.get_ident()

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._owner_thread:
            return
        self.messages.append(self.format(record))


def preview_file(
    conn: sqlite3.Connection,
    path: Path,
    question_id: str | None = None,
    file_format: str | None = None,
) -> PreviewResult:
    """
    Parse a file and report what an ingest would do, without writing.

    This deliberately reuses the same parser modules (datashop, runestone)
    that ingest_file uses, so the preview is faithful: anything you see
    here is exactly what would land in the DB if you commit.

    The function does *not* register a raw_imports row and does *not*
    insert into responses. It is safe to call repeatedly.
    """
    fmt = file_format or detect_format(path)
    if fmt is None:
        raise ValueError(
            f"Cannot auto-detect format for {path.name!r}; "
            f"choose one explicitly: {sorted(KNOWN_FORMATS)}"
        )
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ValueError(
            f"Unknown file_format {fmt!r}; known: {sorted(KNOWN_FORMATS)}"
        )

    # Capture WARNING-level records the parser emits while parsing (e.g.
    # "Skipping malformed row ...") so the UI can surface them. The handler
    # is attached to the *specific* parser module's logger (e.g.
    # "proteus.ingest.datashop"), not the root, so we don't sweep up
    # unrelated warnings that happen to fire elsewhere during the preview.
    # try/finally guarantees detachment even if the parse raises.
    parser_logger = logging.getLogger(parser.__name__)
    warning_collector = _WarningCollector()
    parser_logger.addHandler(warning_collector)
    try:
        # Materialize the generator so we can count and dedup. For the file
        # sizes this pipeline handles (hundreds to low thousands of rows),
        # holding them in memory is fine.
        responses: list[Response] = list(parser.parse(path, question_id=question_id))

        # Check whether this exact file has been imported before.
        fhash = file_hash(path)
        existing = conn.execute(
            "SELECT import_id FROM raw_imports WHERE file_hash = ?", (fhash,)
        ).fetchone()
        duplicate_file_import = existing["import_id"] if existing else None

        # Per-row dedup against responses already in the DB.
        duplicate_ids: set[str] = set()
        if responses:
            # Batch the lookup. SQLite parameter limits are 999 by default;
            # we chunk to be safe even though we don't expect that many.
            rids = [r.response_id for r in responses]
            for i in range(0, len(rids), 500):
                batch = rids[i : i + 500]
                placeholders = ",".join("?" * len(batch))
                q = f"SELECT response_id FROM responses WHERE response_id IN ({placeholders})"
                for row in conn.execute(q, batch):
                    duplicate_ids.add(row["response_id"])

        preview_rows = [
            PreviewRow(
                response_id=r.response_id,
                student_id=r.student_id,
                question_id=r.question_id,
                response_text=r.response_text,
                submitted_at=r.submitted_at.isoformat(),
                course_id=r.course_id,
            )
            for r in responses
        ]

        return PreviewResult(
            rows=preview_rows,
            duplicate_response_ids=duplicate_ids,
            duplicate_file_import=duplicate_file_import,
            file_format=fmt,
            file_hash=fhash,
            parse_errors=list(warning_collector.messages),
        )
    finally:
        parser_logger.removeHandler(warning_collector)


# ---------------------------------------------------------------------------
# Commit (thin wrapper over ingest_file)
# ---------------------------------------------------------------------------

def commit_file(
    conn: sqlite3.Connection,
    path: Path,
    question_id: str | None = None,
    file_format: str | None = None,
    notes: str | None = None,
) -> IngestResult:
    """
    Call the real ingest_file. Kept as a one-liner pass-through so that
    the UI never imports from proteus.ingest directly; everything goes
    through this module.

    An empty-string `notes` is normalized to None so a user who leaves
    the notes field blank doesn't persist a literal "" in raw_imports.
    """
    return ingest_file(
        conn,
        file_path=path,
        question_id=question_id,
        file_format=file_format,
        notes=notes or None,
    )


# ---------------------------------------------------------------------------
# Read-only queries used by the import-history view
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportSummary:
    import_id: int
    file_path: str
    file_hash: str
    file_format: str
    imported_at: str
    row_count: int | None
    rows_in_db: int
    notes: str | None


def list_imports(conn: sqlite3.Connection) -> list[ImportSummary]:
    """Return one row per raw_imports entry, joined to a count of its
    surviving responses in the DB (which may be lower than row_count if
    rows have been deleted, or higher in pathological cases where
    response_ids collide across imports — currently impossible)."""
    sql = """
        SELECT i.import_id, i.file_path, i.file_hash, i.file_format,
               i.imported_at, i.row_count, i.notes,
               (SELECT COUNT(*) FROM responses r WHERE r.import_id = i.import_id)
                   AS rows_in_db
        FROM raw_imports i
        ORDER BY i.import_id DESC
    """
    out: list[ImportSummary] = []
    for row in conn.execute(sql):
        out.append(
            ImportSummary(
                import_id=row["import_id"],
                file_path=row["file_path"],
                file_hash=row["file_hash"],
                file_format=row["file_format"],
                imported_at=row["imported_at"],
                row_count=row["row_count"],
                rows_in_db=row["rows_in_db"],
                notes=row["notes"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def open_connection(db_path: Path) -> sqlite3.Connection:
    """Open a DB connection with PROTEUS conventions. If the database
    file does not exist, bootstrap it with the canonical schema so the
    UI is usable on a fresh checkout."""
    fresh = not db_path.exists()
    conn = db.connect(db_path)
    if fresh:
        log.info("Database %s did not exist; bootstrapping schema.", db_path)
        db.bootstrap(conn)
    return conn
