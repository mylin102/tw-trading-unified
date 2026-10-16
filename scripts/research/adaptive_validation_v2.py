"""2026-10-16 Hermes Agent: causal calibration and paired MTS bar-proxy lifecycle study.

This is an offline counterfactual inventory model, not broker/tick reconstruction.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import socket
import statistics
import subprocess

import yaml

from core.deployment_role_gate import assert_research_allowed
from scripts.research.replay_adaptive_vwap import load_completed_bars
from scripts.research.run_adaptive_ab_backtest import audit_source, sha256
from strategies.plugins.futures.active.mts_adaptive_vwap import AdaptiveVWAP, AdaptiveVWAPConfig, TW
from strategies.plugins.futures.active.mts_hvwap_candidate import vwap_session_bounds
from strategies.plugins.futures.active.mts_vol_adjuster import VolAdjuster, ZSCORE_PARAMS
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    LifecycleAction, LifecycleContext, PositionLifecycle, PositionPhase, ReleaseGroup,
    ReleaseGroupStatus, Side, TrailGroup, TrailGroupStatus, counter_trend_leg_from_sides,
    evaluate_lifecycle_actions,
)
from strategies.plugins.futures.active.mts_renko_signal import compute_renko, renko_signal_state
from strategies.plugins.futures.active.mts_trend_signal_adapter import (
    TrendDirection, SubSignalState, adl_signal_state, arbitrate_trend, compute_adl_snr,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL_NAME = "OFFLINE_MTS_LIFECYCLE_BAR_PROXY_V2"


@dataclass(frozen=True)
class ValidationConfig:
    minimum_distance_fraction: float = .001
    minimum_alpha: float = .3
    confirmation_bars: int = 2
    volume_minimum_history: int = 30
    volume_history_limit: int = 120
    oi_intensity_assumed: float = 1.5
    oi_direction_assumed: float = .65
    maximum_hold_secs: float = 3600
    max_spread_loss_points: float = 200
    initial_equity_twd: float = 100000
    minimum_decision_difference_pct: float = 5
    mode: str = MODEL_NAME

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "mode":
                continue
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid validation config: {name}")
        for name in ("confirmation_bars", "volume_minimum_history", "volume_history_limit"):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be integer")
        if (self.confirmation_bars < 2 or self.volume_minimum_history < 2
                or self.volume_history_limit < self.volume_minimum_history
                or self.minimum_alpha > 1 or self.oi_direction_assumed > 1 or self.mode != MODEL_NAME):
            raise ValueError("invalid validation config bounds")


class CausalVolume:
    """2026-10-16 Hermes Agent: score current bar from strictly earlier same-leg/segment bars."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.history = defaultdict(lambda: deque(maxlen=cfg.volume_history_limit))
        self.last_ts = {}

    def observe(self, leg, ts, volume):
        segment = VolAdjuster.get_time_segment(ts) or "off_segment"
        history = self.history[leg, segment]
        n = len(history)
        mean = statistics.mean(history) if n else None
        std = statistics.stdev(history) if n >= 2 else None
        valid = n >= self.cfg.volume_minimum_history and std is not None and std > 0
        z = (volume - mean) / std if valid else None
        result = dict(segment=segment, history_count=n, mean=mean, std=std,
                      max_calibration_ts=self.last_ts.get((leg, segment)),
                      zscore=z, quality=VolAdjuster.compute_quality(z) if valid else None,
                      anomaly=VolAdjuster.classify_anomaly(z) if valid else "UNKNOWN",
                      source="PRIOR_ONLY_SAME_LEG_SEGMENT_ROLLING_5M_VOLUME")
        history.append(volume)
        self.last_ts[leg, segment] = ts
        return result


