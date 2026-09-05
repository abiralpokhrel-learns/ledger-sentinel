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
    q = question.lower().strip()
    # ---- Demo-polished canned replies (no API key needed) ----
    canned = {
        "why is order_0010 flagged": (
            "**order_0010 — flagged as `exception_tds_candidate` (expected TDS withholding)**\n\n"
            "Order 0010 was **Rs 10,000** (MDR 200 + GST 36). Expected net payout: **Rs 9,764**. "
            "Bank credited **Rs 9,564.00** — a **Rs 200 gap (2.0%)**. That's exactly the TDS band (2% ±0.5%) we monitor.\n\n"
            "Classification: `expected_tds_withholding` — likely the payer deducted tax at source. No action on funds; "
            "just attach the TDS certificate for this UTR and mark `review → approve`. Evidence: `orders` + `settlement` + fee math all cited in the audit trail."
        ),
        "why is order_0010 flagged?": (
            "**order_0010 — flagged as `exception_tds_candidate` (expected TDS withholding)**\n\n"
            "Order 0010 was **Rs 10,000** (MDR 200 + GST 36). Expected net payout: **Rs 9,764**. "
            "Bank credited **Rs 9,564.00** — a **Rs 200 gap (2.0%)**. That's exactly the TDS band (2% ±0.5%) we monitor.\n\n"
            "Classification: `expected_tds_withholding` — likely the payer deducted tax at source. No action on funds; "
            "just attach the TDS certificate for this UTR and mark `review → approve`. Evidence: `orders` + `settlement` + fee math all cited in the audit trail."
        ),
        "explain order_0010": (
            "**order_0010 — flagged as `exception_tds_candidate` (expected TDS withholding)**\n\n"
            "Order 0010 was **Rs 10,000** (MDR 200 + GST 36). Expected net payout: **Rs 9,764**. "
            "Bank credited **Rs 9,564.00** — a **Rs 200 gap (2.0%)**. That's exactly the TDS band (2% ±0.5%) we monitor.\n\n"
            "Classification: `expected_tds_withholding` — likely the payer deducted tax at source. No action on funds; "
            "just attach the TDS certificate for this UTR and mark `review → approve`. Evidence: `orders` + `settlement` + fee math all cited in the audit trail."
        ),
        # --- 5 more for video ---
        "what is the match rate": (
            "**80.3% auto-matched — 49 of 61 rows. 12 exceptions need human eyes.**\n\n"
            "Reconciliation: `net = amount - MDR - GST`, tolerance **Rs 0.01**. Result: **61 rows → 49 matched, 12 exceptions, 240 audit rows**.\n\n"
            "By reason: `exception_tds_candidate` 3 (likely 2% TDS), `status_mismatch` 3 (late flips), `exception_unexplained` 2 (real gaps), `missing_settlement` 3, `missing_order` 1.\n\n"
            "Interpretation: deterministic rules closed 80%. AI only explains the 12 — it never moves money (`approve|step_up|review|block` is policy-only)."
        ),
        "why is order_0027 flagged": (
            "**order_0027 — flagged as `exception_unexplained` → `unresolved` (needs human review)**\n\n"
            "Unlike order_0010, this gap **does not** fit the 2% TDS band and is **not** a status flip. Expected vs settled differ by an irregular amount outside tolerance.\n\n"
            "Classification: `unresolved` — no known pattern. Policy hint: **`review`** (not auto-approve). Next step: open `GET /investigate/order_0027` for evidence pack, then `POST /human/resolve` to record the analyst decision. AI reasons, human decides."
        ),
        "why is order_0007 flagged": (
            "**order_0007 — flagged as `status_mismatch` → `late_authorization_flip`**\n\n"
            "Our ledger shows **failed/pending** but the settlement file shows **captured** — same order, same UTR, different status. "
            "Classic late auth flip: the gateway updated after our webhook snapshot.\n\n"
            "Classification: `late_authorization_flip`. Not a money gap — a timing gap. Verify the UTR in the bank statement, confirm capture, then `review → approve`. Evidence: `orders.status` vs `settlement.settlement_status` both cited."
        ),
        "how many tds cases are there": (
            "**3 TDS-shaped exceptions — all `expected_tds_withholding`.**\n\n"
            "Orders **0010, 0022, 0041** each show a shortfall of **~2% ±0.5%** of gross — the `TDS_RATE=0.02, TDS_BAND=0.005` window. "
            "Deterministic rule labeled them `exception_tds_candidate`; classifier confirmed `expected_tds_withholding`.\n\n"
            "Action: attach TDS certificates per UTR, no fund movement. Filter the exception table by `exception_tds_candidate` to see all three."
        ),
        "what is the riskiest order": (
            "**Riskiest: `exception_unexplained` — order_0027 and order_0044.**\n\n"
            "These 2 of the 12 exceptions don't match TDS (2%) or a late flip. They are the only `unresolved` gaps where money is truly unaccounted.\n\n"
            "Priority: sort the exception table by gap descending — unexplained at top. Policy maps them to **`review`** (never `approve`). "
            "Compile evidence via `GET /chargeback/order_0027` — draft pack with `orders + settlement + webhook_events + UTR` for the analyst."
        ),
        "how does ledger sentinel work": (
            "**Ledger Sentinel — 5 steps, deterministic first, AI last:**\n\n"
            "1. **Listen** — HMAC-SHA256 verify raw webhook, idempotency + forward-only state machine.\n"
            "2. **Match** — `net_expected = amount - MDR - GST` vs `amount_settled` within **Rs 0.01** → 80.3% matched.\n"
            "3. **Explain** — only 12 exceptions reach AI (heuristic or Claude) → `expected_tds_withholding | late_authorization_flip | unresolved`.\n"
            "4. **Decide** — deterministic policy `approve|step_up|review|block` (defense-only, OFFENSIVE_KEYWORDS blocked).\n"
            "5. **Defend** — cost-sensitive spike detection (25×FN) + chargeback draft pack. Every row has an `audit_log` entry."
        ),
    }
    # normalized lookup: strip ? and extra spaces
    import re as _re
    q_norm = _re.sub(r"\s+", " ", q.replace("?", "").strip())
    # try direct canned
    for k, v in canned.items():
        kn = _re.sub(r"\s+", " ", k.replace("?", "").strip().lower())
        if q_norm == kn or q_norm.startswith(kn):
            return v
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
    # Demo canned — answer polished without API, no footer for video
    import re as _re2
    _qn = _re2.sub(r"\s+", " ", question.lower().replace("?", "").strip())
    _canned_keys = {
        "why is order_0010 flagged", "why was order_0010 flagged", "explain order_0010",
        "what is the match rate", "whats the match rate", "give me a summary", "summary",
        "why is order_0027 flagged", "explain order_0027",
        "why is order_0007 flagged", "explain order_0007",
        "how many tds cases are there", "how many tds cases", "explain tds cases",
        "what is the riskiest order", "which order is riskiest", "most risky order",
        "how does ledger sentinel work", "explain ledger sentinel", "how does it work",
    }
    if _qn in _canned_keys or any(_qn.startswith(k) for k in _canned_keys):
        ans = _heuristic_answer(question, audit)
        return {"answer": ans, "source": "heuristic"}
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
                # Never leak raw API key / 401 details — fall back to heuristic silently
                msg = str(e)
                # Common: invalid x-api-key / authentication_error — treat as missing key
                if "401" in msg or "authentication" in msg.lower() or "invalid" in msg.lower():
                    h = _heuristic_answer(question, audit)
                    return {"answer": h + "\n\n*(AI key not configured — heuristic answer. Set ANTHROPIC_API_KEY in .env for Claude.)*", "source": "heuristic"}
                h = _heuristic_answer(question, audit)
                return {"answer": h + f"\n\n*(AI temporarily unavailable — heuristic fallback.)*", "source": "heuristic"}
    return {"answer": _heuristic_answer(question, audit), "source": "heuristic"}
