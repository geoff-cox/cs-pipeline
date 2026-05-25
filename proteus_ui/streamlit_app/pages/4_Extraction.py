"""
Extraction page — Stage 3 of the PROTEUS pipeline.

Mirrors the page pattern from `1_Ingest.py`. The stage-specific bit
is the detail panel: extracted chains are rendered as an ordered list
of steps (premise → conclusion) with `oov_flag` highlighting so
researchers can spot out-of-vocabulary terminology at a glance.

Stages 2 and 3 use the same `pipeline_runs` row — a run is screened
first, then extracted. The page makes this explicit in the run picker
(the eligible-pool count is "INCLUDE-screened" not "all responses")
and in the empty-state guidance.

All business logic is in services.extraction_service. This page only
arranges widgets and forwards user actions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# pages/4_Extraction.py is two levels below proteus_ui/; services/ is one below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.ingest_service import open_connection  # noqa: E402

# ---------------------------------------------------------------------------
# Page config and shared state
# ---------------------------------------------------------------------------

st.set_page_config(page_title="PROTEUS · Extraction", layout="wide")
st.title("Extraction")

# DB path and dev toggle are owned by Homepage.py and persisted in
# st.session_state. Fall back to a repo-root default so this page
# works if a user lands here directly. parents[3] is the repo root:
# pages/ → streamlit_app/ → proteus_ui/ → repo root.
_DEFAULT_DB = str(Path(__file__).resolve().parents[3] / "proteus.db")
db_path = Path(st.session_state.get("db_path", _DEFAULT_DB))
show_dev = st.session_state.get("show_dev", False)

st.caption(f"Reading from `{db_path}`")

# Placeholder for the extraction stage
st.write("🚧 Place holder for Extraction stage 🚧")

try:
    conn = open_connection(db_path)
except Exception as exc:
    st.error("Something went wrong opening the database. Toggle "
             "'Show technical details' for more.")
    if show_dev:
        st.exception(exc)
    st.stop()