def volume_diagnosis(near, far, causal):
    grouped = defaultdict(list)
    for leg, bars in (("near", near), ("far", far)):
        for ts, bar in bars.items():
            grouped[leg, VolAdjuster.get_time_segment(ts) or "off_segment"].append(bar["volume"])
    stats = []
    for (leg, segment), values in sorted(grouped.items()):
        old = ZSCORE_PARAMS.get(segment)
        old_z = [(v-old["mean"])/old["std"] for v in values] if old else []
        rolling = [item[leg]["volume_model"]["zscore"] for item in causal.values()
                   if leg in item and item[leg]["volume_model"]["segment"] == segment
                   and item[leg]["volume_model"]["zscore"] is not None]
        stats.append(dict(leg=leg, segment=segment, count=len(values), mean=statistics.mean(values),
            std=statistics.stdev(values) if len(values)>1 else None, old_params=old,
            old_median_z=statistics.median(old_z) if old_z else None,
            old_extreme_pct=100*sum(z>2 for z in old_z)/len(old_z) if old_z else None,
            causal_count=len(rolling), causal_median_z=statistics.median(rolling) if rolling else None,
            causal_extreme_pct=100*sum(z>2 for z in rolling)/len(rolling) if rolling else None))
    return {"snapshot_statistics": stats, "volume_unit": "UNVERIFIED_SOURCE_CSV_VOLUME",
            "resampling": "sum five complete source minutes; no point-value conversion",
            "training": "prior-only rolling samples per leg/segment; descriptive whole-snapshot statistics NOT used for calibration",
            "production_parameters_changed": False}


def source_unit_evidence(path):
    """2026-10-16 Hermes Agent: numerical evidence is not a vendor unit certificate."""
    with Path(path).open() as stream:
        rows = list(csv.DictReader(stream))
    volumes = [float(r["Volume"]) for r in rows]
    ratios = [float(r["Amount"])/(float(r["Volume"])*float(r["Close"])) for r in rows
              if r.get("Amount") and float(r["Volume"]) > 0]
    return dict(source_minutes=len(rows), volume_integral_pct=100*sum(v.is_integer() for v in volumes)/len(volumes),
                amount_over_volume_close_median=statistics.median(ratios) if ratios else None,
                unit_verified=False)


def direction(price, ref):
    return 0 if ref is None or abs(price-ref) <= 1e-9 else (1 if price > ref else -1)


