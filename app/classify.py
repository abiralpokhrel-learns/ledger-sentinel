"""AI exception classification — the ONLY place the LLM is used.

Only rows that survived deterministic reconciliation (and are therefore
genuinely ambiguous) reach this module. Each gets a single classification and
a one-sentence audit note. If no Anthropic key is configured the module falls
back to a deterministic heuristic so the demo still runs end-to-end — but the
real submission should have the key set.

Scale note: run_pipeline() batches + caches so 1000s of exceptions don't
cost 1000s of API calls. Cache key = hash(order_id, reason, diff).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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

    # Cache hit? (hash of stable fields so re-runs don't re-pay)
    cache_key = _cache_key(row)
    if cache_key:
        cached = _cache_get(cache_key)
        if cached:
            return cached

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
        result = _parse(resp.content[0].text)
        if cache_key:
            _cache_set(cache_key, result)
        return result
    except Exception as exc:  # never let the AI block the pipeline
        # Log once, fallback deterministically — strip auth details
        fallback = _heuristic_classify(row)
        msg = str(exc).lower()
        if "401" in msg or "authentication" in msg or "invalid" in msg or "api key" in msg:
            # auth failure — don't pollute audit_note with it
            return fallback
        short_exc = str(exc)[:80].replace("\n", " ")
        fallback["audit_note"] = f"[AI unavailable] {fallback['audit_note']}"
        return fallback


# --- batch + cache (scale fix) ----------------------------------------------

CACHE_PATH = Path(__file__).resolve().parent.parent / ".classify_cache.json"
_CACHE: dict | None = None

def _cache_key(row: dict) -> str | None:
    try:
        # Stable key: order_id + reason + diff (rounded) + amount_calc
        oid = str(row.get("order_id", ""))
        reason = str(row.get("reason", ""))
        diff = str(round(float(row.get("diff", 0) or 0), 2))
        amt = str(row.get("amount_calc", ""))
        raw = f"{oid}|{reason}|{diff}|{amt}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    except Exception:
        return None

def _cache_load() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if os.getenv("LEDGER_NO_CACHE", "").lower() in ("1", "true", "yes"):
        _CACHE = {}
        return _CACHE
    try:
        if CACHE_PATH.exists():
            _CACHE = json.loads(CACHE_PATH.read_text())
            if not isinstance(_CACHE, dict):
                _CACHE = {}
        else:
            _CACHE = {}
    except Exception:
        _CACHE = {}
    return _CACHE

def _cache_get(key: str) -> dict | None:
    d = _cache_load()
    v = d.get(key)
    if isinstance(v, dict) and "classification" in v:
        return v
    return None

def _cache_set(key: str, value: dict) -> None:
    if os.getenv("LEDGER_NO_CACHE", "").lower() in ("1", "true", "yes"):
        return
    d = _cache_load()
    d[key] = value
    try:
        # atomic-ish: write to temp then rename
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(CACHE_PATH)
    except Exception:
        pass

def classify_exceptions_batch(rows: list[dict], max_workers: int = 5) -> list[dict]:
    """Classify many rows in parallel with cache. Returns list aligned with input."""
    if not rows:
        return []
    # If no API key, just heuristic — no need for threads
    if not anthropic_api_key():
        return [_heuristic_classify(r) for r in rows]
    # Pre-check cache to avoid spawning threads for hits
    results: list[dict | None] = [None] * len(rows)
    to_call: list[tuple[int, dict]] = []
    for i, r in enumerate(rows):
        k = _cache_key(r)
        if k:
            c = _cache_get(k)
            if c:
                results[i] = c
                continue
        to_call.append((i, r))
    if to_call:
        # ThreadPool — IO-bound (HTTP), not CPU
        with ThreadPoolExecutor(max_workers=min(max_workers, len(to_call))) as pool:
            fut_to_idx = {pool.submit(classify_exception, r): i for i, r in to_call}
            for fut in as_completed(fut_to_idx):
                idx = fut_to_idx[fut]
                try:
                    results[idx] = fut.result()
                except Exception as e:
                    results[idx] = {"classification": "unresolved", "audit_note": f"batch classify failed: {e}"}
    # Fill any None (shouldn't happen)
    for i, v in enumerate(results):
        if v is None:
            results[i] = _heuristic_classify(rows[i])
    return results  # type: ignore
