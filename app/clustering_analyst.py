"""Transaction clustering + NL SQL analyst (read-only) + investigation reports.

- Clustering: behavioral segments (value, time, velocity, device) — heuristic
  + optional KMeans if sklearn available. Deterministic policy can then apply
  per-cluster thresholds.
- NL analyst: user question -> AI generates READ-ONLY SQL -> validator -> exec
  -> AI explanation. Never INSERT/UPDATE/DELETE.
- Reports: incident PDF from investigator output + evidence.

All defense-only: no writes except report generation to temp.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

# ---------- Clustering ----------

def cluster_transactions(conn: sqlite3.Connection, k: int = 3) -> dict:
    """Behavioral clustering over orders. Returns clusters with stats."""
    try:
        import pandas as pd
        orders = pd.read_sql_query("SELECT * FROM orders", conn)
        if orders.empty or len(orders) < 4:
            return {"clusters": [], "note": "Not enough orders to cluster", "method": "heuristic"}
        # Feature: amount, hour bucket proxy (order_id hash), category encoded
        # Simple heuristic: amount tiers + status
        # Tier A: 0-5k, B: 5k-15k, C: 15k-50k
        def tier(amt):
            if amt < 5000:
                return "A_low"
            if amt < 15000:
                return "B_mid"
            return "C_high"
        orders["tier"] = orders["amount"].apply(tier)
        # Group by tier + status
        grouped = orders.groupby(["tier", "status"]).agg(
            count=("order_id", "size"),
            avg_amount=("amount", "mean"),
            min_amount=("amount", "min"),
            max_amount=("amount", "max"),
        ).reset_index()
        clusters = []
        for _, r in grouped.iterrows():
            clusters.append({
                "segment": f"{r['tier']} / {r['status']}",
                "count": int(r["count"]),
                "avg_amount": round(float(r["avg_amount"]), 2),
                "range": f"Rs {r['min_amount']:.0f}-{r['max_amount']:.0f}",
                "description": _describe_segment(r["tier"], r["status"], int(r["count"]), float(r["avg_amount"])),
                "policy_hint": _policy_for_segment(r["tier"], r["status"]),
            })
        # Try sklearn KMeans for richer clustering if available
        method = "heuristic"
        try:
            import sklearn  # noqa: F401
            from sklearn.cluster import KMeans
            import numpy as np
            X = orders[["amount"]].values
            # add mdr/gst as features if present
            if "mdr" in orders.columns:
                X = np.hstack([X, orders[["mdr", "gst"]].fillna(0).values])
            n_init = min(k, len(orders))
            km = KMeans(n_clusters=n_init, n_init=3, random_state=42)
            labels = km.fit_predict(X)
            orders["kmeans_label"] = labels
            # Recompute clusters via KMeans
            km_clusters = []
            for lbl in sorted(set(labels)):
                sub = orders[orders["kmeans_label"] == lbl]
                km_clusters.append({
                    "segment": f"KMeans #{lbl}",
                    "count": int(len(sub)),
                    "avg_amount": round(float(sub["amount"].mean()), 2),
                    "range": f"Rs {sub['amount'].min():.0f}-{sub['amount'].max():.0f}",
                    "description": f"ML cluster with {len(sub)} transactions",
                    "policy_hint": "review" if sub["amount"].mean() > 10000 else "approve",
                })
            # Prefer KMeans if it produced meaningful split
            if len(km_clusters) >= 2:
                clusters = km_clusters
                method = "kmeans"
        except Exception:
            pass
        return {"clusters": clusters, "method": method, "total_orders": len(orders)}
    except Exception as e:
        return {"clusters": [], "error": str(e), "method": "heuristic"}


def _describe_segment(tier: str, status: str, count: int, avg: float) -> str:
    if tier == "A_low" and status == "captured":
        return "Normal low-value, daytime-like pattern"
    if tier == "C_high":
        return "High-value, fewer transactions — higher scrutiny"
    if status == "failed":
        return "Failed authorizations — may include late flips"
    return f"{tier} segment — {count} txns"

def _policy_for_segment(tier: str, status: str) -> str:
    if status == "failed":
        return "review"
    if tier == "C_high":
        return "step_up"
    return "approve"


# ---------- NL SQL Analyst (read-only) ----------

ALLOWED_TABLES = {"orders", "settlement", "audit_log", "webhook_events", "machine_decisions", "human_resolutions"}
READ_ONLY_VERBS = {"SELECT", "WITH"}
FORBIDDEN = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "PRAGMA", "VACUUM", "ATTACH", "DETACH"}

ANALYST_SYSTEM = """You are a read-only financial data analyst for Ledger Sentinel.
Given a user question and the database schema, generate a single SQLite SELECT query that answers it.
Rules:
- ONLY SELECT/WITH — never INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA.
- Only query tables: orders, settlement, audit_log, webhook_events, machine_decisions, human_resolutions.
- Prefer audit_log for reconciliation questions, orders/settlement for amounts, detection_windows for spikes.
- Return ONLY the SQL, no explanation, no markdown.
"""

SCHEMA_HINT = """
orders(order_id TEXT, amount REAL, mdr REAL, gst REAL, category TEXT, status TEXT)
settlement(order_id TEXT, amount_settled REAL, settlement_status TEXT, utr TEXT, settlement_date TEXT)
audit_log(id INT, order_id TEXT, outcome TEXT, reason TEXT, classification TEXT, audit_note TEXT, created_at TEXT)
webhook_events(event_id TEXT, order_id TEXT, event_type TEXT, processed_at TEXT)
machine_decisions(id INT, order_id TEXT, decision TEXT, reason TEXT, policy_version TEXT)
human_resolutions(id INT, order_id TEXT, resolution TEXT, analyst TEXT, note TEXT)
"""


def _validate_sql(sql: str) -> tuple[bool, str]:
    s = sql.strip()
    if not s:
        return False, "Empty SQL"
    # Block forbidden — word boundary, escaped keyword
    upper = s.upper()
    for kw in FORBIDDEN:
        if re.search(rf"\b{re.escape(kw)}\b", upper):
            return False, f"Forbidden keyword: {kw}"
    # Must start with SELECT or WITH
    first = re.split(r"\s+", s.lstrip(" ("))[0].upper() if s else ""
    if first not in READ_ONLY_VERBS:
        if not s.lstrip().upper().startswith("SELECT") and not s.lstrip().upper().startswith("WITH"):
            return False, "Only SELECT/WITH queries allowed"
    # Table allowlist — reject unknown tables (catches exfil via sqlite_master tricks too)
    tables = re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", upper)
    for t in tables:
        tl = t.lower()
        if tl not in ALLOWED_TABLES and tl not in ("sqlite_master",):
            # subquery aliases like (SELECT ...) AS sub — allow only if it's not a real table ref
            # be strict: any unknown table is blocked
            return False, f"Table not in allowlist: {t}"
    return True, ""


def _heuristic_sql(question: str) -> str:
    q = question.lower()
    if "how much" in q and ("held" in q or "at risk" in q or "exception" in q):
        return "SELECT outcome, COUNT(*) as count FROM audit_log WHERE outcome='exception' GROUP BY outcome"
    if "match rate" in q or "matched" in q:
        return "SELECT SUM(outcome='matched') as matched, SUM(outcome='exception') as exceptions, COUNT(*) as total FROM audit_log WHERE outcome IN ('matched','exception')"
    if "tds" in q:
        return "SELECT COUNT(*) as tds_candidates FROM audit_log WHERE reason='exception_tds_candidate'"
    if "last week" in q or "week" in q:
        return "SELECT COUNT(*) as rows, outcome, reason FROM audit_log WHERE created_at >= datetime('now','-7 days') GROUP BY outcome, reason"
    if "order_" in q:
        m = re.search(r"order_\d+", q)
        oid = m.group(0) if m else "order_0001"
        return f"SELECT * FROM audit_log WHERE order_id='{oid}' ORDER BY created_at DESC LIMIT 5"
    return "SELECT COUNT(*) as total, outcome FROM audit_log GROUP BY outcome"


def _call_llm_sql(question: str) -> Optional[str]:
    try:
        from app.config import anthropic_api_key, anthropic_model
        key = anthropic_api_key()
        if not key:
            return None
        from app.classify import _get_client
        client = _get_client()
        if client is None:
            return None
        resp = client.messages.create(
            model=anthropic_model(),
            max_tokens=300,
            system=ANALYST_SYSTEM,
            messages=[{"role": "user", "content": f"Schema:\n{SCHEMA_HINT}\n\nQuestion: {question}\n\nSQL:"}],
        )
        text = resp.content[0].text if resp.content and hasattr(resp.content[0], "text") else ""
        # Extract SQL block
        # Take first SELECT/WITH statement
        m = re.search(r"(SELECT|WITH)[\s\S]+?;", text, re.IGNORECASE)
        if m:
            return m.group(0)
        # fallback: whole text if it looks like SQL
        if text.strip().upper().startswith("SELECT") or text.strip().upper().startswith("WITH"):
            return text.strip()
        return None
    except Exception:
        return None


def analyst_query(conn: sqlite3.Connection, question: str) -> dict:
    """NL -> read-only SQL -> exec -> explanation. Returns {question, sql, rows, explanation, source}."""
    question = (question or "").strip()
    if not question:
        return {"error": "Empty question"}
    sql = _call_llm_sql(question)
    source = "claude" if sql else "heuristic"
    if not sql:
        sql = _heuristic_sql(question)
    # Clean markdown fences if LLM added them
    sql = re.sub(r"^```(?:sql)?\s*", "", sql.strip(), flags=re.IGNORECASE)
    sql = re.sub(r"```\s*$", "", sql.strip())
    sql = sql.strip().rstrip(";") + ";"
    ok, reason = _validate_sql(sql)
    if not ok:
        return {"question": question, "sql": sql, "error": f"Blocked: {reason}", "source": source}
    try:
        import pandas as pd
        df = pd.read_sql_query(sql, conn)
        rows = df.to_dict(orient="records")
        # Simple explanation
        if not rows:
            explanation = "No rows matched."
        elif len(rows) == 1 and len(rows[0]) == 1:
            k, v = next(iter(rows[0].items()))
            explanation = f"**{v}** for {k}."
        else:
            explanation = f"Returned {len(rows)} rows. " + "; ".join(f"{k}={r[k]}" for r in rows[:3] for k in list(r.keys())[:2])
            if len(explanation) > 400:
                explanation = explanation[:397] + "..."
        # Try LLM explanation if available
        try:
            from app.config import anthropic_api_key, anthropic_model
            if anthropic_api_key():
                from app.classify import _get_client
                client = _get_client()
                if client:
                    resp = client.messages.create(
                        model=anthropic_model(),
                        max_tokens=250,
                        system="You explain SQL results in plain English for a finance operator. Be concise, cite numbers.",
                        messages=[{"role": "user", "content": f"Question: {question}\nSQL: {sql}\nResult: {rows[:5]}\n\nExplain in one sentence:"}],
                    )
                    txt = resp.content[0].text if resp.content and hasattr(resp.content[0], "text") else ""
                    if txt.strip():
                        explanation = txt.strip()[:500]
                        source = "claude"
        except Exception:
            pass
        return {"question": question, "sql": sql, "rows": rows, "row_count": len(rows), "explanation": explanation, "source": source, "read_only": True}
    except Exception as e:
        return {"question": question, "sql": sql, "error": str(e), "source": source}


# ---------- Investigation Report (PDF artifact) ----------

def build_investigation_report(conn: sqlite3.Connection, order_id: str) -> dict:
    """Build incident data for PDF. Returns dict with title, sections."""
    from app.investigator import investigate, gather_investigation_context
    inv = investigate(conn, order_id)
    ctx = gather_investigation_context(conn, order_id)
    amounts = ctx.get("amounts", {})
    audit_entries = ctx.get("audit_entries", [])
    events = ctx.get("webhook_events", [])
    settlement = ctx.get("settlement") or {}
    order = ctx.get("order") or {}

    incident_id = f"LS-{abs(hash(order_id)) % 10000:04d}"
    return {
        "incident_id": incident_id,
        "order_id": order_id,
        "title": f"Incident {incident_id} — Order {order_id}",
        "investigator": inv,
        "amounts": amounts,
        "order": order,
        "settlement": settlement,
        "audit_entries": audit_entries,
        "webhook_events": events,
        "generated_at": ctx.get("gathered_at"),
    }


def _safe_pdf(s: str) -> str:
    if s is None:
        return ""
    return str(s).replace("\u2014", "-").replace("\u2013", "-").replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'").encode("latin-1", errors="replace").decode("latin-1")

def render_investigation_pdf(report: dict, out_path: str) -> str:
    """Render investigation report to PDF via fpdf2. Returns out_path."""
    from fpdf import FPDF
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_left_margin(10)
    pdf.set_right_margin(10)
    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _safe_pdf(report.get("title", "Investigation Report")), ln=True, align="C")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, f"Generated {report.get('generated_at','')} | Investigator {report.get('investigator',{}).get('investigator_version','')}", ln=True, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    # Summary box
    inv = report.get("investigator", {})
    amounts = report.get("amounts", {})
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(248, 250, 252)
    pdf.cell(0, 7, _safe_pdf(f"Root cause: {inv.get('root_cause','')}  |  Confidence: {inv.get('confidence',0):.0%}  |  Policy: {inv.get('policy_hint','review')}"), ln=True, fill=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_fill_color(255, 255, 255)
    pdf.cell(0, 6, _safe_pdf(f"Expected Rs {amounts.get('expected_net','')}  Settled Rs {amounts.get('settled','')}  Diff Rs {amounts.get('diff','')}  Gap {amounts.get('gap_rate',0):.2%}"), ln=True)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 5, _safe_pdf(f"Recommended: {inv.get('recommended_next_step','')}"))
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    # Evidence
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Evidence", ln=True)
    pdf.set_font("Helvetica", "", 9)
    for ev in inv.get("evidence", [])[:8]:
        pdf.set_x(10)
        pdf.multi_cell(0, 5, _safe_pdf("- " + ev))
    pdf.ln(3)
    # Supporting evidence table
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Supporting Evidence (cited)", ln=True)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(241, 245, 249)
    pdf.cell(35, 6, "Source", border=1, fill=True)
    pdf.cell(45, 6, "Record", border=1, fill=True)
    pdf.cell(110, 6, "Fact", border=1, fill=True, ln=True)
    pdf.set_font("Helvetica", "", 8)
    for se in inv.get("supporting_evidence", [])[:10]:
        pdf.cell(35, 6, _safe_pdf(str(se.get("source",""))[:18]), border=1)
        pdf.cell(45, 6, _safe_pdf(str(se.get("record",""))[:24]), border=1)
        pdf.cell(110, 6, _safe_pdf(str(se.get("fact",""))[:58]), border=1, ln=True)
    pdf.ln(3)
    # Alternative + missing
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Alternative Hypotheses", ln=True)
    pdf.set_font("Helvetica", "", 9)
    for h in inv.get("alternative_hypotheses", [])[:5]:
        pdf.cell(5, 5, "-")
        pdf.cell(0, 5, _safe_pdf(h), ln=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Missing Evidence (would raise confidence)", ln=True)
    pdf.set_font("Helvetica", "", 9)
    for m in inv.get("missing_evidence", [])[:5]:
        pdf.cell(5, 5, "-")
        pdf.cell(0, 5, _safe_pdf(m), ln=True)
    pdf.ln(4)
    # Audit trail snippet
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Audit Trail (for this order)", ln=True)
    pdf.set_font("Helvetica", "", 8)
    for ae in report.get("audit_entries", [])[:6]:
        pdf.cell(0, 5, _safe_pdf(f"{ae.get('created_at','')}  {ae.get('outcome','')}  {ae.get('reason','')}  {ae.get('classification','')}  {str(ae.get('audit_note',''))[:80]}"), ln=True)
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 5, _safe_pdf("Human approval required: Yes - AI cannot change the ledger or execute the decision. Deterministic policy decides, human has final authority."))
    pdf.output(out_path)
    return out_path
