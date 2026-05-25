"""
Shared utilities for ingest paths.

Each export format (DataShop, Runestone, future others) lives in its own
module and converts its raw rows into Response objects. The shared layer
then takes care of validation, deduplication via deterministic IDs, and
writing into the database.

Keeping all the database-touching logic here means the format-specific
parsers stay pure: they read a file and yield Response objects, nothing
more.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from proteus.db import transaction

log = logging.getLogger(__name__)

# Length of the truncated SHA-256 we use for response_id. Sixteen hex chars
# is 64 bits of entropy, which is more than enough for a per-question
# corpus of a few hundred responses; it stays short enough to be
# eyeball-readable in tables and logs.
_ID_LENGTH = 16


@dataclass(frozen=True)
class Response:
    """
    The canonical post-ingest representation of one student response.

    Frozen because once a parser has produced one of these, downstream
    code should treat it as immutable. Any normalisation happens during
    construction, not after.
    """

    student_id: str
    question_id: str
    response_text: str
    submitted_at: datetime  # timezone-aware
    course_id: str | None = None

    @property
    def response_id(self) -> str:
        """
        Deterministic hash of (student, question, timestamp).

        Truncated SHA-256 in hex. Two ingests of the same row of the same
        export produce the same response_id, which is what makes
        re-imports idempotent.
        """
        key = f"{self.student_id}|{self.question_id}|{self.submitted_at.isoformat()}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:_ID_LENGTH]


def normalize_text(s: str) -> str:
    """
    Apply Unicode NFC normalisation and collapse trailing whitespace.

    We deliberately do NOT collapse internal whitespace: students sometimes
    use line breaks meaningfully (e.g. to separate matrix rows from prose),
    and we don't want to flatten that signal away.
    """
    if s is None:
        return ""
    return unicodedata.normalize("NFC", s).rstrip()


def file_hash(path: Path) -> str:
    """SHA-256 of a file's contents, full hex."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_timestamp(raw: str) -> datetime:
    """
    Parse a timestamp string from an export file into a timezone-aware
    datetime. Accepts a few common formats; raises ValueError on
    unrecognised input.

    DataShop exports use 'YYYY-MM-DD HH:MM:SS.ffffff' without a tz; we
    interpret these as UTC. If a future export carries explicit tz info,
    add a parse path here rather than guessing downstream.
    """
    raw = raw.strip()
    # ISO 8601 with explicit offset
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # DataShop's format with microseconds, no tz
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised timestamp format: {raw!r}")


def register_import(
    conn: sqlite3.Connection,
    file_path: Path,
    file_format: str,
    row_count: int,
    notes: str | None = None,
) -> int:
    """
    Record a raw_imports row and return its import_id. Refuses to register
    a file that has already been imported (unique constraint on file_hash);
    the caller can catch the IntegrityError and decide how to react.
    """
    fhash = file_hash(file_path)
    cur = conn.execute(
        "INSERT INTO raw_imports (file_path, file_hash, file_format, "
        "row_count, notes) VALUES (?, ?, ?, ?, ?)",
        (str(file_path.resolve()), fhash, file_format, row_count, notes),
    )
    return cur.lastrowid


def write_responses(
    conn: sqlite3.Connection,
    import_id: int,
    responses: Iterable[Response],
) -> tuple[int, int]:
    """
    Insert responses for one import. Returns (inserted, skipped).

    A response is "skipped" if its (student_id, question_id, submitted_at)
    triple already exists in the database. This is the idempotency
    mechanism: re-running the same import doesn't double-insert, and
    importing a file that overlaps an earlier import only adds the new
    rows.

    The whole batch runs in one transaction; if any insert raises an
    error we don't recognise, everything rolls back and the import_id
    row is also undone (it lives in the same transaction).
    """
    inserted = 0
    skipped = 0
    with transaction(conn):
        for r in responses:
            try:
                conn.execute(
                    "INSERT INTO responses (response_id, import_id, "
                    "student_id, question_id, response_text, submitted_at, "
                    "course_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        r.response_id,
                        import_id,
                        r.student_id,
                        r.question_id,
                        r.response_text,
                        r.submitted_at.isoformat(),
                        r.course_id,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError as e:
                # Treat duplicate response_id or unique-constraint hit as
                # idempotent skip; re-raise anything else.
                msg = str(e).lower()
                if "unique" in msg or "primary key" in msg:
                    skipped += 1
                else:
                    raise
    log.info("Wrote %d responses (%d skipped as duplicates)", inserted, skipped)
    return inserted, skipped
