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
        market=MarketData(last_bar={"Close": 100.0, "volume_spike": 2.0}, regime="NEUTRAL"),
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


# ═══════════════════════════════════════════════════════════════
# WEAK regime bias-aware routing contract tests
# ═══════════════════════════════════════════════════════════════


def test_weak_bullish_promotes_weak_bull_first():
    """WEAK + BULLISH → weak_bull_trend must appear first in candidates."""
    bull = _FakeStrategy("weak_bull_trend", None)
    bear = _FakeStrategy("weak_bear_trend", None)
    counter = _FakeStrategy("counter_vwap", None)
    registry = _FakeRegistry({"weak_bull_trend": bull, "weak_bear_trend": bear, "counter_vwap": counter})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("WEAK", "BULLISH"),
        active_strategy_name=None,
    )

    assert decision.candidates, "must have candidates"
    first = decision.candidates[0]
    assert first == "weak_bull_trend", f"expected weak_bull_trend first, got {first}"


def test_weak_short_promotes_weak_bear_first():
    """WEAK + SHORT → weak_bear_trend must appear first in candidates."""
    bull = _FakeStrategy("weak_bull_trend", None)
    bear = _FakeStrategy("weak_bear_trend", None)
    counter = _FakeStrategy("counter_vwap", None)
    registry = _FakeRegistry({"weak_bull_trend": bull, "weak_bear_trend": bear, "counter_vwap": counter})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("WEAK", "SHORT"),
        active_strategy_name=None,
    )

    assert decision.candidates, "must have candidates"
    first = decision.candidates[0]
    assert first == "weak_bear_trend", f"expected weak_bear_trend first, got {first}"


def test_weak_neutral_does_not_promote_either_weak_trend():
    """WEAK + NEUTRAL → neither weak_bull_trend nor weak_bear_trend gets priority."""
    bull = _FakeStrategy("weak_bull_trend", None)
    bear = _FakeStrategy("weak_bear_trend", None)
    counter = _FakeStrategy(
        "counter_vwap",
        Signal("SELL", "COUNTER_VWAP", stop_loss=103.0, confidence=0.8),
    )
    registry = _FakeRegistry({"weak_bull_trend": bull, "weak_bear_trend": bear, "counter_vwap": counter})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("WEAK", "NEUTRAL"),
        active_strategy_name=None,
    )

    assert decision.candidates, "must have candidates"
    first = decision.candidates[0]
    assert first not in ("weak_bull_trend", "weak_bear_trend"), \
        f"weak trend strategy {first} should not be promoted under NEUTRAL bias"


# ═══════════════════════════════════════════════════════════════
# SQUEEZE regime bias-aware fallback contract tests
# ═══════════════════════════════════════════════════════════════


def test_squeeze_bullish_appends_weak_bull_fallback():
    """SQUEEZE + BULLISH → weak_bull_trend must appear as last fallback candidate."""
    bull = _FakeStrategy("weak_bull_trend", None)
    scout = _FakeStrategy("squeeze_fire_scout", None)
    registry = _FakeRegistry({"weak_bull_trend": bull, "squeeze_fire_scout": scout})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("SQUEEZE", "BULLISH"),
        active_strategy_name=None,
    )

    assert decision.candidates, "must have candidates"
    assert "weak_bull_trend" in decision.candidates, \
        f"weak_bull_trend must appear in SQUEEZE+BULLISH candidates: {decision.candidates}"
    # weak_bull_trend should be last (fallback after squeeze-native strategies)
    assert decision.candidates[-1] == "weak_bull_trend", \
        f"weak_bull_trend should be the last fallback, got {decision.candidates[-1]}"


def test_squeeze_short_appends_weak_bear_fallback():
    """SQUEEZE + SHORT → weak_bear_trend must appear as last fallback candidate."""
    bear = _FakeStrategy("weak_bear_trend", None)
    scout = _FakeStrategy("squeeze_fire_scout", None)
    registry = _FakeRegistry({"weak_bear_trend": bear, "squeeze_fire_scout": scout})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("SQUEEZE", "SHORT"),
        active_strategy_name=None,
    )

    assert decision.candidates, "must have candidates"
    assert "weak_bear_trend" in decision.candidates, \
        f"weak_bear_trend must appear in SQUEEZE+SHORT candidates: {decision.candidates}"
    assert decision.candidates[-1] == "weak_bear_trend", \
        f"weak_bear_trend should be the last fallback, got {decision.candidates[-1]}"


def test_squeeze_neutral_no_weak_fallback():
    """SQUEEZE + NEUTRAL → must NOT include weak_bull_trend or weak_bear_trend."""
    bull = _FakeStrategy("weak_bull_trend", None)
    bear = _FakeStrategy("weak_bear_trend", None)
    scout = _FakeStrategy("squeeze_fire_scout", None)
    registry = _FakeRegistry({"weak_bull_trend": bull, "weak_bear_trend": bear, "squeeze_fire_scout": scout})

    decision = route_futures_signal(
        registry=registry,
        context=_context(),
        regime_result=_regime("SQUEEZE", "NEUTRAL"),
        active_strategy_name=None,
    )

    for strat in ("weak_bull_trend", "weak_bear_trend"):
        assert strat not in decision.candidates, \
            f"{strat} should NOT appear in SQUEEZE+NEUTRAL candidates: {decision.candidates}"


def test_weak_volume_gate_blocks_when_spike_low():
    """WEAK + volume_spike < 1.5 → candidates must be empty."""
    bull = _FakeStrategy("weak_bull_trend", None)
    bear = _FakeStrategy("weak_bear_trend", None)
    bias_test_cases = ["LONG", "SHORT"]
    for bias in bias_test_cases:
        ctx = StrategyContext(
            market=MarketData(
                last_bar={"Close": 100.0, "volume_spike": 0.8},
                regime="NEUTRAL",
            ),
            position=PositionView(size=0),
            config={},
            bar_counter=10,
        )
        decision = route_futures_signal(
            registry=_FakeRegistry({"weak_bull_trend": bull, "weak_bear_trend": bear}),
            context=ctx,
            regime_result=_regime("WEAK", bias),
            active_strategy_name=None,
        )
        assert not decision.candidates, \
            f"WEAK+{bias} with volume_spike=0.8 should have empty candidates: {decision.candidates}"
        assert any("WEAK_VOLUME_GATE" in n for n in decision.notes), \
            f"expected WEAK_VOLUME_GATE in notes: {decision.notes}"
