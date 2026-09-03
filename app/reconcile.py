"""Reconciliation engine — tolerance-based matching, clean exception isolation.

Deterministic. Compares the net amount we EXPECT to be paid out
(order amount minus MDR minus GST) against what settlement actually shows.
Money is never compared with `==`; a tolerance band absorbs rounding noise.
Anything that doesn't cleanly match — or whose settlement status disagrees
with our ledger — is isolated as an exception for the AI layer.
"""
from __future__ import annotations

import pandas as pd

from app.config import TOLERANCE, TDS_RATE, TDS_BAND, tolerance

# settlement_status values that mean "money actually moved"
SETTLED_STATES = {"captured"}


def _coerce_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def prepare_orders(orders_df: pd.DataFrame) -> pd.DataFrame:
    df = orders_df.copy()
    if df.empty:
        # Ensure expected columns exist for downstream merge
        for col in ["order_id", "amount", "mdr", "gst", "category", "status"]:
            if col not in df.columns:
                df[col] = pd.Series(dtype="object" if col in ("order_id", "category", "status") else "float")
        df["amount_calc"] = pd.Series(dtype=float)
        return df
    df = _coerce_numeric(df, ["amount", "mdr", "gst"])
    # Fill missing MDR/GST with 0 so net calc doesn't become NaN
    df["mdr"] = df["mdr"].fillna(0)
    df["gst"] = df["gst"].fillna(0)
    df["amount_calc"] = (df["amount"] - df["mdr"] - df["gst"]).round(2)
    return df


def reconcile(orders_df: pd.DataFrame, settlement_df: pd.DataFrame):
    """Return (matched, exceptions) DataFrames, both with a `reason` column."""
    # Defensive: deduplicate order_id (last wins) to avoid cartesian explosion
    # and normalize column names
    if "order_id" not in orders_df.columns or "order_id" not in settlement_df.columns:
        raise ValueError("Both orders and settlement must have 'order_id' column")
    orders_df = orders_df.drop_duplicates(subset=["order_id"], keep="last")
    settlement_df = settlement_df.drop_duplicates(subset=["order_id"], keep="last")

    orders = prepare_orders(orders_df)
    settled = settlement_df.copy()
    # Coerce settlement amounts to numeric; non-numeric becomes NaN -> missing_settlement
    settled = _coerce_numeric(settled, ["amount_settled"])
    if "amount_settled" not in settled.columns:
        settled["amount_settled"] = pd.Series(dtype=float)

    merged = orders.merge(
        settled, on="order_id", how="outer", suffixes=("_calc", "_settled")
    )

    # Amount difference where both sides exist; NaN where a side is missing.
    merged["diff"] = (
        merged["amount_calc"] - merged["amount_settled"]
    ).abs()

    # Status consistency: if both sides exist, do they agree on money moving?
    def status_consistent(row) -> bool:
        calc_status = row.get("status")
        sett_status = row.get("settlement_status")
        if pd.isna(calc_status) or pd.isna(sett_status):
            return True  # missing side handled as its own exception below
        calc_moved = calc_status in SETTLED_STATES
        sett_moved = sett_status in SETTLED_STATES
        return calc_moved == sett_moved

    merged["status_consistent"] = merged.apply(status_consistent, axis=1)
    tol = tolerance()

    # Classify each merged row.
    def classify(row) -> str:
        if pd.isna(row["amount_calc"]):
            return "missing_order"          # settlement exists, no order
        if pd.isna(row["amount_settled"]):
            return "missing_settlement"     # order exists, no settlement
        if not row["status_consistent"]:
            return "status_mismatch"        # late-auth flip etc.
        if row["diff"] <= tol:
            return "matched"
        # Above tolerance: candidate for AI. Tag a hint for the classifier.
        # Use gross amount for TDS rate check; only shortfalls (settled < calc) can be TDS
        # diff is abs, so also check settled < calc
        is_shortfall = row["amount_settled"] < row["amount_calc"]
        if is_shortfall and row["amount"] and not pd.isna(row["amount"]) and row["amount"] != 0:
            gap_rate = row["diff"] / float(row["amount"])
            if TDS_RATE - TDS_BAND <= gap_rate <= TDS_RATE + TDS_BAND:
                return "exception_tds_candidate"
        # Fallback: also try calc-based rate for backwards compat (small orders rounding)
        gap_rate_calc = (row["diff"] / row["amount_calc"]) if row["amount_calc"] else 0
        if is_shortfall and TDS_RATE - TDS_BAND <= gap_rate_calc <= TDS_RATE + TDS_BAND:
            return "exception_tds_candidate"
        return "exception_unexplained"

    merged["reason"] = merged.apply(classify, axis=1)

    matched = merged[merged["reason"] == "matched"].copy()
    exceptions = merged[merged["reason"] != "matched"].copy()
    return matched, exceptions


def summarize(matched: pd.DataFrame, exceptions: pd.DataFrame) -> dict:
    """Human-friendly match-rate summary for the dashboard / README."""
    total = len(matched) + len(exceptions)
    match_rate = (len(matched) / total * 100) if total else 0.0
    # Sort by_reason for deterministic output (stable across pandas versions)
    by_reason = {}
    if not exceptions.empty and "reason" in exceptions.columns:
        by_reason = exceptions["reason"].value_counts().sort_index().to_dict()
    return {
        "total_rows": total,
        "matched": len(matched),
        "exceptions": len(exceptions),
        "match_rate_pct": round(match_rate, 1),
        "by_reason": by_reason,
    }
