"""Cost-Sensitive Detection — beyond single-transaction probability.

Standard models (XGBoost, Isolation Forest) score each transaction in isolation.
This module wraps *any* scorer with:

  1. Real cost function — FN (missed fraud) = 25 × FP (false alarm). Threshold
     is chosen to minimize expected financial cost, not accuracy.
  2. Rolling time windows — transactions are aggregated into windows (default
     1 hour). Risk is evaluated on window-level fraud rates, not per-row.
  3. Baseline spike detection — flag only when window rate exceeds its
     historical baseline (mean + k*std). Isolated anomalies that don't spike
     the window rate are not flagged, cutting FP cost.

Defense-only: this never takes offensive actions; it only produces signals
for the deterministic policy engine (app.policy).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

# Cost weights — business truth: missing fraud costs far more than a false alarm.
FN_COST = 25.0  # missed fraud
FP_COST = 1.0   # false alarm (review cost, friction)
# Financial: each FP also has a rupee review cost for honest metrics.
FP_REVIEW_COST_RUPEES = 500.0  # cost of manual review per FP


@dataclass
class ThresholdResult:
    threshold: float
    total_cost: float
    fn: int
    fp: int
    tp: int
    tn: int


def compute_cost(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    amounts: Optional[np.ndarray] = None,
    fn_cost: float = FN_COST,
    fp_cost: float = FP_COST,
) -> float:
    """Cost = fn_cost * FN + fp_cost * FP, optionally amount-weighted for FN."""
    y_true = np.asarray(y_true, dtype=int)
    y_pred = np.asarray(y_pred, dtype=int)
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    if amounts is not None:
        amounts = np.asarray(amounts, dtype=float)
        # FN cost scales with transaction amount (missing large fraud hurts more)
        fn_mask = (y_true == 1) & (y_pred == 0)
        # normalize by mean amount so cost stays comparable; weight FN by amount
        if fn_mask.any() and len(amounts) == len(y_true):
            mean_amt = float(np.mean(amounts[amounts > 0])) if (amounts > 0).any() else 1.0
            # amount-weighted FN cost: base 25x + amount factor
            fn_cost_weighted = float(np.sum(amounts[fn_mask]) / mean_amt) * fn_cost / max(fn, 1) if fn else 0
            # fallback to count-based if amounts weird
            if not np.isfinite(fn_cost_weighted):
                fn_cost_weighted = fn_cost
            return fn_cost_weighted * fn + fp_cost * fp
    return fn_cost * fn + fp_cost * fp


def find_optimal_threshold(
    scores: np.ndarray,
    y_true: np.ndarray,
    amounts: Optional[np.ndarray] = None,
    fn_cost: float = FN_COST,
    fp_cost: float = FP_COST,
) -> ThresholdResult:
    """Search thresholds to minimize cost. Returns best threshold + confusion."""
    scores = np.asarray(scores, dtype=float)
    y_true = np.asarray(y_true, dtype=int)
    # candidate thresholds: unique scores + midpoints
    uniq = np.unique(scores)
    # add slightly below min and above max
    candidates = np.concatenate([[uniq.min() - 1e-6], (uniq[:-1] + uniq[1:]) / 2, [uniq.max() + 1e-6]])
    best = None
    best_cost = float("inf")
    for thr in candidates:
        y_pred = (scores >= thr).astype(int)
        cost = compute_cost(y_true, y_pred, amounts, fn_cost, fp_cost)
        if cost < best_cost:
            best_cost = cost
            # confusion
            fn = int(((y_true == 1) & (y_pred == 0)).sum())
            fp = int(((y_true == 0) & (y_pred == 1)).sum())
            tp = int(((y_true == 1) & (y_pred == 1)).sum())
            tn = int(((y_true == 0) & (y_pred == 0)).sum())
            best = ThresholdResult(threshold=float(thr), total_cost=float(cost), fn=fn, fp=fp, tp=tp, tn=tn)
    assert best is not None
    return best


def rolling_window_features(
    df: pd.DataFrame,
    time_col: str = "timestamp",
    window: str = "1h",
    min_periods: int = 1,
) -> pd.DataFrame:
    """Aggregate transactions into rolling windows.

    Expects df with `time_col` (datetime) and optionally `is_fraud` / `amount` / `score`.
    Returns DataFrame indexed by window start with: count, fraud_count, fraud_rate,
    amount_sum, avg_score, fraud_amount_sum.
    """
    if df.empty:
        return pd.DataFrame(columns=["window_start", "count", "fraud_count", "fraud_rate", "amount_sum", "avg_score"])
    d = df.copy()
    d[time_col] = pd.to_datetime(d[time_col], errors="coerce")
    d = d.dropna(subset=[time_col]).sort_values(time_col)
    # floor to window
    d["window_start"] = d[time_col].dt.floor(window)
    agg = d.groupby("window_start").agg(
        count=("window_start", "size"),
        fraud_count=("is_fraud", "sum") if "is_fraud" in d.columns else ("window_start", "size"),
        fraud_rate=("is_fraud", "mean") if "is_fraud" in d.columns else ("window_start", "mean"),
        amount_sum=("amount", "sum") if "amount" in d.columns else ("window_start", "size"),
        avg_score=("score", "mean") if "score" in d.columns else ("window_start", "mean"),
        fraud_amount_sum=("amount", "sum") if "amount" in d.columns else ("window_start", "size"),
    )
    # clean up: if is_fraud not present, fraud_rate defaults to 1; fix
    if "is_fraud" not in df.columns:
        agg["fraud_count"] = 0
        agg["fraud_rate"] = 0.0
        agg["fraud_amount_sum"] = 0.0
    agg = agg.reset_index().sort_values("window_start")
    return agg


@dataclass
class BaselineModel:
    mean: float
    std: float
    count: int
    threshold: float  # mean + k*std

    def to_dict(self):
        return {"mean": self.mean, "std": self.std, "count": self.count, "threshold": self.threshold}


def compute_baseline(
    windows: pd.DataFrame,
    rate_col: str = "fraud_rate",
    k: float = 2.0,
    min_windows: int = 10,
) -> BaselineModel:
    """Establish historical baseline from windows. Uses stable period (first 70% or min)."""
    if windows.empty or rate_col not in windows.columns:
        return BaselineModel(mean=0.0, std=0.0, count=0, threshold=0.0)
    rates = windows[rate_col].dropna().astype(float)
    if len(rates) < 2:
        m = float(rates.mean()) if len(rates) else 0.0
        return BaselineModel(mean=m, std=0.0, count=len(rates), threshold=m)
    # Use expanding baseline: all windows except most recent 1 for training, or split
    # For held-out honesty, caller should pass historical windows only.
    mean = float(rates.mean())
    std = float(rates.std(ddof=1))
    thr = mean + k * std
    # also cap threshold away from 0 when baseline is near-zero (cold start)
    # ensures we don't flag single frauds as spikes when history is clean
    if mean < 0.01 and std < 0.01:
        thr = max(thr, 0.02)  # at least 2% fraud rate to be a spike
    return BaselineModel(mean=mean, std=std, count=len(rates), threshold=float(thr))


def detect_spikes(
    windows: pd.DataFrame,
    baseline: BaselineModel,
    rate_col: str = "fraud_rate",
) -> pd.DataFrame:
    """Flag windows where rate > baseline.threshold. Returns windows with `is_spike`."""
    if windows.empty:
        windows = windows.copy()
        windows["is_spike"] = False
        return windows
    w = windows.copy()
    w["baseline_mean"] = baseline.mean
    w["baseline_std"] = baseline.std
    w["baseline_threshold"] = baseline.threshold
    w["is_spike"] = w[rate_col].astype(float) > baseline.threshold
    # spike severity (how many std above mean)
    denom = baseline.std if baseline.std > 1e-9 else 1.0
    w["spike_z"] = (w[rate_col].astype(float) - baseline.mean) / denom
    return w


class CostSensitiveDetector:
    """Stateful detector: fit threshold on labeled data, then detect spikes on streams.

    Usage:
      det = CostSensitiveDetector(window="1h", k=2.0)
      det.fit(scores, y_true, amounts)  # learns cost-optimal threshold
      result = det.evaluate_stream(transactions_df)  # rolling + spike detection
    """

    def __init__(self, window: str = "1h", k: float = 2.0, fn_cost: float = FN_COST, fp_cost: float = FP_COST):
        self.window = window
        self.k = k
        self.fn_cost = fn_cost
        self.fp_cost = fp_cost
        self.threshold: Optional[float] = None
        self.baseline: Optional[BaselineModel] = None
        self.fit_result: Optional[ThresholdResult] = None

    def fit(self, scores: np.ndarray, y_true: np.ndarray, amounts: Optional[np.ndarray] = None) -> ThresholdResult:
        res = find_optimal_threshold(scores, y_true, amounts, self.fn_cost, self.fp_cost)
        self.threshold = res.threshold
        self.fit_result = res
        return res

    def predict(self, scores: np.ndarray) -> np.ndarray:
        if self.threshold is None:
            raise RuntimeError("Detector not fitted — call fit() first")
        return (np.asarray(scores, dtype=float) >= self.threshold).astype(int)

    def evaluate_stream(self, df: pd.DataFrame, time_col: str = "timestamp", score_col: str = "score") -> dict:
        """End-to-end: score -> prediction -> windowing -> baseline -> spikes.

        df must have time_col, score_col, and optionally is_fraud, amount.
        Returns dict with windows, baseline, spike_windows, cost metrics.
        """
        if self.threshold is None:
            raise RuntimeError("Detector not fitted")
        d = df.copy()
        if score_col not in d.columns:
            raise ValueError(f"Missing score column {score_col!r}")
        d["pred"] = self.predict(d[score_col].values)
        # Use pred as is_fraud proxy for windowing if true label not present
        # For spike detection we care about pred rate, not true rate
        d["is_fraud_pred"] = d["pred"]
        # Build windows on pred rate
        tmp = d.rename(columns={"is_fraud_pred": "is_fraud"}) if "is_fraud_pred" in d.columns else d
        windows = rolling_window_features(tmp, time_col=time_col, window=self.window)
        # Baseline from first 70% of windows (historical), evaluate last 30% for spikes
        if len(windows) >= 4:
            split = max(2, int(len(windows) * 0.7))
            hist = windows.iloc[:split]
            recent = windows.iloc[split:]
            baseline = compute_baseline(hist, k=self.k)
            flagged = detect_spikes(recent, baseline)
            self.baseline = baseline
            # combine for return
            all_flagged = detect_spikes(windows, baseline)
        else:
            baseline = compute_baseline(windows, k=self.k)
            self.baseline = baseline
            flagged = detect_spikes(windows, baseline)
            all_flagged = flagged
        spike_count = int(all_flagged["is_spike"].sum()) if "is_spike" in all_flagged.columns else 0
        return {
            "threshold": self.threshold,
            "baseline": baseline.to_dict() if baseline else None,
            "windows": windows.to_dict(orient="records"),
            "spike_windows": all_flagged[all_flagged["is_spike"]].to_dict(orient="records") if "is_spike" in all_flagged.columns else [],
            "spike_count": spike_count,
            "total_windows": len(windows),
            "fit_result": {"threshold": self.fit_result.threshold, "cost": self.fit_result.total_cost, "fn": self.fit_result.fn, "fp": self.fit_result.fp} if self.fit_result else None,
        }

    def to_dict(self):
        return {
            "window": self.window,
            "k": self.k,
            "fn_cost": self.fn_cost,
            "fp_cost": self.fp_cost,
            "threshold": self.threshold,
            "baseline": self.baseline.to_dict() if self.baseline else None,
        }
