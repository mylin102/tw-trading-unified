"""
Deterministic routing for futures strategy signals.

This router sits between bar-level regime detection and execution:
- strategies still generate their own signals
- the router decides which strategies are allowed to speak on a bar
- execution remains owned by monitor / OrderManager / PaperTrader
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .attribution_recorder import AttributionRecorder, log_router_event
from .futures_bar_regime import FuturesBarRegimeResult, FuturesBarRegimeConfig, _safe_float
from core.signal import Signal
from core.strategy_context import StrategyContext
from core.strategy_eval import StrategyEval, RouterTrace, write_trace, print_trace_summary
from core.replay_engine import replay_engine
from core.shioaji_session import get_shared_system_status

class StrategyLookup(Protocol):
    def get(self, name: str) -> Any: ...


# ═══════════════════════════════════════════════════════════════
# Strategy Router v2 — STRATEGY_POLICY
# ═══════════════════════════════════════════════════════════════
# Declarative policy per strategy: which regimes, max weight, kill switch.
# This runs BEFORE per-strategy signal generation — strategies blocked here
# never have their on_bar() called.

STRATEGY_POLICY: dict[str, dict] = {
    "adaptive_orb": {
        "enabled_regimes": ["TREND", "SQUEEZE"],
        "max_weight": 1.0,
        "kill_if_cagr_below": -0.02,
    },
    "adaptive_orb_v15": {
        "enabled_regimes": ["TREND", "SQUEEZE", "WEAK", "CHOP", "TRANSITION", "STRETCHED"],
        "max_weight": 1.0,
        "kill_if_cagr_below": -0.02,
        "description": "v1 ORB + ATR breakout confirmation gate (observation mode)",
    },
    "trend_continuation_v1": {
        "enabled_regimes": ["TREND"],
        "max_weight": 1.0,
        "kill_if_cagr_below": -0.02,
        "description": "Trend continuation 'catch-up' entry for strong bull markets",
    },
    "counter_vwap": {
        "enabled_regimes": ["WEAK", "CHOP", "SQUEEZE"],
        "max_weight": 0.2,
        "kill_if_cagr_below": -0.05,
    },
    "cumulative_delta": {
        "enabled_regimes": [],
        "max_weight": 0.0,
        "kill_if_cagr_below": 0.0,
    },
    "calendar_condor_v2": {
        "enabled_regimes": ["WEAK", "CHOP"],
        "max_weight": 0.3,
        "kill_if_cagr_below": -999,  # debug only — never kill-switch
        "debug_skip_reason": True,
    },
    "spring_upthrust": {
        "enabled_regimes": ["WEAK", "CHOP", "SQUEEZE"],
        "max_weight": 0.8,
        "kill_if_cagr_below": -0.05,
    },
    "weak_bear_trend": {
        "enabled_regimes": ["WEAK", "CHOP", "SQUEEZE"],
        "max_weight": 0.5,
        "kill_if_cagr_below": -0.05,
        "required_bias": "SHORT",
        "description": "WEAK regime 空头趋势：弱勢反彈失敗後做空 (SQUEEZE fallback)",
    },
    "weak_bull_trend": {
        "enabled_regimes": ["WEAK", "CHOP", "SQUEEZE"],
        "max_weight": 0.35,
        "kill_if_cagr_below": -0.05,
        "required_bias": "BULLISH",
        "description": "WEAK regime 防守型多頭：價格>VWAP + EMA 多頭排列 + 溫和動能 (SQUEEZE fallback)",
    },
    "kbar_feature": {
        "enabled_regimes": ["WEAK", "CHOP"],
        "max_weight": 0.5,
        "kill_if_cagr_below": -0.05,
    },
    "vol_squeeze": {
        "enabled_regimes": ["SQUEEZE"],
        "max_weight": 0.5,
        "kill_if_cagr_below": -0.03,
    },
    "psar": {
        "enabled_regimes": ["TREND"],
        "max_weight": 0.5,
        "kill_if_cagr_below": -0.03,
    },
    "squeeze_fire_scout": {
        "enabled_regimes": ["SQUEEZE", "TREND", "BEAR", "WEAK", "CHOP", "TRANSITION"],
        "max_weight": 0.25,
        "kill_if_cagr_below": -999,
        "description": "Early scout during squeeze release, 0.25x size, tight stop, time-stop 6 bars",
    },
    "tmf_spread": {
        "enabled_regimes": ["SQUEEZE", "WEAK", "CHOP", "TREND", "BEAR"],
        "max_weight": 1.0,
        "kill_if_cagr_below": -999,
        "description": "Phase 0 spread: Long Near / Short Far on squeeze, 20pt release, 20pt trail",
    },
    "range_mean_reversion_v1": {
        "enabled_regimes": ["WEAK", "CHOP", "SQUEEZE", "STRETCHED"],
        "max_weight": 0.5,
        "kill_if_cagr_below": -0.05,
        "description": "Mean reversion using BB and RSI for ranging/choppy markets",
    },
}


logger = logging.getLogger(__name__)


def _check_strategy_policy(
    strategy_name: str,
    regime: str,
    metrics: dict | None = None,
    bias: str | None = None,
) -> tuple[bool, str]:
    """Check if a strategy is allowed by policy.

    Returns (allowed: bool, reason: str).
    Always returns True + 'ENABLED' for unnamed strategies not in policy.
    """
    policy = STRATEGY_POLICY.get(strategy_name)
    if policy is None:
        return True, "ENABLED"

    if not policy.get("enabled_regimes"):
        return False, "DISABLED_BY_POLICY"

    # Normalise regime name
    regime_normalized = regime.upper() \
        .replace("BEAR", "TREND") \
        .replace("WEAK", "CHOP") \
        .replace("STRETCHED", "CHOP")
    if regime_normalized not in policy["enabled_regimes"]:
        return False, f"REGIME_BLOCKED:{regime}"

    # [V-Model] Enforce required_bias if set in policy
    required_bias = policy.get("required_bias")
    if required_bias and bias:
        bias_norm = bias.strip().upper()
        required_norm = required_bias.strip().upper()
        
        # Normalize: LONG == BULLISH, SHORT == SHORT
        bias_norm = "BULLISH" if bias_norm == "LONG" else bias_norm
        required_norm = "BULLISH" if required_norm == "LONG" else required_norm
        
        if bias_norm != required_norm:
            return False, f"BIAS_MISMATCH: requires {required_bias}, got {bias}"

    # Kill-switch: CAGR below threshold
    if metrics and isinstance(metrics, dict):
        strategy_metrics = metrics.get(strategy_name, {})
        if isinstance(strategy_metrics, dict):
            cagr = strategy_metrics.get("cagr")
            kill_below = policy.get("kill_if_cagr_below", -999)
            if cagr is not None and kill_below is not None and cagr < kill_below:
                return False, f"KILL_SWITCH_CAGR:{cagr:.2%}"

    return True, "ENABLED"


def _apply_strategy_policy(
    candidates: list[str],
    regime: str,
    metrics: dict | None = None,
    bias: str | None = None,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Apply STRATEGY_POLICY to a list of candidate strategies.

    Returns:
      checked: list of (name, allowed, reason) — one per candidate
      keep:    list of names that passed the policy gate
    """
    checked: list[tuple[str, str, str]] = []
    keep: list[str] = []

    for name in candidates:
        allowed, reason = _check_strategy_policy(name, regime, metrics, bias)
        status = "ALLOW" if allowed else "BLOCK"
        if allowed:
            keep.append(name)
        else:
            logger.info(f"[STRATEGY_POLICY] strategy={name} regime={regime} "
                        f"enabled={allowed} reason={reason}")
            # Print for console visibility
            print(f"[STRATEGY_POLICY][{status}] {name}: {reason}", flush=True)
        checked.append((name, status, reason))

    return checked, keep


