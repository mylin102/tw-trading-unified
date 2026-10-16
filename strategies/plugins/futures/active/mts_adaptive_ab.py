"""2026-10-15 Hermes Agent: paired shadow candidates and offline net-outcome comparison."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import math
import statistics
from itertools import groupby
from typing import Any, Mapping, Sequence

from strategies.plugins.futures.active.mts_adaptive_vwap import (
    AdaptiveVWAPConfig, TW, _number, parse_ts,
)


def evaluate_hvwap_ab(*, baseline=None, config=None, **inputs) -> dict[str, Any]:
    """Evaluate the same immutable inputs; neither arm emits orders or writes logs."""
    from strategies.plugins.futures.active.mts_hvwap_candidate import evaluate_hvwap_candidate, add_adaptive_observation
    cfg = config or AdaptiveVWAPConfig()
    if baseline is None:
        baseline = evaluate_hvwap_candidate(**inputs, use_adaptive=False, adaptive_config=cfg)
    # 2026-10-16 Hermes Agent: the observation arm shares all unchanged voters.
    adaptive = add_adaptive_observation(baseline, near_bars=inputs["near_bars"],
                                        oi_snapshot=inputs.get("oi_snapshot"), config=cfg,
                                        decision_ts=inputs["decision_ts"])
    return {"baseline": baseline, "adaptive": adaptive}


def paired_record(pair_id: str, arms: Mapping[str, Any], config=None) -> dict[str, Any]:
    """One record contains both arms, so observations cannot silently become unpaired."""
    cfg = config or AdaptiveVWAPConfig()
    baseline, adaptive = arms["baseline"], arms["adaptive"]
    # 2026-10-16 Hermes Agent: distinguish hypothetical candidate eligibility
    # from actual execution; Phase 1 observers never invent exit fills.
    def decision(verdict):
        eligible = (verdict.status.value == "ALIGNED_PASS" and verdict.position_phase == "SPREAD"
                    and verdict.hypothetical_release_leg in ("NEAR", "FAR"))
        leg = verdict.near if verdict.hypothetical_release_leg == "NEAR" else verdict.far
        return {**verdict.to_dict(), "candidate_release_eligible": eligible,
                "hypothetical_exit_price": leg.price if eligible else None,
                "exit_executed": False, "exit_price": None}
    return {
        "schema_version": 1, "pair_id": pair_id,
        "decision_ts": baseline.decision_ts,
        "baseline": decision(baseline), "adaptive": decision(adaptive),
        "adaptive_data_valid": adaptive.adaptive_price is not None,
        "config": asdict(cfg), "execution_mode": "SHADOW_ONLY",
        "release_action_emitted": False,
    }


def log_ab_pair(pair: Mapping[str, Any], directory) -> str:
    """2026-10-16 Hermes Agent: one locked, mode-scoped JSONL contains both arms."""
    import fcntl
    import json
    from pathlib import Path
    ts = parse_ts(pair["decision_ts"])
    if ts is None:
        raise ValueError("invalid pair decision timestamp")
    day = datetime.fromtimestamp(ts, TW).strftime("%Y%m%d")
    data = json.dumps(pair, allow_nan=False, separators=(",", ":")) + "\n"
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"paired_{day}.jsonl"
    with path.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(data)
        stream.flush()
    return str(path)


def _metrics(rows, arm: str, initial_equity: float) -> dict[str, Any]:
    equity, peak, mdd = initial_equity, initial_equity, 0.0
    wins = 0
    daily_pnl: dict[str, float] = {}
    # 2026-10-16 Hermes Agent: simultaneous closes share one equity observation,
    # so pair-id ordering cannot manufacture a drawdown.
    for ts, batch in groupby(rows, key=lambda row: row["ts"]):
        group = list(batch)
        pnl = sum(row[arm] for row in group)
        wins += sum(row[arm] > 0 for row in group)
        equity += pnl
        if not math.isfinite(equity):
            raise ValueError("non-finite outcome equity")
        peak = max(peak, equity)
        # 2026-10-16 Hermes Agent: reject finite inputs whose derived ratios overflow.
        drawdown = (equity - peak) / peak
        if not math.isfinite(drawdown):
            raise ValueError("non-finite drawdown")
        mdd = min(mdd, drawdown)
        day = datetime.fromtimestamp(ts, TW).date().isoformat()
        daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl
        if not math.isfinite(daily_pnl[day]):
            raise ValueError("non-finite daily PnL")
    daily_equity = initial_equity
    daily_returns = []
    for pnl in daily_pnl.values():
        if daily_equity <= 0:
            daily_returns = []
            break
        daily_return = pnl / daily_equity
        if not math.isfinite(daily_return):
            raise ValueError("non-finite daily return")
        daily_returns.append(daily_return)
        daily_equity += pnl
    sharpe = None
    if len(daily_returns) >= 2:
        deviation = statistics.stdev(daily_returns)
        if not math.isclose(deviation, 0.0, abs_tol=1e-12):
            sharpe = statistics.mean(daily_returns) / deviation * math.sqrt(252)
    return {"trade_count": len(rows), "win_rate_pct": wins / len(rows) * 100,
            "max_drawdown_pct": mdd * 100, "sharpe": sharpe,
            "final_equity": equity, "net_pnl": equity - initial_equity,
            "daily_observations": len(daily_pnl),
            # 2026-10-16 Hermes Agent: weekend clock dates cannot satisfy the
            # five-trading-date gate; official holiday/session provenance remains external.
            "trading_date_observations": sum(datetime.fromisoformat(day).weekday() < 5 for day in daily_pnl)}


def compare_outcomes(pairs: Sequence[Mapping[str, Any]], *, initial_equity: float,
                     config=None) -> dict[str, Any]:
    """Compare supplied cost-inclusive net PnLs, never infer profit from price distance.

    Each row: {pair_id, ts, baseline: {net_pnl}, adaptive: {net_pnl}}.
    MDD is measured on the chronological closed-trade equity path. Sharpe is
    annualized from observed daily net returns (252); zero variance is unknown.
    """
    cfg = config or AdaptiveVWAPConfig()
    capital = _number(initial_equity)
    if capital is None or capital <= 0:
        raise ValueError("initial_equity must be finite and positive")
    rows, seen = [], set()
    for pair in pairs:
        try:
            pair_id = pair["pair_id"]
            ts = parse_ts(pair["ts"])
            a, b = _number(pair["baseline"]["net_pnl"]), _number(pair["adaptive"]["net_pnl"])
        except (KeyError, TypeError) as exc:
            raise ValueError("outcomes must be paired cost-inclusive net_pnl records") from exc
        if not isinstance(pair_id, str) or not pair_id or pair_id in seen or ts is None or a is None or b is None:
            raise ValueError("invalid or duplicate outcome pair")
        try:
            datetime.fromtimestamp(ts, TW)
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("outcome timestamp out of range") from exc
        seen.add(pair_id)
        rows.append({"pair_id": pair_id, "ts": ts, "baseline": a, "adaptive": b})
    rows.sort(key=lambda row: (row["ts"], row["pair_id"]))
    if not rows:
        return {"status": "INSUFFICIENT_DATA", "promotion": "NO_GO", "paired_count": 0,
                "baseline": None, "adaptive": None, "observation_days": 0.0}
    a, b = _metrics(rows, "baseline", capital), _metrics(rows, "adaptive", capital)
    span = (rows[-1]["ts"] - rows[0]["ts"]) / 86400
    win_gain = b["win_rate_pct"] - a["win_rate_pct"]
    base_dd, adaptive_dd = abs(a["max_drawdown_pct"]), abs(b["max_drawdown_pct"])
    dd_reduction = (base_dd - adaptive_dd) / base_dd * 100 if base_dd > 0 else None
    # 2026-10-16 Hermes Agent: paired observations on five distinct dates.
    sufficient = a["trading_date_observations"] >= cfg.minimum_trading_days
    go = (sufficient and win_gain >= cfg.minimum_win_rate_gain_pp
          and dd_reduction is not None and dd_reduction >= cfg.minimum_mdd_reduction_pct)
    return {"status": "EVALUATED" if sufficient else "INSUFFICIENT_DATA",
            "promotion": "GO_CANDIDATE" if go else "NO_GO", "paired_count": len(rows),
            "observation_days": span, "baseline": a, "adaptive": b,
            "win_rate_gain_pp": win_gain, "mdd_reduction_pct": dd_reduction,
            "sharpe_gain": (b["sharpe"] - a["sharpe"]
                            if a["sharpe"] is not None and b["sharpe"] is not None else None),
            "thresholds": {"minimum_trading_days": cfg.minimum_trading_days,
                           "minimum_win_rate_gain_pp": cfg.minimum_win_rate_gain_pp,
                           "minimum_mdd_reduction_pct": cfg.minimum_mdd_reduction_pct}}


def main(argv=None) -> int:
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Paired net-outcome JSONL")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--initial-equity", required=True, type=float)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    cfg = AdaptiveVWAPConfig.load(args.config) if args.config else AdaptiveVWAPConfig()
    with args.input.open() as stream:
        pairs = [json.loads(line) for line in stream if line.strip()]
    report = compare_outcomes(pairs, initial_equity=args.initial_equity, config=cfg)
    # 2026-10-16 Hermes Agent: create report directories after successful comparison.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":  # 2026-10-15 Hermes Agent: macOS E-Core policy for offline loops.
    import os
    import sys
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    raise SystemExit(main())
