"""
experiment.py — Counterfactual Experiment Layer (R-003).

Architecture:
  ParameterGenerator
      ↓ override dicts
  ReplayEngine(fit)
      ↓ per-case results
  MetricsCollector
      ↓ per-level + per-case aggregates
  StatisticalAnalysis
      ↓ Decision Boundary Dataset + Sensitivity Report

Contract with Replay Layer:
  ReplayEngine receives a modified DecisionReplayCase with override parameters applied.
  Experiment Layer never modifies the original case — always produces a copy.

Design principle:
  Experiment Layer does NOT know what parameters mean.
  It only knows: "generate override dict → run replay → collect metrics → analyze".
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from core.replay_contracts import DecisionReplayCase
from core.replay_release import (
    ReplayResult,
    build_release_context,
    reconstruct_lifecycle,
    replay_single_release,
)
from strategies.plugins.futures.active.tmf_spread import evaluate_lifecycle_actions


# ---------------------------------------------------------------------------
# Experiment configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentConfig:
    """Describes one experiment dimension."""
    parameter: str                  # e.g. "release_threshold"
    levels: list[Any]               # e.g. [6, 8, 10, 12, 14, 16, 18, 20]
    label: str = ""
    override_fn: Callable | None = None  # (case, level) → dict; default: {param: level}


def default_override(case: DecisionReplayCase, level: Any) -> dict[str, Any]:
    """Default override: set the parameter directly."""
    return {}


# ---------------------------------------------------------------------------
# Per-experiment result
# ---------------------------------------------------------------------------


@dataclass
class ExperimentResult:
    """Result of one counterfactual experiment run (one case × one level)."""
    experiment_id: str
    case_id: str
    trade_id: str
    parameter: str
    level: Any

    # Comparison with historical
    historical_action: str
    replayed_action: str
    decision_changed: bool
    historical_leg: str | None
    replayed_leg: str | None

    # Historical margin (how close was the original decision to the boundary?)
    historical_margin: float | None  # pnl - threshold at historical decision point
    flip_threshold: float | None     # threshold at which decision would flip

    # PnL impact (estimated from the decision change)
    pnl_delta: float | None

    # Diagnostics
    replayed_threshold: float | None
    exception: str | None = None


# ---------------------------------------------------------------------------
# Parameter generator
# ---------------------------------------------------------------------------


def generate_experiments(
    cases: list[DecisionReplayCase],
    config: ExperimentConfig,
) -> list[tuple[DecisionReplayCase, dict[str, Any]]]:
    """Generate (case, override_dict) pairs for an experiment.
    Returns a list of (modified_case_copy, override_dict) for each case × level.
    """
    experiments: list[tuple[DecisionReplayCase, dict[str, Any]]] = []
    original_thresholds: dict[str, float] = {}

    # Determine the override function
    override_fn = config.override_fn or (lambda case, level: {config.parameter: level})

    for case in cases:
        if not case.recorded_action.startswith("RELEASE"):
            continue
        # Store original threshold for margin computation
        orig = case.release_stop_threshold or 0.0
        original_thresholds[case.replay_case_id] = orig

        for level in config.levels:
            override = override_fn(case, level)
            experiments.append((case, override))

    return experiments


# ---------------------------------------------------------------------------
# Metrics collector
# ---------------------------------------------------------------------------


def _compute_historical_margin(case: DecisionReplayCase) -> float | None:
    """Compute how close the recorded decision was to the threshold boundary.
    Margin = min(near_pnl, far_pnl) - (-threshold), i.e. how far past threshold.
    Positive margin = would have triggered at slightly tighter threshold.
    """
    # Reconstruct the context to get the PnL values
    ctx = build_release_context(case)
    threshold = case.release_stop_threshold or 0.0

    # The release check: near_pnl_pts <= -threshold or far_pnl_pts <= -threshold
    margin_near = -abs(ctx.near_pnl_pts) - threshold if ctx.near_pnl_pts != 0 else None
    margin_far = -abs(ctx.far_pnl_pts) - threshold if ctx.far_pnl_pts != 0 else None

    # Which leg was released?
    if case.recorded_release_leg == "NEAR" and margin_near is not None:
        return margin_near
    elif case.recorded_release_leg == "FAR" and margin_far is not None:
        return margin_far
    elif margin_near is not None and margin_far is not None:
        return max(margin_near, margin_far)  # whichever was closer to threshold
    return None


def _find_flip_threshold(case: DecisionReplayCase) -> float | None:
    """Find the critical threshold at which the decision would flip.
    Replays the case with tighter/looser thresholds to find the flip point.
    Returns the threshold value, or None if decision never flips.
    """
    ctx = build_release_context(case)

    # Compute which leg PnL triggered the release
    threshold = case.release_stop_threshold or 0.0
    near_pnl = abs(ctx.near_pnl_pts) if ctx.near_pnl_pts != 0 else 0
    far_pnl = abs(ctx.far_pnl_pts) if ctx.far_pnl_pts != 0 else 0

    # The release check: pnl_pts <= -threshold
    # So flip threshold = abs(pnl_pts) + epsilon
    if case.recorded_release_leg == "NEAR" and near_pnl > 0:
        return round(near_pnl + 0.1, 2)
    elif case.recorded_release_leg == "FAR" and far_pnl > 0:
        return round(far_pnl + 0.1, 2)
    elif near_pnl > 0 or far_pnl > 0:
        # The leg with larger absolute PnL is the one that triggered
        return round(max(near_pnl, far_pnl) + 0.1, 2)

    return None


# ---------------------------------------------------------------------------
# Override injection
# ---------------------------------------------------------------------------


def _apply_override(case: DecisionReplayCase, override: dict[str, Any]) -> DecisionReplayCase:
    """Create a copy of case with override parameters applied.
    Does NOT modify the original case object.
    """
    new_kwargs = {}
    for k, v in case.__dict__.items():
        new_kwargs[k] = copy.deepcopy(v)

    # Apply overrides
    for param, value in override.items():
        if param == "release_threshold":
            new_kwargs["release_stop_threshold"] = value
        elif param in new_kwargs:
            new_kwargs[param] = value

    return DecisionReplayCase(**new_kwargs)


# ---------------------------------------------------------------------------
# Run single experiment cell
# ---------------------------------------------------------------------------


def run_cell(
    case: DecisionReplayCase,
    override: dict[str, Any],
    experiment_id: str,
    parameter: str,
    level: Any,
) -> ExperimentResult:
    """Run one counterfactual experiment cell: one case × one parameter level."""
    # Create modified case
    modified_case = _apply_override(case, override)

    # Run replay
    result = replay_single_release(modified_case)

    # Compute historical margin
    historical_margin = _compute_historical_margin(case)

    # Compute decision flip
    decision_changed = result.replayed_action != "RELEASE"
    replayed_leg = result.replayed_release_leg
    leg_match = (replayed_leg or "").upper() == (case.recorded_release_leg or "").upper() if result.replayed_action == "RELEASE" else False

    # Flip threshold
    flip_threshold = None
    if decision_changed:
        flip_threshold = _find_flip_threshold(case)

    return ExperimentResult(
        experiment_id=experiment_id,
        case_id=case.replay_case_id,
        trade_id=case.trade_id,
        parameter=parameter,
        level=level,
        historical_action=case.recorded_action,
        replayed_action=result.replayed_action or "NONE",
        decision_changed=decision_changed,
        historical_leg=case.recorded_release_leg,
        replayed_leg=replayed_leg,
        historical_margin=historical_margin,
        flip_threshold=flip_threshold,
        pnl_delta=None,   # Requires outcome tracking — deferred
        replayed_threshold=modified_case.release_stop_threshold,
        exception=result.exception_type,
    )


# ---------------------------------------------------------------------------
# Batch experiment
# ---------------------------------------------------------------------------


def run_experiment(
    cases: list[DecisionReplayCase],
    config: ExperimentConfig,
    experiment_id: str,
) -> list[ExperimentResult]:
    """Run a full experiment: all cases × all levels for one parameter."""
    experiments = generate_experiments(cases, config)
    results: list[ExperimentResult] = []

    total = len(experiments)
    print(f"Experiment {experiment_id}: {len(config.levels)} levels × {len([c for c in cases if c.recorded_action.startswith('RELEASE')])} cases = {total} cells")

    for i, (case, override) in enumerate(experiments):
        result = run_cell(case, override, experiment_id, config.parameter, config.levels[i % len(config.levels)])
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_experiment(results: list[ExperimentResult]) -> dict[str, Any]:
    """Compute per-level and overall analysis from experiment results."""
    df = pd.DataFrame([r.__dict__ for r in results])
    if df.empty:
        return {}

    analysis: dict[str, Any] = {}

    # Per-level aggregates
    level_groups = df.groupby("level")
    level_stats = []
    for level, grp in sorted(level_groups):
        total = len(grp)
        changed = grp["decision_changed"].sum()
        level_stats.append({
            "level": level,
            "total_cases": total,
            "decision_changed": int(changed),
            "decision_stability": round(1 - changed / total, 4) if total > 0 else 1.0,
            "decision_change_rate": round(changed / total, 4) if total > 0 else 0.0,
            "historical_margin_mean": round(grp["historical_margin"].mean(), 2),
            "historical_margin_median": round(grp["historical_margin"].median(), 2),
            "flip_count": int(grp["flip_threshold"].notna().sum()),
        })

    analysis["per_level"] = level_stats

    # Decision Boundary Dataset: per-case flip threshold
    flip_cases = df[df["decision_changed"]].copy()
    boundary_cases = flip_cases.groupby("case_id").agg({
        "flip_threshold": "min",
        "trade_id": "first",
        "historical_margin": "first",
        "level": "min",
    }).reset_index()
    boundary_cases.columns = ["case_id", "flip_threshold", "trade_id", "historical_margin", "first_flip_level"]
    analysis["decision_boundary"] = boundary_cases.to_dict("records")

    # Four-dimensional analysis
    analysis["dimensions"] = {
        "decision_stability": {
            "description": "How stable is the decision across parameter changes?",
            "overall_stability": round(1 - df["decision_changed"].mean(), 4) if len(df) > 0 else 1.0,
            "total_flips": int(df["decision_changed"].sum()),
            "total_cells": len(df),
        },
        "pnl_sensitivity": {
            "description": "How much does PnL change? (Requires outcome tracking — partial)",
            "note": "PnL tracking requires outcome data integration (Phase 3C+)",
        },
        "boundary_distance": {
            "description": "How close are decisions to the flip boundary?",
            "cases_with_boundary": int(df["flip_threshold"].notna().sum()),
            "cases_without_boundary": int(df["flip_threshold"].isna().sum()),
        },
        "case_clustering": {
            "description": "Which cases are most/least sensitive?",
            "sensitive_cases": int(df[df["decision_changed"]]["case_id"].nunique()),
            "stable_cases": len(set(df["case_id"].unique()) - set(df[df["decision_changed"]]["case_id"].unique())),
        },
    }

    # Most/least sensitive cases
    case_sensitivity = df.groupby("case_id").agg({
        "decision_changed": "sum",
        "trade_id": "first",
        "historical_margin": "first",
    }).reset_index()
    case_sensitivity.columns = ["case_id", "flip_count", "trade_id", "historical_margin"]
    case_sensitivity = case_sensitivity.sort_values("flip_count", ascending=False)

    analysis["most_sensitive_cases"] = case_sensitivity.head(5).to_dict("records")
    analysis["most_stable_cases"] = case_sensitivity[case_sensitivity["flip_count"] == 0].head(5).to_dict("records")

    return analysis


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def build_report(
    results: list[ExperimentResult],
    config: ExperimentConfig,
    analysis: dict[str, Any],
    experiment_id: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build structured experiment report."""
    df = pd.DataFrame([r.__dict__ for r in results])

    return {
        "experiment_id": experiment_id,
        "parameter": config.parameter,
        "levels": config.levels,
        "total_cells": len(results),
        "total_cases": df["case_id"].nunique() if not df.empty else 0,
        "analysis": analysis,
        "metadata": metadata,
    }