@dataclass(frozen=True)
class FuturesRouterConfig:
    """Routing policy for futures entry signals."""

    trend_strategies: tuple[str, ...] = ("adaptive_orb", "adaptive_orb_v15", "trend_continuation_v1", "squeeze_fire_scout")
    weak_strategies: tuple[str, ...] = ("weak_bear_trend", "weak_bull_trend", "counter_vwap", "spring_upthrust", "kbar_feature", "range_mean_reversion_v1", "adaptive_orb_v15", "trend_continuation_v1", "calendar_condor_v2", "squeeze_fire_scout")
    bear_strategies: tuple[str, ...] = ("counter_vwap", "spring_upthrust", "range_mean_reversion_v1", "weak_bear_trend", "squeeze_fire_scout")  # [Bear] conservative short — countertrend + upthrust + weak trend
    stretched_strategies: tuple[str, ...] = ("counter_vwap", "spring_upthrust", "range_mean_reversion_v1", "weak_bear_trend", "weak_bull_trend", "squeeze_fire_scout")
    squeeze_strategies: tuple[str, ...] = ("squeeze_fire_scout", "range_mean_reversion_v1", "adaptive_orb_v15")
    countertrend_strategies: tuple[str, ...] = ("counter_vwap", "spring_upthrust", "range_mean_reversion_v1")
    hard_block_countertrend_in_trend: bool = True

    # ── Squeeze Fire Scout config ──
    squeeze_fire_scout_enabled: bool = True
    squeeze_fire_scout_size_multiplier: float = 0.25
    squeeze_fire_scout_min_mom_state: int = 3
    squeeze_fire_scout_requires_vwap: bool = True
    squeeze_fire_scout_requires_bias: bool = True
    squeeze_fire_scout_max_breakout_strength: float = 0.25
    squeeze_fire_scout_stop_atr_mult: float = 0.6
    squeeze_fire_scout_time_stop_bars: int = 6

    # ── Theta-gate: router decides if market regime is suitable for theta ──
    theta_enabled: bool = True
    theta_weak_only: bool = True          # only allow in WEAK/CHOP, not TREND/BEAR
    theta_max_mom_state: int = 2          # [Patch] relaxed from 1 to 2
    theta_max_macd_abs: float = 50.0      # [Patch] scale corrected from 0.5 to 50.0
    theta_max_macd_hist_atr: float = 0.8  # [Patch] new ATR-normalized threshold
    theta_block_volume_spike: bool = True # block when volume_spike >= threshold
    theta_volume_spike_threshold: float = 1.5 # [Patch] new threshold to fix truthiness bug
    theta_min_hold_seconds: int = 1800
    theta_min_edge_multiple: float = 2.0


@dataclass(frozen=True)
class FuturesRouterDecision:
    """Final router output for a single bar."""

    action: str
    reason: str
    regime: str
    bias: str
    selected_strategy: str | None = None
    signal: Signal | None = None
    candidates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ── Theta-gate — consumed by OptionsMonitor ──
    theta_allowed: bool = False
    theta_block_reason: str | None = None

    # ── Squeeze Fire Scout — size override ──
    size_multiplier: float = 1.0

    @property
    def is_trade(self) -> bool:
        return self.signal is not None and self.action == "TRADE"


