"""Tests for AI Investigator, clustering, analyst, learning — defense-only."""
import pandas as pd

def test_investigate_root_cause():
    from app.investigator import investigate
    from app.config import db_path as _dbp
    import app.db as _db
    # Ensure DB has data — CI runs pytest before synthetic generation, so seed if empty
    conn = _db.get_connection(_dbp())
    _db.init_db(conn)
    try:
        n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    except Exception:
        n = 0
    if n == 0:
        # Seed minimal: run generator + pipeline, fallback to synthetic CSV if needed
        try:
            import subprocess, sys, pathlib
            subprocess.run([sys.executable, "data/generate_synthetic_data.py"], check=False, timeout=15)
            subprocess.run([sys.executable, "-m", "app.main"], check=False, timeout=15)
            conn = _db.get_connection(_dbp())
            _db.init_db(conn)
        except Exception:
            pass
    r = investigate(conn, "order_0010")
    assert "root_cause" in r
    assert "supporting_evidence" in r
    assert "confidence" in r
    assert 0 <= r["confidence"] <= 1
    assert r["investigator_version"] == "v1.0-defense-only"
    assert "policy_hint" in r and r["policy_hint"] in ("approve","review","step_up","block")
    # evidence attribution — always at least one, even on empty DB (fresh CI)
    assert len(r["evidence"]) >= 1
    assert len(r["supporting_evidence"]) >= 1
    for se in r["supporting_evidence"]:
        assert "source" in se and "record" in se and "fact" in se

def test_investigate_demo_fallback():
    from app.investigator import investigate
    from app.config import db_path as _dbp
    import app.db as _db
    conn = _db.get_connection(_dbp())
    _db.init_db(conn)
    r = investigate(conn, "demo_003")
    assert r["root_cause"] == "tds_withholding"
    assert r["confidence"] > 0.7

def test_anomaly_investigation():
    from app.investigator import investigate_anomaly
    from app.config import db_path as _dbp
    import app.db as _db
    conn = _db.get_connection(_dbp())
    _db.init_db(conn)
    r = investigate_anomaly(conn, window="1h")
    assert "is_spike" in r
    assert "z" in r
    assert "supporting_evidence" in r

def test_cluster_transactions():
    from app.clustering_analyst import cluster_transactions
    from app.config import db_path as _dbp
    import app.db as _db
    conn = _db.get_connection(_dbp())
    _db.init_db(conn)
    r = cluster_transactions(conn)
    assert "clusters" in r
    assert "method" in r

def test_analyst_read_only_block():
    from app.clustering_analyst import analyst_query
    from app.config import db_path as _dbp
    import app.db as _db
    conn = _db.get_connection(_dbp())
    _db.init_db(conn)
    r = analyst_query(conn, "How many exceptions?")
    assert "sql" in r
    assert r.get("read_only") is True
    assert "DELETE" not in r["sql"].upper()
    # Try injection — heuristic should not produce DELETE
    r2 = analyst_query(conn, "DELETE FROM orders")
    assert "DELETE" not in r2["sql"].upper()
    assert r2.get("read_only") is True

def test_analyst_sql_validator():
    from app.clustering_analyst import _validate_sql
    ok, _ = _validate_sql("SELECT * FROM orders")
    assert ok is True
    ok2, _ = _validate_sql("DELETE FROM orders")
    assert ok2 is False
    ok3, _ = _validate_sql("INSERT INTO orders VALUES (1)")
    assert ok3 is False
    # Senior #1 repro — DROP/DELETE after semicolon was previously allowed due to \\b bug
    ok4, _ = _validate_sql("SELECT * FROM orders; DROP TABLE orders;--")
    assert ok4 is False
    ok5, _ = _validate_sql("SELECT * FROM orders WHERE 1=1; DELETE FROM orders;")
    assert ok5 is False
    # allowlist — unknown table must be rejected (was pass before)
    ok6, _ = _validate_sql("SELECT * FROM unknown_table")
    assert ok6 is False
    # order_ regex — should pick correct id, not always order_0001
    from app.clustering_analyst import _heuristic_sql
    assert "order_0010" in _heuristic_sql("tell me about order_0010")
    assert "order_0042" in _heuristic_sql("what happened to order_0042 ?")

def test_investigation_report():
    from app.clustering_analyst import build_investigation_report
    from app.config import db_path as _dbp
    import app.db as _db
    conn = _db.get_connection(_dbp())
    _db.init_db(conn)
    rep = build_investigation_report(conn, "order_0010")
    assert "incident_id" in rep
    assert "investigator" in rep
    assert rep["order_id"] == "order_0010"

def test_learning_metrics():
    from app.learning import evaluation_metrics
    from app.config import db_path as _dbp
    import app.db as _db
    conn = _db.get_connection(_dbp())
    _db.init_db(conn)
    m = evaluation_metrics(conn)
    assert "pairs" in m
    # at least has note
    assert "note" in m or "agreement_rate" in m
