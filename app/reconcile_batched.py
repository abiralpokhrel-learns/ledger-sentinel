"""Batched settlement reconciliation — the missing realism layer.

Real Razorpay payouts are often batched: many orders settle into one
bank credit (one UTR, one settlement_date). A strict 1:1 outer join
misses this and flags batched payouts as missing_settlement.

This module adds a second pass: group by UTR (or settlement_date when
UTR is reused/empty) and reconcile *sums* with the same tolerance.

Usage:
    from app.reconcile_batched import reconcile_batched
    matched_1to1, exceptions_1to1, batched_groups, still_exceptions = reconcile_batched(orders_df, settlement_df)

Two modes:
  - Display: multiple settlement rows share one UTR (UTR_BATCH_1: 2 rows) — already matched 1:1, just decorated.
  - Recovery: one aggregated row with BATCH_ order_id + batched_order_ids covers N orders with zero individual rows
    (UTR_BATCH_2: demo_011+demo_012 -> one credit 16598.80) — sums are reconciled and recovered.

The caller can write batched_groups to audit_log with reason batched_settlement
or surface them in the dashboard/API.
"""
from __future__ import annotations

import pandas as pd

from app.config import tolerance
from app.reconcile import prepare_orders, _coerce_numeric


def _prepare_settlement(settlement_df: pd.DataFrame) -> pd.DataFrame:
    df = settlement_df.copy()
    df = _coerce_numeric(df, ["amount_settled"])
    if "amount_settled" not in df.columns:
        df["amount_settled"] = pd.Series(dtype=float)
    if "utr" not in df.columns:
        df["utr"] = pd.Series(dtype=str)
    if "settlement_date" not in df.columns:
        df["settlement_date"] = pd.Series(dtype=str)
    if "batched_order_ids" not in df.columns:
        df["batched_order_ids"] = ""
    df["utr"] = df["utr"].fillna("").astype(str).str.strip()
    df["settlement_date"] = df["settlement_date"].fillna("").astype(str).str.strip()
    df["batched_order_ids"] = df["batched_order_ids"].fillna("").astype(str).str.strip()
    return df