def save_experiment_results(
    results: list[ExperimentResult],
    analysis: dict[str, Any],
    report: dict[str, Any],
    output_dir: Path,
):
    """Save experiment artifacts to disk."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Raw results
    parameter = report.get("parameter", "?")
    levels = report.get("levels", [])

    df = pd.DataFrame([r.__dict__ for r in results])
    df.to_parquet(output_dir / "experiment_results.parquet", compression="snappy", index=False)
    print(f"Saved: {output_dir / 'experiment_results.parquet'} ({len(df)} rows)")

    # Decision boundary dataset
    boundary = analysis.get("decision_boundary", [])
    if boundary:
        df_b = pd.DataFrame(boundary)
        df_b.to_parquet(output_dir / "decision_boundary.parquet", compression="snappy", index=False)
        print(f"Saved: {output_dir / 'decision_boundary.parquet'} ({len(df_b)} rows)")

    # Per-level summary
    per_level = analysis.get("per_level", [])
    if per_level:
        df_pl = pd.DataFrame(per_level)
        df_pl.to_parquet(output_dir / "per_level_summary.parquet", compression="snappy", index=False)
        print(f"Saved: {output_dir / 'per_level_summary.parquet'} ({len(df_pl)} rows)")

    # Full report
    with open(output_dir / "experiment_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"Saved: {output_dir / 'experiment_report.json'}")

    # Sensitivity summary text
    _write_summary_txt(analysis, parameter, levels, output_dir)


def _write_summary_txt(analysis: dict[str, Any], parameter: str, levels: list, output_dir: Path):
    """Write human-readable sensitivity summary."""
    lines = []
    lines.append(f"Experiment: {parameter} Sensitivity Analysis")
    lines.append(f"Levels: {levels}")
    lines.append("-" * 60)

    dims = analysis.get("dimensions", {})
    ds = dims.get("decision_stability", {})
    lines.append(f"\nDecision Stability: {ds.get('overall_stability', '?')*100:.1f}%")
    lines.append(f"  Total flips: {ds.get('total_flips', '?')} / {ds.get('total_cells', '?')} cells")

    bd = dims.get("boundary_distance", {})
    lines.append(f"\nBoundary Distance:")
    lines.append(f"  Cases with known flip threshold: {bd.get('cases_with_boundary', '?')}")
    lines.append(f"  Cases without flips: {bd.get('cases_without_boundary', '?')}")

    cc = dims.get("case_clustering", {})
    lines.append(f"\nCase Clustering:")
    lines.append(f"  Sensitive cases (flipped at least once): {cc.get('sensitive_cases', '?')}")
    lines.append(f"  Stable cases (never flipped): {cc.get('stable_cases', '?')}")

    lines.append(f"\nPer-level summary:")
    for ps in analysis.get("per_level", []):
        lines.append(f"  level={ps['level']!s:6s}: change_rate={ps['decision_change_rate']*100:5.1f}%  stability={ps['decision_stability']*100:5.1f}%  flips={ps['flip_count']}")

    sensitive = analysis.get("most_sensitive_cases", [])
    if sensitive:
        lines.append(f"\nMost sensitive cases (flipped at most levels):")
        for s in sensitive:
            lines.append(f"  {s['trade_id'][:30]:30s} flips={s['flip_count']} margin={s.get('historical_margin','?')}")

    stable = analysis.get("most_stable_cases", [])
    if stable:
        lines.append(f"\nMost stable cases (never flipped):")
        for s in stable:
            lines.append(f"  {s['trade_id'][:30]:30s} margin={s.get('historical_margin','?')}")

    content = "\n".join(lines)
    with open(output_dir / "sensitivity_summary.txt", "w") as f:
        f.write(content)
    print(content)
