"""
CSV export helpers for sharing pipeline results with the group.

Each export is a flat, joined view of one stage's output table — the
shape a reviewer wants in a spreadsheet, not the normalized shape that
lives in the database. Add a new dataset by adding an entry to
DATASETS; the CLI dispatches off that registry.

Designed to be replaced by a frontend; deliberately minimal.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass
from typing import IO, Callable, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Dataset:
    """A named export view: a SQL builder plus its column header."""

    name: str
    columns: tuple[str, ...]
    build_sql: Callable[[dict], tuple[str, list]]
    description: str


def _responses_sql(filters: dict) -> tuple[str, list]:
    sql = (
        "SELECT r.response_id, r.student_id, r.question_id, "
        "r.response_text, r.submitted_at, r.course_id, r.import_id "
        "FROM responses r"
    )
    where: list[str] = []
    params: list = []
    if filters.get("question_id"):
        where.append("r.question_id = ?")
        params.append(filters["question_id"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.question_id, r.submitted_at, r.response_id"
    return sql, params


def _screenings_sql(filters: dict) -> tuple[str, list]:
    sql = (
        "SELECT s.run_id, r.response_id, r.student_id, r.question_id, "
        "r.response_text, r.submitted_at, "
        "s.label, s.confidence, s.rationale "
        "FROM screenings s "
        "JOIN responses r ON r.response_id = s.response_id"
    )
    where: list[str] = []
    params: list = []
    if filters.get("run_id") is not None:
        where.append("s.run_id = ?")
        params.append(filters["run_id"])
    if filters.get("question_id"):
        where.append("r.question_id = ?")
        params.append(filters["question_id"])
    if filters.get("label"):
        where.append("s.label = ?")
        params.append(filters["label"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.run_id, r.question_id, r.submitted_at, r.response_id"
    return sql, params


def _matches_sql(filters: dict) -> tuple[str, list]:
    # One row per match. The Stage 4 matcher writes 0..N matches per
    # step, so a single step can show up here multiple times (one per
    # matched code). Joined with the step + chain + response context
    # so the row is self-contained.
    sql = (
        "SELECT c.run_id, m.match_id, m.code_id, m.codebook_version_id, "
        "m.rationale, "
        "cs.step_id, cs.step_index, cs.premise, cs.conclusion, "
        "cs.oov_flag, "
        "r.response_id, r.student_id, r.question_id, r.response_text "
        "FROM matches m "
        "JOIN chain_steps cs ON cs.step_id = m.step_id "
        "JOIN chains c ON c.chain_id = cs.chain_id "
        "JOIN responses r ON r.response_id = c.response_id"
    )
    where: list[str] = []
    params: list = []
    if filters.get("run_id") is not None:
        where.append("c.run_id = ?")
        params.append(filters["run_id"])
    if filters.get("question_id"):
        where.append("r.question_id = ?")
        params.append(filters["question_id"])
    if filters.get("code_id"):
        where.append("m.code_id = ?")
        params.append(filters["code_id"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    # submitted_at and step_index aren't globally unique on their
    # own (two responses can share a timestamp; step_index is only
    # unique within a chain). Tiebreak by chain_id and match_id so
    # the export ordering is stable across runs.
    sql += (
        " ORDER BY c.run_id, r.question_id, r.submitted_at, "
        "c.chain_id, cs.step_index, m.code_id, m.match_id"
    )
    return sql, params


def _dispositions_sql(filters: dict) -> tuple[str, list]:
    # One row per disposition (per response per run). The full
    # candidate packet: response text, screening verdict, chain
    # summary, matched codes, disposition. This is the view a
    # reviewer wants for triage — one wide row that says everything
    # the pipeline thinks about the response.
    sql = (
        "SELECT "
        "  d.run_id, d.disposition, d.trigger_note, "
        "  r.response_id, r.student_id, r.question_id, r.response_text, "
        "  s.label AS screening_label, "
        "  s.confidence AS screening_confidence, "
        "  s.rationale AS screening_rationale, "
        "  c.chain_id, c.extractor_notes, "
        "  (SELECT count(*) FROM chain_steps cs WHERE cs.chain_id = c.chain_id) "
        "    AS chain_step_count, "
        "  (SELECT count(*) FROM chain_steps cs WHERE cs.chain_id = c.chain_id "
        "    AND cs.oov_flag = 1) AS chain_oov_count, "
        "  (SELECT group_concat(code_id) FROM ("
        "     SELECT DISTINCT m.code_id "
        "     FROM matches m JOIN chain_steps cs2 ON cs2.step_id = m.step_id "
        "     WHERE cs2.chain_id = c.chain_id "
        "     ORDER BY m.code_id"
        "   )) AS matched_codes "
        "FROM dispositions d "
        "JOIN responses r ON r.response_id = d.response_id "
        "LEFT JOIN screenings s "
        "  ON s.response_id = d.response_id AND s.run_id = d.run_id "
        "LEFT JOIN chains c "
        "  ON c.response_id = d.response_id AND c.run_id = d.run_id"
    )
    where: list[str] = []
    params: list = []
    if filters.get("run_id") is not None:
        where.append("d.run_id = ?")
        params.append(filters["run_id"])
    if filters.get("question_id"):
        where.append("r.question_id = ?")
        params.append(filters["question_id"])
    if filters.get("disposition"):
        where.append("d.disposition = ?")
        params.append(filters["disposition"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.run_id, r.question_id, r.submitted_at, r.response_id"
    return sql, params


def _chains_sql(filters: dict) -> tuple[str, list]:
    # One row per chain step. Chains with no steps still appear (LEFT
    # JOIN), with empty step columns, so reviewers can see the
    # extractor's "I gave up" cases alongside the productive ones.
    sql = (
        "SELECT c.run_id, c.chain_id, r.response_id, r.student_id, "
        "r.question_id, r.response_text, c.extractor_notes, "
        "cs.step_index, cs.premise, cs.conclusion, cs.oov_flag "
        "FROM chains c "
        "JOIN responses r ON r.response_id = c.response_id "
        "LEFT JOIN chain_steps cs ON cs.chain_id = c.chain_id"
    )
    where: list[str] = []
    params: list = []
    if filters.get("run_id") is not None:
        where.append("c.run_id = ?")
        params.append(filters["run_id"])
    if filters.get("question_id"):
        where.append("r.question_id = ?")
        params.append(filters["question_id"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY c.run_id, r.question_id, r.submitted_at, "
        "c.chain_id, cs.step_index"
    )
    return sql, params


DATASETS: dict[str, _Dataset] = {
    "responses": _Dataset(
        name="responses",
        columns=(
            "response_id", "student_id", "question_id", "response_text",
            "submitted_at", "course_id", "import_id",
        ),
        build_sql=_responses_sql,
        description="Stage 1 ingested responses (one row per response).",
    ),
    "screenings": _Dataset(
        name="screenings",
        columns=(
            "run_id", "response_id", "student_id", "question_id",
            "response_text", "submitted_at",
            "label", "confidence", "rationale",
        ),
        build_sql=_screenings_sql,
        description=(
            "Stage 2 verdicts joined with their response text "
            "(one row per response per run)."
        ),
    ),
    "chains": _Dataset(
        name="chains",
        columns=(
            "run_id", "chain_id", "response_id", "student_id", "question_id",
            "response_text", "extractor_notes",
            "step_index", "premise", "conclusion", "oov_flag",
        ),
        build_sql=_chains_sql,
        description=(
            "Stage 3 chains joined with their response text "
            "(one row per chain step; empty chains appear once with "
            "blank step columns)."
        ),
    ),
    "matches": _Dataset(
        name="matches",
        columns=(
            "run_id", "match_id", "code_id", "codebook_version_id",
            "rationale",
            "step_id", "step_index", "premise", "conclusion", "oov_flag",
            "response_id", "student_id", "question_id", "response_text",
        ),
        build_sql=_matches_sql,
        description=(
            "Stage 4 matches joined with their chain step and response "
            "context (one row per match)."
        ),
    ),
    "dispositions": _Dataset(
        name="dispositions",
        columns=(
            "run_id", "disposition", "trigger_note",
            "response_id", "student_id", "question_id", "response_text",
            "screening_label", "screening_confidence", "screening_rationale",
            "chain_id", "extractor_notes",
            "chain_step_count", "chain_oov_count",
            "matched_codes",
        ),
        build_sql=_dispositions_sql,
        description=(
            "Stage 5 candidate packets: one row per disposition with the "
            "response text, screening verdict, chain summary, and matched "
            "codes joined in."
        ),
    ),
}


def export_dataset(
    conn: sqlite3.Connection,
    dataset: str,
    out: IO[str],
    *,
    question_id: str | None = None,
    run_id: int | None = None,
    label: str | None = None,
    code_id: str | None = None,
    disposition: str | None = None,
) -> int:
    """
    Write `dataset` to `out` as CSV. Returns the number of data rows
    written (header excluded).

    The same set of named filters is accepted for every dataset, and
    each dataset's SQL builder uses only the keys that apply to it
    (e.g. 'responses' ignores `run_id`, `label`, `code_id`). This
    keeps the CLI surface uniform without a per-dataset argument
    fan-out.

    Uses '\\n' as the CSV line terminator so writing to stdout on
    Windows doesn't produce '\\r\\r\\n' endings via the platform's
    default newline translation. Callers writing to a file should open
    it with `newline=''` (as the CLI does).
    """
    try:
        spec = DATASETS[dataset]
    except KeyError as e:
        raise ValueError(
            f"unknown dataset {dataset!r}; "
            f"known: {sorted(DATASETS)}"
        ) from e

    filters = {
        "question_id": question_id,
        "run_id": run_id,
        "label": label,
        "code_id": code_id,
        "disposition": disposition,
    }
    sql, params = spec.build_sql(filters)
    cursor = conn.execute(sql, params)

    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(spec.columns)
    n = 0
    for row in cursor:
        writer.writerow([row[c] for c in spec.columns])
        n += 1
    log.info("Exported %d row(s) from dataset %r", n, dataset)
    return n


def dataset_names() -> Sequence[str]:
    """Datasets the CLI knows how to export."""
    return tuple(DATASETS)
