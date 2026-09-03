"""Ledger Sentinel — Pro Dashboard

streamlit run dashboard/app.py

Professional, demo-ready. One screen with:
- KPI cards + amount at risk
- Charts (exception by reason, match gauge)
- Priority inbox (largest gaps)
- Searchable / filterable exception table
- AI Finance Assistant chat
- CSV upload & live reconcile
- PDF report download
- Live webhook feed
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import streamlit as st

from app.config import db_path
from app import db

st.set_page_config(page_title="Ledger Sentinel — Finance Control", layout="wide", page_icon="◈")

# --- Professional styling ---
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.block-container { padding-top: 1.2rem; }
.kpi-card { background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
.kpi-label { font-size: 0.75rem; letter-spacing: 0.06em; text-transform: uppercase; color: #64748b; font-weight: 600; margin-bottom: 4px; }
.kpi-value { font-size: 1.65rem; font-weight: 700; color: #0f172a; line-height: 1; }
.kpi-sub { font-size: 0.78rem; color: #94a3b8; margin-top: 6px; }
.badge { display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.7rem; font-weight:600; }
.badge-green { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; }
.badge-amber { background:#fffbeb; color:#92400e; border:1px solid #fde68a; }
.badge-red { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; }
.badge-slate { background:#f8fafc; color:#475569; border:1px solid #e2e8f0; }
hr { margin: 0.8rem 0; }
</style>
""", unsafe_allow_html=True)

# Header
col_h1, col_h2 = st.columns([4, 1])
with col_h1:
    st.markdown("## ◈ Ledger Sentinel")
    st.caption("Razorpay AI Finance Controller — reconciliation, exception audit & AI explanations  ·  Deterministic first, AI only on the residue")
