"""
Database access for the PROTEUS pipeline.

Centralises the (very small) amount of low-level SQLite handling we need so
the rest of the package never has to think about pragmas, foreign keys,
transactions, or schema location.

Public surface:

    connect(db_path)        Return a configured sqlite3.Connection.
    bootstrap(conn)         Apply schema.sql to a fresh database.
    reset(db_path)          Delete the database file and re-bootstrap it.
    transaction(conn)       Context manager for an atomic block.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# Path inside the package where the canonical schema lives.
_SCHEMA_RESOURCE = ("proteus", "schema.sql")


def connect(db_path: str | os.PathLike) -> sqlite3.Connection:
    """
    Open a SQLite connection with PROTEUS conventions applied:

    - Foreign keys enforced (off by default in SQLite, which has bitten us once).
    - Row factory returns sqlite3.Row, so callers can index by column name.
    - Detect_types so TIMESTAMP/DATE columns round-trip as Python objects.

    The caller is responsible for closing the connection (or using it as a
    context manager).
    """
    conn = sqlite3.connect(
        str(db_path),
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def bootstrap(conn: sqlite3.Connection) -> None:
    """
    Apply the canonical schema to a connection. Idempotent only insofar as
    schema.sql uses CREATE TABLE IF NOT EXISTS (it doesn't, currently); call
    on a fresh database.
    """
    schema_sql = resources.files(_SCHEMA_RESOURCE[0]).joinpath(_SCHEMA_RESOURCE[1]).read_text()
    conn.executescript(schema_sql)
    conn.commit()
    log.info("Bootstrapped schema into database")


def reset(db_path: str | os.PathLike) -> None:
    """
    Delete the database file (if it exists) and bootstrap a fresh one.

    Intended for interactive development, not production. There is no
    confirmation prompt; if you point this at a real database, it will
    delete it without asking. The CLI wrapper adds a confirmation step.
    """
    p = Path(db_path)
    if p.exists():
        p.unlink()
        log.info("Deleted existing database at %s", p)
    conn = connect(p)
    try:
        bootstrap(conn)
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """
    Run a block inside a single transaction. Commits on success, rolls back
    on any exception (including KeyboardInterrupt).

    Usage:

        with transaction(conn):
            conn.execute(...)
            conn.execute(...)
    """
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