def confirmation(frames, arm, cfg, *, brick_size=None, reversal=False):
    """2026-10-16 Hermes Agent: symmetrical price-distance gate plus treatment-specific shift/alpha."""
    current = frames[-1]
    refs = {leg: current[leg]["vwap"] if arm == "baseline" else current[leg]["adaptive_price"]
            for leg in ("near", "far")}
    result = {"pass_release": False, "release_leg": None, "direction": "CHOP", "reason": "REFERENCE_UNAVAILABLE"}
    if any(ref is None for ref in refs.values()):
        return result
    signs = [direction(current[leg]["bar"]["close"], refs[leg]) for leg in refs]
    if not signs[0] or signs[0] != signs[1]:
        return {**result, "reason": "LEG_DIRECTION_DIVERGENCE"}
    expected = TrendDirection.BULLISH if signs[0] > 0 else TrendDirection.BEARISH
    result["direction"] = expected.value
    # 2026-10-16 Hermes Agent: preserve actual entry-side -> counter-trend-leg mapping.
    retained, release_leg = counter_trend_leg_from_sides("LONG", "SHORT")
    if not reversal and ((retained == Side.LONG) != (expected == TrendDirection.BULLISH)):
        return {**result, "reason": "ENTRY_SIDE_DIRECTION_MISMATCH"}
    if any(abs(current[leg]["bar"]["close"] - refs[leg]) / refs[leg] < cfg.minimum_distance_fraction for leg in refs):
        return {**result, "reason": "PRICE_DISTANCE_BELOW_THRESHOLD"}
    if arm == "adaptive":
        if any(current[leg]["alpha"] < cfg.minimum_alpha for leg in refs):
            return {**result, "reason": "ALPHA_BELOW_THRESHOLD"}
        if any(abs(refs[leg]-current[leg]["vwap"]) / current[leg]["vwap"] < cfg.minimum_distance_fraction for leg in refs):
            return {**result, "reason": "REFERENCE_SHIFT_BELOW_THRESHOLD"}
        if any(direction(current[leg]["bar"]["close"], current[leg]["vwap"]) != signs[0] for leg in refs):
            return {**result, "reason": "BASELINE_DIRECTION_DISAGREES"}
    recent = frames[-cfg.confirmation_bars:]
    if len(recent) < cfg.confirmation_bars or any(b["ts"]-a["ts"] != 300 for a,b in zip(recent,recent[1:])):
        return {**result, "reason": "CONFIRMATION_GAP"}
    for frame in recent:
        for leg in refs:
            ref = frame[leg]["vwap"] if arm == "baseline" else frame[leg]["adaptive_price"]
            if direction(frame[leg]["bar"]["close"], ref) != signs[0]:
                return {**result, "reason": "DIRECTION_NOT_CONFIRMED"}
    history = frames[-12:]
    if len(history) < 12 or any(b["ts"]-a["ts"] != 300 for a,b in zip(history,history[1:])):
        return {**result, "reason": "ADL_RENKO_WARMUP_OR_GAP"}
    bars = [f["near"]["bar"] for f in history]
    brick = brick_size or max(1.0, statistics.mean(b["high"]-b["low"] for b in bars) * .5)
    renko = renko_signal_state(compute_renko(current["ts"], [b["close"] for b in bars], brick), expected)
    adl = adl_signal_state(compute_adl_snr(current["ts"], bars), expected)
    vwap = SubSignalState(source="BAR_REFERENCE_PROXY_NOT_MICRO_TICK_VWAP", direction=expected, score=1.0)
    trend = arbitrate_trend(current["ts"], renko, adl, vwap)
    return {**result, "pass_release": trend.pass_release,
            "release_leg": release_leg if trend.pass_release else None,
            "reason": trend.block_reason or "CONFIRMED", "confidence": trend.confidence,
            "renko": renko.to_dict() if hasattr(renko, "to_dict") else asdict(renko), "adl_snr": adl.detail.get("snr")}


@dataclass
class Inventory:
    """2026-10-16 Hermes Agent: isolated research inventory; never a broker/PaperTrader state."""
    cash: float
    lifecycle: PositionLifecycle = field(default_factory=PositionLifecycle)
    episode: dict | None = None
    pending: dict | None = None
    peak_net: float = 0
    peak_price: float = 0
    nadir_price: float = 0
    episodes: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    last_flat_ts: float = -float("inf")


def fill_fee(price, product):
    return product["broker_fee_per_side"] + product["exchange_fee_per_side"] + price * product["point_value"] * product["tax_rate"]


def mark_inventory(state, frame, product, slippage=0.0):
    floating, liquidation_cost = 0.0, 0.0
    if state.episode:
        for leg, entry in state.episode["open_legs"].items():
            price = frame[leg]["bar"]["close"] + (-slippage if leg == "near" else slippage)
            sign = 1 if leg == "near" else -1
            floating += sign * (price-entry) * product["point_value"]
            liquidation_cost += fill_fee(price, product)
    return state.cash + floating - liquidation_cost