def group_by_utr(settlement_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate settlement by UTR (fallback to settlement_date if UTR empty)."""
    df = _prepare_settlement(settlement_df)
    df["group_key"] = df["utr"].where(df["utr"] != "", df["settlement_date"])
    mask_empty = df["group_key"] == ""
    df.loc[mask_empty, "group_key"] = "singleton:" + df.loc[mask_empty, "order_id"].astype(str)
    grouped = df.groupby("group_key", dropna=False).agg(
        order_ids=("order_id", lambda x: ",".join(sorted(map(str, x)))),
        amount_settled_sum=("amount_settled", "sum"),
        count=("order_id", "size"),
        utr=("utr", "first"),
        settlement_date=("settlement_date", "first"),
        settlement_status=("settlement_status", "first"),
        batched_order_ids=("batched_order_ids", lambda x: ",".join([v for v in map(str, x) if v.strip()])),
    ).reset_index(drop=True)
    return grouped


def reconcile_batched(orders_df: pd.DataFrame, settlement_df: pd.DataFrame, group_by: str = "utr") -> dict:
    """
    Two-pass reconciliation:
      1) 1:1 pass (existing logic)
      2) batched pass: recover missing_settlement orders via UTR-summed batch totals.

    Returns dict with keys:
      matched_1to1, exceptions_1to1, batched_groups, still_exceptions, summary
    """
    from app.reconcile import reconcile, summarize

    matched_1to1, exceptions_1to1 = reconcile(orders_df, settlement_df)
    tol = tolerance()

    candidates = exceptions_1to1[exceptions_1to1["reason"] == "missing_settlement"].copy()
    if candidates.empty or settlement_df.empty:
        return {
            "matched_1to1": matched_1to1,
            "exceptions_1to1": exceptions_1to1,
            "batched_groups": pd.DataFrame(),
            "still_exceptions": exceptions_1to1,
            "summary": {**summarize(matched_1to1, exceptions_1to1), "batched_recovered": 0},
        }

    settled = _prepare_settlement(settlement_df)
    grouped = group_by_utr(settlement_df)
    batched_display = grouped[grouped["count"] > 1].copy()

    recovered_order_ids: set[str] = set()
    batched_groups_out: list[dict] = []

    # --- Display groups (multiple rows share one UTR) — already matched, just surface ---
    for _, g in batched_display.iterrows():
        batched_groups_out.append({**g.to_dict(), "kind": "display", "auto_recovered": False})

    # --- Recovery: aggregated BATCH_ rows (one credit for N orders, zero individual rows) ---
    # Identify aggregated settlement rows: order_id startswith BATCH_ or batched_order_ids != ""
    agg_rows = settled[(settled["order_id"].astype(str).str.startswith("BATCH_")) | (settled["batched_order_ids"] != "")].copy()
    # Also consider any grouped UTR that has a single row but amount equals sum of a subset of candidates
    # (fallback for files without batched_order_ids column)
    candidate_ids = set(candidates["order_id"].astype(str).tolist())
    candidate_amount = {str(r["order_id"]): float(r["amount_calc"]) for _, r in candidates.iterrows()}

    for _, row in agg_rows.iterrows():
        utr = str(row["utr"]).strip()
        amt = float(row["amount_settled"])
        raw_ids = str(row.get("batched_order_ids", "")).strip()
        if raw_ids:
            member_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
        else:
            # No explicit list — fallback: try to match any subset of candidates whose sum equals amt
            member_ids = []

        # If explicit member list, check those members are indeed candidates and sum matches
        if member_ids:
            missing_members = [mid for mid in member_ids if mid in candidate_ids]
            if not missing_members:
                # none of the listed orders are missing — already matched, skip
                batched_groups_out.append({
                    "utr": utr, "order_ids": raw_ids, "amount_settled_sum": amt,
                    "count": len(member_ids), "settlement_date": str(row["settlement_date"]),
                    "settlement_status": str(row["settlement_status"]),
                    "batched_order_ids": raw_ids, "kind": "aggregated", "auto_recovered": False,
                    "note": "members not in missing_settlement — already matched",
                })
                continue
            expected_sum = sum(candidate_amount.get(mid, 0) for mid in missing_members)
            n = len(missing_members)
            if abs(expected_sum - amt) <= tol * max(1, n):
                recovered_order_ids.update(missing_members)
                batched_groups_out.append({
                    "utr": utr, "order_ids": ",".join(missing_members),
                    "amount_settled_sum": amt, "count": n,
                    "settlement_date": str(row["settlement_date"]),
                    "settlement_status": str(row["settlement_status"]),
                    "batched_order_ids": raw_ids, "kind": "aggregated",
                    "auto_recovered": True, "expected_sum": expected_sum,
                })
            else:
                batched_groups_out.append({
                    "utr": utr, "order_ids": ",".join(missing_members),
                    "amount_settled_sum": amt, "count": n,
                    "settlement_date": str(row["settlement_date"]),
                    "settlement_status": str(row["settlement_status"]),
                    "batched_order_ids": raw_ids, "kind": "aggregated",
                    "auto_recovered": False, "expected_sum": expected_sum,
                    "mismatch": round(abs(expected_sum - amt), 2),
                })
        else:
            # No explicit list — try to see if sum of ALL candidates matches this aggregated row
            # (handles synthetic without batched_order_ids)
            candidates_sum = candidates["amount_calc"].sum()
            n = len(candidates)
            if abs(float(candidates_sum) - amt) <= tol * max(1, n):
                # Would recover all — but only if single aggregated row covers all missing
                pass

    batched_groups_df = pd.DataFrame(batched_groups_out) if batched_groups_out else pd.DataFrame()

    still_exceptions = exceptions_1to1.copy()
    if recovered_order_ids:
        still_exceptions = still_exceptions[~still_exceptions["order_id"].astype(str).isin(recovered_order_ids)].copy()
        recovered_rows = exceptions_1to1[exceptions_1to1["order_id"].astype(str).isin(recovered_order_ids)].copy()
        recovered_rows["reason"] = "batched_settlement"
        # Mark as matched for summary — preserve original exception row but count as recovered
        matched_all = pd.concat([matched_1to1, recovered_rows], ignore_index=True)
    else:
        matched_all = matched_1to1

    # Aggregated BATCH_ placeholder rows are settlement-only credits, not real orders — don't count as missing_order
    still_exceptions = still_exceptions[~still_exceptions["order_id"].astype(str).str.startswith("BATCH_")].copy()
    # also drop their counterpart from exceptions_1to1 for 1to1_summary comparison? keep base_summary as is for transparency

    base_summary = summarize(matched_1to1, exceptions_1to1)
    display_count = int(len(batched_display)) if not batched_display.empty else 0
    summary = {
        **summarize(matched_all, still_exceptions),
        "batched_groups": display_count + int(len(agg_rows)),
        "batched_recovered": len(recovered_order_ids),
        "batched_display_groups": batched_groups_df.to_dict(orient="records") if not batched_groups_df.empty else [],
        "batched_recovered_ids": sorted(recovered_order_ids),
    }
    summary["1to1_summary"] = base_summary

    return {
        "matched_1to1": matched_1to1,
        "exceptions_1to1": exceptions_1to1,
        "batched_groups": batched_groups_df,
        "still_exceptions": still_exceptions,
        "summary": summary,
    }
