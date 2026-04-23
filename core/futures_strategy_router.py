"""
Deterministic routing for futures strategy signals.

This router sits between bar-level regime detection and execution:
- strategies still generate their own signals
- the router decides which strategies are allowed to speak on a bar
- execution remains owned by monitor / OrderManager / PaperTrader
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .attribution_recorder import AttributionRecorder, log_router_event
from .futures_bar_regime import FuturesBarRegimeResult
from core.signal import Signal
from core.strategy_context import StrategyContext


class StrategyLookup(Protocol):
    def get(self, name: str) -> Any: ...


@dataclass(frozen=True)
class FuturesRouterConfig:
    """Routing policy for futures entry signals."""

    trend_strategies: tuple[str, ...] = ("adaptive_orb",)
    weak_strategies: tuple[str, ...] = ("adaptive_orb", "counter_vwap", "spring_upthrust", "kbar_feature", "calendar_condor", "calendar_condor_v2")
    stretched_strategies: tuple[str, ...] = ("counter_vwap", "spring_upthrust")
    squeeze_strategies: tuple[str, ...] = ()
    countertrend_strategies: tuple[str, ...] = ("counter_vwap", "spring_upthrust")
    hard_block_countertrend_in_trend: bool = True


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
        if active_strategy_name:
            base.insert(0, active_strategy_name)
        return _dedupe(base)

    if regime == "STRETCHED":
        base = list(config.stretched_strategies)
        if active_strategy_name:
            base.insert(0, active_strategy_name)
        return _dedupe(base)

    if regime == "SQUEEZE":
        base = list(config.squeeze_strategies)
        if active_strategy_name:
            base.insert(0, active_strategy_name)
        return _dedupe(base)

    return _dedupe([active_strategy_name] if active_strategy_name else [])


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

    cfg = router_config or FuturesRouterConfig()
    notes: list[str] = []
    working_orders = list(current_working_orders or [])
    
    # Extract timestamp and symbol for attribution logging
    timestamp = context.market.timestamp
    symbol = context.market.last_bar.get("symbol", "TX") if context.market.last_bar else "TX"

    if context.position.size != 0:
        return FuturesRouterDecision(
            action="BLOCKED",
            reason=f"position already open ({context.position.size})",
            regime=regime_result.regime,
            bias=regime_result.bias,
            notes=["router only emits fresh entry decisions while flat"],
        )

    candidates = _strategy_order_for_regime(regime_result, active_strategy_name, cfg)
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
        )

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
            continue

        if prepare_strategy is not None:
            prepare_strategy(name, strategy)

        signal = strategy.on_bar(context)
        if signal is None:
            notes.append(f"{name}: no signal")
            # Log no signal
            log_router_event(
                recorder=recorder,
                timestamp=timestamp,
                symbol=symbol,
                regime=regime_result.regime,
                strategy_name=name,
                candidate_order=i,
                status="no_signal",
                evaluated=True,
                winner=False,
                note="strategy returned None",
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
            )

        return FuturesRouterDecision(
            action="TRADE",
            reason=f"selected by {name}",
            regime=regime_result.regime,
            bias=regime_result.bias,
            selected_strategy=name,
            signal=signal,
            candidates=candidates,
            notes=notes,
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
    
    return FuturesRouterDecision(
        action="FLAT",
        reason=f"no eligible signal for regime {regime_result.regime}",
        regime=regime_result.regime,
        bias=regime_result.bias,
        candidates=candidates,
        notes=notes,
    )