def execute_pending(state, frame, product, slippage):
    pending = state.pending
    if pending is None:
        return []
    # Missing next complete paired bar never executes an old decision on a later bar.
    state.pending = None
    if frame["ts"] != pending["decision_ts"] + 300:
        return [{"event": "UNFILLED_GAP", **pending}]
    events = []
    if pending["action"] == "ENTRY":
        if state.episode is not None or state.cash < product["margin_per_lot"] * 2:
            raise ValueError("research entry inventory/margin guard violated")
        state.episode = dict(episode_id=pending["episode_id"], entry_ts=pending["decision_ts"],
            entry_decision_ts=pending["decision_ts"], open_legs={}, gross_twd=0.0, cost_twd=0.0,
            exits=[], data_gap_count=0, brick_size=pending["brick_size"])
        for leg in ("near", "far"):
            price = frame[leg]["bar"]["open"] + (slippage if leg == "near" else -slippage)
            fee = fill_fee(price, product)
            state.cash -= fee
            state.episode["cost_twd"] += fee
            state.episode["open_legs"][leg] = price
            events.append(dict(event="ENTRY_FILL", leg=leg, ts=pending["decision_ts"], price=price, fee_twd=fee))
        state.lifecycle = PositionLifecycle(phase=PositionPhase.SPREAD,
            release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED))
        state.peak_net = 0.0
        state.peak_price = frame["near"]["bar"]["open"]
        state.nadir_price = frame["near"]["bar"]["open"]
    else:
        if state.episode is None:
            raise ValueError("exit without research inventory")
        legs = [pending["leg"].lower()] if pending.get("leg") else list(state.episode["open_legs"])
        for leg in legs:
            entry = state.episode["open_legs"].pop(leg)
            price = frame[leg]["bar"]["open"] + (-slippage if leg == "near" else slippage)
            gross = (price-entry) * (1 if leg == "near" else -1) * product["point_value"]
            fee = fill_fee(price, product)
            state.cash += gross - fee
            state.episode["gross_twd"] += gross
            state.episode["cost_twd"] += fee
            event = dict(event="EXIT_FILL", leg=leg, ts=pending["decision_ts"], price=price,
                         gross_twd=gross, fee_twd=fee, reason=pending["action"], winner=pending.get("winner"))
            state.episode["exits"].append(event)
            events.append(event)
        if not state.episode["open_legs"]:
            state.episode["net_pnl_twd"] = state.episode["gross_twd"] - state.episode["cost_twd"]
            state.episodes.append(state.episode)
            state.episode = None
            state.lifecycle = PositionLifecycle()
            state.last_flat_ts = frame["ts"]
        else:
            leg = next(iter(state.episode["open_legs"]))
            state.lifecycle = PositionLifecycle(phase=PositionPhase.SINGLE_LEG,
                release_group=ReleaseGroup(status=ReleaseGroupStatus.COMPLETED),
                trail_group=TrailGroup(status=TrailGroupStatus.ARMED))
            state.peak_price = state.nadir_price = frame[leg]["bar"]["open"]
    state.fills.extend(events)
    return events


def lifecycle_decision(state, frame, trend, cfg, params):
    if state.episode is None:
        return None
    ep = state.episode
    pnl = {}
    for leg in ("near", "far"):
        pnl[leg] = ((frame[leg]["bar"]["close"]-ep["open_legs"][leg]) * (1 if leg == "near" else -1)
                    if leg in ep["open_legs"] else sum(e["gross_twd"]/10 for e in ep["exits"] if e["leg"] == leg))
    # The actual adapter owns a legacy 10-TWD/point, 92-TWD friction formula.
    # We preserve that decision contract; the fill ledger below accounts actual config fees/tax/slippage.
    net = sum(pnl.values()) * 10 - 92
    state.peak_net = max(state.peak_net, net)
    remaining = next(iter(ep["open_legs"]))
    price = frame[remaining]["bar"]["close"]
    state.peak_price = max(state.peak_price, price)
    state.nadir_price = min(state.nadir_price, price)
    retained_direction = "BULLISH" if remaining == "near" else "BEARISH"
    ctx = LifecycleContext(near_pnl_pts=pnl["near"], far_pnl_pts=pnl["far"],
        floating_pnl_pts=sum(pnl.values()), entry_age_secs=frame["ts"]-ep["entry_ts"],
        release_stop_threshold=params["release_stop_points"], trail_dist=params["trail_distance_points"],
        max_loss_pts=cfg.max_spread_loss_points, max_hold_secs=cfg.maximum_hold_secs,
        enable_combined_upl_trail=params["enable_combined_upl_trail"],
        combined_upl_activation_net_pnl_twd=params["combined_upl_activation_net_pnl_twd"],
        combined_upl_giveback_twd=params["combined_upl_giveback_twd"], peak_net_exit_pnl_twd=state.peak_net,
        trend_release_enabled=True, release_trend_gate_enabled=True, trend_confirmed=trend,
        trend_reversal_confirmed={"reversal_confirmed": trend["pass_release"] and trend["direction"] != retained_direction},
        trailing_side=Side.LONG if remaining == "near" else Side.SHORT,
        peak=state.peak_price, nadir=state.nadir_price, rem_high=price, rem_low=price,
        is_backtest=True, marks_live=False)
    decision = evaluate_lifecycle_actions(ctx, state.lifecycle)
    return ({"action": decision.action.value, "leg": decision.release_leg.value if decision.release_leg else None,
             "winner": decision.winner, "decision_ts": frame["ts"]} if decision else None)