def _normalize_side(value: Any) -> str:
    if value is None:
        return ""
    text = str(getattr(value, "value", value)).strip().upper()
    if text in {"BUY", "LONG"}:
        return "BUY"
    if text in {"SELL", "SHORT"}:
        return "SELL"
    return text


def _has_opposite_side_working_order(
    desired_action: str,
    working_orders: Sequence[Any],
) -> bool:
    desired_side = _normalize_side(desired_action)
    if desired_side not in {"BUY", "SELL"}:
        return False
    opposite_side = "SELL" if desired_side == "BUY" else "BUY"
    return any(_normalize_side(getattr(order, "side", None)) == opposite_side for order in working_orders)


def _dedupe(names: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _strategy_order_for_regime(
    regime_result: FuturesBarRegimeResult,
    active_strategy_name: str | None,
    config: FuturesRouterConfig,
) -> list[str]:
    regime = regime_result.regime

    if regime == "TREND":
        base = list(config.trend_strategies)
        if (
            active_strategy_name
            and not (
                config.hard_block_countertrend_in_trend
                and active_strategy_name in config.countertrend_strategies
            )
        ):
            base.insert(0, active_strategy_name)
        return _dedupe(base)

    if regime == "WEAK":
        base = list(config.weak_strategies)
        # [V-Model] Insert active_strategy BEFORE bias-aware sorting so natural ordering works
        if active_strategy_name:
            base.insert(0, active_strategy_name)
        # Bias-aware sorting: promote the trend strategy matching current bias to front
        bias = str(getattr(regime_result, "bias", "")).strip().upper()
        # Normalize: regime classifier outputs LONG/SHORT, strategies expect BULLISH/SHORT
        _b = "BULLISH" if bias == "LONG" else bias
        if _b == "SHORT":
            # Promote weak_bear_trend to front
            if "weak_bear_trend" in base:
                base.remove("weak_bear_trend")
                base.insert(0, "weak_bear_trend")
        elif _b == "BULLISH":
            # Promote weak_bull_trend to front
            if "weak_bull_trend" in base:
                base.remove("weak_bull_trend")
                base.insert(0, "weak_bull_trend")
        else:
            # NEUTRAL bias — demote both directional trend strategies to end
            for _trend_strat in ("weak_bear_trend", "weak_bull_trend"):
                if _trend_strat in base:
                    base.remove(_trend_strat)
                    base.append(_trend_strat)
        return _dedupe(base)

    if regime == "BEAR":
        base = list(config.bear_strategies)
        if active_strategy_name:
            base.insert(0, active_strategy_name)
        return _dedupe(base)

    if regime == "STRETCHED":
        base = list(config.stretched_strategies)
        # [V-Model] Insert active_strategy BEFORE bias-aware sorting
        if active_strategy_name:
            base.insert(0, active_strategy_name)
        # Bias-aware sorting: promote the trend strategy matching current bias to front
        _st_bias = str(getattr(regime_result, "bias", "")).strip().upper()
        _st_bias = "BULLISH" if _st_bias == "LONG" else _st_bias
        if _st_bias == "SHORT":
            if "weak_bear_trend" in base:
                base.remove("weak_bear_trend")
                base.insert(0, "weak_bear_trend")
        elif _st_bias == "BULLISH":
            if "weak_bull_trend" in base:
                base.remove("weak_bull_trend")
                base.insert(0, "weak_bull_trend")
        return _dedupe(base)

    if regime == "SQUEEZE":
        base = list(config.squeeze_strategies)
        # [V-Model] Insert active_strategy BEFORE bias-aware sorting
        if active_strategy_name:
            base.insert(0, active_strategy_name)
        # Bias-aware fallback: append weak directional strategies when bias is non-neutral
        # (squeeze專屬策略優先，weak directional作為fallback排最後)
        _sq_bias = str(getattr(regime_result, "bias", "")).strip().upper()
        # Normalize: regime classifier outputs LONG/SHORT, strategies expect BULLISH/SHORT
        _sq_bias = "BULLISH" if _sq_bias == "LONG" else _sq_bias
        if _sq_bias == "BULLISH" and "weak_bull_trend" in config.weak_strategies:
            base.append("weak_bull_trend")
        elif _sq_bias == "SHORT" and "weak_bear_trend" in config.weak_strategies:
            base.append("weak_bear_trend")
        return _dedupe(base)

    return _dedupe([active_strategy_name] if active_strategy_name else [])


def _evaluate_theta_environment(
    regime_result: FuturesBarRegimeResult,
    context: StrategyContext,
    cfg: FuturesRouterConfig,
) -> tuple[bool, str | None]:
    """Evaluate whether current bar is suitable for theta (option selling).

    Router decides the *market regime gate* — not quote quality or edge.
    Those are the OptionsMonitor's responsibility.

    Returns (allowed: bool, block_reason: str | None).
    """
    if not cfg.theta_enabled:
        return False, "THETA_DISABLED_BY_CONFIG"

    bar = context.market.last_bar
    if not bar:
        return False, "NO_BAR_DATA"

    regime = regime_result.regime

    # 1. Regime gate — theta_weak_only: only WEAK/SQUEEZE
    if cfg.theta_weak_only:
        if regime not in ("WEAK", "SQUEEZE"):
            return False, f"REGIME_NOT_SUITABLE_THETA_WEAK_ONLY (regime={regime})"
    else:
        if regime in ("TREND", "BEAR"):
            return False, f"REGIME_HAS_DIRECTION (regime={regime})"

    # 2. Momentum gate — low momentum only
    mom_state = bar.get("mom_state", 999)
    if isinstance(mom_state, (int, float)):
        if int(mom_state) > cfg.theta_max_mom_state:
            return False, f"MOMENTUM_TOO_HIGH mom_state={mom_state} > {cfg.theta_max_mom_state}"

    # 3. Volume gate — block when spike exceeds threshold (Fixes truthiness bug)
    volume_spike_ratio = float(bar.get("volume_spike", 0.0) or 0.0)
    if cfg.theta_block_volume_spike and volume_spike_ratio >= cfg.theta_volume_spike_threshold:
        return False, f"VOLUME_SPIKE (ratio={volume_spike_ratio:.2f} >= {cfg.theta_volume_spike_threshold})"

    # 4. MACD gate — near zero only (Scale mismatch fix + ATR normalization support)
    macd_hist = bar.get("macd_hist", 0.0)
    if macd_hist is not None:
        macd_hist_abs = abs(float(macd_hist))
        
        # Priority 1: ATR-normalized MACD (Recommended)
        atr_used = bar.get("atr_used", bar.get("atr", 0.0))
        if atr_used and atr_used > 0:
            macd_hist_atr = macd_hist_abs / atr_used
            if macd_hist_atr >= cfg.theta_max_macd_hist_atr:
                return False, f"MACD_NOT_FLAT (ATR) |macd_hist/atr|={macd_hist_atr:.2f} >= {cfg.theta_max_macd_hist_atr}"
        
        # Priority 2: Absolute MACD threshold (Fallback/Patch)
        if macd_hist_abs >= cfg.theta_max_macd_abs:
            return False, f"MACD_NOT_FLAT |macd_hist|={macd_hist_abs:.2f} >= {cfg.theta_max_macd_abs}"

    return True, None


def route_futures_signal(
    *,
    registry: StrategyLookup,
    context: StrategyContext,
    regime_result: FuturesBarRegimeResult,
    active_strategy_name: str | None,
    current_working_orders: Sequence[Any] | None = None,
    is_flattening: bool = False,
    router_config: FuturesRouterConfig | None = None,
    prepare_strategy: Callable[[str, Any], None] | None = None,
    recorder: AttributionRecorder | None = None,
) -> FuturesRouterDecision:
    """
    Route exactly one futures signal for the current bar.

    The router never executes a trade. It either:
    - returns one validated signal from the first allowed strategy, or
    - returns an explicit no-trade / blocked decision with reasons.
    """
    print(f"[ROUTE_SIGNAL_ENTER] ts={context.market.timestamp} regime={regime_result.regime} bias={regime_result.bias}", flush=True)

    cfg = router_config or FuturesRouterConfig()
    notes: list[str] = []
    working_orders = list(current_working_orders or [])

    # Extract timestamp and symbol for attribution logging
    timestamp = context.market.timestamp
    symbol = context.market.last_bar.get("symbol", "TX") if context.market.last_bar else "TX"

    # ── [ThetaGate] Evaluate theta BEFORE any strategy-specific gate ──
    # Theta permission is a regime-level decision, not a spread/futures decision.
    # Must be available on every return so OptionsMonitor never falls into bootstrap.
    _theta_allowed, _theta_block_reason = False, "NO_BAR_DATA"
    if context.market.last_bar is not None and regime_result.regime not in ("NO_DATA", "UNKNOWN"):
        try:
            _theta_allowed, _theta_block_reason = _evaluate_theta_environment(regime_result, context, cfg)
        except Exception as _e:
            _theta_block_reason = f"THETA_EVAL_ERROR:{_e}"
    # ── [ThetaGate] Evaluate theta — only affects options premium selling ──
    # Theta permission is a regime-level decision, consumed by OptionsMonitor.
    # Does NOT block futures directional strategies.
    print(f"[ThetaGate] theta={'ALLOW' if _theta_allowed else 'BLOCK'} reason={_theta_block_reason or 'OK'} — futures directional NOT blocked", flush=True)
    notes.append(f"theta_premium={'ALLOW' if _theta_allowed else 'BLOCK'}:reason={_theta_block_reason or 'OK'}")

    # ── [NO_DATA] Gate: bar is None or regime can't be determined ──
    bar = context.market.last_bar
    print(
        "[BIAS_TRACE_V20260508] bar_regime_bias=%r bar_bias=%r regime=%r ts=%s"
        % (getattr(regime_result, "bias", None), bar.get("bias") if bar else None, regime_result.regime, context.market.timestamp),
        flush=True,
    )
    if bar is not None:
        # [P1] Single Source of Truth Contract: inject into bar dict
        _b = str(regime_result.bias).strip().upper()
        _r = str(regime_result.regime).strip().upper()
        # Normalize bias to BULLISH/BEAR/SHORT for strategy consumption
        # Regime classifier outputs LONG/SHORT; strategies expect BULLISH/BEAR/SHORT
        _b_normalized = "BULLISH" if _b == "LONG" else _b
        bar["router_bias"] = _b_normalized
        bar["router_regime"] = _r
        # Legacy compatibility
        bar["bias"] = _b_normalized
        bar["regime"] = _r
    if bar is None or regime_result.regime in ("NO_DATA", "UNKNOWN"):
        _ts = context.market.timestamp or "?"
        print(f"[Router] NO_DATA mode — bar={bar is not None} regime={regime_result.regime} ts={_ts}", flush=True)
        return FuturesRouterDecision(
            action="HOLD",
            reason=f"NO_DATA regime={regime_result.regime}",
            regime=regime_result.regime,
            bias="",
            candidates=[],
            notes=notes,
            theta_allowed=_theta_allowed,
            theta_block_reason=_theta_block_reason,
        )

    # ── Position gate ──

    if context.position.size != 0:
        print(f"[Router][EARLY_RETURN] reason=POSITION_OPEN size={context.position.size} "
              f"entry_price={context.position.entry_price} "
              f"bar_ts={context.market.last_bar.get('timestamp','?') if context.market.last_bar else 'None'}", flush=True)
        return FuturesRouterDecision(
            action="BLOCKED",
            reason=f"position already open ({context.position.size})",
            regime=regime_result.regime,
            bias=regime_result.bias,
            notes=["router only emits fresh entry decisions while flat"],
            theta_allowed=_theta_allowed,
            theta_block_reason=_theta_block_reason,
        )

    # ── [V-Model] Spread Staleness Gate + Bias Modifier ──
    # Two config-driven checks:
    #   1. Staleness gate: if CSV data is too old, block only spread/options strategies
    #   2. Bias modifier: extreme spread_z adds note (not trade action)
    #
    # Config shape (from context.config, typically futures.yaml / futures_night.yaml):
    #   spread_gate:
    #     enabled: true
    #     night_only: true
    #     max_age_minutes: 120
    #     extreme_z: 2.0
    #     stale_action: warn_only      # 'flat' blocks all, 'warn_only' only blocks spread
    #
    spread_cfg = context.config.get("spread_gate", {}) if hasattr(context, "config") and context.config else {}
    spread_enabled = spread_cfg.get("enabled", False)
    max_age_minutes = spread_cfg.get("max_age_minutes", 120)
    extreme_z = spread_cfg.get("extreme_z", 2.0)
    stale_action = spread_cfg.get("stale_action", "flat")
    _spread_gate_block_short = False
    _spread_gate_block_long = False

    if spread_enabled:
        last_bar = context.market.last_bar or {}
        spread_age = last_bar.get("spread_age_minutes", None)
        spread_z = last_bar.get("spread_z", None)
        is_night = last_bar.get("is_night_session", False)

        # ═══ Spread quality classification ═══
        # Far-month (e.g. MXFG6) has low night liquidity — gaps are normal.
        # Never forward-fill; quality-aware only.
        spread_quality = "UNKNOWN"
        if spread_age is not None:
            if spread_age <= 60:
                spread_quality = "FRESH"
            elif spread_age <= 120:
                spread_quality = "DEGRADED"
            else:
                spread_quality = "STALE"

        if is_night:
            print(f"[V-Model][SpreadGate] NIGHT spread_age={spread_age}m quality={spread_quality}", flush=True)

            # ═══ 1) Staleness gate (STALE → block only spread/options, not futures) ═══
            if spread_quality == "STALE":
                if stale_action == "flat":
                    # Legacy mode: block everything (historical compat)
                    print(f"[V-Model][SpreadGate] STALE — FLAT entry (legacy stale_action=flat)", flush=True)
                    if recorder is not None:
                        recorder.log_router_row(
                            timestamp=timestamp, symbol=symbol, regime=regime_result.regime,
                            strategy_name="router", candidate_order=0, status="spread_stale",
                            evaluated=False, winner=False,
                            notes=f"NIGHT spread_age={spread_age}m STALE action=flat",
                        )
                    return FuturesRouterDecision(
                        action="FLAT",
                        reason=f"SPREAD_STALE_NIGHT_age_{spread_age}m",
                        regime=regime_result.regime, bias=regime_result.bias,
                        candidates=[],
                        notes=[f"spread STALE: age={spread_age}m — FLAT (legacy)"],
                        theta_allowed=_theta_allowed,
                        theta_block_reason=_theta_block_reason,
                    )
                # warn_only: block only spread-sensitive strategies, let futures pass
                print(f"[V-Model][SpreadGate] STALE — warn_only (futures OK, spread blocked)", flush=True)
                notes.append(f"spread_quality=STALE age={spread_age}m warn_only — spread/options family blocked")
                _spread_stale_block_families = True
            elif spread_quality == "DEGRADED":
                # Log degradation — no bias impact, no flat
                notes.append(f"spread_quality=DEGRADED age={spread_age}m — log only, no bias")

            # ═══ 2) Spread bias modifier — only when FRESH or DEGRADED (not STALE) ═══
            if spread_quality in ("FRESH", "DEGRADED") and spread_z is not None and abs(spread_z) >= extreme_z:
                # bias modifier only applies when quality is FRESH
                if spread_quality == "FRESH":
                    spread_bias = "SHORT" if spread_z < 0 else "LONG"
                    regime_bias = regime_result.bias.upper() if regime_result.bias else ""
                    print(
                        f"[V-Model][SpreadBias] night spread_z={spread_z:.2f} FRESH "
                        f"spread_bias={spread_bias} regime_bias={regime_bias}",
                        flush=True,
                    )
                    if spread_bias == "SHORT" and regime_bias == "LONG":
                        _spread_gate_block_long = True
                        notes.append(f"spread_bias_block: spread_z={spread_z:.1f} says SHORT but regime says LONG")
                    elif spread_bias == "LONG" and regime_bias == "SHORT":
                        _spread_gate_block_short = True
                        notes.append(f"spread_bias_block: spread_z={spread_z:.1f} says LONG but regime says SHORT")
                    else:
                        notes.append(f"spread_align: spread_z={spread_z:.1f} → {spread_bias}")
                else:
                    # DEGRADED: log the extreme_z value but don't influence bias
                    notes.append(f"spread_extreme: z={spread_z:.1f} (DEGRADED — logged, no bias impact)")
            elif spread_quality in ("FRESH", "DEGRADED") and spread_z is not None:
                print(f"[V-Model][SpreadBias] NIGHT spread_z={spread_z:.2f} abs < extreme_z={extreme_z} — no bias mod", flush=True)

    # ── [ThetaGate] Evaluate whether theta is allowed on this bar ──
    theta_allowed, theta_block_reason = _evaluate_theta_environment(regime_result, context, cfg)
    notes.append(f"theta={'ALLOW' if theta_allowed else 'BLOCK'}:reason={theta_block_reason or 'OK'}")
    print(f"[ThetaGate] theta={'ALLOW' if theta_allowed else 'BLOCK'} reason={theta_block_reason or 'OK'} regime={regime_result.regime}", flush=True)

    candidates = _strategy_order_for_regime(regime_result, active_strategy_name, cfg)

    # ═══ [Strategy Router v2] Apply STRATEGY_POLICY to filter candidates ═══
    # This runs before per-strategy on_bar() — blocked strategies never evaluate.
    _policy_checked, candidates = _apply_strategy_policy(
        candidates,
        regime_result.regime,
        metrics=None,  # TODO: pass live metrics for kill-switch
        bias=regime_result.bias,
    )
    # Log all policy checks to notes
    for name, status, reason in _policy_checked:
        notes.append(f"policy:{name}={status}({reason})")

    # ═══ [SpreadGate warn_only] STALE spread blocks only spread/options families ═══
    _spread_families = ("calendar", "condor", "theta", "spread")
    if locals().get("_spread_stale_block_families", False):
        _pre_count = len(candidates)
        candidates = [c for c in candidates if not any(f in c.lower() for f in _spread_families)]
        _blocked = _pre_count - len(candidates)
        if _blocked:
            print(f"[V-Model][SpreadGate] Blocked {_blocked} spread-family strategies from candidates", flush=True)
            notes.append(f"spread_stale_blocked_{_blocked}_strategies")

    # ═══ [WEAK Volume Gate] Block entry in WEAK regime without volume confirmation ═══
    if regime_result.regime == "WEAK" and regime_result.bias in ("LONG", "SHORT"):
        _vol_spike = _safe_float(bar.get("volume_spike", 0.0))
        _bar_regime_cfg = FuturesBarRegimeConfig()
        
        # [Night Fix] Relax volume gate for night session and early day session
        is_night = bar.get("is_night_session", False)
        bars_since_open = _safe_float(bar.get("bars_since_open", 999))
        is_early_day = not is_night and bars_since_open < 6
        
        # GSD: 1.3 was still blocking active periods. Lower to 0.8 for all quiet periods.
        _thresh = 0.8 if (is_night or is_early_day or _vol_spike < 1.0) else 1.0
        
        _fired = bar.get("fired") or bar.get("sqz_fire", False)
        
        # [Hybrid Phase 2b] Paper debug bypass: let debug_gate_bypass override WEAK_VOLUME_GATE
        _debug_bypass = False
        _bar_cfg = bar.get("_config", {}) or {}
        _gate_cfg = _bar_cfg.get("debug_gate_bypass", {}) or {}
        if _gate_cfg.get("enabled", False) and _gate_cfg.get("paper_only", True):
            _debug_bypass = True
            print(
                f"[ROUTER_GATE_BYPASS] gate=WEAK_VOLUME_GATE reason=paper_debug "
                f"vol_spike={_vol_spike:.2f} threshold={_thresh}",
                flush=True,
            )
        
        if _vol_spike <= _thresh and not _fired and not _debug_bypass:
            _note = f"WEAK_VOLUME_GATE: vol_spike={_vol_spike:.2f} <= {_thresh} (night={is_night}, early={is_early_day}, fired={_fired})"
            print(f"[Router][WEAK_VOLUME_GATE] {_note} — blocking all candidates", flush=True)
            notes.append(_note)
            candidates = []
        elif _fired:
            notes.append(f"WEAK_VOLUME_GATE: bypassed (fired=True)")
        elif _vol_spike < 1.2:
             notes.append(f"WEAK_VOLUME_GATE: passed (quiet={_vol_spike:.2f})")

    if regime_result.regime == "SQUEEZE" and not candidates:
        # Log SQUEEZE regime with no candidates
        if recorder is not None:
            recorder.log_router_row(
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime,
                strategy_name="router",
                candidate_order=0,
                status="squeeze_no_candidates",
                evaluated=False,
                winner=False,
                notes="SQUEEZE regime: wait for expansion confirmation",
            )
        return FuturesRouterDecision(
            action="FLAT",
            reason="SQUEEZE regime: wait for expansion confirmation",
            regime=regime_result.regime,
            bias=regime_result.bias,
            candidates=[],
            notes=notes,
            theta_allowed=_theta_allowed,
            theta_block_reason=_theta_block_reason,
        )

    if not candidates:
        # Log no candidates for regime
        if recorder is not None:
            recorder.log_router_row(
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime,
                strategy_name="router",
                candidate_order=0,
                status="no_candidates",
                evaluated=False,
                winner=False,
                notes=f"no candidate strategies configured for regime {regime_result.regime}",
            )
        return FuturesRouterDecision(
            action="FLAT",
            reason=f"no candidate strategies configured for regime {regime_result.regime}",
            regime=regime_result.regime,
            bias=regime_result.bias,
            candidates=[],
            notes=notes,
            theta_allowed=_theta_allowed,
            theta_block_reason=_theta_block_reason,
        )

    # [BearRoute] Log bear routing context
    if regime_result.regime == "BEAR":
        _bear_bs = context.market.last_bar.get("bear_breakout_strength", 0) if context.market.last_bar else 0
        _vwap_dist = context.market.last_bar.get("price_vs_vwap", 0) if context.market.last_bar else 0
        console.print(
            f"[yellow][BearRoute] regime=BEAR bias={regime_result.bias} "
            f"bear_bs={_bear_bs:.3f} vwap_dist={_vwap_dist:.4f} "
            f"candidates={candidates}[/yellow]"
        )

    # Collect strategy evals for RouterTrace
    _evals: list[dict] = []

    # Log all candidates as pre-evaluation (for candidate_count invariant)
    for i, name in enumerate(candidates):
        log_router_event(
            recorder=recorder,
            timestamp=timestamp,
            symbol=symbol,
            regime=regime_result.regime,
            strategy_name=name,
            candidate_order=i,
            status="candidate",
            evaluated=False,
            winner=False,
            note="pre-evaluation candidate",
        )
        
        strategy = registry.get(name)
        if strategy is None:
            notes.append(f"{name}: not registered")
            # Log missing strategy
            log_router_event(
                recorder=recorder,
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime,
                strategy_name=name,
                candidate_order=i,
                status="missing",
                evaluated=False,
                winner=False,
                note="strategy not registered",
            )
            _evals.append({"name": name, "enabled": False, "triggered": False, "action": None, "edge_score": None, "skip_reason": "NOT_REGISTERED", "notes": {}})
            continue

        if prepare_strategy is not None:
            prepare_strategy(name, strategy)

        try:
            signal = strategy.on_bar(context)
            # Collect eval from strategy.last_eval (set by _set_eval in each strategy)
            _eval_dict = {"name": name, "enabled": True}
            se = getattr(strategy, "last_eval", None)
            if se is not None:
                # Enforce skip_reason contract
                # When triggered=True, skip_reason is meaningless — leave it as-is
                reason = se.skip_reason
                if not se.triggered:
                    reason = reason or "MISSING_REASON"
                    if not reason.startswith("SKIP:"):
                        reason = f"SKIP:{reason}"
                
                _eval_dict.update({
                    "triggered": se.triggered,
                    "action": se.action,
                    "edge_score": se.edge_score,
                    "skip_reason": reason,
                    "notes": se.notes,
                })
            else:
                import logging as _cl
                _cl.getLogger(__name__).error(
                    "[STRATEGY_CONTRACT_VIOLATION] %s.on_bar returned None but last_eval is None — "
                    "a return path in on_bar() is missing _set_eval()",
                    name,
                )
                _eval_dict.update({"triggered": False, "action": None, "edge_score": None, "skip_reason": "SKIP:NO_EVAL_RETURNED", "notes": {}})
            _evals.append(_eval_dict)
        except Exception as e:
            notes.append(f"{name}: error in on_bar ({e})")
            import traceback
            console.print(f"[red][Router error] {name}.on_bar: {e}[/red]")
            console.print(f"[dim]{''.join(traceback.format_exception(type(e), e, e.__traceback__)).strip()}[/dim]")
            # [StrategyError] Log strategy name, reason, df_5m ready state, bar count
            df_5m = getattr(context.market, 'df_5m', None)
            df_5m_ready = f"ready(len={len(df_5m)})" if df_5m is not None and not df_5m.empty else "none/empty"
            bar_count = getattr(context, 'bar_counter', -1)
            console.print(f"[bold yellow][StrategyError] name={name} reason={e} df_5m={df_5m_ready} bar_count={bar_count}[/bold yellow]")
            # Also log to attribution if recorder is present
            log_router_event(
                recorder=recorder,
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime if hasattr(regime_result, 'regime') else context.market.regime,
                strategy_name=name,
                candidate_order=i,
                status="error",
                evaluated=True,
                winner=False,
                note=f"error: {e} | df_5m={df_5m_ready} bar_count={bar_count}",
            )
            continue

        if signal is None:
            # [Fix] Use se.skip_reason which was populated from strategy.last_eval above
            strategy_reason = se.skip_reason if se is not None else "SKIP:NO_EVAL"
            notes.append(f"{name}: {strategy_reason}")
            
            print(f"[Router][{name}] no signal — {strategy_reason}", flush=True)
            log_router_event(
                recorder=recorder,
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime if hasattr(regime_result, 'regime') else context.market.regime,
                strategy_name=name,
                candidate_order=i,
                status="no_signal",
                evaluated=True,
                winner=False,
                note=f"strategy returned None | reason={strategy_reason}",
            )
            continue
        
        # ── Handle _SkipMarker: falsy sentinel with _skip_reason ──
        skip_reason = getattr(signal, '_skip_reason', None)
        if skip_reason is not None:
            notes.append(f"{name}: skip — {skip_reason}")
            print(f"[Router][{name}] skip — {skip_reason}", flush=True)
            log_router_event(
                recorder=recorder,
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime,
                strategy_name=name,
                candidate_order=i,
                status="skipped",
                evaluated=True,
                winner=False,
                note=skip_reason,
            )
            continue

        is_valid, error = signal.validate()
        if not is_valid:
            notes.append(f"{name}: invalid signal ({error})")
            # Log invalid signal
            log_router_event(
                recorder=recorder,
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime,
                strategy_name=name,
                candidate_order=i,
                status="invalid",
                evaluated=True,
                winner=False,
                signal=signal,
                note=f"invalid: {error}",
            )
            continue

        # Mark remaining candidates as shadowed
        remaining = candidates[i+1:]
        for offset, shadow_name in enumerate(remaining):
            log_router_event(
                recorder=recorder,
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime,
                strategy_name=shadow_name,
                candidate_order=i+1+offset,
                status="shadowed",
                evaluated=False,
                winner=False,
                note=f"short-circuited by winner={name}",
            )

        # Log winner
        log_router_event(
            recorder=recorder,
            timestamp=timestamp,
            symbol=symbol,
            regime=regime_result.regime,
            strategy_name=name,
            candidate_order=i,
            status="winner",
            evaluated=True,
            winner=True,
            signal=signal,
            note="router selected this signal",
        )

        # Also log signal to recorder
        if recorder is not None:
            recorder.log_signal(
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime,
                strategy_name=name,
                candidate_order=i,
                side=signal.action,
                signal_type=signal.type,
                selected=True,
                score=getattr(signal, "score", None),
                notes="router selected",
            )

        if is_flattening:
            return FuturesRouterDecision(
                action="BLOCKED",
                reason="flattening action unresolved",
                regime=regime_result.regime,
                bias=regime_result.bias,
                selected_strategy=name,
                signal=signal,
                candidates=candidates,
                notes=[*notes, "exit/partial-exit order still resolving"],
                theta_allowed=_theta_allowed,
                theta_block_reason=_theta_block_reason,
            )

        if working_orders:
            reason = "active working order unresolved"
            if _has_opposite_side_working_order(signal.action, working_orders):
                reason = "opposite-side working order unresolved"
            return FuturesRouterDecision(
                action="BLOCKED",
                reason=reason,
                regime=regime_result.regime,
                bias=regime_result.bias,
                selected_strategy=name,
                signal=signal,
                candidates=candidates,
                notes=[*notes, reason],
                theta_allowed=_theta_allowed,
                theta_block_reason=_theta_block_reason,
            )

        # ── Winner — return TRADE immediately ──
        _ts_str = str(timestamp) if timestamp else str(context.market.timestamp)
        _trace = RouterTrace(
            ts=_ts_str,
            regime=regime_result.regime,
            bias=regime_result.bias,
            selected=name,
            selected_action=getattr(signal, "action", None),
            strategies=_evals,
        )
        write_trace(_trace)
        print_trace_summary(_trace)
        
        # ── [GSD Upgrade] P1: Runtime Replay System ──────────────────────
        try:
            sys_status = get_shared_system_status().name
            replay_engine.record_bar_decision(
                bar=bar,
                regime=regime_result.regime,
                bias=regime_result.bias,
                candidates=candidates,
                winner=selected_name,
                winner_action=selected_signal.action if selected_signal else None,
                strategy_evals=_evals,
                system_status=sys_status
            )
        except Exception as re_err:
            logger.error(f"[ReplayEngine] failed to record: {re_err}")
        # ──────────────────────────────────────────────────────────────

        return FuturesRouterDecision(
                action="TRADE",
                reason=f"selected by {name}",
                regime=regime_result.regime,
                bias=regime_result.bias,
                selected_strategy=name,
                signal=signal,
                candidates=candidates,
                notes=notes,
                theta_allowed=_theta_allowed,
                theta_block_reason=_theta_block_reason,
            )

    # If we get here, no candidate produced a valid signal
    # Log that all candidates were evaluated but no winner
    if recorder is not None:
        recorder.log_router_row(
            timestamp=timestamp,
            symbol=symbol,
            regime=regime_result.regime,
            strategy_name="router",
            candidate_order=0,
            status="no_winner",
            evaluated=False,
            winner=False,
            notes=f"no eligible signal for regime {regime_result.regime}",
        )
    
    # ── Write RouterTrace — every bar gets a decision record ──
    _ts_str = str(timestamp) if timestamp else str(context.market.timestamp)
    _trace = RouterTrace(
        ts=_ts_str,
        regime=regime_result.regime,
        bias=regime_result.bias,
        selected=None,
        selected_action=None,
        strategies=_evals,
    )
    write_trace(_trace)
    print_trace_summary(_trace)
    
    # ── [GSD Upgrade] P1: Runtime Replay System ──────────────────────
    try:
        sys_status = get_shared_system_status().name
        replay_engine.record_bar_decision(
            bar=bar,
            regime=regime_result.regime,
            bias=regime_result.bias,
            candidates=candidates,
            winner=None,
            winner_action=None,
            strategy_evals=_evals,
            system_status=sys_status
        )
    except Exception as re_err:
        logger.error(f"[ReplayEngine] failed to record: {re_err}")
    # ──────────────────────────────────────────────────────────────
    
    return FuturesRouterDecision(
        action="FLAT",
        reason=f"no eligible signal for regime {regime_result.regime}",
        regime=regime_result.regime,
        bias=regime_result.bias,
        candidates=candidates,
        notes=notes,
        theta_allowed=_theta_allowed,
        theta_block_reason=_theta_block_reason,
    )
