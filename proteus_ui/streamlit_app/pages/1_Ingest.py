"""
Ingest page for the PROTEUS UI prototype.

Run with:

    streamlit run streamlit_app/Homepage.py

This page does one job: accept an upload, show what would be ingested,
and commit on confirmation. It deliberately does not include import
history, rollback, or export — those will live on their own pages.

Architecture notes:

* All business logic is in services.ingest_service. This page only
  arranges widgets and forwards user actions.
* st.session_state holds the file bytes and the latest PreviewResult.
  Streamlit re-runs the whole script on every interaction, so anything
  we want to persist across reruns lives in session_state.
* The temp file is written once per upload (keyed on the upload's
  name + size + a content hash) so re-runs don't churn the disk.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

# Make `services` importable when running `streamlit run streamlit_app/Homepage.py`
# from the project root.
import sys
# pages/1_Ingest.py is two levels below proteus_ui/; services/ is one below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.ingest_service import (  # noqa: E402
    KNOWN_FORMATS,
    PreviewResult,
    commit_file,
    detect_format,
    open_connection,
    preview_file,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Page config & sidebar
# ---------------------------------------------------------------------------

st.set_page_config(page_title="PROTEUS · Ingest", layout="wide")
st.title("Ingest a student-response export")

# DB path and dev toggle are owned by Homepage.py and persisted in
# st.session_state. Fall back to a repo-root default so this page works
# if a user lands here directly. parents[3] is the repo root:
# pages/ → streamlit_app/ → proteus_ui/ → repo root.
_DEFAULT_DB = str(Path(__file__).resolve().parents[3] / "proteus.db")
db_path = Path(st.session_state.get("db_path", _DEFAULT_DB))
show_dev = st.session_state.get("show_dev", False)

st.caption(f"Reading from `{db_path}`")


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

_UPLOAD_STATE_KEYS = ("upload_key", "tmp_path", "preview", "committed_result")


def _reset_state() -> None:
    """Clear everything we cached for a previous upload.

    Resets to None rather than popping; downstream code reads these
    keys unconditionally and expects them to exist.
    """
    for key in _UPLOAD_STATE_KEYS:
        st.session_state[key] = None


for key in _UPLOAD_STATE_KEYS:
    st.session_state.setdefault(key, None)


# ---------------------------------------------------------------------------
# Step 1: upload + options
# ---------------------------------------------------------------------------

st.subheader("1. Choose a file")

uploaded = st.file_uploader(
    "Drop a DataShop .tab or Runestone .csv file",
    type=["tab", "csv"],
    accept_multiple_files=False,
)

if uploaded is not None:
    # Stable key for this upload: anything changing here means the
    # cached preview is stale and we throw it away.
    raw = uploaded.getvalue()
    content_hash = hashlib.sha256(raw).hexdigest()[:16]
    upload_key = f"{uploaded.name}:{len(raw)}:{content_hash}"

    if st.session_state["upload_key"] != upload_key:
        # New upload (or different file): persist to a stable temp path
        # whose name preserves the suffix so format auto-detection works.
        tmp_dir = Path(tempfile.gettempdir()) / "proteus_ui_uploads"
        tmp_dir.mkdir(exist_ok=True)
        tmp_path = tmp_dir / f"{content_hash}_{uploaded.name}"
        tmp_path.write_bytes(raw)

        _reset_state()
        st.session_state["upload_key"] = upload_key
        st.session_state["tmp_path"] = tmp_path

    tmp_path: Path = st.session_state["tmp_path"]

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        auto = detect_format(tmp_path)
        _AUTO_LABEL = "(auto-detect)"
        format_choices = [_AUTO_LABEL] + list(KNOWN_FORMATS.keys())
        chosen_format = st.selectbox(
            "Format",
            options=format_choices,
            index=0,
            format_func=lambda key: (
                key if key == _AUTO_LABEL else KNOWN_FORMATS[key]
            ),
            help=(
                "Auto-detect uses the file extension. Override only if "
                "the extension lies (rare)."
            ),
        )
        file_format = None if chosen_format == _AUTO_LABEL else chosen_format

    with col_b:
        question_id = st.text_input(
            "Question filter (optional)",
            value="",
            help="Restrict to one question_id. Leave blank to ingest all.",
        ).strip() or None

    with col_c:
        st.markdown("**File details**")
        st.write(f"Name: `{uploaded.name}`")
        st.write(f"Size: {len(raw):,} bytes")
        if show_dev:
            st.write(f"Temp path: `{tmp_path}`")
            st.write(f"Auto-detected: `{auto}`")

    # -----------------------------------------------------------------
    # Step 2: parse-preview
    # -----------------------------------------------------------------
    st.subheader("2. Preview what would be ingested")

    if st.button("Parse and preview", type="primary"):
        try:
            conn = open_connection(db_path)
            preview = preview_file(
                conn,
                tmp_path,
                question_id=question_id,
                file_format=file_format,
            )
            st.session_state["preview"] = preview
            st.session_state["committed_result"] = None
        except ValueError as exc:
            # e.g. unknown format / unsupported extension; user can fix.
            st.warning(str(exc))
            st.session_state["preview"] = None
        except Exception as exc:
            st.error(
                "Something went wrong while parsing. Toggle "
                "'Show technical details' for more."
            )
            if show_dev:
                st.exception(exc)
            st.session_state["preview"] = None

    preview: PreviewResult | None = st.session_state["preview"]

    if preview is not None:
        # ----- Summary metrics --------------------------------------
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Parsed rows", preview.n_total)
        m2.metric("New (would insert)", preview.n_new)
        m3.metric("Duplicates (would skip)", preview.n_duplicates)
        m4.metric("Unique students", preview.student_count)

        # ----- Parser warnings --------------------------------------
        # Rows the parser logged-and-skipped during preview (malformed
        # timestamps, missing required fields, etc.). Surfaced so a
        # researcher notices dropped rows instead of silently trusting
        # the parsed count.
        if preview.parse_errors:
            st.warning(
                f"**Parser warnings ({len(preview.parse_errors)})**\n\n"
                + "\n".join(f"- {msg}" for msg in preview.parse_errors)
            )

        # ----- File-level dup warning -------------------------------
        if preview.duplicate_file_import is not None:
            st.warning(
                f"This exact file (by hash) was already imported as "
                f"`import_id = {preview.duplicate_file_import}`. "
                f"The ingest step will refuse to re-import it. If you "
                f"need to re-process, delete the prior import first."
            )

        # ----- Question summary -------------------------------------
        if preview.question_ids:
            with st.expander(f"Question IDs in this file ({len(preview.question_ids)})"):
                for q in preview.question_ids:
                    n = sum(1 for r in preview.rows if r.question_id == q)
                    st.write(f"- `{q}` — {n} responses")

        # ----- The simplified table previewer -----------------------
        # This is the throwaway-or-enhance widget. It uses a pandas
        # DataFrame and st.dataframe; later we can swap in a more
        # capable component (e.g. AgGrid) without touching the
        # service layer.
        st.markdown("**Parsed rows**")
        if not preview.rows:
            if preview.parse_errors:
                st.info(
                    "Parser yielded zero responses — every candidate row was "
                    "skipped. See the parser warnings above for why (e.g. "
                    "malformed timestamps or missing required fields)."
                )
            else:
                st.info(
                    "Parser yielded zero responses. Common causes: the "
                    "question filter matched nothing, or the file contains "
                    "only non-short-answer events."
                )
        else:
            hide_dupes = st.checkbox(
                "Hide rows that would be skipped (duplicates)", value=False
            )
            visible_rows = [
                r for r in preview.rows
                if (not hide_dupes) or r.response_id not in preview.duplicate_response_ids
            ]
            df_view = pd.DataFrame([
                {
                    "response_id": r.response_id,
                    "student_id": r.student_id,
                    "question_id": r.question_id,
                    "submitted_at": r.submitted_at,
                    "text_preview": (
                        r.response_text[:120] + "…"
                        if len(r.response_text) > 120
                        else r.response_text
                    ),
                    "duplicate": r.response_id in preview.duplicate_response_ids,
                    "course_id": r.course_id or "",
                }
                for r in visible_rows
            ], columns=[
                "response_id",
                "student_id",
                "question_id",
                "submitted_at",
                "text_preview",
                "duplicate",
                "course_id",
            ])

            # Researcher view hides the response_id; dev view shows it.
            display_cols = (
                ["response_id", "student_id", "question_id", "submitted_at",
                 "text_preview", "duplicate", "course_id"]
                if show_dev
                else ["student_id", "question_id", "submitted_at",
                      "text_preview", "duplicate", "course_id"]
            )

            st.dataframe(
                df_view[display_cols],
                width='stretch',
                hide_index=True,
                height=380,
            )

            # Full-text drill-down: pick a row, see the whole response.
            with st.expander("Inspect one response in full"):
                if visible_rows:
                    def _row_label(idx: int) -> str:
                        row = visible_rows[idx]
                        if show_dev:
                            return row.response_id
                        return f"{row.student_id} · {row.question_id} · {row.submitted_at}"

                    pick = st.selectbox(
                        "Row",
                        options=list(range(len(visible_rows))),
                        index=0,
                        format_func=_row_label,
                    )
                    chosen = visible_rows[pick]
                    st.text_area(
                        "response_text",
                        value=chosen.response_text,
                        height=200,
                        disabled=True,
                    )
                    if show_dev:
                        st.code(
                            f"response_id = {chosen.response_id}\n"
                            f"student_id  = {chosen.student_id}\n"
                            f"question_id = {chosen.question_id}\n"
                            f"submitted_at= {chosen.submitted_at}\n"
                            f"course_id   = {chosen.course_id!r}",
                            language="text",
                        )

        # ----- Dev panel --------------------------------------------
        if show_dev:
            with st.expander("Technical details"):
                st.write(f"file_hash: `{preview.file_hash}`")
                st.write(f"file_format: `{preview.file_format}`")
                st.write(f"duplicate_response_ids: {len(preview.duplicate_response_ids)}")
                st.write(f"duplicate_file_import: {preview.duplicate_file_import}")

        # -----------------------------------------------------------------
        # Step 3: commit
        # -----------------------------------------------------------------
        st.subheader("3. Commit to the database")

        can_commit = preview.duplicate_file_import is None and preview.n_total > 0
        if not can_commit:
            if preview.duplicate_file_import is not None:
                st.info("Commit disabled: this file is already imported.")
            else:
                st.info("Commit disabled: nothing to insert.")

        # A form so the notes value is guaranteed current when the commit
        # fires. Outside a form, the button click can be processed before
        # the text_input's latest value registers, dropping the note.
        with st.form("commit_form"):
            notes = st.text_input(
                "Notes (optional, stored in raw_imports.notes)",
                value="" if question_id is None else f"question_id={question_id!r}",
                disabled=not can_commit,
            )
            submitted = st.form_submit_button(
                "Commit import",
                type="primary",
                disabled=not can_commit,
                help="Writes a raw_imports row and inserts new responses in one transaction.",
            )

        if submitted:
            try:
                conn = open_connection(db_path)
                result = commit_file(
                    conn,
                    tmp_path,
                    question_id=question_id,
                    file_format=file_format,
                    notes=notes,
                )
                st.session_state["committed_result"] = result
                # Invalidate the preview so we don't double-commit.
                st.session_state["preview"] = None
                st.rerun()
            except FileExistsError as exc:
                st.error(f"Refused: {exc}")
            except ValueError as exc:
                st.warning(str(exc))
            except Exception as exc:
                st.error(
                    "Something went wrong while committing. Toggle "
                    "'Show technical details' for more."
                )
                if show_dev:
                    st.exception(exc)

    # -----------------------------------------------------------------
    # Post-commit confirmation (shown after st.rerun above)
    # -----------------------------------------------------------------
    result = st.session_state.get("committed_result")
    if result is not None:
        st.success(
            f"Committed `import_id = {result.import_id}` · "
            f"inserted {result.inserted} · skipped {result.skipped} · "
            f"total parsed {result.total_seen}"
        )
        if show_dev:
            st.code(
                f"file_path   = {result.file_path}\n"
                f"file_format = {result.file_format}\n"
                f"import_id   = {result.import_id}\n"
                f"inserted    = {result.inserted}\n"
                f"skipped     = {result.skipped}\n"
                f"total_seen  = {result.total_seen}",
                language="text",
            )
        if st.button("Ingest another file"):
            _reset_state()
            st.rerun()

else:
    st.info("Upload a file to begin.")
    # Clear state if user removed the upload after committing.
    if st.session_state["upload_key"] is not None:
        _reset_state()
