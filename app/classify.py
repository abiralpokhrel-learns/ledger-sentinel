"""AI exception classification — the ONLY place the LLM is used.

Only rows that survived deterministic reconciliation (and are therefore
genuinely ambiguous) reach this module. Each gets a single classification and
a one-sentence audit note. If no Anthropic key is configured the module falls
back to a deterministic heuristic so the demo still runs end-to-end — but the
real submission should have the key set.
"""
from __future__ import annotations

import os
import re

from app.config import anthropic_api_key, anthropic_model

VALID = {
    "expected_tds_withholding",
    "late_authorization_flip",
    "unresolved",
}

CLASSIFY_PROMPT = """You are reconciling a payment exception for a Razorpay merchant.
Order amount (gross): {amount}
Expected net payout (amount - MDR - GST): {amount_calc}
Settled amount: {amount_settled}
Difference: {diff}
Order status in our ledger: {order_status}
Status shown in settlement file: {settlement_status}
Merchant category: {category}

Classify this exception as exactly one of:
- "expected_tds_withholding" (gap matches a typical TDS/TCS deduction for this category)
- "late_authorization_flip" (our ledger shows failed/pending but settlement shows captured)
- "unresolved" (none of the above cleanly applies)

Respond with the chosen label on its own line, then a one-sentence audit note
explaining the reasoning in plain English a finance operator would understand."""


def _heuristic_classify(row: dict) -> dict:
    """Deterministic fallback used when no API key is present."""
    reason = row.get("reason", "")
    if reason == "status_mismatch":
        return {
            "classification": "late_authorization_flip",
            "audit_note": (
                "Our ledger shows the order did not complete, but settlement "
                "reported a capture — likely a late authorization flip."
            ),
        }
    if reason == "exception_tds_candidate":
        return {
            "classification": "expected_tds_withholding",
            "audit_note": (
                "The shortfall matches a typical TDS/TCS withholding rate for "
                "this merchant category and is expected."
            ),
        }
    if reason in ("missing_order", "missing_settlement"):
        return {
            "classification": "unresolved",
            "audit_note": (
                "One side of the match is missing entirely; needs manual review "
                "to confirm the order/settlement linkage."
            ),
        }
    return {
        "classification": "unresolved",
        "audit_note": "Gap does not match any known pattern; flagged for manual review.",
    }


# Deterministic priority order for parsing — set iteration is random
VALID_ORDERED = ["expected_tds_withholding", "late_authorization_flip", "unresolved"]

_anthropic_client = None


def _get_client():
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    key = anthropic_api_key()
    if not key:
        return None
    import anthropic

    _anthropic_client = anthropic.Anthropic(api_key=key, timeout=15.0, max_retries=1)
    return _anthropic_client


def _parse(response_text: str) -> dict:
    text = response_text.strip()
    # Priority 1: first line is exactly a valid label (common LLM format)
    first_line = text.splitlines()[0].strip().strip('"').strip("'").strip()
    if first_line in VALID:
        classification = first_line
    else:
        # Priority 2: search in deterministic order
        found = None
        for label in VALID_ORDERED:
            if label in text:
                found = label
                break
        classification = found or "unresolved"
    note = text.strip()
    if classification in note:
        # keep the note but drop the bare label line for readability
        note = re.sub(rf"^\s*{re.escape(classification)}\s*[\n:]*", "", note).strip()
    # Truncate to avoid DB bloat / UI overflow
    if len(note) > 500:
        note = note[:497] + "..."
    return {"classification": classification, "audit_note": note or "(no note returned)"}


def classify_exception(row: dict) -> dict:
    key = anthropic_api_key()
    if not key:
        return _heuristic_classify(row)

    client = _get_client()
    if client is None:
        return _heuristic_classify(row)

    prompt = CLASSIFY_PROMPT.format(
        amount=row.get("amount", ""),
        amount_calc=row.get("amount_calc", ""),
        amount_settled=row.get("amount_settled", ""),
        diff=row.get("diff", ""),
        order_status=row.get("status", row.get("order_status", "")),
        settlement_status=row.get("settlement_status", ""),
        category=row.get("category", ""),
    )
    try:
        resp = client.messages.create(
            model=anthropic_model(),
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        # Defensive: ensure content exists
        if not resp.content or not hasattr(resp.content[0], "text"):
            raise ValueError("empty response")
        return _parse(resp.content[0].text)
    except Exception as exc:  # never let the AI block the pipeline
        # Log once, fallback deterministically
        fallback = _heuristic_classify(row)
        # Don't leak full exception to audit_note if it contains secrets
        short_exc = str(exc)[:120].replace("\n", " ")
        fallback["audit_note"] = f"[AI call failed: {short_exc}] {fallback['audit_note']}"
        return fallback
