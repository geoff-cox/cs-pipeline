"""
Read-only service layer for the import-history view.

Companion to ingest_service.py. Same architectural rule: no Streamlit
imports here; this module must be callable from a notebook, a test, or
a future API handler.

The interesting query is `get_downstream_status`: for one import, how
many of its responses have moved through each pipeline stage. Because
the pipeline can be run multiple times against the same response (the
prompt-A vs prompt-B comparison case the schema is explicitly designed
to support), every count is `COUNT(DISTINCT response_id)`, not
`COUNT(*)`. Otherwise re-running stage 2 with a new prompt would inflate
the "responses screened" number, which would be wrong.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime


# ---------------------------------------------------------------------------
# Per-import detail
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DownstreamStatus:
    """How many of an import's responses have reached each pipeline stage.

    Each count is the number of *distinct* responses that have at least
    one row at that stage, across any pipeline_run. A response is
    counted once even if it has been screened twice (e.g. with two
    different prompt versions).
    """
    n_responses: int
    n_screened: int
    n_extracted: int  # has at least one chain
    n_matched: int    # has at least one chain step with a match
    n_disposed: int

    @property
    def pct_screened(self) -> float:
        return (self.n_screened / self.n_responses * 100) if self.n_responses else 0.0

    @property
    def pct_extracted(self) -> float:
        return (self.n_extracted / self.n_responses * 100) if self.n_responses else 0.0

    @property
    def pct_matched(self) -> float:
        return (self.n_matched / self.n_responses * 100) if self.n_responses else 0.0

    @property
    def pct_disposed(self) -> float:
        return (self.n_disposed / self.n_responses * 100) if self.n_responses else 0.0


def get_downstream_status(conn: sqlite3.Connection, import_id: int) -> DownstreamStatus:
    """Count how many of `import_id`'s responses have rows in each
    downstream stage table. One query per stage; SQLite handles this
    fast enough at the corpus sizes the pipeline targets."""
    # Total responses for this import
    n_responses = conn.execute(
        "SELECT COUNT(*) FROM responses WHERE import_id = ?",
        (import_id,),
    ).fetchone()[0]

    # Screened: distinct responses that have any screening row
    n_screened = conn.execute(
        """
        SELECT COUNT(DISTINCT s.response_id)
        FROM screenings s
        JOIN responses r ON r.response_id = s.response_id
        WHERE r.import_id = ?
        """,
        (import_id,),
    ).fetchone()[0]

    # Extracted: distinct responses with any chain
    n_extracted = conn.execute(
        """
        SELECT COUNT(DISTINCT c.response_id)
        FROM chains c
        JOIN responses r ON r.response_id = c.response_id
        WHERE r.import_id = ?
        """,
        (import_id,),
    ).fetchone()[0]

    # Matched: distinct responses with at least one matched chain step
    n_matched = conn.execute(
        """
        SELECT COUNT(DISTINCT c.response_id)
        FROM matches m
        JOIN chain_steps cs ON cs.step_id = m.step_id
        JOIN chains c ON c.chain_id = cs.chain_id
        JOIN responses r ON r.response_id = c.response_id
        WHERE r.import_id = ?
        """,
        (import_id,),
    ).fetchone()[0]

    # Disposed: distinct responses with any disposition
    n_disposed = conn.execute(
        """
        SELECT COUNT(DISTINCT d.response_id)
        FROM dispositions d
        JOIN responses r ON r.response_id = d.response_id
        WHERE r.import_id = ?
        """,
        (import_id,),
    ).fetchone()[0]

    return DownstreamStatus(
        n_responses=n_responses,
        n_screened=n_screened,
        n_extracted=n_extracted,
        n_matched=n_matched,
        n_disposed=n_disposed,
    )


# ---------------------------------------------------------------------------
# Per-import response listing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResponseRow:
    """One response, flattened for tabular display."""
    response_id: str
    student_id: str
    question_id: str
    response_text: str
    submitted_at: str
    course_id: str | None


def _like_substring(term: str) -> str:
    r"""Build a `%term%` pattern for SQL LIKE, escaping the wildcards
    `%` and `_` so user input is treated as a literal substring.

    The backslash must be escaped first so we don't double-escape on
    the subsequent replacements. The matching LIKE clause must specify
    `ESCAPE '\'` for SQLite to honor these escapes.
    """
    escaped = (
        term.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _responses_filter(
    import_id: int,
    question_id: str | None,
    text_search: str | None,
) -> tuple[str, list]:
    """Build the shared WHERE clause for the response queries.

    Returned as a (clause, params) pair so the list and count functions
    apply identical filtering — if they diverged, the "of N" total on
    the page would no longer match the rows shown.
    """
    clause = ["WHERE import_id = ?"]
    params: list = [import_id]

    if question_id:
        clause.append("AND question_id = ?")
        params.append(question_id)

    if text_search:
        clause.append(r"AND response_text LIKE ? ESCAPE '\'")
        params.append(_like_substring(text_search))

    return " ".join(clause), params


def list_responses_for_import(
    conn: sqlite3.Connection,
    import_id: int,
    question_id: str | None = None,
    text_search: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[ResponseRow]:
    """Return responses tied to an import, optionally filtered.

    text_search does a substring match against response_text. The `%`
    and `_` LIKE wildcards are escaped so user input is matched
    literally; sufficient for the prototype, replace with FTS5 if it
    ever matters.

    `offset` skips that many rows for pagination. It defaults to 0, so
    existing callers that pass only `limit` (or neither) are unaffected.
    """
    where, params = _responses_filter(import_id, question_id, text_search)

    # submitted_at alone is not a total order: it is nullable and only
    # unique in combination with (student_id, question_id). Paginating
    # over a non-total order can drop or duplicate rows across pages, so
    # we break ties on the primary key to make the order deterministic
    # while keeping submission time as the canonical sort.
    sql = [
        "SELECT response_id, student_id, question_id, response_text, "
        "submitted_at, course_id FROM responses",
        where,
        "ORDER BY submitted_at, response_id",
    ]

    if limit is not None:
        sql.append("LIMIT ?")
        params.append(limit)
        if offset:
            sql.append("OFFSET ?")
            params.append(offset)
    elif offset:
        # SQLite requires a LIMIT before OFFSET; -1 means "no limit".
        sql.append("LIMIT -1 OFFSET ?")
        params.append(offset)

    query = " ".join(sql)
    return [
        ResponseRow(
            response_id=row["response_id"],
            student_id=row["student_id"],
            question_id=row["question_id"],
            response_text=row["response_text"],
            submitted_at=row["submitted_at"],
            course_id=row["course_id"],
        )
        for row in conn.execute(query, params)
    ]


def count_responses_for_import(
    conn: sqlite3.Connection,
    import_id: int,
    question_id: str | None = None,
    text_search: str | None = None,
) -> int:
    """Total responses matching the same filters as
    `list_responses_for_import`, without fetching the rows.

    Lets the page show "Showing 1–100 of 847" and compute the last page
    for the prev/next controls.
    """
    where, params = _responses_filter(import_id, question_id, text_search)
    query = f"SELECT COUNT(*) FROM responses {where}"
    return conn.execute(query, params).fetchone()[0]


def get_question_ids_for_import(
    conn: sqlite3.Connection, import_id: int
) -> list[str]:
    """Distinct question_ids in an import. Used to populate filter
    dropdowns in the detail view."""
    return [
        row["question_id"]
        for row in conn.execute(
            "SELECT DISTINCT question_id FROM responses WHERE import_id = ? "
            "ORDER BY question_id",
            (import_id,),
        )
    ]


# ---------------------------------------------------------------------------
# Convenience: bundled detail object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportDetail:
    """Everything the detail panel needs in one shot."""
    import_id: int
    file_path: str
    file_hash: str
    file_format: str
    imported_at: str
    row_count: int | None
    notes: str | None
    status: DownstreamStatus
    question_ids: list[str]


def get_import_detail(
    conn: sqlite3.Connection, import_id: int
) -> ImportDetail | None:
    """Fetch the full detail bundle for one import. Returns None if the
    import_id doesn't exist."""
    row = conn.execute(
        "SELECT import_id, file_path, file_hash, file_format, imported_at, "
        "row_count, notes FROM raw_imports WHERE import_id = ?",
        (import_id,),
    ).fetchone()
    if row is None:
        return None

    return ImportDetail(
        import_id=row["import_id"],
        file_path=row["file_path"],
        file_hash=row["file_hash"],
        file_format=row["file_format"],
        imported_at=row["imported_at"],
        row_count=row["row_count"],
        notes=row["notes"],
        status=get_downstream_status(conn, import_id),
        question_ids=get_question_ids_for_import(conn, import_id),
    )
