"""
Pipeline-run helpers.

A pipeline_run row groups together the per-stage artifacts produced from a
single end-to-end pass over the data (screenings, chains, matches,
dispositions). Every stage that writes to the database does so under some
run_id, which is how we keep multiple parallel attempts (e.g. comparing
two prompt versions) from overwriting each other.

The schema's pipeline_runs table requires a vocabulary_version and a
codebook_version foreign key, even for stages that don't actually consult
either of them (Stage 2 doesn't). To keep the early stages usable before
the group has finalised a vocabulary and codebook, this module will
create stub 'default' versions on demand.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

DEFAULT_VOCAB_LABEL = "default"
DEFAULT_CODEBOOK_LABEL = "default"


def ensure_vocabulary_version(
    conn: sqlite3.Connection,
    label: str = DEFAULT_VOCAB_LABEL,
) -> int:
    """
    Return the version_id for a vocabulary label, creating it if needed.

    Uses INSERT OR IGNORE so two callers racing to create the same label
    can't trip the UNIQUE constraint; one wins the insert, the other
    sees the existing row on the follow-up SELECT.
    """
    conn.execute(
        "INSERT OR IGNORE INTO vocabulary_versions (label, notes) VALUES (?, ?)",
        (label, "Auto-created stub; populate before running Stage 3."),
    )
    row = conn.execute(
        "SELECT version_id FROM vocabulary_versions WHERE label = ?", (label,)
    ).fetchone()
    return row["version_id"]


def ensure_codebook_version(
    conn: sqlite3.Connection,
    label: str = DEFAULT_CODEBOOK_LABEL,
) -> int:
    """
    Return the version_id for a codebook label, creating it if needed.

    Uses INSERT OR IGNORE so two callers racing to create the same label
    can't trip the UNIQUE constraint; one wins the insert, the other
    sees the existing row on the follow-up SELECT.
    """
    conn.execute(
        "INSERT OR IGNORE INTO codebook_versions (label, notes) VALUES (?, ?)",
        (label, "Auto-created stub; populate before running Stage 4."),
    )
    row = conn.execute(
        "SELECT version_id FROM codebook_versions WHERE label = ?", (label,)
    ).fetchone()
    return row["version_id"]


def start_run(
    conn: sqlite3.Connection,
    *,
    prompt_version: str,
    model_id: str,
    vocab_label: str = DEFAULT_VOCAB_LABEL,
    codebook_label: str = DEFAULT_CODEBOOK_LABEL,
    notes: str | None = None,
) -> int:
    """
    Open a new pipeline_run and return its run_id.

    The vocabulary and codebook versions referenced are looked up by label
    and created if absent, so callers don't have to pre-populate those
    tables just to run an early stage.

    Transaction boundaries are the caller's responsibility (mirroring the
    ingest helpers): this function does not commit. Callers typically
    either wrap their flow in `proteus.db.transaction(conn)` or call
    `conn.commit()` explicitly once the run row should be persisted.
    """
    vocab_version = ensure_vocabulary_version(conn, vocab_label)
    codebook_version = ensure_codebook_version(conn, codebook_label)
    cur = conn.execute(
        "INSERT INTO pipeline_runs (status, prompt_version, model_id, "
        "vocab_version, codebook_version, notes) VALUES (?, ?, ?, ?, ?, ?)",
        ("running", prompt_version, model_id, vocab_version, codebook_version, notes),
    )
    run_id = cur.lastrowid
    log.info(
        "Started pipeline run %d (prompt=%s model=%s)",
        run_id, prompt_version, model_id,
    )
    return run_id


def complete_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str = "completed",
    notes: str | None = None,
) -> None:
    """
    Mark a pipeline_run as finished. Status must satisfy the schema CHECK.

    Like `start_run`, this leaves transaction management to the caller
    and does not commit on its own.
    """
    if status not in {"completed", "failed", "partial"}:
        raise ValueError(f"invalid run status: {status!r}")
    if notes is None:
        conn.execute(
            "UPDATE pipeline_runs SET status = ?, "
            "completed_at = datetime('now') WHERE run_id = ?",
            (status, run_id),
        )
    else:
        conn.execute(
            "UPDATE pipeline_runs SET status = ?, "
            "completed_at = datetime('now'), notes = ? WHERE run_id = ?",
            (status, notes, run_id),
        )
