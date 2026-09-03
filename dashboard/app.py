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

# --- Killer one-screen story (judge glance) ---
st.markdown(
    "<div style='background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:14px 18px; margin:8px 0 14px 0;'>"
    "<b>One-screen story:</b> orders &rarr; expected settlement &rarr; actual settlement &rarr; "
    f"<span class='badge badge-green'>{matched} auto-reconciled</span> &mdash; "
    f"<span class='badge badge-amber'>{exceptions_n} exceptions</span> &mdash; "
    f"Rs {at_risk:,.0f} at risk &mdash; "
    f"{exceptions_n} for human review &rarr; drill down below. "
    "<span style='color:#64748b'>Deterministic first, AI only on residue.</span></div>",
    unsafe_allow_html=True,
)

# --- 10-order beautiful story + batched insight ---
with st.expander("10-Order Beautiful Story — click to see the judge-legible demo (each row a different path)", expanded=False):
    st.caption("Run `PYTHONPATH=. python scripts/demo_story.py` to reproduce — or hit the API `GET /demo/story`")
    try:
        import pandas as _pd
        from pathlib import Path as _P
        from app.reconcile import reconcile as _rec
        _p_orders = _P("data/demo_story_orders.csv")
        if _p_orders.exists():
            _orders = _pd.read_csv(_P("data/demo_story_orders.csv"))
            _sett = _pd.read_csv(_P("data/demo_story_settlement.csv"))
            _m, _e = _rec(_orders, _sett)
            try:
                from app.classify import classify_exceptions_batch as _cls
                if not _e.empty:
                    _e = _cls(_e)
            except Exception:
                pass
            st.dataframe(_e[["order_id","reason","classification","amount_calc","amount_settled","diff"]].fillna("") if not _e.empty else _e, use_container_width=True, height=280)
            st.markdown("**AI visibly useful moment (demo_003):** Expected Rs 9,764.00 → Settled Rs 9,564.00 → Diff Rs 200.00 (2.00%) → _AI: 'likely TDS withholding, verify certificate'_ — **AI did NOT change the record**. Policy: `review` (deterministic). Human has final authority.")
            try:
                from app.reconcile_batched import group_by_utr as _gbu
                _g = _gbu(_sett)
                _b = _g[_g["count"]>1]
                if not _b.empty:
                    st.markdown("**Batched settlement insight:** many orders → one UTR (one bank credit)")
                    st.dataframe(_b, use_container_width=True, height=100)
                    st.caption("Real Razorpay payouts are often batched. 1:1 is MVP; batched groups by UTR/date and matches sums. See `GET /reconcile/batched` and `app/reconcile_batched.py`.")
            except Exception as _e2:
                st.caption(f"Batched group unavailable: {_e2}")
        else:
            st.info("Run `PYTHONPATH=. python scripts/demo_story.py --csv` to generate demo files, then refresh.")
    except Exception as e:
        st.caption(f"Demo story unavailable: {e}")

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

st.markdown("---")
st.markdown("## Defense, Policy & Honest Metrics - Track 02 + 04")

# Two cols: left honest metrics, right policy guardrails
col_a, col_b = st.columns([1.2, 1])

