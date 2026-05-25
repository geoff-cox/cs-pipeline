"""
Imports page — browse the raw_imports table.

Master/detail layout:

* Master: a dataframe of every raw_imports row, newest first, with
  joined response counts.
* Detail: when the user selects a row, a panel below shows the import's
  metadata, downstream-stage progress (how many of its responses have
  been screened, extracted, matched, disposed), and a paginated /
  filterable view of its responses.

Read-only. Rollback will be a separate action and is not implemented
on this page yet — when added, it will live as a button inside the
detail panel with a two-step confirmation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# pages/2_Imports.py is two levels below proteus_ui/; services/ is one below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.ingest_service import (  # noqa: E402
    ImportSummary,
    list_imports,
    open_connection,
)
from services.imports_service import (  # noqa: E402
    ImportDetail,
    count_responses_for_import,
    get_import_detail,
    list_responses_for_import,
)


# ---------------------------------------------------------------------------
# Page config and shared state
# ---------------------------------------------------------------------------

st.set_page_config(page_title="PROTEUS · Imports", layout="wide")
st.title("Imports")

# Pull the DB path and dev toggle from session_state (set on the landing
# page). Fall back to a repo-root default so this page works if a user
# lands here directly. parents[3] is the repo root:
# pages/ → streamlit_app/ → proteus_ui/ → repo root.
_DEFAULT_DB = str(Path(__file__).resolve().parents[3] / "proteus.db")
db_path = Path(st.session_state.get("db_path", _DEFAULT_DB))
show_dev = st.session_state.get("show_dev", False)

st.caption(f"Reading from `{db_path}`")

try:
    conn = open_connection(db_path)
except Exception as exc:
    st.error(f"Could not open database: {exc}")
    if show_dev:
        st.exception(exc)
    st.stop()


# ---------------------------------------------------------------------------
# Master table
# ---------------------------------------------------------------------------

imports: list[ImportSummary] = list_imports(conn)

if not imports:
    st.info(
        "No imports yet. Head to the **Ingest** page to add your first file."
    )
    st.stop()

# Build the display dataframe. Researcher view hides file_hash and
# truncates file_path to the filename only; dev view shows both.
def _row(i: ImportSummary) -> dict:
    base = {
        "import_id": i.import_id,
        "file": Path(i.file_path).name,
        "format": i.file_format,
        "imported_at": i.imported_at,
        "rows in DB": i.rows_in_db,
        "rows in file": i.row_count if i.row_count is not None else "",
        "notes": i.notes or "",
    }
    if show_dev:
        base["file_hash"] = i.file_hash[:12] + "…"
        base["full_path"] = i.file_path
    return base


master_df = pd.DataFrame([_row(i) for i in imports])

st.markdown("**All imports** — click a row to see details below.")

selection = st.dataframe(
    master_df,
    width='stretch',
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    height=min(380, 80 + 35 * len(imports)),
)

selected_rows = selection.selection.rows if selection and selection.selection else []
selected_import_id: int | None = None
if selected_rows:
    selected_import_id = int(master_df.iloc[selected_rows[0]]["import_id"])


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------

st.divider()

if selected_import_id is None:
    st.info("Select an import above to see its details.")
    st.stop()

detail: ImportDetail | None = get_import_detail(conn, selected_import_id)
if detail is None:
    # Shouldn't happen unless the table is stale; refresh and bail.
    st.warning(f"Import #{selected_import_id} not found. Reload the page.")
    st.stop()

st.subheader(f"Import #{detail.import_id}")

# ----- Metadata strip ---------------------------------------------------
col_a, col_b, col_c = st.columns([2, 1, 1])
with col_a:
    st.write(f"**File:** `{Path(detail.file_path).name}`")
    if show_dev:
        st.write(f"**Full path:** `{detail.file_path}`")
        st.write(f"**Hash:** `{detail.file_hash}`")
with col_b:
    st.write(f"**Format:** `{detail.file_format}`")
    st.write(f"**Imported:** {detail.imported_at}")
with col_c:
    st.write(f"**Rows in file:** {detail.row_count or '—'}")
    st.write(f"**Rows in DB:** {detail.status.n_responses}")

if detail.notes:
    st.markdown(f"**Notes:** {detail.notes}")


# ----- Downstream-stage progress ---------------------------------------
st.markdown("**Pipeline progress**")
st.caption(
    "Distinct responses that have reached each stage. A response counts "
    "once even if it has been processed in multiple pipeline runs."
)

s = detail.status
p1, p2, p3, p4 = st.columns(4)
p1.metric("Screened", s.n_screened, f"{s.pct_screened:.0f}%")
p2.metric("Extracted", s.n_extracted, f"{s.pct_extracted:.0f}%")
p3.metric("Matched", s.n_matched, f"{s.pct_matched:.0f}%")
p4.metric("Disposed", s.n_disposed, f"{s.pct_disposed:.0f}%")

if s.n_responses == 0:
    st.warning(
        "This import has zero responses in the database. It may have "
        "been rolled back, or the ingest may have skipped every row "
        "as a duplicate."
    )


# ----- Response list ----------------------------------------------------
st.markdown("**Responses in this import**")

filter_col_a, filter_col_b = st.columns([1, 2])
with filter_col_a:
    q_options = ["(all questions)"] + detail.question_ids
    q_choice = st.selectbox(
        "Question filter",
        options=q_options,
        index=0,
        key=f"q_filter_{detail.import_id}",
    )
    q_filter = None if q_choice == "(all questions)" else q_choice

with filter_col_b:
    text_search = st.text_input(
        "Text search (response_text contains)",
        value="",
        key=f"text_filter_{detail.import_id}",
    ).strip() or None

# ----- Pagination -------------------------------------------------------
# Page size is a session-wide preference; the current page resets to 1
# whenever the import, filters, or page size change (a context shift
# would otherwise leave us on a page that no longer exists).
PAGE_SIZE_OPTIONS = [25, 50, 100, 250]
PAGE_KEY = "imports_current_page"
SIG_KEY = "imports_page_context"

page_size = st.selectbox(
    "Rows per page",
    options=PAGE_SIZE_OPTIONS,
    index=PAGE_SIZE_OPTIONS.index(100),
    key="imports_page_size",
)

total = count_responses_for_import(
    conn,
    detail.import_id,
    question_id=q_filter,
    text_search=text_search,
)
n_pages = max(1, -(-total // page_size))  # ceil division

context = (detail.import_id, q_filter, text_search, page_size)
if st.session_state.get(SIG_KEY) != context:
    st.session_state[SIG_KEY] = context
    st.session_state[PAGE_KEY] = 1

# Clamp in case a callback or a shrunk result set left us past the end.
page = min(max(1, st.session_state.get(PAGE_KEY, 1)), n_pages)
st.session_state[PAGE_KEY] = page


def _go_prev() -> None:
    st.session_state[PAGE_KEY] = max(1, st.session_state.get(PAGE_KEY, 1) - 1)


def _go_next() -> None:
    st.session_state[PAGE_KEY] = st.session_state.get(PAGE_KEY, 1) + 1


responses = list_responses_for_import(
    conn,
    detail.import_id,
    question_id=q_filter,
    text_search=text_search,
    limit=page_size,
    offset=(page - 1) * page_size,
)

if not responses:
    st.info("No responses match the current filter.")
else:
    start = (page - 1) * page_size + 1
    end = start + len(responses) - 1
    nav_prev, nav_caption, nav_next = st.columns([1, 3, 1])
    with nav_prev:
        st.button(
            "◀ Prev",
            on_click=_go_prev,
            disabled=page <= 1,
            width='stretch',
            key=f"prev_{detail.import_id}",
        )
    with nav_caption:
        st.markdown(
            f"<div style='text-align:center'>Showing {start}–{end} of "
            f"{total} &nbsp;·&nbsp; page {page} of {n_pages}</div>",
            unsafe_allow_html=True,
        )
    with nav_next:
        st.button(
            "Next ▶",
            on_click=_go_next,
            disabled=page >= n_pages,
            width='stretch',
            key=f"next_{detail.import_id}",
        )

    df_rows = pd.DataFrame([
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
            "course_id": r.course_id or "",
        }
        for r in responses
    ])

    display_cols = (
        ["response_id", "student_id", "question_id", "submitted_at",
         "text_preview", "course_id"]
        if show_dev
        else ["student_id", "question_id", "submitted_at",
              "text_preview", "course_id"]
    )

    st.dataframe(
        df_rows[display_cols],
        width='stretch',
        hide_index=True,
        height=380,
    )

    # Inline export — researchers will want this immediately. Exports the
    # current page only; the file name records which rows it covers.
    csv_bytes = df_rows.to_csv(index=False).encode("utf-8")
    st.download_button(
        label=f"Download this page ({len(responses)} rows) as CSV",
        data=csv_bytes,
        file_name=f"import_{detail.import_id}_responses_{start}-{end}.csv",
        mime="text/csv",
    )

    # Drill-down: pick a response, see the full text.
    with st.expander("Inspect one response in full"):
        options = (
            [r.response_id for r in responses] if show_dev
            else [f"{r.student_id} · {r.question_id} · {r.submitted_at}"
                  for r in responses]
        )
        pick = st.selectbox("Row", options, index=0, key=f"pick_{detail.import_id}")
        idx = options.index(pick)
        chosen = responses[idx]
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


# ----- Placeholder for rollback ----------------------------------------
st.divider()
st.markdown("**Danger zone**")
st.caption(
    "Rollback (delete this import and all derived rows) is not yet "
    "implemented. It will live here, behind a two-step confirmation, "
    "once the cascade behavior is decided. See README for the design note."
)
st.button("Delete this import", disabled=True, help="Coming soon.")
