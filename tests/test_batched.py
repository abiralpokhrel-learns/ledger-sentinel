"""Batched settlement + 10-order demo story tests."""
import pandas as pd
from app.reconcile import reconcile
from app.reconcile_batched import group_by_utr, reconcile_batched

def test_group_by_utr_counts():
    df = pd.DataFrame([
        {"order_id":"a","amount_settled":100,"utr":"UTR1","settlement_date":"2026-08-10","settlement_status":"captured"},
        {"order_id":"b","amount_settled":200,"utr":"UTR1","settlement_date":"2026-08-10","settlement_status":"captured"},
        {"order_id":"c","amount_settled":50,"utr":"UTR2","settlement_date":"2026-08-11","settlement_status":"captured"},
    ])
    g = group_by_utr(df)
    assert len(g)==2
    r1 = g[g["utr"]=="UTR1"].iloc[0]
    assert int(r1["count"])==2
    assert float(r1["amount_settled_sum"])==300

def test_reconcile_batched_structure():
    orders = pd.DataFrame([
        {"order_id":"demo_001","amount":10000,"mdr":200,"gst":36,"category":"retail_goods","status":"captured"},
    ])
    settlement = pd.DataFrame([
        {"order_id":"demo_001","amount_settled":9764,"settlement_status":"captured","utr":"UTR1","settlement_date":"2026-08-10"},
    ])
    res = reconcile_batched(orders, settlement)
    assert "batched_groups" in res
    assert "summary" in res
    # summary always has at least total_rows etc; batched_groups only when batch exists
    assert "total_rows" in res["summary"]

def test_demo_story_runs():
    from pathlib import Path
    import subprocess, sys
    r = subprocess.run([sys.executable, "scripts/demo_story.py"], capture_output=True, text=True)
    assert r.returncode==0, r.stderr[:500]
    assert "Beautiful Story" in r.stdout
    assert "Batched groups" in r.stdout

def test_reconcile_tolerance_rounding():
    # diff 0.01 should be matched (rounded)
    orders = pd.DataFrame([{"order_id":"x","amount":10000,"mdr":200,"gst":36,"category":"software","status":"captured"}])
    settlement = pd.DataFrame([{"order_id":"x","amount_settled":9763.99,"settlement_status":"captured","utr":"U","settlement_date":"2026-08-10"}])
    m,e = reconcile(orders, settlement)
    assert len(m)==1 and len(e)==0, f"expected matched, got {len(m)} matched {len(e)} exc"
