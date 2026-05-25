"""
Vocabulary management for Stage 3 (chain extraction).

The chain extractor's output is normalised against a controlled
vocabulary so downstream pattern matching (Stage 4) doesn't have to
deal with paraphrase noise: 'infinitely many solutions', 'not unique',
and 'more than one solution' all become a single canonical form.

This module is the small layer that owns the vocabulary tables. It
exposes:

    register_term(conn, version_id, canonical_form, description=None)
        Add one canonical form to a vocabulary version. Idempotent on
        (version_id, canonical_form): re-adding the same form returns
        the existing term_id without raising.

    list_terms(conn, version_id)
        Yield the canonical forms of one vocabulary version, in
        insertion order, for display.

    match_term(conn, version_id, text)
        Return the canonical form of the vocabulary entry that best
        matches a free-text fragment, or None. Matching is intentionally
        modest (case-insensitive substring): if the fragment contains a
        canonical form's text, that's a hit. The longest match wins so
        more specific terms (e.g. '#vars > #pivots') beat the substrings
        of less specific ones ('#vars').

The matcher is the seam an LLM-backed extractor would replace; for now
it gives the keyword extractor enough to map clauses to canonical forms
when the vocab has been seeded, and to return None (so the extractor
can flag the step OOV) when it hasn't.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)


def register_term(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    canonical_form: str,
    description: str | None = None,
) -> int:
    """
    Insert (or look up) one vocabulary term and return its term_id.

    Uses INSERT OR IGNORE on the (version_id, canonical_form) UNIQUE
    constraint so two callers seeding the same term don't race; if the
    row already exists, the description argument is ignored (we do not
    silently overwrite an existing description).
    """
    canonical_form = canonical_form.strip()
    if not canonical_form:
        raise ValueError("canonical_form must not be empty")
    conn.execute(
        "INSERT OR IGNORE INTO vocabulary (version_id, canonical_form, description) "
        "VALUES (?, ?, ?)",
        (version_id, canonical_form, description),
    )
    row = conn.execute(
        "SELECT term_id FROM vocabulary WHERE version_id = ? "
        "AND canonical_form = ?",
        (version_id, canonical_form),
    ).fetchone()
    return row["term_id"]


def list_terms(
    conn: sqlite3.Connection,
    *,
    version_id: int,
) -> list[sqlite3.Row]:
    """Return all (term_id, canonical_form, description) rows for a version."""
    return conn.execute(
        "SELECT term_id, canonical_form, description FROM vocabulary "
        "WHERE version_id = ? ORDER BY term_id",
        (version_id,),
    ).fetchall()


def match_term(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    text: str,
) -> str | None:
    """
    Return the canonical form of the longest vocabulary entry contained
    (case-insensitively) in `text`, or None if nothing matches.

    'Longest match wins' so a more specific canonical form beats a less
    specific one when both happen to match: '#vars > #pivots' beats
    '#vars'. Ties (rare) are broken by the smaller `term_id`, so the
    chosen match is deterministic regardless of SQLite's row order.
    """
    if not text:
        return None
    haystack = text.lower()
    rows = conn.execute(
        "SELECT term_id, canonical_form FROM vocabulary WHERE version_id = ? "
        "ORDER BY length(canonical_form) DESC, term_id ASC",
        (version_id,),
    ).fetchall()
    for row in rows:
        if row["canonical_form"].lower() in haystack:
            return row["canonical_form"]
    return None
