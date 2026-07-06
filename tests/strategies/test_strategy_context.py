"""Level 1 Unit Tests — core/strategy_context.py"""
import pytest
from core.strategy_context import StrategyContext, PositionView, MarketData


class TestStrategyContextImmutability:
    """Verify StrategyContext is frozen and cannot be mutated."""

    def test_context_is_frozen(self):
        ctx = StrategyContext(
            market=MarketData(last_bar={"Close": 35000.0}),
            position=PositionView(),
            config={},
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            ctx.position = PositionView(size=1)

    def test_position_view_isolated(self):
        pos = PositionView(size=1, entry_price=35000.0)
        ctx = StrategyContext(
            market=MarketData(last_bar={}),
            position=pos,
            config={},
        )
        # Context returns the same view — changes to the source are visible
        assert ctx.position.size == 1

    def test_market_data_readonly(self):
        ctx = StrategyContext(
            market=MarketData(last_bar={"Close": 35000.0}),
            position=PositionView(),
            config={},
        )
        with pytest.raises(Exception):
            ctx.market = MarketData(last_bar={})  # frozen field

    def test_all_fields_present(self):
        ctx = StrategyContext(
            market=MarketData(last_bar={"Close": 1.0}),
            position=PositionView(size=0),
            config={"foo": "bar"},
            bar_counter=42,
        )
        assert ctx.market is not None
        assert ctx.position is not None
        assert ctx.config == {"foo": "bar"}
        assert ctx.bar_counter == 42


class TestPositionView:
    def test_defaults(self):
        p = PositionView()
        assert p.size == 0
        assert p.entry_price == 0.0
        assert p.current_stop_loss is None
        assert p.unrealized_pnl == 0.0
        assert p.has_tp1_hit is False

    def test_long_position(self):
        p = PositionView(size=1, entry_price=35000.0)
        assert p.size == 1

    def test_short_position(self):
        p = PositionView(size=-1, entry_price=35100.0)
        assert p.size == -1


class TestMarketData:
    def test_defaults(self):
        m = MarketData(last_bar={})
        assert m.df_5m is None
        assert m.timestamp == ""
        assert m.session == 0