def run_v2(near_path, far_path, *, start_date, end_date, output_dir,
           config_path=ROOT / "config/adaptive_vwap_config.yaml",
           validation_path=ROOT / "config/adaptive_validation_config.yaml",
           futures_path=ROOT / "config/futures.yaml"):
    assert_research_allowed(ROOT)
    if datetime.now().astimezone().utcoffset() != timedelta(hours=8):
        raise ValueError("VolAdjuster clock segments require Asia/Taipei timezone")
    cfg = ValidationConfig(**yaml.safe_load(Path(validation_path).read_text()))
    adaptive_cfg = AdaptiveVWAPConfig.load(config_path)
    if adaptive_cfg.reference_method != "ema":
        raise ValueError("v2 requires the declared EMA reference")
    futures = yaml.safe_load(Path(futures_path).read_text())
    product = futures["products"][futures["ticker"]]
    if product["point_value"] != 10:
        raise ValueError("MTS adapter hardcodes 10 TWD/point; other products unsupported")
    for key in ("point_value", "margin_per_lot", "tick_size"):
        if not math.isfinite(product[key]) or product[key] <= 0:
            raise ValueError("invalid product configuration")
    for key in ("broker_fee_per_side", "exchange_fee_per_side", "tax_rate"):
        if not math.isfinite(product[key]) or product[key] < 0:
            raise ValueError("invalid product cost configuration")
    params = futures["mts"]["params"]
    slippage = futures["execution"]["market_slippage_pts"]
    root = Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("output directory must be empty")
    source_audit = {str(path): audit_source(path) for path in (near_path, far_path)}
    start = datetime.fromisoformat(start_date).replace(tzinfo=TW).timestamp()
    end = (datetime.fromisoformat(end_date).replace(tzinfo=TW)+timedelta(days=1)).timestamp()
    if start >= end:
        raise ValueError("invalid date range")
    bars = {"near": load_completed_bars([near_path], timestamp_kind="start", source_bar_secs=60),
            "far": load_completed_bars([far_path], timestamp_kind="start", source_bar_secs=60)}
    volume = CausalVolume(cfg)
    features = defaultdict(dict)
    for leg in bars:
        history, label, pv, vol = deque(maxlen=320), None, 0.0, 0.0
        for ts, bar in sorted(bars[leg].items()):
            if ts >= end:
                break
            session, _ = vwap_session_bounds(ts)
            if session != label:
                history.clear()
                label, pv, vol = session, 0.0, 0.0
            history.append(bar)
            pv += bar["close"]*bar["volume"]
            vol += bar["volume"]
            vm = volume.observe(leg, ts, bar["volume"])
            vwap = pv/vol if vol else None
            adaptive = (AdaptiveVWAP.compute(ts, cfg.oi_intensity_assumed, cfg.oi_direction_assumed,
                vm["quality"], vm["zscore"], vwap, list(history), config=adaptive_cfg)
                if vm["zscore"] is not None else None)
            features[ts][leg] = dict(bar=bar, vwap=vwap, volume_model=vm,
                adaptive_price=adaptive.adaptive_price if adaptive else None,
                ema=adaptive.twap if adaptive else None, alpha=adaptive.alpha if adaptive else None,
                oi_source="ASSUMED_CONSTANT_NOT_OBSERVED")
    frames = [dict(ts=ts, **features[ts]) for ts in sorted(bars["near"].keys() & bars["far"].keys()) if ts < end]
    states = {arm: Inventory(cfg.initial_equity_twd) for arm in ("baseline", "adaptive")}
    records, history = [], []
    previous = None
    for frame in frames:
        ts = frame["ts"]
        history.append(frame)
        if ts < start:
            continue
        events = {arm: execute_pending(state, frame, product, slippage) for arm, state in states.items()}
        if previous is not None and ts-previous != 300:
            for state in states.values():
                if state.episode:
                    state.episode["data_gap_count"] += 1
        probes = {arm: confirmation(history, arm, cfg) for arm in states}
        actions = {}
        for arm, state in states.items():
            trend = confirmation(history, arm, cfg, brick_size=state.episode["brick_size"],
                reversal=state.lifecycle.phase == PositionPhase.SINGLE_LEG) if state.episode else probes[arm]
            actions[arm] = lifecycle_decision(state, frame, trend, cfg, params)
            if actions[arm]:
                state.pending = actions[arm]
        next_pair = ts+300 in bars["near"] and ts+300 in bars["far"] and ts+300 < end
        ready = all(frame[leg]["adaptive_price"] is not None for leg in ("near", "far"))
        cooldown = futures.get("cooldown_bars", 5)*300
        common_entry = (ready and next_pair and all(s.episode is None and s.pending is None
            and s.cash >= product["margin_per_lot"]*2 and ts-s.last_flat_ts >= cooldown for s in states.values()))
        # 2026-10-16 Hermes Agent: expose opportunity/margin dilution of the all-record denominator.
        entry_status = ("POSITION_OR_EXIT_PENDING" if any(s.episode is not None or s.pending is not None for s in states.values())
            else "REFERENCE_NOT_READY" if not ready else "NEXT_PAIR_MISSING" if not next_pair
            else "COOLDOWN" if any(ts-s.last_flat_ts < cooldown for s in states.values())
            else "MARGIN_INSUFFICIENT" if any(s.cash < product["margin_per_lot"]*2 for s in states.values())
            else "ENTRY_SCHEDULED")
        if common_entry:
            brick = max(product["tick_size"], statistics.mean(f["near"]["bar"]["high"]-f["near"]["bar"]["low"] for f in history[-12:]) * .5)
            for arm, state in states.items():
                state.pending = dict(action="ENTRY", decision_ts=ts, episode_id=f"spread:{ts}", brick_size=brick)
                actions[arm] = state.pending
        for arm, state in states.items():
            probe = probes[arm]
            records.append(dict(schema_version=2, mode=MODEL_NAME, pair_id=f"v2:{ts}", ts=ts, arm=arm,
                features={leg: frame[leg] for leg in ("near", "far")},
                current_spread=frame["near"]["bar"]["close"]-frame["far"]["bar"]["close"],
                reference_spread=(frame["near"]["vwap"]-frame["far"]["vwap"] if arm == "baseline" else
                    frame["near"]["adaptive_price"]-frame["far"]["adaptive_price"] if ready else None),
                probe=probe, action=actions[arm], phase=state.lifecycle.phase.value,
                episode_id=state.episode["episode_id"] if state.episode else None,
                fills=events[arm], cash_twd=state.cash, liquidation_equity_twd=mark_inventory(state, frame, product, slippage),
                pending=state.pending, common_entry_status=entry_status, real_order_emitted=False))
        previous = ts
    if not records:
        raise ValueError("no paired decisions in requested dates")
    diagnosis = volume_diagnosis(bars["near"], bars["far"], features)
    diagnosis["source_unit_evidence"] = {str(p):source_unit_evidence(p) for p in (near_path,far_path)}
    root.mkdir(parents=True, exist_ok=True)
    grouped = defaultdict(list)
    for row in records:
        grouped[row["arm"], datetime.fromtimestamp(row["ts"], TW).strftime("%Y%m%d")].append(row)
    for (arm, day), rows in grouped.items():
        (root / f"{arm}_{day}.jsonl").write_text("".join(json.dumps(r, allow_nan=False)+"\n" for r in rows))
    summary = {arm: dict(closed_episodes=s.episodes, open_episode=s.episode,
        unfilled_pending=s.pending, fills=s.fills, final_cash_twd=s.cash) for arm,s in states.items()}
    (root / "portfolio.json").write_text(json.dumps(summary, indent=2, allow_nan=False)+"\n")
    (root / "volume_diagnosis.json").write_text(json.dumps(diagnosis, indent=2, allow_nan=False)+"\n")
    (root / "replay_trace.jsonl").write_text("".join(json.dumps({k:r[k] for k in
        ("pair_id", "ts", "arm", "probe", "action", "phase", "fills")}, allow_nan=False)+"\n" for r in records))
    sources = [near_path, far_path, config_path, validation_path, futures_path, __file__,
        ROOT / "scripts/research/run_adaptive_ab_backtest.py", ROOT / "scripts/research/replay_adaptive_vwap.py",
        ROOT / "strategies/plugins/futures/active/mts_adaptive_vwap.py",
        ROOT / "strategies/plugins/futures/active/mts_vol_adjuster.py",
        ROOT / "strategies/plugins/futures/active/mts_hvwap_candidate.py",
        ROOT / "strategies/plugins/futures/active/mts_lifecycle_adapter.py",
        ROOT / "strategies/plugins/futures/active/mts_trend_signal_adapter.py",
        ROOT / "strategies/plugins/futures/active/mts_renko_signal.py"]
    manifest = dict(schema_version=2, model=MODEL_NAME, start_date=start_date, end_date=end_date,
        config=asdict(adaptive_cfg), validation_config=asdict(cfg), product=product, ticker=futures["ticker"],
        mts_parameters=params, slippage_points_per_fill=slippage, source_audit=source_audit,
        source_files={str(p):sha256(p) for p in sources},
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        source_working_tree=subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True),
        hostname=socket.gethostname(), records_per_arm=len(records)//2,
        outputs={p.name:sha256(p) for p in sorted(root.iterdir())}, promotion="NO_GO",
        assumptions=["Shared hypothetical LONG NEAR / SHORT FAR entries, not reconstructed MTS entry signals",
            "Both portfolios wait until jointly flat and cooldown satisfied before next shared entry",
            "Entry-side mapping uses canonical LONG NEAR / SHORT FAR -> retain LONG, release FAR",
            "Research inventory only; initial capital is a scenario, not permission for PAPER trading",
            "Five-minute close proxies replace tick MID/Renko/micro-VWAP inputs; no fake BBO freshness",
            "marks_live=False preserves Policy J release-intercept refusal on closed bars",
            "Policy J peak from observed closes only, not tick-confirmed durable peaks",
            "Canonical lifecycle adapter uses hardcoded 10 TWD/point and 92 TWD decision friction",
            "Portfolio uses configured commission, exchange fee, tax, and adverse slippage separately",
            "Current bars may form features; calibration parameters use strictly earlier bars only",
            "Missing next pair skips queued fill; inventory remains open, gaps are counted",
            "No forced terminal fills; open episode and pending decision remain visible",
            "OI and source volume/contract provenance unverified; no production promotion"])
    (root / "model_manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False)+"\n")
    return manifest
