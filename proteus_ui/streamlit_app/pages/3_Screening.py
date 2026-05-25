"""
Screening page — Stage 2 of the PROTEUS pipeline.

Mirrors the page pattern described in the UI guide:

* Header strip: run selector dropdown (defaults to most recent),
  'Run screening' button that screens any pending responses in the
  selected run, and a 'Start new run' button for prompt-A vs prompt-B
  comparison runs.
* Status panel: pending / screened / total metrics plus the label
  distribution for the selected run.
* Result browser: master `st.dataframe` filterable by label, question,
  and substring; detail panel below showing the full screening
  rationale and response text for the selected row.
* CSV download of the visible (filtered) rows.

All business logic is in services.screening_service. This page only
arranges widgets and forwards user actions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# pages/3_Screening.py is two levels below proteus_ui/; services/ is one below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.ingest_service import open_connection  # noqa: E402



# ---------------------------------------------------------------------------
# Page config and shared state
# ---------------------------------------------------------------------------

st.set_page_config(page_title="PROTEUS · Screening", layout="wide")
st.title("Screening")

# DB path and dev toggle are owned by Homepage.py and persisted in
# st.session_state. Fall back to a repo-root default so this page works
# if a user lands here directly. parents[3] is the repo root:
# pages/ → streamlit_app/ → proteus_ui/ → repo root.
_DEFAULT_DB = str(Path(__file__).resolve().parents[3] / "proteus.db")
db_path = Path(st.session_state.get("db_path", _DEFAULT_DB))
show_dev = st.session_state.get("show_dev", False)

st.caption(f"Reading from `{db_path}`")

# Placeholder for the screening stage
st.write("🚧 Place holder for Screening stage 🚧")

try:
    conn = open_connection(db_path)
except Exception as exc:
    st.error("Something went wrong opening the database. Toggle "
             "'Show technical details' for more.")
    if show_dev:
        st.exception(exc)
    st.stop()