with col_a:
    st.markdown("### Honest metrics (held-out test set)")
    st.caption("Threshold fitted on earliest 70% - tested on latest 30% (no leakage). FP = Rs 500 review cost, FN = 25x FP in policy cost. This is not training accuracy.")
    try:
        from app.metrics import honest_evaluation_pipeline
        hm = honest_evaluation_pipeline()
        tm = hm["test_metrics"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Precision", f"{tm['precision']:.2%}")
        m2.metric("Recall", f"{tm['recall']:.2%}")
        m3.metric("FPR", f"{tm['fpr']:.2%}")
        m4.metric(" Held-out acc", f"{tm['accuracy']:.1%}")
        st.markdown(f"<div style='background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:12px; font-size:0.85rem'>"
                    f"<b>Threshold</b> {tm['threshold']:.3f} (cost-optimal, 25x FN) &nbsp;|&nbsp; "
                    f"<b>Cost</b> {tm['total_cost_units']:.0f} units (baseline {tm['baseline_cost_units']:.0f} -> saved {tm['cost_saved_vs_baseline']:.0f})<br>"
                    f"<b>FP financial cost</b>: Rs {tm['fp_financial_cost_rupees']:,.0f} &nbsp;|&nbsp; "
                    f"<b>FN loss</b>: Rs {tm['fn_financial_cost_rupees']:,.0f} &nbsp;|&nbsp; "
                    f"<b>Total</b>: Rs {tm['total_financial_cost_rupees']:,.0f} &nbsp; "
                    f"<span class='badge badge-slate'>held-out {tm['test_size']} rows</span>"
                    f"</div>", unsafe_allow_html=True)
        # Confusion strip
        st.markdown(f"<div style='margin-top:8px; font-size:0.78rem; color:#64748b'>TP {tm['tp']} &nbsp; TN {tm['tn']} &nbsp; "
                    f"<span style='color:#dc2626'>FP {tm['fp']}</span> &nbsp; <span style='color:#991b1b; font-weight:700'>FN {tm['fn']}</span> - "
                    f"FN costs 25x FP by policy; FP cost explicitly shown above.</div>", unsafe_allow_html=True)
        with st.expander("What makes this honest?"):
            st.markdown("- **Time-split**, not random shuffle - no leakage from future.\n"
                        "- **Held-out test** (30% latest) never seen during threshold fit.\n"
                        "- **Financial FP cost** (Rs 500 x FP) shown separately from cost units (25xFN+1xFP).\n"
                        "- Baseline (flag nothing) cost shown for comparison - we show savings, not just accuracy.\n"
                        "- Windowed spike detection further cuts FP vs per-transaction flagging.")
    except Exception as e:
        st.error(f"Honest metrics unavailable: {e}")

    st.markdown("### Cost-sensitive detection (rolling windows)")
    st.caption("Single-row scores are not flagged in isolation. We aggregate into rolling windows and fire only when the fraud rate spikes above its historical baseline (mean + 2 std).")
    try:
        from app.metrics import generate_synthetic_fraud_dataset
        from app.detection import CostSensitiveDetector
        from app.metrics import time_based_split as _split
        import pandas as pd
        demo_df = generate_synthetic_fraud_dataset(n=600, seed=7)
        # fit detector cost-sensitively on train
        train, test = _split(demo_df, test_frac=0.3)
        det = CostSensitiveDetector(window="6h", k=2.0)
        det.fit(train["score"].values, train["is_fraud"].values, train["amount"].values)
        res = det.evaluate_stream(demo_df)
        st.markdown(f"<div style='background:white; border:1px solid #e2e8f0; border-radius:10px; padding:12px; font-size:0.85rem'>"
                    f"<b>Window</b> {res['total_windows']} x 6h &nbsp;|&nbsp; <b>Spikes</b> {res['spike_count']} &nbsp;|&nbsp; "
                    f"<b>Baseline</b> {res['baseline']['mean']:.3%} +/- {res['baseline']['std']:.3%} (thr {res['baseline']['threshold']:.3%})<br>"
                    f"<b>Threshold</b> {res['threshold']:.3f} (25x FN-optimal) &nbsp;|&nbsp; spikes are the only flag - isolates do not trigger.</div>", unsafe_allow_html=True)
        if res["spike_windows"]:
            sw = res["spike_windows"][:4]
            for w in sw:
                st.markdown(f"<div style='font-size:0.8rem; padding:4px 0; border-bottom:1px solid #f1f5f9'>"
                            f"Spike <b>{w['window_start']}</b> - fraud_rate {w['fraud_rate']:.1%} (z={w.get('spike_z',0):.1f}) &nbsp; count {w['count']}</div>", unsafe_allow_html=True)
        else:
            st.caption("No spikes in demo window - baseline is calm (defense: we don't flag single rows).")
    except Exception as e:
        st.error(f"Detection demo unavailable: {e}")

with col_b:
    st.markdown("### Policy engine - deterministic, defense-only")
    st.caption("AI = signals only. One auditable function maps signals -> approve | step_up | review | block. No offensive actions exist.")
    try:
        from app.policy import POLICY_VERSION, ALLOWED_DECISIONS
        st.markdown(f"<div style='background:#f0fdf4; border:1px solid #bbf7d0; border-radius:10px; padding:12px; font-size:0.85rem'>"
                    f"<b>Policy</b> {POLICY_VERSION} &nbsp; <span class='badge badge-green'>defense-only</span><br>"
                    f"<b>Allowed</b>: {', '.join(sorted(ALLOWED_DECISIONS))}<br>"
                    f"<span style='color:#166534'>No external writes, no fund movement, no charge creation - enforced by allowlist.</span></div>", unsafe_allow_html=True)
    except Exception as e:
        st.error(str(e))
    # Live policy test
    st.markdown("<div style='margin-top:10px; font-size:0.82rem; font-weight:600'>Live check - type signals</div>", unsafe_allow_html=True)
    pc1, pc2 = st.columns(2)
    risk = pc1.slider("risk_score", 0.0, 1.0, 0.75, 0.05, key="pol_risk")
    z = pc2.slider("spike_z", 0.0, 5.0, 1.5, 0.5, key="pol_z")
    is_spike = st.checkbox("is_spike (rolling window flagged)", value=False, key="pol_spike")
    if st.button("Run policy -> decide", key="pol_run"):
        try:
            from app.policy import decide, Signals
            sig = Signals(risk_score=risk, is_spike=is_spike, spike_z=z, reason="exception_unexplained", diff=200, amount=1000)
            dec = decide(sig)
            color = {"approve":"#059669","step_up":"#d97706","review":"#7c3aed","block":"#dc2626"}.get(dec.decision, "#334155")
            st.markdown(f"<div style='background:white; border-left:4px solid {color}; padding:10px; border-radius:6px; font-size:0.9rem'>"
                        f"<b style='color:{color}; text-transform:uppercase'>{dec.decision}</b> - {dec.reason}<br>"
                        f"<span style='color:#94a3b8; font-size:0.75rem'>{dec.policy_version} - {dec.created_at}</span></div>", unsafe_allow_html=True)
        except Exception as e:
            st.error(str(e))
    st.markdown("<div style='margin-top:8px; font-size:0.75rem; color:#64748b'>Stored separately: <code>machine_decisions</code> (auto) vs <code>human_resolutions</code> (analyst). Human wins if present.</div>", unsafe_allow_html=True)

    st.markdown("### Chargeback responder - structured, not raw logs")
    st.caption("Read-only gather -> compiled evidence pack (draft). Human must approve before filing - never auto-submits.")
    cb_order = st.text_input("Order ID for chargeback pack", value="order_0005", key="cb_order")
    if st.button("Compile chargeback draft", key="cb_run"):
        try:
            from app import db
            from app.chargeback import gather_evidence, compile_response
            from app.config import db_path
            conn = db.get_connection(db_path())
            db.init_db(conn)
            try:
                b = gather_evidence(conn, cb_order.strip())
                if not b.order and not b.audit_entries:
                    st.warning(f"No evidence for {cb_order}")
                else:
                    resp = compile_response(b)
                    st.markdown(f"<div style='background:white; border:1px solid #e2e8f0; border-radius:10px; padding:12px'>"
                                f"<b>{resp.case_id}</b> <span class='badge badge-amber'>draft</span> <span class='badge badge-slate'>{resp.version}</span><br>"
                                f"<div style='font-size:0.82rem; color:#475569; margin-top:6px'>{resp.summary}</div>"
                                f"<div style='font-size:0.75rem; color:#94a3b8; margin-top:6px'>Evidence: {len(resp.evidence_cited)} items cited, {len(resp.timeline)} timeline events</div>"
                                f"<div style='background:#fffbeb; border:1px solid #fde68a; border-radius:8px; padding:8px; margin-top:8px; font-size:0.78rem'>{resp.recommended_action}</div>"
                                f"<div style='font-size:0.72rem; color:#94a3b8; margin-top:6px'>{resp.disclosure}</div></div>", unsafe_allow_html=True)
                    with st.expander("Evidence cited (sources)"):
                        for ev in resp.evidence_cited:
                            st.json(ev)
                    with st.expander("Timeline"):
                        for t in resp.timeline[:10]:
                            st.markdown(f"- {t.get('at')}: {t.get('what')} [{t.get('source')}]")
                    with st.expander("Amount analysis"):
                        st.json(resp.amount_analysis)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            st.error(f"Chargeback failed: {e}")

st.caption("Auditable: machine drafts in <code>machine_decisions</code>, human filings in <code>human_resolutions</code>. Pipeline halts at draft - filing requires POST /human/resolve.")


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
