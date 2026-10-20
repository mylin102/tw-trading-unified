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


# 2026-10-20 Hermes Agent: fixed simulation limits from the approved A/B spec.
AB_SIM_MAX_WAIT_SECS = 300.0
AB_SIM_NEAR_MAX_AGE_MS = 500.0
AB_SIM_FAR_MAX_AGE_MS = 1500.0
AB_SIM_TICK_SIZE = 1.0


def _ab_sim_quote(bar: Mapping[str, Any], leg: str, side: str):
    prefix = "near" if leg == "NEAR" else "far"
    age = bar.get(f"{prefix}_bidask_age_ms")
    state = bar.get(f"{prefix}_entry_bbo_state")
    bid, ask = bar.get(f"{prefix}_bid"), bar.get(f"{prefix}_ask")
    try:
        age = float(age)
        bid, ask = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    max_age = AB_SIM_NEAR_MAX_AGE_MS if leg == "NEAR" else AB_SIM_FAR_MAX_AGE_MS
    if (not math.isfinite(age) or age < 0 or age > max_age
            or not math.isfinite(bid) or not math.isfinite(ask)
            or bid <= 0 or ask < bid or str(state or "").upper() != "OK"):
        return None
    return {"bid": bid, "ask": ask, "age_ms": age, "px": bid if side == "SELL" else ask}


