"""Deterministic Policy Engine — the only place hard outcomes are made.

AI (classifier, detector) produces *signals*. This engine turns signals into
hard outcomes: approve | step_up | review | block.

Strict separation:
  - AI never decides. It only returns signals with confidence.
  - Policy is pure functions, fully auditable, versioned, defense-only.
  - No offensive capabilities (no charge creation, no fund movement, no external
    write). Any attempt to add offensive actions is rejected by design.

Machine vs human storage:
  - Machine decisions → `machine_decisions` table (automated, auditable, never
    overwrites human).
  - Human analyst resolutions → `human_resolutions` table (separate, authoritative).
  - Reconciliation wins: human resolution, if present, is the final outcome.

Honest metrics: policy logs every decision with the signal snapshot so held-out
evaluation can compute cost of false positives accurately.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Literal, Optional

Decision = Literal["approve", "step_up", "review", "block"]

# Policy version — bump when rules change so audits are reproducible.
POLICY_VERSION = "v1.0-defense-only"

# Defense-only allowlist — any decision outside this set is rejected.
ALLOWED_DECISIONS: set[str] = {"approve", "step_up", "review", "block"}


@dataclass
class Signals:
    """Signals from AI / deterministic layers. All optional — policy is defensive."""
    risk_score: Optional[float] = None  # 0..1 from detector / classifier
    is_spike: Optional[bool] = None  # rolling-window spike flag
    spike_z: Optional[float] = None  # spike severity in std
    classification: Optional[str] = None  # from app.classify
    reason: Optional[str] = None  # reconcile reason
    diff: Optional[float] = None  # absolute amount gap
    amount: Optional[float] = None  # gross order amount
    history_fraud_rate: Optional[float] = None  # baseline fraud rate
    chargeback_evidence_score: Optional[float] = None  # 0..1 from chargeback responder


@dataclass
class PolicyDecision:
    decision: Decision
    reason: str
    policy_version: str
    signals_snapshot: dict
    created_at: str

    def to_dict(self):
        return asdict(self)


# Offensive guard: reject any tool/action that would move money, create charges,
# or write to external systems. This list is intentionally exhaustive for review.
OFFENSIVE_KEYWORDS = {
    "create_charge",
    "capture_funds",
    "refund",
    "payout",
    "transfer",
    "external_write",
    "send_funds",
}


def _assert_defense_only(action: str):
    if action.lower() in OFFENSIVE_KEYWORDS or "offense" in action.lower():
        raise ValueError(f"Offensive action blocked by guardrail: {action!r}. Pipeline is defense-only.")


def decide(signals: Signals) -> PolicyDecision:
    """Pure function: signals → decision. No IO, no LLM, no side effects.

    Rules (ordered, first match wins; all thresholds documented):
      - block: spike + high risk_score (>0.85) + large amount gap (>10% of gross)
      - step_up: spike + medium risk (>0.65) OR chargeback evidence strong
      - review: any exception that didn't match spike but has risk (>0.45) or
                is an unresolved / missing_* case.
      - approve: clean match or low-risk spike.
    """
    now = datetime.now(timezone.utc).isoformat()
    snap = {k: v for k, v in asdict(signals).items() if v is not None}

    # Defensive defaults
    risk = float(signals.risk_score) if signals.risk_score is not None else 0.0
    # spike alone is not enough — require risk confluence
    spike = bool(signals.is_spike) if signals.is_spike is not None else False
    z = float(signals.spike_z) if signals.spike_z is not None else 0.0
    diff_pct = 0.0
    if signals.diff is not None and signals.amount and signals.amount != 0:
        diff_pct = abs(float(signals.diff)) / abs(float(signals.amount))

    # Rule 1: block — high-confidence spike + large gap
    # Requires at least 2 std spike and high risk to avoid FP cost
    if spike and z >= 2.0 and risk >= 0.85 and diff_pct >= 0.10:
        _assert_defense_only("block")
        return PolicyDecision(
            decision="block",
            reason=f"Fraud-rate spike (z={z:.1f}) with high risk {risk:.2f} and gap {diff_pct:.1%} — requires blocking review",
            policy_version=POLICY_VERSION,
            signals_snapshot=snap,
            created_at=now,
        )

    # Rule 2: step_up — spike with medium risk, or strong chargeback evidence
    cb = float(signals.chargeback_evidence_score) if signals.chargeback_evidence_score is not None else 0.0
    if (spike and risk >= 0.65) or cb >= 0.75:
        _assert_defense_only("step_up")
        return PolicyDecision(
            decision="step_up",
            reason=f"Step-up required: spike={spike} risk={risk:.2f} chargeback_evidence={cb:.2f}",
            policy_version=POLICY_VERSION,
            signals_snapshot=snap,
            created_at=now,
        )

    # Rule 3: review — any unresolved / missing / medium risk
    reasons_review = {"exception_unexplained", "missing_order", "missing_settlement", "unresolved"}
    if (signals.reason in reasons_review) or (signals.classification == "unresolved") or (risk >= 0.45):
        _assert_defense_only("review")
        return PolicyDecision(
            decision="review",
            reason=f"Manual review: reason={signals.reason!r} classification={signals.classification!r} risk={risk:.2f}",
            policy_version=POLICY_VERSION,
            signals_snapshot=snap,
            created_at=now,
        )

    # Rule 4: approve — low risk, no spike, known-good patterns (TDS, late flip with low risk)
    _assert_defense_only("approve")
    return PolicyDecision(
        decision="approve",
        reason=f"Approved: low risk {risk:.2f}, no actionable spike (spike={spike})",
        policy_version=POLICY_VERSION,
        signals_snapshot=snap,
        created_at=now,
    )


def decide_from_dict(signals_dict: dict) -> PolicyDecision:
    """Dict wrapper for API convenience — validates keys."""
    # Guardrail: reject offensive keys even if caller tries to smuggle them
    for k in signals_dict:
        if k.lower() in OFFENSIVE_KEYWORDS:
            raise ValueError(f"Rejected offensive signal key: {k!r}")
    sig = Signals(**{k: v for k, v in signals_dict.items() if k in Signals.__dataclass_fields__})
    return decide(sig)
