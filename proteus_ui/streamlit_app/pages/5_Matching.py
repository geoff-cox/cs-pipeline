"""
Matching page — Stage 4 of the PROTEUS pipeline.

Mirrors the page pattern from `1_Ingest.py`.

All business logic is in services.matching_service. This page only
arranges widgets and forwards user actions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# pages/5_Matching.py is two levels below proteus_ui/; services/ is one below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.ingest_service import open_connection  # noqa: E402

# ---------------------------------------------------------------------------
# Page config and shared state
# ---------------------------------------------------------------------------

st.set_page_config(page_title="PROTEUS · Matching", layout="wide")
st.title("Matching")

# DB path and dev toggle are owned by Homepage.py and persisted in
# st.session_state. Fall back to a repo-root default so this page
# works if a user lands here directly. parents[3] is the repo root:
# pages/ → streamlit_app/ → proteus_ui/ → repo root.
_DEFAULT_DB = str(Path(__file__).resolve().parents[3] / "proteus.db")
db_path = Path(st.session_state.get("db_path", _DEFAULT_DB))
show_dev = st.session_state.get("show_dev", False)

st.caption(f"Reading from `{db_path}`")

# Placeholder for the matching stage
st.write("🚧 Place holder for Matching stage 🚧")

try:
    conn = open_connection(db_path)
except Exception as exc:
    st.error("Something went wrong opening the database. Toggle "
             "'Show technical details' for more.")
    if show_dev:
        st.exception(exc)
    st.stop()