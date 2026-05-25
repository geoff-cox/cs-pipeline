"""
Codebook management for Stage 4 (control-structure matching).

The codebook is a versioned list of control-structure codes (CS1, CS2,
...) each with a human-readable name and description, and one or more
codebook_patterns that the Stage 4 matcher consults to find candidate
codes for a chain step.

This module is the small layer that owns the codebook / codebook_patterns
tables. It exposes:

    register_code(conn, version_id, code_id, name, description)
        Insert (or look up) one codebook entry. Idempotent on the
        (code_id, version_id) primary key; re-inserting the same
        code_id returns the existing row, the supplied name and
        description are NOT overwritten.

    register_pattern(conn, version_id, code_id, pattern_text, kind, notes)
        Append a codebook_patterns row. Multiple patterns per code are
        intentional: a code can be matched on more than one keyword or
        structural hint.

    list_codes(conn, version_id)
    list_patterns(conn, version_id, code_id=None)
        Read-side helpers, ordered by insertion for stable display.

The matcher consumes the same tables directly — there isn't a separate
'compile patterns' step because the keyword baseline just does
substring matching against pattern_text. An LLM-backed matcher could
add structure later without changing the table shape.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

PATTERN_KINDS = ("keyword", "structural", "composite")


def register_code(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    code_id: str,
    name: str,
    description: str,
) -> None:
    """
    Insert one codebook entry. Idempotent on the (code_id, version_id)
    primary key: re-registering the same code is a no-op (existing
    name / description are preserved, not overwritten).
    """
    code_id = code_id.strip()
    name = name.strip()
    description = description.strip()
    if not code_id:
        raise ValueError("code_id must not be empty")
    if not name:
        raise ValueError("name must not be empty")
    if not description:
        raise ValueError("description must not be empty")
    conn.execute(
        "INSERT OR IGNORE INTO codebook (code_id, version_id, name, description) "
        "VALUES (?, ?, ?, ?)",
        (code_id, version_id, name, description),
    )


def register_pattern(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    code_id: str,
    pattern_text: str,
    kind: str = "keyword",
    notes: str | None = None,
) -> int:
    """
    Append one codebook_patterns row for an existing code. Returns the
    new pattern_id.

    Multiple patterns per code are allowed and common: a code may be
    matched on more than one keyword or structural hint. Adding the
    same pattern twice will create two rows — dedup, if it ever
    matters, is the caller's responsibility.
    """
    code_id = code_id.strip()
    kind = kind.strip().lower()
    pattern_text = pattern_text.strip()
    if not code_id:
        raise ValueError("code_id must not be empty")
    if kind not in PATTERN_KINDS:
        raise ValueError(
            f"invalid pattern_kind {kind!r}; expected one of {PATTERN_KINDS}"
        )
    if not pattern_text:
        raise ValueError("pattern_text must not be empty")
    # Verify the code exists in this version (the FK would catch it,
    # but the error message would be cryptic).
    existing = conn.execute(
        "SELECT 1 FROM codebook WHERE code_id = ? AND version_id = ?",
        (code_id, version_id),
    ).fetchone()
    if existing is None:
        raise ValueError(
            f"no codebook entry for code_id={code_id!r} in version {version_id}; "
            f"register the code first"
        )
    cur = conn.execute(
        "INSERT INTO codebook_patterns (code_id, version_id, pattern_text, "
        "pattern_kind, notes) VALUES (?, ?, ?, ?, ?)",
        (code_id, version_id, pattern_text, kind, notes),
    )
    return cur.lastrowid


def list_codes(
    conn: sqlite3.Connection,
    *,
    version_id: int,
) -> list[sqlite3.Row]:
    """Return all (code_id, name, description) rows for a version."""
    return conn.execute(
        "SELECT code_id, name, description FROM codebook "
        "WHERE version_id = ? ORDER BY code_id",
        (version_id,),
    ).fetchall()


def list_patterns(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    code_id: str | None = None,
) -> list[sqlite3.Row]:
    """
    Return codebook_patterns rows for a version, optionally restricted
    to one code_id. Ordered by (code_id, pattern_id) for stable display.
    """
    if code_id is None:
        return conn.execute(
            "SELECT pattern_id, code_id, pattern_text, pattern_kind, notes "
            "FROM codebook_patterns WHERE version_id = ? "
            "ORDER BY code_id, pattern_id",
            (version_id,),
        ).fetchall()
    return conn.execute(
        "SELECT pattern_id, code_id, pattern_text, pattern_kind, notes "
        "FROM codebook_patterns WHERE version_id = ? AND code_id = ? "
        "ORDER BY pattern_id",
        (version_id, code_id),
    ).fetchall()
