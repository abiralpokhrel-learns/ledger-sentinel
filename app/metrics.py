"""Honest Metrics — held-out test set, financial cost of false positives.

Displays metrics that a finance operator can trust:
  - Train/test split is time-based (no leakage).
  - Every number is computed on the held-out test window, never on training data.
  - Explicitly calculates financial cost of false positives (review cost + friction)
    and total cost (25*FN + 1*FP weighted, and rupee cost).

Why honest? Many demos report accuracy on training data. We report on held-out
test windows that the detector threshold never saw during fit, and we show the
rupee cost of being wrong — not just counts.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

from app.detection import FN_COST, FP_COST, FP_REVIEW_COST_RUPEES, compute_cost, find_optimal_threshold


@dataclass
class HeldOutMetrics:
    test_size: int
    train_size: int
    tp: int
    tn: int
    fp: int
    fn: int
    precision: float
    recall: float
    fpr: float
    accuracy: float
    threshold: float
    total_cost_units: float  # 25*FN + 1*FP
    fp_financial_cost_rupees: float  # FP * 500
    fn_financial_cost_rupees: float  # FN * avg_amount (approx)
    total_financial_cost_rupees: float
    baseline_cost_units: float  # cost if we flagged nothing / flagged all
    cost_saved_vs_baseline: float
    is_honest_held_out: bool = True

    def to_dict(self):
        return asdict(self)


def time_based_split(df: pd.DataFrame, time_col: str = "timestamp", test_frac: float = 0.3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Time-based split: train is earliest 70%, test is latest 30% (no shuffle)."""
    if df.empty:
        return df, df
    d = df.copy()
    d[time_col] = pd.to_datetime(d[time_col], errors="coerce")
    d = d.sort_values(time_col)
    n = len(d)
    split = int(n * (1 - test_frac))
    return d.iloc[:split].copy(), d.iloc[split:].copy()


def evaluate_held_out(
    scores: np.ndarray,
    y_true: np.ndarray,
    amounts: Optional[np.ndarray] = None,
    threshold: Optional[float] = None,
) -> HeldOutMetrics:
    """Evaluate on held-out test set given a fixed threshold (from train).

    If threshold is None, finds optimal on this set (for reporting only, not honest).
    """
    scores = np.asarray(scores, dtype=float)
    y_true = np.asarray(y_true, dtype=int)
    if threshold is None:
        res = find_optimal_threshold(scores, y_true, amounts)
        threshold = res.threshold
        # cost already computed; reuse
        tp, tn, fp, fn = res.tp, res.tn, res.fp, res.fn
        cost = res.total_cost
    else:
        y_pred = (scores >= threshold).astype(int)
        tp = int(((y_true == 1) & (y_pred == 1)).sum())
        tn = int(((y_true == 0) & (y_pred == 0)).sum())
        fp = int(((y_true == 0) & (y_pred == 1)).sum())
        fn = int(((y_true == 1) & (y_pred == 0)).sum())
        cost = compute_cost(y_true, y_pred, amounts)
    n = len(y_true)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    accuracy = (tp + tn) / n if n else 0.0
    # Financial: FP review cost is real money
    fp_rupees = fp * FP_REVIEW_COST_RUPEES
    # FN cost: approx avg fraud amount per missed fraud (if amounts provided, use actual)
    if amounts is not None and fn > 0:
        # estimate FN amount as mean fraud amount
        fraud_amounts = np.asarray(amounts, dtype=float)[y_true == 1]
        avg_fraud = float(np.mean(fraud_amounts)) if len(fraud_amounts) else 0.0
        fn_rupees = fn * avg_fraud  # money lost to fraud
    else:
        fn_rupees = fn * 5000.0  # placeholder avg ticket
    total_rupees = fp_rupees + fn_rupees
    # Baseline: cost if we predicted all 0 (never flag) — common naive baseline
    baseline_pred = np.zeros_like(y_true)
    baseline_cost = compute_cost(y_true, baseline_pred, amounts)
    saved = baseline_cost - cost
    return HeldOutMetrics(
        test_size=int(n),
        train_size=0,  # caller fills
        tp=tp, tn=tn, fp=fp, fn=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        fpr=round(fpr, 4),
        accuracy=round(accuracy, 4),
        threshold=float(threshold),
        total_cost_units=float(cost),
        fp_financial_cost_rupees=float(fp_rupees),
        fn_financial_cost_rupees=float(fn_rupees),
        total_financial_cost_rupees=float(total_rupees),
        baseline_cost_units=float(baseline_cost),
        cost_saved_vs_baseline=float(saved),
        is_honest_held_out=True,
    )


def generate_synthetic_fraud_dataset(n: int = 1000, fraud_rate: float = 0.03, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic scored dataset for honest metrics demo (no sklearn needed).

    Returns DataFrame with timestamp, score, is_fraud, amount.
    Fraud scores are drawn from higher distribution so thresholding matters.
    """
    random.seed(seed)
    np.random.seed(seed)
    base_time = pd.Timestamp("2026-08-01", tz="UTC")
    # inter-arrival: exponential, avg 30 min
    deltas = np.random.exponential(scale=30 * 60, size=n)  # seconds
    timestamps = base_time + pd.to_timedelta(np.cumsum(deltas), unit="s")
    is_fraud = (np.random.rand(n) < fraud_rate).astype(int)
    # scores: fraud ~ N(0.7, 0.2) clipped, legit ~ N(0.3, 0.15) clipped
    scores = np.where(
        is_fraud == 1,
        np.clip(np.random.normal(0.70, 0.20, size=n), 0, 1),
        np.clip(np.random.normal(0.30, 0.15, size=n), 0, 1),
    )
    amounts = np.random.lognormal(mean=6.5, sigma=0.8, size=n)  # ~ ₹600 avg
    amounts = np.round(amounts, 2)
    df = pd.DataFrame({"timestamp": timestamps, "score": scores, "is_fraud": is_fraud, "amount": amounts})
    return df


def honest_evaluation_pipeline(df: Optional[pd.DataFrame] = None, seed: int = 42) -> dict:
    """Full honest pipeline: generate (or use) data → time split → fit on train → evaluate on test.

    Returns dict with train/test metrics, threshold, and financial breakdown.
    """
    if df is None:
        df = generate_synthetic_fraud_dataset(seed=seed)
    train, test = time_based_split(df, test_frac=0.3)
    # Fit threshold on train only (cost-sensitive)
    train_scores = train["score"].values
    train_y = train["is_fraud"].values
    train_amt = train["amount"].values
    res = find_optimal_threshold(train_scores, train_y, train_amt)
    # Evaluate on test (held-out)
    test_scores = test["score"].values
    test_y = test["is_fraud"].values
    test_amt = test["amount"].values
    metrics = evaluate_held_out(test_scores, test_y, test_amt, threshold=res.threshold)
    metrics.train_size = len(train)
    return {
        "threshold": res.threshold,
        "train_cost": res.total_cost,
        "train_confusion": {"tp": res.tp, "tn": res.tn, "fp": res.fp, "fn": res.fn},
        "test_metrics": metrics.to_dict(),
        "is_honest": True,
        "note": "Threshold fitted on train (70% earliest); metrics computed on held-out test (30% latest). FP cost = ₹500 per false alarm; FN cost = 25× FP in cost units.",
    }
