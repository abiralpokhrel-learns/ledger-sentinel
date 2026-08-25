"""Ledger Sentinel dashboard — one screen, demo-ready.

    streamlit run dashboard/app.py

Shows the match rate, exception count, and the full exception list with the
AI-written audit notes. Reads directly from the populated SQLite audit_log.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

from app.config import db_path
from app import db

st.set_page_config(page_title="Ledger Sentinel", layout="wide")
st.title("Ledger Sentinel")
st.caption("Razorpay AI Finance Controller — reconciliation & exception audit")


@st.cache_data(ttl=5)
def load_audit():
    conn = db.get_connection(db_path())
    try:
        return db.load_audit_df(conn)
    finally:
        conn.close()


audit = load_audit()

if audit.empty:
    st.warning("No audit data yet. Run the pipeline first: `python -m app.main`")
    st.stop()

# Match rate is graded over RECONCILIATION rows only (matched + exception).
# Webhook-stage audit rows (applied / duplicate_skipped / dropped / rejected)
# are ingestion history, not reconciliation verdicts, and must not dilute it.
recon = audit[audit["outcome"].isin(["matched", "exception"])]
matched = (recon["outcome"] == "matched").sum()
exceptions = (recon["outcome"] == "exception").sum()
match_rate = matched / len(recon) * 100 if len(recon) else 0.0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Match rate", f"{match_rate:.1f}%")
col2.metric("Matched", int(matched))
col3.metric("Exceptions", int(exceptions))
col4.metric("Webhook events processed", int((audit["outcome"].isin(
    ["applied", "duplicate_skipped"])).sum()))

st.subheader("Exceptions")
exceptions_df = audit[audit["outcome"] == "exception"].copy()
if exceptions_df.empty:
    st.success("No exceptions — everything reconciled.")
else:
    view = exceptions_df[
        ["order_id", "reason", "classification", "audit_note", "created_at"]
    ]
    st.dataframe(view, use_container_width=True, height=420)

with st.expander("Full audit trail"):
    st.dataframe(audit, use_container_width=True, height=400)
