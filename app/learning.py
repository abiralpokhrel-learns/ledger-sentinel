"""Human-feedback learning loop — closed-loop AI.

Stores machine_decision vs human_resolution pairs, computes agreement,
and surfaces an evaluation dataset for prompt/model improvement.

No auto-training — just measurement and dataset export. Human is the
supervisor.

Tables already exist: machine_decisions, human_resolutions.
This module adds evaluation helpers.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def record_feedback(conn: sqlite3.Connection, order_id: str, machine_decision: str, human_resolution: str, analyst: str = "analyst") -> dict:
    """Log a human resolution and return agreement info. Caller should have already inserted human_resolutions."""
    # Fetch latest pair
    md = conn.execute("SELECT decision, reason FROM machine_decisions WHERE order_id=? ORDER BY created_at DESC LIMIT 1", (order_id,)).fetchone()
    hr = conn.execute("SELECT resolution, note FROM human_resolutions WHERE order_id=? ORDER BY created_at DESC LIMIT 1", (order_id,)).fetchone()
    machine = dict(md) if md else {"decision": machine_decision, "reason": ""}
    human = dict(hr) if hr else {"resolution": human_resolution, "note": ""}
    agree = (machine.get("decision") == human.get("resolution")) or (machine.get("decision") == human_resolution)
    return {
        "order_id": order_id,
        "machine": machine.get("decision"),
        "human": human.get("resolution") or human_resolution,
        "agreement": agree,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def evaluation_metrics(conn: sqlite3.Connection) -> dict:
    """Compute agreement rate and confusion over all pairs where both exist."""
    try:
        import pandas as pd
        md = pd.read_sql_query("SELECT order_id, decision as machine FROM machine_decisions", conn)
        hr = pd.read_sql_query("SELECT order_id, resolution as human FROM human_resolutions", conn)
        if md.empty or hr.empty:
            return {"pairs": 0, "agreement_rate": None, "note": "No paired decisions yet — need both machine and human for an order"}
        # Join on order_id, keep latest per order
        md_latest = md.drop_duplicates(subset=["order_id"], keep="last")
        hr_latest = hr.drop_duplicates(subset=["order_id"], keep="last")
        merged = md_latest.merge(hr_latest, on="order_id", how="inner")
        if merged.empty:
            return {"pairs": 0, "agreement_rate": None, "note": "No overlapping order_ids between machine and human"}
        merged["agree"] = merged["machine"] == merged["human"]
        agreement_rate = float(merged["agree"].mean())
        # Per-decision breakdown
        by_machine = merged.groupby("machine")["agree"].mean().to_dict() if not merged.empty else {}
        confusion = merged.value_counts(subset=["machine", "human"]).reset_index(name="count").to_dict(orient="records") if len(merged) > 1 else []
        return {
            "pairs": int(len(merged)),
            "agreement_rate": round(agreement_rate, 3),
            "by_machine_decision": {k: round(float(v), 3) for k, v in by_machine.items()},
            "confusion": confusion[:10],
            "note": "Closed-loop: AI recommendation vs human decision. Use to improve prompts.",
        }
    except Exception as e:
        return {"pairs": 0, "error": str(e)}


def export_evaluation_dataset(conn: sqlite3.Connection) -> list[dict]:
    """Export paired dataset for prompt improvement — each row is a training example."""
    try:
        import pandas as pd
        md = pd.read_sql_query("SELECT * FROM machine_decisions ORDER BY created_at", conn)
        hr = pd.read_sql_query("SELECT * FROM human_resolutions ORDER BY created_at", conn)
        if md.empty or hr.empty:
            return []
        # Join on order_id
        merged = md.merge(hr, on="order_id", suffixes=("_machine", "_human"), how="inner")
        out = []
        for _, r in merged.iterrows():
            out.append({
                "order_id": r["order_id"],
                "machine_decision": r.get("decision"),
                "machine_reason": r.get("reason"),
                "machine_signals": r.get("signals_json"),
                "human_resolution": r.get("resolution"),
                "human_note": r.get("note"),
                "analyst": r.get("analyst"),
                "agreement": r.get("decision") == r.get("resolution"),
            })
        return out
    except Exception:
        return []
