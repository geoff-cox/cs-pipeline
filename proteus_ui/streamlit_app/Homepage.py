"""
PROTEUS UI — landing page.

Run with:

    streamlit run streamlit_app/Homepage.py

This is the home page of the multipage Streamlit app. The other pages
(Ingest, Imports, etc.) live under streamlit_app/pages/ and are picked
up automatically by Streamlit's multipage convention.

The landing page's job:

1. Tell first-time users what this app is and what they can do.
2. Let them point at the right database (persisted across pages via
   st.session_state so they only set it once).
3. Show top-level health stats so they know whether the DB they're
   pointed at has any data in it yet.

No heavy lifting here. Anything beyond a count belongs on a dedicated
page.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Make `services` importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ingest_service import list_imports, open_connection  # noqa: E402


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="PROTEUS UI",
    page_icon="📋",
    layout="wide",
)


# ---------------------------------------------------------------------------
# DB selector — persisted in session_state so other pages reuse it
# ---------------------------------------------------------------------------

# Anchor the default to the repo root via __file__ so it doesn't depend
# on where streamlit was launched from. Homepage.py lives at
# <repo>/proteus_ui/streamlit_app/, so parents[2] is the repo root.
_DEFAULT_DB = str(Path(__file__).resolve().parents[2] / "proteus.db")
st.session_state.setdefault("db_path", _DEFAULT_DB)
st.session_state.setdefault("show_dev", False)

with st.sidebar:
    st.header("Settings")
    db_path_str = st.text_input(
        "Database path",
        value=st.session_state["db_path"],
        help=(
            "SQLite database file. Created with the canonical PROTEUS "
            "schema if it does not exist. Other pages reuse this value."
        ),
        key="db_path_input",
    )
    # Sync into session_state so other pages see the chosen path.
    st.session_state["db_path"] = db_path_str

    show_dev = st.toggle(
        "Show technical details",
        value=st.session_state["show_dev"],
        help="Surface file hashes, response_ids, and raw SQL summaries.",
        key="show_dev_input",
    )
    st.session_state["show_dev"] = show_dev

    st.caption("Researcher view by default; toggle for dev view.")

db_path = Path(db_path_str)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("Control-Structure Coding")
st.markdown(
    "A PROTEUS tool for control-structure "
    "coding of student short-answer responses."
)

# Acknowledge that PROTEUS as an acronym is a backronym; the team can
# rename later. If they already have a name, replace the line above.

st.markdown(
    "This interface drives the five-stage automation pipeline. Right now "
    "it covers **Stage 1 (Ingest)**, import history, **Stage 2 "
    "(Screening)**, and **Stage 3 (Extraction)**. Other stages will "
    "appear in the sidebar as they're built."
)


# ---------------------------------------------------------------------------
# Database status panel
# ---------------------------------------------------------------------------

st.subheader("Database")

db_exists = db_path.exists()
col_a, col_b, col_c = st.columns([2, 1, 1])
with col_a:
    st.write(f"**Path:** `{db_path}`")
with col_b:
    if db_exists:
        st.success("Found")
    else:
        st.info("Will be created on first use")
with col_c:
    if db_exists:
        size_kb = db_path.stat().st_size / 1024
        st.write(f"**Size:** {size_kb:,.1f} KB")

# Top-level counts. Open the connection eagerly so users know immediately
# whether the schema is valid. open_connection bootstraps a missing file.
try:
    conn = open_connection(db_path)
    n_imports = conn.execute("SELECT COUNT(*) FROM raw_imports").fetchone()[0]
    n_responses = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
    n_runs = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
    n_screenings = conn.execute(
        "SELECT COUNT(DISTINCT response_id) FROM screenings"
    ).fetchone()[0]
except Exception as exc:
    st.error(f"Could not open database: {exc}")
    if show_dev:
        st.exception(exc)
    st.stop()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Imports", n_imports)
m2.metric("Responses", n_responses)
m3.metric("Pipeline runs", n_runs)
m4.metric("Responses screened", n_screenings)


# ---------------------------------------------------------------------------
# What to do next
# ---------------------------------------------------------------------------

st.subheader("Where to go")

if n_imports == 0:
    st.info(
        "**This database is empty.** Head to the **Ingest** page in the "
        "sidebar to upload a DataShop `.tab` or Runestone `.csv` export."
    )
else:
    most_recent = list_imports(conn)[0]
    st.markdown(
        f"Most recent import: **#{most_recent.import_id}** — "
        f"`{Path(most_recent.file_path).name}` "
        f"({most_recent.rows_in_db:,} responses), "
        f"imported {most_recent.imported_at}."
    )
    st.markdown(
        "- **Ingest** — add another file.\n"
        "- **Imports** — browse, inspect, and (eventually) roll back prior imports."
    )

# ---------------------------------------------------------------------------
# Dev panel
# ---------------------------------------------------------------------------

if show_dev:
    with st.expander("Technical details"):
        st.write(f"Database path: `{db_path.resolve()}`")
        st.write(f"File exists: `{db_exists}`")
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name"
            )
        ]
        st.write("Tables present:")
        st.code("\n".join(tables) or "(none)", language="text")


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.caption(
    "Prototype Frontend for Control-Structure Coding · Feedback Welcome!"
)
