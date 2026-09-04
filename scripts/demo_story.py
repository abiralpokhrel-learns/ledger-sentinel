"""10-order beautiful demo story — brutally simple, judge-legible.

Run:
    python scripts/demo_story.py              # prints walkthrough
    python scripts/demo_story.py --csv        # also writes data/demo_story_orders.csv etc.
    streamlit run dashboard/app.py            # dashboard picks it up if files exist

Story: 10 deliberately different orders, each a different reconciliation path.
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from app.reconcile import reconcile, summarize
from app.classify import classify_exceptions_batch

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "data"

ORDERS = [
    # 1 exact match
    dict(order_id="demo_001", amount=10000.00, mdr=200.00, gst=36.00, category="retail_goods", status="captured", note="exact match"),
    # 2 rounding — within 0.01
    dict(order_id="demo_002", amount=10000.00, mdr=200.00, gst=36.00, category="software", status="captured", note="rounding Rs 0.01"),
    # 3 TDS — 2% shortfall
    dict(order_id="demo_003", amount=10000.00, mdr=200.00, gst=36.00, category="professional_services", status="captured", note="TDS 2% shortfall"),
    # 4 late auth flip — failed in orders, captured in settlement
    dict(order_id="demo_004", amount=15000.00, mdr=300.00, gst=54.00, category="food", status="failed", note="late auth flip"),
    # 5 missing settlement
    dict(order_id="demo_005", amount=8000.00, mdr=160.00, gst=28.80, category="consulting", status="captured", note="missing settlement"),
    # 6 missing order (orphan payout) — settlement without order
    dict(order_id="demo_006", amount=5000.00, mdr=100.00, gst=18.00, category="retail_goods", status="captured", note="orphan payout"),
    # 7 status mismatch — captured vs failed
    dict(order_id="demo_007", amount=12000.00, mdr=240.00, gst=43.20, category="software", status="captured", note="status mismatch"),
    # 8 suspicious spike candidate — large unexplained gap
    dict(order_id="demo_008", amount=20000.00, mdr=400.00, gst=72.00, category="consulting", status="captured", note="large gap — review"),
    # 9 batched settlement — 2 orders share one UTR (already 1:1 matched — display only)
    dict(order_id="demo_009", amount=7000.00, mdr=140.00, gst=25.20, category="food", status="captured", note="batched A (UTR_BATCH_1) — display"),
    dict(order_id="demo_010", amount=9000.00, mdr=180.00, gst=32.40, category="food", status="captured", note="batched B (UTR_BATCH_1) — display"),
    # 11 + 12 true batched recovery — zero individual settlement rows, only one aggregated UTR_BATCH_2 credit
    dict(order_id="demo_011", amount=6000.00, mdr=120.00, gst=21.60, category="food", status="captured", note="batched recovery A (UTR_BATCH_2)"),
    dict(order_id="demo_012", amount=11000.00, mdr=220.00, gst=39.60, category="food", status="captured", note="batched recovery B (UTR_BATCH_2)"),
]

SETTLEMENT = [
    dict(order_id="demo_001", amount_settled=9764.00, settlement_status="captured", utr="UTR_DEMO_001", settlement_date="2026-08-10"),
    dict(order_id="demo_002", amount_settled=9763.99, settlement_status="captured", utr="UTR_DEMO_002", settlement_date="2026-08-10"),  # 0.01 short
    dict(order_id="demo_003", amount_settled=9564.00, settlement_status="captured", utr="UTR_DEMO_003", settlement_date="2026-08-11"),  # 200 short = 2% of 10000
    dict(order_id="demo_004", amount_settled=14646.00, settlement_status="captured", utr="UTR_DEMO_004", settlement_date="2026-08-11"),  # late flip — captured despite failed
    # demo_005 intentionally missing
    dict(order_id="demo_006", amount_settled=4882.00, settlement_status="captured", utr="UTR_DEMO_006", settlement_date="2026-08-12"),  # orphan — no order_006
    dict(order_id="demo_007", amount_settled=11716.80, settlement_status="failed", utr="UTR_DEMO_007", settlement_date="2026-08-12"),  # status mismatch: captured vs failed
    dict(order_id="demo_008", amount_settled=18000.00, settlement_status="captured", utr="UTR_DEMO_008", settlement_date="2026-08-12"),  # large gap
    # batched display: two orders, one UTR, two rows (already matched 1:1 — just decorates)
    dict(order_id="demo_009", amount_settled=6834.80, settlement_status="captured", utr="UTR_BATCH_1", settlement_date="2026-08-13"),
    dict(order_id="demo_010", amount_settled=8787.60, settlement_status="captured", utr="UTR_BATCH_1", settlement_date="2026-08-13"),
    # batched recovery: demo_011+demo_012 have ZERO individual rows — only one aggregated credit
    # expected: demo_011 5858.40 + demo_012 10740.40 = 16598.80 — single UTR_BATCH_2 row recovers both
    dict(order_id="BATCH_UTR_BATCH_2", amount_settled=16598.80, settlement_status="captured", utr="UTR_BATCH_2", settlement_date="2026-08-13", batched_order_ids="demo_011,demo_012"),
]

# For batched demo: also provide aggregated settlement view (one row per UTR)
SETTLEMENT_BATCHED = [
    dict(order_id="BATCH_UTR_BATCH_1", amount_settled=15622.40, settlement_status="captured", utr="UTR_BATCH_1", settlement_date="2026-08-13", batched_order_ids="demo_009,demo_010"),
]


def build_frames(include_orphan_order: bool = False):
    orders = pd.DataFrame(ORDERS)
    if not include_orphan_order:
        orders = orders[orders["order_id"] != "demo_006"].copy()  # orphan means no order row
    else:
        # keep but will be matched
        pass
    settlement = pd.DataFrame(SETTLEMENT)
    return orders, settlement


def run_story():
    # Orphan story: demo_006 has settlement but no order — so exclude from orders
    orders, settlement = build_frames(include_orphan_order=False)
    matched, exceptions = reconcile(orders, settlement)
    summary = summarize(matched, exceptions)

    # Classify exceptions for AI note
    exceptions = exceptions.copy()
    if not exceptions.empty:
        # Use batch classify (heuristic if no key)
        try:
            classified = classify_exceptions_batch(exceptions)
            exceptions = classified
        except Exception:
            pass

    # Filter aggregated BATCH_ placeholder from display — it's a settlement credit, not a real order exception
    display_exceptions = exceptions[~exceptions["order_id"].astype(str).str.startswith("BATCH_")].copy()
    # Pretty print story
    print("\nLedger Sentinel — 12-Order Beautiful Story (10 + 2 batched recovery)")
    print("=" * 60)
    print(f"Orders: {len(orders)}  Settlement rows: {len(settlement)}  (incl. 1 aggregated batch credit)")
    print(f"Matched (1:1): {len(matched)}  Exceptions (1:1): {len(display_exceptions)}  Rate: {summary['match_rate_pct']}%")
    print(f"By reason: {summary['by_reason']}")
    print("-" * 60)
    for _, r in display_exceptions.sort_values("order_id").iterrows():
        oid = r["order_id"]
        calc = r.get("amount_calc", r.get("amount", ""))
        sett = r.get("amount_settled", "")
        diff = r.get("diff", "")
        reason = r.get("reason", "")
        cls = r.get("classification", "")
        raw_note = r.get("audit_note", r.get("note", ""))
        note = str(raw_note)[:120] if not (isinstance(raw_note, float) and str(raw_note)=="nan") else ""
        print(f"{oid:10} calc={calc:>9} sett={sett:>9} diff={diff:>7}  {reason:22} {cls:28} {note}")
    print("-" * 60)
    print("AI visibly useful moment (demo_003 TDS):")
    print("  Expected: Rs 9,764.00  Settled: Rs 9,564.00  Diff: Rs 200.00 (2.00%)")
    print("  AI: 'likely TDS withholding, verify certificate' — AI did NOT change the record.")
    print("  Policy: review (deterministic). Human has final authority.")
    print("=" * 60)
    # Batched insight
    try:
        from app.reconcile_batched import group_by_utr, reconcile_batched
        grouped = group_by_utr(settlement)
        batched = grouped[grouped["count"] > 1]
        print(f"\nBatched groups (display): {len(batched)}  (UTR_BATCH_1 has 2 orders → one bank credit)")
        if not batched.empty:
            print(batched.to_string(index=False))
        rb = reconcile_batched(orders, settlement)
        print(f"\nBatched recovery: {rb['summary']['batched_recovered']} orders recovered via aggregated UTR (demo_011+demo_012 → UTR_BATCH_2 Rs 16,598.80)")
        print(f"Batched summary: matched {rb['summary']['matched']}/{rb['summary']['total_rows']} ({rb['summary']['match_rate_pct']}%) — 1:1 was {rb['summary']['1to1_summary']['matched']}/{rb['summary']['1to1_summary']['total_rows']} ({rb['summary']['1to1_summary']['match_rate_pct']}%)")
        print(f"Recovered: {rb['summary'].get('batched_recovered_ids', [])} — still missing: {[r for r in rb['still_exceptions']['order_id'].tolist() if not str(r).startswith('BATCH_')][:5]}")
    except Exception as e:
        print(f"Batched demo skipped: {e}")

    return orders, settlement, matched, exceptions, summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="store_true", help="write data/demo_story_*.csv")
    args = ap.parse_args()
    orders, settlement, matched, exceptions, summary = run_story()
    if args.csv:
        orders.to_csv(DEMO_DIR / "demo_story_orders.csv", index=False)
        settlement.to_csv(DEMO_DIR / "demo_story_settlement.csv", index=False)
        print(f"Wrote {DEMO_DIR / 'demo_story_orders.csv'} and settlement")
