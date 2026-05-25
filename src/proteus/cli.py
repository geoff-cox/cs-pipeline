"""
Command-line interface to the PROTEUS pipeline.

Run with:

    python -m proteus.cli --help

Subcommands:

    init     Create a fresh database with the schema applied.
    reset    Delete and re-create the database (destructive; asks for
             confirmation unless --yes is passed).
    ingest   Import a DataShop .tab or Runestone .csv file.
    screen   Run Stage 2 (eligibility screening) over ingested responses.
    extract  Run Stage 3 (chain extraction) over screened responses.
    match    Run Stage 4 (codebook matching) over extracted chain steps.
    dispose  Run Stage 5 (flagging and disposition) over screened responses.
    vocab    Inspect or seed the controlled vocabulary.
    codebook Inspect or seed the codebook and its patterns.
    export   Dump pipeline outputs to CSV for review or sharing.
    status   Print a short summary of what's in the database.
    sync-questions
             Walk a built PreTeXt textbook tree and cache one
             standalone HTML preview per short-answer question.

Database location defaults to ./proteus.db; override with --db.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from proteus import (
    codebook as codebook_mod,
    db,
    disposition,
    export,
    extraction,
    ingest,
    matching,
    runs,
    screening,
    vocabulary,
)
from proteus.sync import questions as sync_questions_mod

DEFAULT_DB_PATH = Path("proteus.db")

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    return args.func(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m proteus.cli",
        description="PROTEUS automation pipeline CLI.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    sub = parser.add_subparsers(required=True, dest="command")

    p_init = sub.add_parser("init", help="Create a fresh database.")
    p_init.set_defaults(func=cmd_init)

    p_reset = sub.add_parser(
        "reset", help="Delete and re-create the database (DESTRUCTIVE)."
    )
    p_reset.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt.",
    )
    p_reset.set_defaults(func=cmd_reset)

    p_ingest = sub.add_parser("ingest", help="Import an export file.")
    p_ingest.add_argument("file", type=Path, help="Path to the export file.")
    p_ingest.add_argument(
        "--question",
        help="If given, only ingest responses to this question_id.",
    )
    p_ingest.add_argument(
        "--format",
        choices=["datashop_tab", "runestone_useinfo_csv"],
        help="Override format auto-detection.",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_screen = sub.add_parser(
        "screen",
        help="Run Stage 2 (eligibility screening) over ingested responses.",
    )
    p_screen.add_argument(
        "--question",
        help="If given, only screen responses for this question_id.",
    )
    p_screen.add_argument(
        "--run-id",
        type=int,
        help=(
            "Use an existing pipeline_runs row instead of starting a new one. "
            "Useful for resuming a partial screen."
        ),
    )
    p_screen.add_argument(
        "--prompt-version",
        default="stage2-rules-v1",
        help="prompt_version recorded on the pipeline_runs row.",
    )
    p_screen.add_argument(
        "--model-id",
        default="rule-baseline",
        help="model_id recorded on the pipeline_runs row.",
    )
    p_screen.add_argument(
        "--notes",
        help="Free-text notes recorded on the pipeline_runs row.",
    )
    p_screen.set_defaults(func=cmd_screen)

    p_extract = sub.add_parser(
        "extract",
        help="Run Stage 3 (chain extraction) over INCLUDE-screened responses.",
    )
    p_extract.add_argument(
        "--question",
        help="If given, only extract chains for this question_id.",
    )
    p_extract.add_argument(
        "--run-id",
        type=int,
        help=(
            "Use an existing pipeline_runs row instead of starting a new one. "
            "Useful for resuming a partial extract or running Stage 3 against "
            "the same run as Stage 2."
        ),
    )
    p_extract.add_argument(
        "--prompt-version",
        default="stage3-keywords-v1",
        help="prompt_version recorded on the pipeline_runs row when --run-id "
             "is not given.",
    )
    p_extract.add_argument(
        "--model-id",
        default="keyword-baseline",
        help="model_id recorded on the pipeline_runs row when --run-id is not given.",
    )
    p_extract.add_argument(
        "--notes",
        help="Free-text notes recorded on the pipeline_runs row.",
    )
    p_extract.set_defaults(func=cmd_extract)

    p_match = sub.add_parser(
        "match",
        help="Run Stage 4 (codebook matching) over chain steps.",
    )
    p_match.add_argument(
        "--question",
        help="If given, only match steps for responses to this question_id.",
    )
    p_match.add_argument(
        "--run-id",
        type=int,
        help=(
            "Use an existing pipeline_runs row instead of starting a new one. "
            "Most often you want to pass the same run_id used for screen/extract."
        ),
    )
    p_match.add_argument(
        "--prompt-version",
        default="stage4-keywords-v1",
        help="prompt_version recorded on the pipeline_runs row when --run-id "
             "is not given.",
    )
    p_match.add_argument(
        "--model-id",
        default="keyword-baseline",
        help="model_id recorded on the pipeline_runs row when --run-id is not given.",
    )
    p_match.add_argument(
        "--notes",
        help="Free-text notes recorded on the pipeline_runs row.",
    )
    p_match.set_defaults(func=cmd_match)

    p_dispose = sub.add_parser(
        "dispose",
        help="Run Stage 5 (flagging and disposition) over screened responses.",
    )
    p_dispose.add_argument(
        "--question",
        help="If given, only dispose responses for this question_id.",
    )
    p_dispose.add_argument(
        "--run-id",
        type=int,
        help=(
            "Use an existing pipeline_runs row. Most often you want to pass "
            "the same run_id used for screen/extract/match."
        ),
    )
    p_dispose.add_argument(
        "--prompt-version",
        default="stage5-rules-v1",
        help="prompt_version recorded when --run-id is not given.",
    )
    p_dispose.add_argument(
        "--model-id",
        default="rule-baseline",
        help="model_id recorded when --run-id is not given.",
    )
    p_dispose.add_argument(
        "--notes",
        help="Free-text notes recorded on the pipeline_runs row.",
    )
    p_dispose.set_defaults(func=cmd_dispose)

    p_vocab = sub.add_parser(
        "vocab", help="Inspect or seed the controlled vocabulary.",
    )
    vocab_sub = p_vocab.add_subparsers(required=True, dest="vocab_command")

    p_vocab_add = vocab_sub.add_parser(
        "add", help="Register a canonical form against a vocabulary version.",
    )
    p_vocab_add.add_argument("canonical_form", help="The canonical form to add.")
    p_vocab_add.add_argument(
        "--description", help="Optional human-readable description of the form.",
    )
    p_vocab_add.add_argument(
        "--label",
        default=runs.DEFAULT_VOCAB_LABEL,
        help=(
            f"Vocabulary version label to add to "
            f"(default: {runs.DEFAULT_VOCAB_LABEL!r}; created on demand)."
        ),
    )
    p_vocab_add.set_defaults(func=cmd_vocab_add)

    p_vocab_list = vocab_sub.add_parser(
        "list", help="List the canonical forms in a vocabulary version.",
    )
    p_vocab_list.add_argument(
        "--label",
        default=runs.DEFAULT_VOCAB_LABEL,
        help=(
            f"Vocabulary version label to list "
            f"(default: {runs.DEFAULT_VOCAB_LABEL!r})."
        ),
    )
    p_vocab_list.set_defaults(func=cmd_vocab_list)

    p_codebook = sub.add_parser(
        "codebook", help="Inspect or seed the codebook and its patterns.",
    )
    cb_sub = p_codebook.add_subparsers(required=True, dest="codebook_command")

    p_cb_add = cb_sub.add_parser(
        "add", help="Register a codebook entry (CS1, CS2, ...).",
    )
    p_cb_add.add_argument("code_id", help="Code ID, e.g. 'CS7'.")
    p_cb_add.add_argument("name", help="Short human-readable name.")
    p_cb_add.add_argument("description", help="One-sentence description.")
    p_cb_add.add_argument(
        "--label",
        default=runs.DEFAULT_CODEBOOK_LABEL,
        help=(
            f"Codebook version label "
            f"(default: {runs.DEFAULT_CODEBOOK_LABEL!r}; created on demand)."
        ),
    )
    p_cb_add.set_defaults(func=cmd_codebook_add)

    p_cb_pat = cb_sub.add_parser(
        "add-pattern",
        help="Append a codebook_patterns row for an existing code.",
    )
    p_cb_pat.add_argument("code_id", help="Code ID the pattern attaches to.")
    p_cb_pat.add_argument("pattern_text", help="The pattern text.")
    p_cb_pat.add_argument(
        "--kind",
        choices=list(codebook_mod.PATTERN_KINDS),
        default="keyword",
        help="pattern_kind (default: 'keyword').",
    )
    p_cb_pat.add_argument(
        "--notes", help="Optional notes for the pattern.",
    )
    p_cb_pat.add_argument(
        "--label",
        default=runs.DEFAULT_CODEBOOK_LABEL,
        help=f"Codebook version label (default: {runs.DEFAULT_CODEBOOK_LABEL!r}).",
    )
    p_cb_pat.set_defaults(func=cmd_codebook_add_pattern)

    p_cb_list = cb_sub.add_parser(
        "list", help="List codes (and optionally their patterns) in a version.",
    )
    p_cb_list.add_argument(
        "--label",
        default=runs.DEFAULT_CODEBOOK_LABEL,
        help=f"Codebook version label (default: {runs.DEFAULT_CODEBOOK_LABEL!r}).",
    )
    p_cb_list.add_argument(
        "--patterns", action="store_true",
        help="Also print each code's patterns.",
    )
    p_cb_list.set_defaults(func=cmd_codebook_list)

    p_export = sub.add_parser(
        "export",
        help="Dump a pipeline-stage view to CSV for review or sharing.",
    )
    p_export.add_argument(
        "dataset",
        choices=export.dataset_names(),
        help=(
            "Which view to export. 'responses' is Stage 1; 'screenings' "
            "joins Stage 2 verdicts with response text; 'chains' joins "
            "Stage 3 chains+steps with response text; 'matches' joins "
            "Stage 4 matches with step+response context."
        ),
    )
    p_export.add_argument(
        "-o", "--output",
        type=Path,
        help="File to write to. Defaults to stdout.",
    )
    p_export.add_argument(
        "--question",
        help="Filter to one question_id.",
    )
    p_export.add_argument(
        "--run-id",
        type=int,
        help="Filter to one pipeline_runs row.",
    )
    p_export.add_argument(
        "--label",
        choices=list(screening.LABELS),
        help="Filter screenings to one verdict label.",
    )
    p_export.add_argument(
        "--code-id",
        help="Filter matches to one codebook code (e.g. 'CS7').",
    )
    p_export.add_argument(
        "--disposition",
        choices=list(disposition.DISPOSITIONS),
        help="Filter dispositions to one bucket (e.g. 'auto_accept').",
    )
    p_export.set_defaults(func=cmd_export)

    p_status = sub.add_parser("status", help="Print a summary of DB contents.")
    p_status.set_defaults(func=cmd_status)

    p_sync = sub.add_parser(
        "sync-questions",
        help=(
            "Walk a built PreTeXt textbook tree and cache one standalone "
            "HTML preview per short-answer question."
        ),
    )
    p_sync.add_argument(
        "--build-dir",
        type=Path,
        required=True,
        help="Path to the built PreTeXt HTML directory to walk.",
    )
    p_sync.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help=(
            "Directory to write per-question standalone HTML files into. "
            "Files land in <output-dir>/<textbook-id>/<xml_id>.html."
        ),
    )
    p_sync.add_argument(
        "--textbook-id",
        type=int,
        required=True,
        help="textbook_id (from the textbooks table) the build belongs to.",
    )
    p_sync.set_defaults(func=cmd_sync_questions)

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    if args.db.exists():
        print(
            f"Database already exists at {args.db}; "
            f"use 'reset' to recreate it.",
            file=sys.stderr,
        )
        return 1
    conn = db.connect(args.db)
    try:
        db.bootstrap(conn)
    finally:
        conn.close()
    print(f"Created fresh database at {args.db}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    if args.db.exists() and not args.yes:
        try:
            confirm = input(
                f"This will DELETE {args.db} and create a fresh database. "
                f"Type 'yes' to continue: "
            )
        except EOFError:
            confirm = ""
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 1
    db.reset(args.db)
    print(f"Reset database at {args.db}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(
            f"No database at {args.db}; run 'init' first.",
            file=sys.stderr,
        )
        return 1

    conn = db.connect(args.db)
    try:
        result = ingest.ingest_file(
            conn,
            file_path=args.file,
            question_id=args.question,
            file_format=args.format,
        )
    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(
        f"Ingest complete:\n"
        f"  import_id:  {result.import_id}\n"
        f"  file:       {result.file_path}\n"
        f"  format:     {result.file_format}\n"
        f"  inserted:   {result.inserted}\n"
        f"  skipped:    {result.skipped}  (already in database)\n"
        f"  total seen: {result.total_seen}"
    )
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(
            f"No database at {args.db}; run 'init' first.",
            file=sys.stderr,
        )
        return 1

    conn = db.connect(args.db)
    try:
        if args.run_id is None:
            run_id = runs.start_run(
                conn,
                prompt_version=args.prompt_version,
                model_id=args.model_id,
                notes=args.notes,
            )
            # Persist the run row before screening starts so a crash
            # mid-screen still leaves a 'running' row to investigate.
            conn.commit()
            run_was_started = True
        else:
            existing = conn.execute(
                "SELECT run_id FROM pipeline_runs WHERE run_id = ?",
                (args.run_id,),
            ).fetchone()
            if existing is None:
                print(
                    f"No pipeline_run with run_id={args.run_id}.",
                    file=sys.stderr,
                )
                return 1
            run_id = args.run_id
            run_was_started = False

        try:
            result = screening.screen_responses(
                conn, run_id=run_id, question_id=args.question,
            )
        except Exception as e:
            if run_was_started:
                try:
                    runs.complete_run(conn, run_id, status="failed")
                    conn.commit()
                except Exception:
                    log.debug(
                        "Failed to mark run %d as failed", run_id, exc_info=True,
                    )
            log.debug("Screening raised", exc_info=True)
            print(f"ERROR: screening failed: {e}", file=sys.stderr)
            return 1

        if run_was_started:
            runs.complete_run(conn, run_id)
            conn.commit()
    finally:
        conn.close()

    print(
        f"Screening complete:\n"
        f"  run_id:   {result.run_id}\n"
        f"  screened: {result.inserted}\n"
        f"  INCLUDE:  {result.get('INCLUDE')}\n"
        f"  EXCLUDE:  {result.get('EXCLUDE')}\n"
        f"  FLAG:     {result.get('FLAG')}"
    )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(
            f"No database at {args.db}; run 'init' first.", file=sys.stderr,
        )
        return 1

    conn = db.connect(args.db)
    try:
        if args.run_id is None:
            run_id = runs.start_run(
                conn,
                prompt_version=args.prompt_version,
                model_id=args.model_id,
                notes=args.notes,
            )
            conn.commit()
            run_was_started = True
        else:
            existing = conn.execute(
                "SELECT run_id FROM pipeline_runs WHERE run_id = ?",
                (args.run_id,),
            ).fetchone()
            if existing is None:
                print(
                    f"No pipeline_run with run_id={args.run_id}.",
                    file=sys.stderr,
                )
                return 1
            run_id = args.run_id
            run_was_started = False

        try:
            result = extraction.extract_chains(
                conn, run_id=run_id, question_id=args.question,
            )
        except Exception as e:
            if run_was_started:
                try:
                    runs.complete_run(conn, run_id, status="failed")
                    conn.commit()
                except Exception:
                    log.debug(
                        "Failed to mark run %d as failed", run_id, exc_info=True,
                    )
            log.debug("Extraction raised", exc_info=True)
            print(f"ERROR: extraction failed: {e}", file=sys.stderr)
            return 1

        if run_was_started:
            runs.complete_run(conn, run_id)
            conn.commit()
    finally:
        conn.close()

    print(
        f"Extraction complete:\n"
        f"  run_id:      {result.run_id}\n"
        f"  chains:      {result.extracted}\n"
        f"  with steps:  {result.get('with_steps')}\n"
        f"  empty:       {result.get('empty')}\n"
        f"  with OOV:    {result.get('oov')}"
    )
    return 0


def cmd_vocab_add(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No database at {args.db}; run 'init' first.", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    try:
        version_id = runs.ensure_vocabulary_version(conn, args.label)
        try:
            term_id = vocabulary.register_term(
                conn,
                version_id=version_id,
                canonical_form=args.canonical_form,
                description=args.description,
            )
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        conn.commit()
    finally:
        conn.close()
    print(
        f"Registered term {term_id} ({args.canonical_form!r}) "
        f"in vocabulary {args.label!r}."
    )
    return 0


def cmd_vocab_list(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No database at {args.db}.", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    try:
        row = conn.execute(
            "SELECT version_id FROM vocabulary_versions WHERE label = ?",
            (args.label,),
        ).fetchone()
        if row is None:
            print(
                f"No vocabulary version with label {args.label!r}.",
                file=sys.stderr,
            )
            return 1
        terms = vocabulary.list_terms(conn, version_id=row["version_id"])
    finally:
        conn.close()

    if not terms:
        print(f"No terms in vocabulary {args.label!r}.")
        return 0
    for t in terms:
        desc = f" — {t['description']}" if t["description"] else ""
        print(f"  [{t['term_id']}] {t['canonical_form']}{desc}")
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(
            f"No database at {args.db}; run 'init' first.", file=sys.stderr,
        )
        return 1

    conn = db.connect(args.db)
    try:
        if args.run_id is None:
            run_id = runs.start_run(
                conn,
                prompt_version=args.prompt_version,
                model_id=args.model_id,
                notes=args.notes,
            )
            conn.commit()
            run_was_started = True
        else:
            existing = conn.execute(
                "SELECT run_id FROM pipeline_runs WHERE run_id = ?",
                (args.run_id,),
            ).fetchone()
            if existing is None:
                print(
                    f"No pipeline_run with run_id={args.run_id}.",
                    file=sys.stderr,
                )
                return 1
            run_id = args.run_id
            run_was_started = False

        try:
            result = matching.match_steps(
                conn, run_id=run_id, question_id=args.question,
            )
        except Exception as e:
            if run_was_started:
                try:
                    runs.complete_run(conn, run_id, status="failed")
                    conn.commit()
                except Exception:
                    log.debug(
                        "Failed to mark run %d as failed", run_id, exc_info=True,
                    )
            log.debug("Matching raised", exc_info=True)
            print(f"ERROR: matching failed: {e}", file=sys.stderr)
            return 1

        if run_was_started:
            runs.complete_run(conn, run_id)
            conn.commit()
    finally:
        conn.close()

    print(
        f"Matching complete:\n"
        f"  run_id:        {result.run_id}\n"
        f"  steps visited: {result.steps_visited}\n"
        f"  matches:       {result.matches_written}\n"
        f"  matched:       {result.get('matched')}\n"
        f"  no_match:      {result.get('no_match')}\n"
        f"  multi_code:    {result.get('multi_code')}"
    )
    return 0


def cmd_dispose(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(
            f"No database at {args.db}; run 'init' first.", file=sys.stderr,
        )
        return 1

    conn = db.connect(args.db)
    try:
        if args.run_id is None:
            run_id = runs.start_run(
                conn,
                prompt_version=args.prompt_version,
                model_id=args.model_id,
                notes=args.notes,
            )
            conn.commit()
            run_was_started = True
        else:
            existing = conn.execute(
                "SELECT run_id FROM pipeline_runs WHERE run_id = ?",
                (args.run_id,),
            ).fetchone()
            if existing is None:
                print(
                    f"No pipeline_run with run_id={args.run_id}.",
                    file=sys.stderr,
                )
                return 1
            run_id = args.run_id
            run_was_started = False

        try:
            result = disposition.dispose_responses(
                conn, run_id=run_id, question_id=args.question,
            )
        except Exception as e:
            if run_was_started:
                try:
                    runs.complete_run(conn, run_id, status="failed")
                    conn.commit()
                except Exception:
                    log.debug(
                        "Failed to mark run %d as failed", run_id, exc_info=True,
                    )
            log.debug("Disposition raised", exc_info=True)
            print(f"ERROR: disposition failed: {e}", file=sys.stderr)
            return 1

        if run_was_started:
            runs.complete_run(conn, run_id)
            conn.commit()
    finally:
        conn.close()

    print(
        f"Disposition complete:\n"
        f"  run_id:            {result.run_id}\n"
        f"  written:           {result.written}\n"
        f"  auto_accept:       {result.get('auto_accept')}\n"
        f"  standard_review:   {result.get('standard_review')}\n"
        f"  terminology_flag:  {result.get('terminology_flag')}\n"
        f"  problem_type_flag: {result.get('problem_type_flag')}\n"
        f"  no_match:          {result.get('no_match')}\n"
        f"  escalate:          {result.get('escalate')}"
    )
    return 0


def cmd_codebook_add(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No database at {args.db}; run 'init' first.", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    try:
        version_id = runs.ensure_codebook_version(conn, args.label)
        try:
            codebook_mod.register_code(
                conn,
                version_id=version_id,
                code_id=args.code_id,
                name=args.name,
                description=args.description,
            )
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        conn.commit()
    finally:
        conn.close()
    print(
        f"Registered code {args.code_id!r} in codebook {args.label!r}."
    )
    return 0


def cmd_codebook_add_pattern(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No database at {args.db}; run 'init' first.", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    try:
        version_id = runs.ensure_codebook_version(conn, args.label)
        try:
            pattern_id = codebook_mod.register_pattern(
                conn,
                version_id=version_id,
                code_id=args.code_id,
                pattern_text=args.pattern_text,
                kind=args.kind,
                notes=args.notes,
            )
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        conn.commit()
    finally:
        conn.close()
    print(
        f"Registered pattern {pattern_id} for {args.code_id!r} "
        f"({args.kind}) in codebook {args.label!r}."
    )
    return 0


def cmd_codebook_list(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No database at {args.db}.", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    try:
        row = conn.execute(
            "SELECT version_id FROM codebook_versions WHERE label = ?",
            (args.label,),
        ).fetchone()
        if row is None:
            print(
                f"No codebook version with label {args.label!r}.",
                file=sys.stderr,
            )
            return 1
        version_id = row["version_id"]
        codes = codebook_mod.list_codes(conn, version_id=version_id)
        patterns = (
            codebook_mod.list_patterns(conn, version_id=version_id)
            if args.patterns else []
        )
    finally:
        conn.close()

    if not codes:
        print(f"No codes in codebook {args.label!r}.")
        return 0
    by_code: dict[str, list] = {}
    for p in patterns:
        by_code.setdefault(p["code_id"], []).append(p)
    for c in codes:
        print(f"  {c['code_id']}  {c['name']}")
        print(f"      {c['description']}")
        if args.patterns:
            for p in by_code.get(c["code_id"], []):
                note = f" — {p['notes']}" if p["notes"] else ""
                print(
                    f"      • [{p['pattern_id']}] ({p['pattern_kind']}) "
                    f"{p['pattern_text']}{note}"
                )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No database at {args.db}.", file=sys.stderr)
        return 1

    conn = db.connect(args.db)
    try:
        if args.output is None:
            n = export.export_dataset(
                conn, args.dataset, sys.stdout,
                question_id=args.question,
                run_id=args.run_id,
                label=args.label,
                code_id=args.code_id,
                disposition=args.disposition,
            )
        else:
            with args.output.open("w", encoding="utf-8", newline="") as f:
                n = export.export_dataset(
                    conn, args.dataset, f,
                    question_id=args.question,
                    run_id=args.run_id,
                    label=args.label,
                    code_id=args.code_id,
                    disposition=args.disposition,
                )
            print(f"Wrote {n} row(s) to {args.output}", file=sys.stderr)
    finally:
        conn.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"No database at {args.db}.", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    try:
        imports = conn.execute(
            "SELECT count(*) AS n FROM raw_imports"
        ).fetchone()["n"]
        responses = conn.execute(
            "SELECT count(*) AS n FROM responses"
        ).fetchone()["n"]
        questions = conn.execute(
            "SELECT count(DISTINCT question_id) AS n FROM responses"
        ).fetchone()["n"]
        students = conn.execute(
            "SELECT count(DISTINCT student_id) AS n FROM responses"
        ).fetchone()["n"]
        run_count = conn.execute(
            "SELECT count(*) AS n FROM pipeline_runs"
        ).fetchone()["n"]
        screening_rows = conn.execute(
            "SELECT label, count(*) AS n FROM screenings GROUP BY label"
        ).fetchall()
        chain_count = conn.execute(
            "SELECT count(*) AS n FROM chains"
        ).fetchone()["n"]
        chain_step_count = conn.execute(
            "SELECT count(*) AS n FROM chain_steps"
        ).fetchone()["n"]
        oov_step_count = conn.execute(
            "SELECT count(*) AS n FROM chain_steps WHERE oov_flag = 1"
        ).fetchone()["n"]
        vocab_count = conn.execute(
            "SELECT count(*) AS n FROM vocabulary"
        ).fetchone()["n"]
        match_count = conn.execute(
            "SELECT count(*) AS n FROM matches"
        ).fetchone()["n"]
        matched_step_count = conn.execute(
            "SELECT count(DISTINCT step_id) AS n FROM matches"
        ).fetchone()["n"]
        codebook_code_count = conn.execute(
            "SELECT count(*) AS n FROM codebook"
        ).fetchone()["n"]
        codebook_pattern_count = conn.execute(
            "SELECT count(*) AS n FROM codebook_patterns"
        ).fetchone()["n"]
        disposition_rows = conn.execute(
            "SELECT disposition, count(*) AS n FROM dispositions "
            "GROUP BY disposition"
        ).fetchall()
    finally:
        conn.close()

    screening_counts = {row["label"]: row["n"] for row in screening_rows}
    total_screened = sum(screening_counts.values())
    print(
        f"Database: {args.db}\n"
        f"  imports:   {imports}\n"
        f"  responses: {responses}\n"
        f"  questions: {questions}\n"
        f"  students:  {students}\n"
        f"  runs:      {run_count}\n"
        f"  screened:  {total_screened}"
        f" (INCLUDE={screening_counts.get('INCLUDE', 0)},"
        f" EXCLUDE={screening_counts.get('EXCLUDE', 0)},"
        f" FLAG={screening_counts.get('FLAG', 0)})\n"
        f"  chains:    {chain_count} ({chain_step_count} step(s),"
        f" {oov_step_count} OOV)\n"
        f"  matches:   {match_count}"
        f" across {matched_step_count} step(s)\n"
        f"  vocab:     {vocab_count} term(s)\n"
        f"  codebook:  {codebook_code_count} code(s),"
        f" {codebook_pattern_count} pattern(s)"
    )

    disposition_counts = {row["disposition"]: row["n"] for row in disposition_rows}
    if disposition_counts:
        total_disp = sum(disposition_counts.values())
        bits = ", ".join(
            f"{k}={v}" for k, v in sorted(disposition_counts.items())
        )
        print(f"  disposed:  {total_disp} ({bits})")
    return 0


def cmd_sync_questions(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(
            f"No database at {args.db}; run 'init' first.", file=sys.stderr,
        )
        return 1

    conn = db.connect(args.db)
    try:
        try:
            result = sync_questions_mod.sync_questions(
                conn,
                build_dir=args.build_dir,
                output_dir=args.output_dir,
                textbook_id=args.textbook_id,
            )
        except (ValueError, OSError) as e:
            log.debug("sync_questions raised", exc_info=True)
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    print(
        f"Sync complete:\n"
        f"  textbook_id:                  {args.textbook_id}\n"
        f"  extracted:                    {result.extracted}\n"
        f"  updated:                      {result.updated}\n"
        f"  unchanged:                    {result.unchanged}\n"
        f"  answered_but_not_catalogued:  {len(result.missing)}\n"
        f"  not_yet_answered:             {len(result.orphan)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
