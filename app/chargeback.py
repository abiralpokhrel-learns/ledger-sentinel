"""Active Chargeback Responder — defense-only evidence compiler.

Razor Shield AI previously had read-only investigation tools that dumped raw
logs for a human to read. This module completes the Track 02 direction:

  - Gathers evidence using ONLY read-only tools (no external writes).
  - Compiles it into a structured, audit-ready Chargeback Response.

Guardrails:
  - Defense-only: never initiates charges, refunds, or payouts. Response is a
    *document* for a human to file, not an automated submission.
  - All evidence is cited with source (order, settlement, webhook_events,
    audit_log, machine_decisions). No invented facts.
  - Machine output is explicitly labeled and stored separately from human
    analyst resolutions (see POL-SEC in docs).

The responder does NOT auto-submit to Razorpay/acquirer — human approval is
required. The `status` starts as `draft` and only a human can move it to
`approved`/`filed` via the human_resolutions table.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from app import db

RESPONSE_VERSION = "v1.0-defense-only"
# Defense-only guard: responder never calls offensive APIs
FORBIDDEN_ACTIONS = {"submit_chargeback", "create_dispute", "refund_on_behalf", "payout"}


@dataclass
class EvidenceBundle:
    order_id: str
    order: Optional[dict] = None
    settlement: Optional[dict] = None
    webhook_events: list[dict] = field(default_factory=list)
    audit_entries: list[dict] = field(default_factory=list)
    machine_decision: Optional[dict] = None
    human_resolution: Optional[dict] = None
    gathered_at: str = ""


@dataclass
class ChargebackResponse:
    case_id: str
    order_id: str
    status: str  # draft | human_approved | filed — never auto-filed
    version: str
    summary: str
    timeline: list[dict]
    amount_analysis: dict
    evidence_cited: list[dict]
    recommended_action: str
    disclosure: str
    evidence_bundle: EvidenceBundle
    created_at: str

    def to_dict(self):
        d = asdict(self)
        # evidence_bundle is dataclass, asdict handles it
        return d

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, default=str)


def _assert_defense_only(action: str):
    if action.lower() in FORBIDDEN_ACTIONS or "submit" in action.lower():
        raise ValueError(f"Offensive action blocked: {action!r} — responder is defense-only, human must file.")


def gather_evidence(conn: sqlite3.Connection, order_id: str) -> EvidenceBundle:
    """Read-only evidence gathering. Uses only SELECTs — no writes."""
    # Order
    order = None
    try:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        order = dict(row) if row else None
    except Exception:
        pass
    # Settlement
    settlement = None
    try:
        row = conn.execute("SELECT * FROM settlement WHERE order_id = ?", (order_id,)).fetchone()
        settlement = dict(row) if row else None
    except Exception:
        pass
    # Webhook events (immutable delivery log)
    events = []
    try:
        for r in conn.execute("SELECT * FROM webhook_events WHERE order_id = ? ORDER BY processed_at", (order_id,)).fetchall():
            events.append(dict(r))
    except Exception:
        pass
    # Audit trail
    audits = []
    try:
        for r in conn.execute("SELECT * FROM audit_log WHERE order_id = ? ORDER BY id", (order_id,)).fetchall():
            audits.append(dict(r))
    except Exception:
        pass
    # Machine vs human separation
    machine = None
    try:
        r = conn.execute("SELECT * FROM machine_decisions WHERE order_id = ? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        machine = dict(r) if r else None
    except Exception:
        pass
    human = None
    try:
        r = conn.execute("SELECT * FROM human_resolutions WHERE order_id = ? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        human = dict(r) if r else None
    except Exception:
        pass
    return EvidenceBundle(
        order_id=order_id,
        order=order,
        settlement=settlement,
        webhook_events=events,
        audit_entries=audits,
        machine_decision=machine,
        human_resolution=human,
        gathered_at=datetime.now(timezone.utc).isoformat(),
    )


def compile_response(bundle: EvidenceBundle, case_id: Optional[str] = None) -> ChargebackResponse:
    """Compile bundle into structured, human-filed response. Pure function, no IO."""
    _assert_defense_only("compile")  # guard
    order_id = bundle.order_id
    cid = case_id or f"CB-{order_id}-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    # Amount analysis (deterministic, cited)
    amount_analysis = {}
    if bundle.order and bundle.settlement:
        try:
            gross = float(bundle.order.get("amount") or 0)
            mdr = float(bundle.order.get("mdr") or 0)
            gst = float(bundle.order.get("gst") or 0)
            net_expected = round(gross - mdr - gst, 2)
            settled = float(bundle.settlement.get("amount_settled") or 0)
            diff = round(net_expected - settled, 2)
            amount_analysis = {
                "gross_amount": gross,
                "mdr": mdr,
                "gst": gst,
                "net_expected": net_expected,
                "amount_settled": settled,
                "difference": diff,
                "settlement_status": bundle.settlement.get("settlement_status"),
                "utr_cited": bundle.settlement.get("utr"),
                "source": "orders + settlement (read-only SELECT)",
            }
        except Exception as e:
            amount_analysis = {"error": str(e), "source": "orders + settlement"}
    else:
        amount_analysis = {"note": "One side missing — see evidence_cited", "order_present": bundle.order is not None, "settlement_present": bundle.settlement is not None}

    # Timeline from webhook_events + audit
    timeline = []
    for e in bundle.webhook_events:
        timeline.append({"at": e.get("processed_at"), "what": f"webhook {e.get('event_type')} ({e.get('event_id')})", "source": "webhook_events"})
    for a in bundle.audit_entries:
        timeline.append({"at": a.get("created_at"), "what": f"audit {a.get('outcome')}:{a.get('reason')} — {a.get('classification')}", "source": "audit_log"})
    timeline.sort(key=lambda x: str(x.get("at") or ""))

    # Evidence cited (explicit sources for acquirer)
    evidence_cited = []
    if bundle.order:
        evidence_cited.append({"type": "order_record", "order_id": order_id, "fields": {k: bundle.order.get(k) for k in ("amount", "mdr", "gst", "category", "status")}, "source": "orders table"})
    if bundle.settlement:
        evidence_cited.append({"type": "settlement_record", "order_id": order_id, "fields": {k: bundle.settlement.get(k) for k in ("amount_settled", "settlement_status", "utr", "settlement_date")}, "source": "settlement table"})
    if bundle.webhook_events:
        evidence_cited.append({"type": "webhook_delivery_log", "count": len(bundle.webhook_events), "source": "webhook_events (immutable, idempotent)"})
    if bundle.machine_decision:
        evidence_cited.append({"type": "machine_decision", "decision": bundle.machine_decision.get("decision"), "policy_version": bundle.machine_decision.get("policy_version"), "source": "machine_decisions (separate from human)"})

    # Recommended action is advisory only — human decides
    # Use audit classification to suggest, but never auto-file
    last_cls = None
    if bundle.audit_entries:
        last_cls = bundle.audit_entries[-1].get("classification")
    if last_cls == "expected_tds_withholding":
        rec = "Include TDS certificate / 26AS trail; amount gap is within statutory withholding band — contest as valid deduction."
    elif last_cls == "late_authorization_flip":
        rec = "Include webhook delivery log showing late authorization flip; settlement captured despite earlier failure signal."
    elif bundle.settlement is None:
        rec = "Settlement missing — gather bank UTR / payout recon before filing; do not claim without settlement proof."
    else:
        rec = "Provide full evidence pack for manual review; do not auto-contest without human verification."

    summary = f"Chargeback evidence pack for {order_id}: {len(evidence_cited)} evidence items, {len(timeline)} timeline events. Draft — requires human approval before filing."

    return ChargebackResponse(
        case_id=cid,
        order_id=order_id,
        status="draft",
        version=RESPONSE_VERSION,
        summary=summary,
        timeline=timeline,
        amount_analysis=amount_analysis,
        evidence_cited=evidence_cited,
        recommended_action=rec + " (Advisory — human analyst must approve; this system never auto-submits.)",
        disclosure="Defense-only. Compiled from read-only SELECTs; all sources cited. Machine draft stored in machine_decisions; human resolution, if any, in human_resolutions. Pipeline never performs offensive actions.",
        evidence_bundle=bundle,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# Persistence helpers — keep machine vs human strictly separate
def save_machine_draft(conn: sqlite3.Connection, resp: ChargebackResponse) -> int:
    """Store draft in machine_decisions (never in human table)."""
    _assert_defense_only("save_draft")  # still defense
    conn.execute(
        """INSERT INTO machine_decisions (order_id, case_id, decision, reason, policy_version, signals_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (resp.order_id, resp.case_id, "draft", resp.summary[:500], resp.version, json.dumps(resp.to_dict(), default=str)[:8000], resp.created_at),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def list_chargeback_cases(conn: sqlite3.Connection, limit: int = 20):
    try:
        rows = conn.execute("SELECT * FROM machine_decisions WHERE case_id LIKE 'CB-%' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