with col_h2:
    st.markdown("<div style='text-align:right; margin-top:8px'><span class='badge badge-green'>● Live</span> <span class='badge badge-slate'>v1.1 Pro</span></div>", unsafe_allow_html=True)
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# Load data
@st.cache_data(ttl=5)
def load_audit():
    path = Path(db_path())
    if not path.exists():
        return pd.DataFrame()
    try:
        conn = db.get_connection(db_path())
        try:
            return db.load_audit_df(conn)
        finally:
            conn.close()
    except Exception as e:
        st.error(f"Failed to load audit_log: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=5)
def load_orders_settlement():
    try:
        conn = db.get_connection(db_path())
        try:
            o = db.load_orders_df(conn)
            s = db.load_settlement_df(conn)
            return o, s
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame(), pd.DataFrame()

audit = load_audit()

if audit.empty:
    st.warning("No audit data yet. Run the pipeline first:")
    st.code("python data/generate_synthetic_data.py\npython -m app.main", language="bash")
    st.info(f"Looking for DB at `{db_path()}` — set LEDGER_DB_PATH in .env if you used a custom path.")
    st.stop()

for col in ["outcome", "reason", "classification", "audit_note", "created_at", "order_id"]:
    if col not in audit.columns:
        audit[col] = None

# Compute metrics
recon = audit[audit["outcome"].isin(["matched", "exception"])]
matched = int((recon["outcome"] == "matched").sum())
exceptions_n = int((recon["outcome"] == "exception").sum())
total = matched + exceptions_n
match_rate = matched / len(recon) * 100 if len(recon) else 0.0

# At-risk money (sum of diff for exceptions where available)
try:
    recon_full = recon.copy()
    # diff not stored in audit_log; estimate from at-risk via audit_note? fallback: count * avg gap
    # Better: load orders/settlement to compute actual diff for priority
    orders_df, settlement_df = load_orders_settlement()
    at_risk = 0.0
    if not orders_df.empty and not settlement_df.empty:
        from app.reconcile import reconcile as do_reconcile
        _m, _e = do_reconcile(orders_df, settlement_df)
        if not _e.empty and "diff" in _e.columns:
            at_risk = float(_e["diff"].abs().sum())
except Exception:
    at_risk = 0.0

# KPI row — using columns with card styling
c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    st.markdown(f"<div class='kpi-card'><div class='kpi-label'>Match rate</div><div class='kpi-value'>{match_rate:.1f}%</div><div class='kpi-sub'>{matched} of {total} rows</div></div>", unsafe_allow_html=True)
with c2:
    st.markdown(f"<div class='kpi-card'><div class='kpi-label'>Matched</div><div class='kpi-value' style='color:#059669'>{matched}</div><div class='kpi-sub'>within Rs 0.01 tolerance</div></div>", unsafe_allow_html=True)
with c3:
    color = "#d97706" if exceptions_n > 0 else "#059669"
    st.markdown(f"<div class='kpi-card'><div class='kpi-label'>Exceptions</div><div class='kpi-value' style='color:{color}'>{exceptions_n}</div><div class='kpi-sub'>need attention</div></div>", unsafe_allow_html=True)
with c4:
    st.markdown(f"<div class='kpi-card'><div class='kpi-label'>Amount at risk</div><div class='kpi-value'>Rs {at_risk:,.0f}</div><div class='kpi-sub'>sum of gaps</div></div>", unsafe_allow_html=True)
with c5:
    webhook_ok = int((audit["outcome"].isin(["applied", "duplicate_skipped"])).sum())
    st.markdown(f"<div class='kpi-card'><div class='kpi-label'>Webhook events</div><div class='kpi-value'>{webhook_ok}</div><div class='kpi-sub'>applied + deduped</div></div>", unsafe_allow_html=True)

# Charts row
chart_col1, chart_col2, chart_col3 = st.columns([1.2, 1, 1])

with chart_col1:
    st.markdown("**Exceptions by reason**")
    if exceptions_n == 0:
        st.success("No exceptions — everything reconciled.")
    else:
        by_reason = recon[recon["outcome"] == "exception"]["reason"].value_counts().sort_index()
        # Streamlit bar chart (no extra deps)
        chart_df = pd.DataFrame({"reason": by_reason.index, "count": by_reason.values}).set_index("reason")
        st.bar_chart(chart_df, height=180)
        # also show as small table
        st.dataframe(by_reason.rename("count").to_frame(), height=140, use_container_width=True)

with chart_col2:
    st.markdown("**Match gauge**")
    # Donut via progress + metrics
    st.metric("Reconciliation health", f"{match_rate:.1f}%", delta=f"{exceptions_n} exceptions", delta_color="inverse")
    st.progress(min(1.0, match_rate / 100))
    # Quick legend
    st.markdown("<span class='badge badge-green'>matched</span> <span class='badge badge-amber'>exception</span> <span class='badge badge-slate'>webhook</span>", unsafe_allow_html=True)
    # Mini amount at risk bar
    if at_risk > 0:
        st.caption(f"Rs {at_risk:,.2f} held in exceptions — prioritize largest gaps first.")

with chart_col3:
    st.markdown("**Priority inbox — largest gaps**")
    try:
        orders_df, settlement_df = load_orders_settlement()
        if not orders_df.empty and not settlement_df.empty:
            from app.reconcile import reconcile as do_reconcile
            _m, _e = do_reconcile(orders_df, settlement_df)
            if not _e.empty:
                top = _e.sort_values("diff", ascending=False, na_position="last").head(5)
                for _, r in top.iterrows():
                    diff = r.get("diff", 0)
                    reason = r.get("reason", "")
                    oid = r.get("order_id", "")
                    color_badge = "badge-amber" if reason == "exception_tds_candidate" else "badge-red" if reason == "exception_unexplained" else "badge-slate"
                    st.markdown(f"<div style='padding:6px 0; border-bottom:1px solid #f1f5f9'><span class='badge {color_badge}'>{reason}</span> <b>{oid}</b> — Rs {diff:,.2f} gap</div>", unsafe_allow_html=True)
            else:
                st.caption("No gaps — all matched.")
        else:
            st.caption("Load orders/settlement to see priority.")
    except Exception as e:
        st.caption(f"Priority unavailable: {e}")

st.divider()

# Filters + exception table
st.markdown("### Exceptions — filter, search, export")
exceptions_df = audit[audit["outcome"] == "exception"].copy()

if exceptions_df.empty:
    st.success("No exceptions — everything reconciled.")
else:
    f1, f2, f3, f4 = st.columns([1, 1, 1.2, 1])
    with f1:
        reason_opts = ["(all)"] + sorted(exceptions_df["reason"].dropna().unique().tolist())
        sel_reason = st.selectbox("Reason", reason_opts, index=0)
    with f2:
        class_opts = ["(all)"] + sorted(exceptions_df["classification"].dropna().unique().tolist())
        sel_class = st.selectbox("Classification", class_opts, index=0)
    with f3:
        q = st.text_input("Search order / note", placeholder="e.g. order_0010 or TDS")
    with f4:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        # Export buttons
        csv_all = audit.to_csv(index=False).encode("utf-8")
        csv_exc = exceptions_df.to_csv(index=False).encode("utf-8")
        col_e1, col_e2 = st.columns(2)
        with col_e1:
            st.download_button("⬇ CSV (exceptions)", csv_exc, "exceptions.csv", "text/csv", use_container_width=True)
        with col_e2:
            # PDF via API or direct build
            try:
                from app.report import build_pdf
                recon_tmp = audit[audit["outcome"].isin(["matched", "exception"])]
                m = int((recon_tmp["outcome"] == "matched").sum())
                e = int((recon_tmp["outcome"] == "exception").sum())
                t = m + e
                rr = round(m / t * 100, 1) if t else 0
                by = recon_tmp["reason"].value_counts().to_dict() if "reason" in recon_tmp.columns else {}
                pdf_bytes = build_pdf(audit, {"total_rows": t, "matched": m, "exceptions": e, "match_rate_pct": rr, "by_reason": by})
                st.download_button("⬇ PDF report", pdf_bytes, "Ledger_Sentinel_Audit_Report.pdf", "application/pdf", use_container_width=True)
            except Exception as e:
                st.caption(f"PDF unavailable: {e}")

    view = exceptions_df.copy()
    if sel_reason != "(all)":
        view = view[view["reason"] == sel_reason]
    if sel_class != "(all)":
        view = view[view["classification"] == sel_class]
    if q:
        ql = q.lower()
        view = view[view.apply(lambda r: ql in str(r.get("order_id","")).lower() or ql in str(r.get("audit_note","")).lower() or ql in str(r.get("reason","")).lower(), axis=1)]

    # Nice column order
    cols = [c for c in ["order_id", "reason", "classification", "audit_note", "created_at"] if c in view.columns]
    st.dataframe(view[cols], use_container_width=True, height=380, hide_index=True)
    st.caption(f"Showing {len(view)} of {len(exceptions_df)} exceptions")
    # Quick fix hint
    with st.expander("What do these reasons mean?"):
        st.markdown("""
- **exception_tds_candidate** — gap ~2% of gross, looks like a TDS/TCS tax withholding. Usually expected.
- **status_mismatch** — your ledger says failed/pending but settlement says captured (late auth flip).
- **exception_unexplained** — large gap that doesn't match any known pattern. Needs human review.
- **missing_settlement** — order exists but no settlement row (failed orders).
- **missing_order** — settlement exists but no matching order (orphan).
""")

# Two-column: AI Assistant + Upload
left, right = st.columns([1.1, 0.9])

with left:
    st.markdown("### ◉ AI Finance Assistant — ask your ledger")
    st.caption("Try:  “Why is order_0010 flagged?”  ·  “What is the match rate?”  ·  “Which exceptions are TDS?”")
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    # Show history
    for role, msg in st.session_state.chat_history[-8:]:
        with st.chat_message(role):
            st.markdown(msg)
    q = st.chat_input("Ask about any order, reason, or summary…")
    if q:
        st.session_state.chat_history.append(("user", q))
        with st.chat_message("user"):
            st.markdown(q)
        # Answer
        try:
            from app.assistant import ask as ledger_ask
            res = ledger_ask(q, audit)
            ans = res.get("answer", "")
            src = res.get("source", "")
            label = "Claude" if src == "claude" else "Heuristic" if src == "heuristic" else src
            with st.chat_message("assistant"):
                st.markdown(ans)
                st.caption(f"via {label}")
            st.session_state.chat_history.append(("assistant", ans))
        except Exception as e:
            with st.chat_message("assistant"):
                st.error(f"Assistant error: {e}")

with right:
    st.markdown("### ⤓ Bring your own data — reconcile now")
    st.caption("Upload your own orders.csv + settlement.csv to test live. No data is stored. Amount-only mode (webhook status ignored).")
    st.info("**Note:** Upload does amount-only matching (like the README's 85.2% on sample files). The full pipeline (`python -m app.main`) replays webhooks and shows 80.3% — the 3-row delta is the planted bad-signature test.", icon="ℹ️")
    with st.form("upload_form", clear_on_submit=False):
        up_orders = st.file_uploader("orders.csv", type=["csv"], key="up_orders")
        up_sett = st.file_uploader("settlement.csv", type=["csv"], key="up_sett")
        submitted = st.form_submit_button("Reconcile uploads", use_container_width=True, type="primary")
    if submitted:
        if not up_orders or not up_sett:
            st.warning("Upload both files.")
        else:
            try:
                orders_up = pd.read_csv(up_orders)
                sett_up = pd.read_csv(up_sett)
                # Enforce same guards as API (5MB already by Streamlit, but check rows)
                if len(orders_up) > 10000 or len(sett_up) > 10000:
                    st.error("Too many rows (max 10,000 per file).")
                    st.stop()
                # Amount-only: ignore status column (webhook integrity is pipeline-only)
                if "status" in orders_up.columns:
                    orders_up = orders_up.copy()
                    orders_up["status"] = None
                from app.reconcile import reconcile as do_reconcile, summarize
                from app.classify import classify_exception
                m, e = do_reconcile(orders_up, sett_up)
                s = summarize(m, e)
                st.success(f"Done — {s['matched']} matched, {s['exceptions']} exceptions — **{s['match_rate_pct']}%** match rate")
                st.json(s)
                if not e.empty:
                    # preview with AI notes
                    preview_rows = []
                    for _, r in e.head(10).iterrows():
                        row = {k: (v if not pd.isna(v) else None) for k, v in r.to_dict().items()}
                        try:
                            c = classify_exception(row)
                        except Exception:
                            c = {"classification": "unresolved", "audit_note": ""}
                        preview_rows.append({"order_id": str(r.get("order_id")), "reason": str(r.get("reason")), "diff": float(r.get("diff")) if pd.notna(r.get("diff")) else None, **c})
                    st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)
                    st.download_button("⬇ Download exceptions (upload result)", pd.DataFrame(preview_rows).to_csv(index=False).encode("utf-8"), "upload_exceptions.csv", "text/csv")
                # Sample format hint
                with st.expander("CSV format help"):
                    st.markdown("**orders.csv** needs: `order_id, amount, mdr, gst, category, status`\n**settlement.csv** needs: `order_id, amount_settled, settlement_status, utr, settlement_date`")
            except Exception as e:
                st.error(f"Reconcile failed: {e}")

    st.markdown("---")
    st.markdown("**Live webhook feed** — last 10 deliveries")
    feed = audit.sort_values("id" if "id" in audit.columns else "created_at", ascending=False).head(10)
    for _, r in feed.iterrows():
        outcome = r.get("outcome", "")
        icon = "✓" if outcome == "matched" else "⚠" if outcome == "exception" else "●" if outcome == "applied" else "○"
        color = "#059669" if outcome == "matched" else "#d97706" if outcome == "exception" else "#64748b"
        st.markdown(f"<div style='font-size:0.82rem; padding:3px 0; border-bottom:1px solid #f8fafc'><span style='color:{color}; font-weight:700'>{icon} {outcome}</span> — <b>{r.get('order_id','')}</b> <span style='color:#94a3b8'>{str(r.get('reason','') or r.get('classification',''))[:40]}</span> <span style='float:right; color:#cbd5e1; font-size:0.75rem'>{r.get('created_at','')}</span></div>", unsafe_allow_html=True)

# Full audit trail
with st.expander("Full audit trail — all outcomes"):
    st.dataframe(audit, use_container_width=True, height=360, hide_index=True)
    st.download_button("⬇ Download full audit CSV", audit.to_csv(index=False).encode("utf-8"), "audit_log.csv", "text/csv")
    st.caption(f"Total audit rows: {len(audit)}  ·  DB: `{db_path()}`")

with st.expander("Debug — DB info"):
    st.write(f"DB path: `{db_path()}`")
    st.write(f"Total audit rows: {len(audit)}")
    try:
        st.write(audit["outcome"].value_counts().to_dict())
    except Exception:
        pass
    st.write("API: `GET /health` · `GET /stats` · `POST /ask` · `GET /report.pdf` · `GET /export.csv` · `POST /reconcile-upload` · `POST /run-pipeline`")
