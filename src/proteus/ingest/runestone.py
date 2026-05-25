"""
Runestone useinfo CSV format parser.

Runestone exports interaction logs as CSV with one row per event. Short-
answer submissions are events where the 'event' column equals
'shortanswer'; the response text lives in the 'act' column, the
question identifier in 'div_id'.

Sample header: id, timestamp, sid, event, act, div_id, course_id

Note on timestamps: some Runestone exports we have seen contain
spreadsheet-mangled timestamps (e.g., '44:22.8' instead of a full
datetime), the result of opening the CSV in Excel before saving. The
parser detects this and refuses to silently invent dates; rows with
unparseable timestamps are logged and skipped.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Iterator

from proteus.ingest.common import Response, normalize_text, parse_timestamp

log = logging.getLogger(__name__)

FORMAT_NAME = "runestone_useinfo_csv"

_REQUIRED_COLUMNS = {"id", "timestamp", "sid", "event", "act", "div_id"}
_OPTIONAL_COLUMNS = {"course_id"}

# Heuristic for spreadsheet-mangled timestamps. A real timestamp will have
# a year and at least one date separator; a mangled one looks like 'MM:SS.s'
# or similar.
_LOOKS_LIKE_REAL_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2}")


def parse(
    path: Path,
    question_id: str | None = None,
) -> Iterator[Response]:
    """
    Yield Response objects from a Runestone useinfo CSV file.

    If question_id is given, only responses to that question are yielded.

    Raises ValueError if the file is missing required columns. Skips
    rows that look malformed, logging a warning for each.
    """
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: file has no header row")

        missing = _REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"{path}: missing required columns: {sorted(missing)}"
            )

        for row_num, row in enumerate(reader, start=2):  # row 1 is header
            if row.get("event") != "shortanswer":
                continue
            if question_id is not None and row.get("div_id") != question_id:
                continue

            try:
                yield _row_to_response(row)
            except (ValueError, KeyError) as e:
                log.warning(
                    "Skipping malformed row %d in %s: %s", row_num, path.name, e
                )


def _row_to_response(row: dict) -> Response:
    """Convert a parsed Runestone row to a Response. Raises ValueError on bad data."""
    student_id = (row.get("sid") or "").strip()
    if not student_id:
        raise ValueError("missing sid")

    question_id = (row.get("div_id") or "").strip()
    if not question_id:
        raise ValueError("missing div_id")

    timestamp_raw = (row.get("timestamp") or "").strip()
    if not timestamp_raw:
        raise ValueError("missing timestamp")
    if not _LOOKS_LIKE_REAL_TIMESTAMP.search(timestamp_raw):
        raise ValueError(
            f"timestamp {timestamp_raw!r} looks like a spreadsheet-mangled "
            f"value; export the CSV directly from Runestone instead of "
            f"opening it in Excel first"
        )
    submitted_at = parse_timestamp(timestamp_raw)

    response_text = normalize_text(row.get("act") or "")

    course_id = row.get("course_id")
    if course_id is not None:
        course_id = course_id.strip() or None

    return Response(
        student_id=student_id,
        question_id=question_id,
        response_text=response_text,
        submitted_at=submitted_at,
        course_id=course_id,
    )
