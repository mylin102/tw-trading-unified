from core.futures_bar_regime import FuturesBarRegimeResult
from core.futures_strategy_router import (
    FuturesRouterConfig,
    route_futures_signal,
)
from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import MarketData, PositionView, StrategyContext


class _FakeStrategy(StrategyBase):
    def __init__(self, name: str, signal: Signal | None):
        self._name = name
        self._signal = signal

    @property
    def name(self) -> str:
        return self._name

    def init(self, context: StrategyContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> Signal | None:
        return self._signal


class _FakeRegistry:
    def __init__(self, strategies: dict[str, StrategyBase]):
        self._strategies = strategies

    def get(self, name: str):
        return self._strategies.get(name)


def _context(position_size: int = 0) -> StrategyContext:
    return StrategyContext(
        market=MarketData(last_bar={"Close": 100.0}, regime="NEUTRAL"),
        position=PositionView(size=position_size),
        config={},
        bar_counter=10,
    )


def _regime(regime: str, bias: str = "LONG") -> FuturesBarRegimeResult:
    return FuturesBarRegimeResult(
        regime=regime,
        bias=bias,
        confidence=0.8,
        reasons=["test"],
        session_regime="TRENDING",
    )


def test_trend_routes_to_first_valid_trend_strategy_only():
    adaptive = _FakeStrategy(
        "adaptive_orb",
        Signal("BUY", "ADAPTIVE_TREND_V3", stop_loss=95.0, confidence=0.8),
    )
    spring = _FakeStrategy(
        "spring_upthrust",
        Signal("SELL", "UPTHRUST", stop_loss=105.0, confidence=0.7),
    )
    registry = _FakeRegistry(
        {"adaptive_orb": adaptive, "spring_upthrust": spring}
    )

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("TREND", "LONG"),
        active_strategy_name="spring_upthrust",
    )

    assert decision.is_trade
    assert decision.selected_strategy == "adaptive_orb"
    assert decision.signal is not None
    assert decision.signal.reason == "ADAPTIVE_TREND_V3"
    assert "spring_upthrust" not in decision.candidates


def test_weak_regime_allows_mean_reversion_candidate_after_active_strategy():
    adaptive = _FakeStrategy("adaptive_orb", None)
    counter = _FakeStrategy(
        "counter_vwap",
        Signal("SELL", "COUNTER_VWAP", stop_loss=103.0, confidence=0.8),
    )
    registry = _FakeRegistry({"adaptive_orb": adaptive, "counter_vwap": counter})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("WEAK", "SHORT"),
        active_strategy_name="adaptive_orb",
    )

    assert decision.is_trade
    assert decision.selected_strategy == "counter_vwap"
    assert any("policy:adaptive_orb=BLOCK" in n for n in decision.notes)


def test_squeeze_is_explicit_no_trade_by_default():
    registry = _FakeRegistry({})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("SQUEEZE", "NEUTRAL"),
        active_strategy_name=None,
    )

    assert not decision.is_trade
    assert decision.action == "FLAT"
    assert "no eligible signal" in decision.reason
    assert decision.candidates == ["squeeze_fire_scout", "range_mean_reversion_v1", "adaptive_orb_v15"]


def test_working_order_blocks_new_entry_even_if_strategy_wants_to_trade():
    adaptive = _FakeStrategy(
        "adaptive_orb",
        Signal("BUY", "ADAPTIVE_TREND_V3", stop_loss=95.0, confidence=0.8),
    )
    registry = _FakeRegistry({"adaptive_orb": adaptive})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("TREND", "LONG"),
        active_strategy_name="adaptive_orb",
        current_working_orders=[type("Order", (), {"side": "BUY"})()],
    )

    assert not decision.is_trade
    assert decision.action == "BLOCKED"
    assert "working order" in decision.reason
    assert decision.selected_strategy == "adaptive_orb"


def test_opposite_side_working_order_uses_more_specific_block_reason():
    adaptive = _FakeStrategy(
        "adaptive_orb",
        Signal("BUY", "ADAPTIVE_TREND_V3", stop_loss=95.0, confidence=0.8),
    )
    registry = _FakeRegistry({"adaptive_orb": adaptive})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("TREND", "LONG"),
        active_strategy_name="adaptive_orb",
        current_working_orders=[type("Order", (), {"side": "SELL"})()],
    )

    assert not decision.is_trade
    assert decision.action == "BLOCKED"
    assert decision.reason == "opposite-side working order unresolved"


def test_flattening_blocks_new_entry_after_signal_selection():
    adaptive = _FakeStrategy(
        "adaptive_orb",
        Signal("BUY", "ADAPTIVE_TREND_V3", stop_loss=95.0, confidence=0.8),
    )
    registry = _FakeRegistry({"adaptive_orb": adaptive})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("TREND", "LONG"),
        active_strategy_name="adaptive_orb",
        is_flattening=True,
    )

    assert not decision.is_trade
    assert decision.action == "BLOCKED"
    assert decision.reason == "flattening action unresolved"
    assert decision.selected_strategy == "adaptive_orb"


def test_open_position_blocks_fresh_entry():
    adaptive = _FakeStrategy(
        "adaptive_orb",
        Signal("BUY", "ADAPTIVE_TREND_V3", stop_loss=95.0, confidence=0.8),
    )
    registry = _FakeRegistry({"adaptive_orb": adaptive})

    decision = route_futures_signal(
        registry=registry,
        context=_context(position_size=1),
        regime_result=_regime("TREND", "LONG"),
        active_strategy_name="adaptive_orb",
    )

    assert not decision.is_trade
    assert decision.action == "BLOCKED"
    assert "position already open" in decision.reason


def test_invalid_signal_is_skipped_and_router_returns_flat_when_no_other_candidate():
    broken = _FakeStrategy(
        "adaptive_orb",
        Signal("BUY", "", stop_loss=0.0, confidence=0.8),
    )
    registry = _FakeRegistry({"adaptive_orb": broken})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("TREND", "LONG"),
        active_strategy_name="adaptive_orb",
        router_config=FuturesRouterConfig(trend_strategies=("adaptive_orb",)),
    )

    assert not decision.is_trade
    assert decision.action == "FLAT"
    assert "invalid signal" in " ".join(decision.notes)
