"""AI Finance Assistant — chat with your ledger.

Wraps the existing Anthropic client with a ledger-aware system prompt.
Falls back to heuristic template if no API key.

Usage from dashboard: ask(question, audit_df)
Usage as API: POST /ask  {question}
"""
from __future__ import annotations

import pandas as pd

from app.config import anthropic_api_key, anthropic_model
from app.classify import _get_client

SYSTEM = """You are Ledger Sentinel's Finance Assistant.
You answer questions about a reconciliation audit. Be concise, plain-English,
and cite order_ids and numbers when relevant. If the user asks about a specific
order, explain its reason, classification, and audit_note. If they ask for
summary, use the provided stats. Never hallucinate orders not in the context.
If no API key is set on the server, say so and give a heuristic answer."""

def _build_context(audit: pd.DataFrame, question: str) -> str:
    if audit.empty:
        return "No audit data available. Run the pipeline first."
    # stats
    recon = audit[audit["outcome"].isin(["matched", "exception"])] if "outcome" in audit.columns else audit
    matched = int((recon["outcome"] == "matched").sum()) if not recon.empty else 0
    exc_n = int((recon["outcome"] == "exception").sum()) if not recon.empty else 0
    total = matched + exc_n
    rate = round(matched / total * 100, 1) if total else 0
    by_reason = recon["reason"].value_counts().to_dict() if not recon.empty and "reason" in recon.columns else {}

    # relevant rows: if question mentions order_xxx, pull those
    import re
    order_ids = re.findall(r"order_\d+", question.lower())
    relevant = pd.DataFrame()
    if order_ids:
        relevant = audit[audit["order_id"].isin(order_ids)] if "order_id" in audit.columns else relevant
    if relevant.empty:
        # top exceptions
        relevant = audit[audit["outcome"] == "exception"].head(12) if "outcome" in audit.columns else audit.head(12)

    lines = []
    lines.append(f"Summary: total={total}, matched={matched}, exceptions={exc_n}, match_rate={rate}%")
    lines.append(f"By reason: {by_reason}")
    lines.append("")
    lines.append("Relevant rows (order_id | outcome | reason | classification | audit_note):")
    for _, r in relevant.iterrows():
        lines.append(f"- {r.get('order_id','')} | {r.get('outcome','')} | {r.get('reason','')} | {r.get('classification','')} | {str(r.get('audit_note',''))[:180]}")
    return "\n".join(lines)

def _heuristic_answer(question: str, audit: pd.DataFrame) -> str:
    q = question.lower()
    if audit.empty:
        return "No audit data yet — run `python -m app.main` first."
    # order-specific — prefer exception row, then most recent
    import re
    m = re.search(r"order_\d+", q)
    if m:
        oid = m.group(0)
        rows = audit[audit["order_id"] == oid] if "order_id" in audit.columns else pd.DataFrame()
        if rows.empty:
            return f"I couldn't find {oid} in the audit log. Check the order ID or run the pipeline."
        # prefer exception > matched > latest
        exc = rows[rows["outcome"] == "exception"] if "outcome" in rows.columns else pd.DataFrame()
        if not exc.empty:
            r = exc.iloc[-1]
        else:
            r = rows.iloc[-1]
        return f"**{oid}** — outcome: `{r.get('outcome')}`, reason: `{r.get('reason')}`, classification: `{r.get('classification')}`.\nNote: {r.get('audit_note','(no note)')}"
    if any(w in q for w in ["how many", "match rate", "summary", "overview"]):
        recon = audit[audit["outcome"].isin(["matched", "exception"])]
        m = int((recon["outcome"] == "matched").sum())
        e = int((recon["outcome"] == "exception").sum())
        rate = round(m / (m + e) * 100, 1) if (m + e) else 0
        by = recon[recon["outcome"] == "exception"]["reason"].value_counts().to_dict() if "reason" in recon.columns else {}
        return f"**Summary:** {m} matched, {e} exceptions out of {m+e} rows — **{rate}% match rate**.\nBy reason: {by}\n\nAsk me about a specific order like `order_0010`."
    if "tds" in q or "tax" in q:
        n = int((audit["reason"] == "exception_tds_candidate").sum()) if "reason" in audit.columns else 0
        return f"There are **{n} TDS-shaped exceptions** (gaps ~2% of gross). These are usually expected withholdings. Check the exception table filtered to `exception_tds_candidate`."
    if "risk" in q or "priority" in q or "urgent" in q:
        exc = audit[audit["outcome"] == "exception"].copy() if "outcome" in audit.columns else audit
        if "diff" in exc.columns:
            exc = exc.sort_values("diff", ascending=False)
        top = exc.head(3)
        lines = ["**Top priority exceptions (largest gaps):**"]
        for _, r in top.iterrows():
            lines.append(f"- {r.get('order_id')} — {r.get('reason')} — gap Rs {r.get('diff','?')} — {r.get('audit_note','')[:100]}")
        return "\n".join(lines)
    return "I can answer about match rate, specific orders (e.g. `order_0010`), TDS cases, or priority exceptions. Try: `Why is order_0010 flagged?` or `What is the match rate?`"

def ask(question: str, audit: pd.DataFrame) -> dict:
    """Return {answer, source} dict. Uses LLM if available, else heuristic."""
    question = (question or "").strip()
    if not question:
        return {"answer": "Ask me something — e.g. `Why is order_0010 flagged?`", "source": "heuristic"}
    # Try LLM
    key = anthropic_api_key()
    if key:
        client = _get_client()
        if client is not None:
            ctx = _build_context(audit, question)
            try:
                resp = client.messages.create(
                    model=anthropic_model(),
                    max_tokens=350,
                    system=SYSTEM,
                    messages=[{"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {question}"}],
                )
                text = resp.content[0].text if resp.content and hasattr(resp.content[0], "text") else ""
                if text.strip():
                    return {"answer": text.strip(), "source": "claude"}
            except Exception as e:
                # fall through to heuristic with note
                h = _heuristic_answer(question, audit)
                return {"answer": f"[AI temporarily unavailable: {str(e)[:100]}]\n\n{h}", "source": "heuristic"}
    return {"answer": _heuristic_answer(question, audit), "source": "heuristic"}
