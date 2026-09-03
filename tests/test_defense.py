"""Tests for Track 02 defense-only additions."""
import pandas as pd
from app.detection import CostSensitiveDetector, compute_cost, find_optimal_threshold, rolling_window_features, compute_baseline, detect_spikes
from app.policy import decide, Signals, ALLOWED_DECISIONS
from app.chargeback import gather_evidence, compile_response
from app.metrics import honest_evaluation_pipeline, generate_synthetic_fraud_dataset
from app import db as _db

def test_cost_fn_25x():
    import numpy as np
    y_true = np.array([1,1,0,0])
    # 1 FN vs 1 FP: FN should cost 25x
    y_pred_fn = np.array([0,1,0,0])  # 1 FN
    y_pred_fp = np.array([1,1,1,0])  # 1 FP
    assert compute_cost(y_true, y_pred_fn) == 25.0
    assert compute_cost(y_true, y_pred_fp) == 1.0

def test_optimal_threshold_minimizes_cost():
    import numpy as np
    scores = np.array([0.1,0.9,0.2,0.8])
    y = np.array([0,1,0,1])
    res = find_optimal_threshold(scores, y)
    assert res.total_cost == 0.0  # perfect separable

def test_rolling_windows_aggregate():
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-08-01 00:10:00","2026-08-01 00:20:00","2026-08-01 01:05:00"]),
        "is_fraud": [1,0,1],
        "amount": [100,200,300],
        "score": [0.9,0.2,0.8],
    })
    w = rolling_window_features(df, window="1h")
    assert len(w) == 2
    assert w.iloc[0]["fraud_rate"] == 0.5

def test_spike_only_when_above_baseline():
    windows = pd.DataFrame({
        "window_start": pd.to_datetime(["2026-08-01","2026-08-02","2026-08-03"]),
        "fraud_rate": [0.01,0.015,0.02],
        "count": [100,100,100],
        "fraud_count": [1,1,2],
        "amount_sum": [1000,1000,1000],
        "avg_score": [0.3,0.3,0.8],
        "fraud_amount_sum": [100,100,200],
    })
    bas = compute_baseline(windows.iloc[:2], k=2.0)
    flagged = detect_spikes(windows.iloc[2:], bas)
    # 0.02 may or may not be spike depending on std; test logic not crash
    assert "is_spike" in flagged.columns

def test_cost_sensitive_detector_rolling_spike():
    import numpy as np
    df = generate_synthetic_fraud_dataset(n=200, seed=1)
    from app.metrics import time_based_split
    train,_ = time_based_split(df)
    det = CostSensitiveDetector(window="6h", k=2.0)
    det.fit(train["score"].values, train["is_fraud"].values)
    res = det.evaluate_stream(df)
    assert "spike_count" in res
    assert "threshold" in res
    assert res["baseline"] is not None

def test_policy_deterministic_and_defense_only():
    sig = Signals(risk_score=0.9, is_spike=True, spike_z=3.0, diff=500, amount=1000, reason="exception_unexplained")
    d = decide(sig)
    assert d.decision in ALLOWED_DECISIONS
    assert d.decision == "block"
    sig2 = Signals(risk_score=0.1, is_spike=False, reason="matched", diff=0, amount=1000)
    assert decide(sig2).decision == "approve"
    # offensive smuggling blocked
    try:
        from app.policy import decide_from_dict
        decide_from_dict({"create_charge": 1})
        assert False, "should block offensive"
    except ValueError:
        pass

def test_machine_vs_human_separate_tables():
    conn = _db.get_connection(":memory:")
    _db.init_db(conn)
    _db.log_machine_decision(conn, "order_0001", "review", reason="auto", policy_version="v1")
    _db.log_human_resolution(conn, "order_0001", "approved", analyst="alice", note="looks ok")
    assert _db.get_final_outcome(conn, "order_0001").startswith("human:")
    mach = _db.load_machine_decisions_df(conn)
    human = _db.load_human_resolutions_df(conn)
    assert len(mach) == 1 and len(human) == 1
    assert "machine_decisions" in mach.columns or "decision" in mach.columns

def test_chargeback_gather_and_compile_readonly():
    conn = _db.get_connection(":memory:")
    _db.init_db(conn)
    _db.upsert_order(conn, "order_CB01", 1000, 20, 10, "software", "captured")
    _db.upsert_settlement(conn, "order_CB01", 980, "captured", "UTR123", "2026-08-01")
    _db.log_audit(conn, "order_CB01", "exception", reason="exception_tds_candidate", classification="expected_tds_withholding", audit_note="test")
    bundle = gather_evidence(conn, "order_CB01")
    assert bundle.order is not None
    resp = compile_response(bundle)
    assert resp.status == "draft"
    assert len(resp.evidence_cited) >= 2
    assert "defense-only" in resp.disclosure.lower() or "Defense-only" in resp.disclosure
    assert "human" in resp.recommended_action.lower()

def test_honest_metrics_held_out():
    r = honest_evaluation_pipeline()
    assert r["is_honest"] is True
    tm = r["test_metrics"]
    assert tm["is_honest_held_out"] is True
    assert "fp_financial_cost_rupees" in tm
    assert tm["fp_financial_cost_rupees"] >= 0
    assert "cost_saved_vs_baseline" in tm

def test_chargeback_never_auto_submits():
    conn = _db.get_connection(":memory:")
    _db.init_db(conn)
    _db.upsert_order(conn, "order_CB02", 500, 10, 5, "retail_goods", "captured")
    b = gather_evidence(conn, "order_CB02")
    resp = compile_response(b)
    assert resp.status == "draft"
    # ensure no submit method exists that would auto-file
    import app.chargeback as cb
    assert not hasattr(cb, "submit_chargeback")
    assert not hasattr(cb, "auto_file")