def begin_ab_simulation(*, pair_id: str, leg: str, side: str, decision_ts: Any,
                        adaptive_price: Any, bar: Mapping[str, Any],
                        point_value: float) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Start an in-memory hypothetical limit from a same-decision BBO snapshot."""
    ts = parse_ts(decision_ts)
    if leg not in ("NEAR", "FAR") or side not in ("SELL", "BUY") or ts is None:
        raise ValueError("invalid A/B simulation identity or decision_ts")
    quote = _ab_sim_quote(bar, leg, side)
    if quote is None:
        pending = {"pair_id": pair_id, "leg": leg, "side": side,
                   "decision_ts": ts, "baseline_px": None,
                   "baseline_bbo_age_ms": None, "adaptive_limit": None,
                   "point_value": float(point_value)}
        return None, finish_ab_simulation(pending, "INVALID_BBO", None, ts)
    try:
        adaptive_price = float(adaptive_price)
        point_value = float(point_value)
    except (TypeError, ValueError):
        try:
            point_value = float(point_value)
        except (TypeError, ValueError):
            point_value = 0.0
        return None, finish_ab_simulation(
            {"pair_id": pair_id, "leg": leg, "side": side, "decision_ts": ts,
             "baseline_px": quote["px"], "baseline_bbo_age_ms": quote["age_ms"],
             "adaptive_limit": None, "point_value": float(point_value)},
            "INVALID_BBO", None, ts)
    if not math.isfinite(adaptive_price) or adaptive_price <= 0 or not math.isfinite(point_value):
        return None, finish_ab_simulation(
            {"pair_id": pair_id, "leg": leg, "side": side, "decision_ts": ts,
             "baseline_px": quote["px"], "baseline_bbo_age_ms": quote["age_ms"],
             "adaptive_limit": None, "point_value": point_value},
            "INVALID_BBO", None, ts)
    limit = (math.ceil(adaptive_price / AB_SIM_TICK_SIZE) * AB_SIM_TICK_SIZE
             if side == "SELL" else
             math.floor(adaptive_price / AB_SIM_TICK_SIZE) * AB_SIM_TICK_SIZE)
    pending = {
        "pair_id": str(pair_id), "leg": leg, "side": side,
        "decision_ts": ts, "baseline_px": quote["px"],
        "baseline_bbo_age_ms": quote["age_ms"],
        "adaptive_limit": float(limit), "point_value": point_value,
        "last_seen_ts": ts,
        "last_valid_bbo": {"px": quote["px"], "age_ms": quote["age_ms"], "ts": ts},
    }
    return pending, None


def finish_ab_simulation(pending: Mapping[str, Any], fill_status: str,
                         fill_px: Any, now_ts: Any) -> dict[str, Any]:
    """Build the immutable outcome; only actual simulated exits are compared."""
    ts = parse_ts(now_ts)
    if ts is None:
        ts = float(pending.get("last_seen_ts", pending["decision_ts"]))
    decision_ts = float(pending["decision_ts"])
    wait_ms = max(0.0, (ts - decision_ts) * 1000.0)
    comparable = fill_status in {"FILLED", "TIMEOUT_MARKET", "SESSION_END_MARKET"}
    fill_px = float(fill_px) if comparable and fill_px is not None else None
    diff = None
    if comparable and pending.get("baseline_px") is not None:
        raw = fill_px - float(pending["baseline_px"])
        diff = raw if pending["side"] == "SELL" else -raw
    return {
        "event_type": "AB_SIM_OUTCOME", "pair_id": pending["pair_id"],
        "leg": pending["leg"], "side": pending["side"],
        "decision_ts": decision_ts,
        "baseline_px": pending.get("baseline_px"),
        "baseline_bbo_age_ms": pending.get("baseline_bbo_age_ms"),
        "adaptive_limit": pending.get("adaptive_limit"),
        "adaptive_fill_px": fill_px, "fill_status": fill_status,
        "wait_ms": wait_ms, "diff_points": diff,
        "diff_twd": (diff * float(pending["point_value"]) if diff is not None else None),
    }


def _ab_sim_session_close_between(start_ts: float, end_ts: float) -> bool:
    from datetime import timedelta
    if end_ts < start_ts:
        return False
    start = datetime.fromtimestamp(start_ts, TW)
    end = datetime.fromtimestamp(end_ts, TW)
    day = start.date()
    while day <= end.date():
        for hour, minute in ((5, 0), (13, 45)):
            close = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TW).timestamp()
            if start_ts < close <= end_ts:
                return True
        day += timedelta(days=1)
    return False


def advance_ab_simulation(pending: dict[str, Any], bar: Mapping[str, Any],
                          now_ts: Any) -> dict[str, Any] | None:
    """Advance one callback using only a fresh subsequent BidAsk snapshot."""
    ts = parse_ts(now_ts)
    if ts is None:
        return None
    quote = _ab_sim_quote(bar, pending["leg"], pending["side"])
    if ts <= float(pending["decision_ts"]):
        return None
    previous_ts = float(pending.get("last_seen_ts", pending["decision_ts"]))
    pending["last_seen_ts"] = ts
    if _ab_sim_session_close_between(previous_ts, ts):
        local = datetime.fromtimestamp(ts, TW)
        at_close = (local.hour, local.minute, local.second) in {(5, 0, 0), (13, 45, 0)}
        if quote is not None and at_close:
            pending["last_valid_bbo"] = {"px": quote["px"], "age_ms": quote["age_ms"], "ts": ts}
        last = pending.get("last_valid_bbo")
        return finish_ab_simulation(pending, "SESSION_END_MARKET", last["px"] if last else None, ts)
    if quote is not None:
        pending["last_valid_bbo"] = {"px": quote["px"], "age_ms": quote["age_ms"], "ts": ts}
        touched = (quote["bid"] >= pending["adaptive_limit"] if pending["side"] == "SELL"
                   else quote["ask"] <= pending["adaptive_limit"])
        if touched:
            return finish_ab_simulation(pending, "FILLED", pending["adaptive_limit"], ts)
    elapsed = ts - float(pending["decision_ts"])
    if elapsed >= AB_SIM_MAX_WAIT_SECS:
        if quote is None:
            return finish_ab_simulation(pending, "INVALID_BBO", None, ts)
        return finish_ab_simulation(pending, "TIMEOUT_MARKET", quote["px"], ts)
    return None


def _append_ab_sim_event(event: Mapping[str, Any], directory) -> str:
    import fcntl
    import json
    from pathlib import Path
    ts = parse_ts(event["decision_ts"])
    if ts is None:
        raise ValueError("invalid A/B simulation timestamp")
    path = Path(directory) / f"ab_sim_{datetime.fromtimestamp(ts, TW):%Y%m%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")
        stream.flush()
    return str(path)


def log_ab_sim_pending(pending: Mapping[str, Any], directory) -> str:
    return _append_ab_sim_event({"event_type": "AB_SIM_PENDING", "pending": dict(pending),
                                 "decision_ts": pending["decision_ts"]}, directory)


def log_ab_sim_outcome(outcome: Mapping[str, Any], directory) -> str:
    if outcome.get("event_type") != "AB_SIM_OUTCOME":
        raise ValueError("not an AB_SIM_OUTCOME event")
    return _append_ab_sim_event(outcome, directory)


def recover_ab_simulations(directory) -> list[dict[str, Any]]:
    """On a fresh strategy instance, close only explicitly logged pending simulations."""
    import json
    from pathlib import Path
    root = Path(directory)
    if not root.exists():
        return []
    pending: dict[str, dict[str, Any]] = {}
    completed: set[str] = set()
    for path in sorted(root.glob("ab_sim_*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if event.get("event_type") == "AB_SIM_PENDING":
                item = event.get("pending")
                if isinstance(item, dict) and item.get("pair_id"):
                    pending[str(item["pair_id"])] = item
            elif event.get("event_type") == "AB_SIM_OUTCOME":
                completed.add(str(event.get("pair_id")))
    abandoned = []
    for pair_id, item in pending.items():
        if pair_id not in completed:
            outcome = finish_ab_simulation(item, "ABANDONED_RESTART", None,
                                           item.get("last_seen_ts", item["decision_ts"]))
            log_ab_sim_outcome(outcome, root)
            abandoned.append(outcome)
    return abandoned


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
