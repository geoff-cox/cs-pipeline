"""
DataShop .tab format parser.

DataShop exports are tab-separated, with one row per logged event. Short-
answer submissions are the events where the 'Selection' column equals
'shortanswer'; the response text lives in the 'Action' column. Many other
event types appear in the same file (page views, problem starts, etc.)
and are filtered out.

Parser is read-only and produces Response objects. All database writes
happen in proteus.ingest.common.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterator

from proteus.ingest.common import Response, normalize_text, parse_timestamp

log = logging.getLogger(__name__)

FORMAT_NAME = "datashop_tab"

# DataShop column names we depend on. If an export is missing any of these,
# the file is unrecognised and we refuse to parse rather than silently
# dropping rows.
_REQUIRED_COLUMNS = {
    "Time",
    "Anon Student Id",
    "Action",
    "Problem Name",
    "Selection",
}

# Optional columns we use when present.
_OPTIONAL_COLUMNS = {
    "CF (Institution)",
}


def parse(
    path: Path,
    question_id: str | None = None,
) -> Iterator[Response]:
    """
    Yield Response objects from a DataShop .tab file.

    If question_id is given, only responses to that question are yielded.
    If None, all short-answer submissions in the file are yielded; the
    caller can filter downstream.

    Raises ValueError if the file is missing required columns. Skips rows
    that look malformed (missing required field values), logging a
    warning for each.
    """
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        if reader.fieldnames is None:
            raise ValueError(f"{path}: file has no header row")

        missing = _REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"{path}: missing required columns: {sorted(missing)}"
            )

        for row_num, row in enumerate(reader, start=2):  # row 1 is header
            if row.get("Selection") != "shortanswer":
                continue
            if question_id is not None and row.get("Problem Name") != question_id:
                continue

            try:
                yield _row_to_response(row)
            except (ValueError, KeyError) as e:
                log.warning(
                    "Skipping malformed row %d in %s: %s", row_num, path.name, e
                )


def _row_to_response(row: dict) -> Response:
    """Convert a parsed DataShop row to a Response. Raises ValueError on bad data."""
    student_id = (row.get("Anon Student Id") or "").strip()
    if not student_id:
        raise ValueError("missing Anon Student Id")

    question_id = (row.get("Problem Name") or "").strip()
    if not question_id:
        raise ValueError("missing Problem Name")

    timestamp_raw = (row.get("Time") or "").strip()
    if not timestamp_raw:
        raise ValueError("missing Time")
    submitted_at = parse_timestamp(timestamp_raw)

    # Action holds the response text for shortanswer events.
    response_text = normalize_text(row.get("Action") or "")

    course_id = row.get("CF (Institution)")
    if course_id is not None:
        course_id = course_id.strip() or None

    return Response(
        student_id=student_id,
        question_id=question_id,
        response_text=response_text,
        submitted_at=submitted_at,
        course_id=course_id,
    )
