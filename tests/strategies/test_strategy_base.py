"""Level 1 Unit Tests — core/strategy_base.py"""
import pytest
from abc import ABC

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext, PositionView, MarketData


def _make_ctx() -> StrategyContext:
    return StrategyContext(
        market=MarketData(last_bar={"Close": 35000.0}),
        position=PositionView(),
        config={},
    )


class TestStrategyBaseABC:
    """Verify ABC enforcement and default hook behaviour."""

    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            StrategyBase()

    def test_is_abc(self):
        assert issubclass(StrategyBase, ABC)

    def test_missing_on_bar_raises(self):
        class Incomplete(StrategyBase):
            @property
            def name(self):
                return "incomplete"

            def init(self, ctx):
                pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_missing_init_raises(self):
        class Incomplete(StrategyBase):
            @property
            def name(self):
                return "incomplete"

            def on_bar(self, ctx):
                return None

        with pytest.raises(TypeError):
            Incomplete()

    def test_concrete_subclass_succeeds(self):
        class Ok(StrategyBase):
            @property
            def name(self):
                return "ok"

            def init(self, ctx):
                pass

            def on_bar(self, ctx):
                return None

        instance = Ok()
        assert isinstance(instance, StrategyBase)

    def test_metadata_default(self):
        class Minimal(StrategyBase):
            @property
            def name(self):
                return "minimal"

            def init(self, ctx):
                pass

            def on_bar(self, ctx):
                return None

        m = Minimal().metadata
        assert "asset_class" in m
        assert "version" in m
        assert "backtest_pf" in m

    def test_on_tick_default_noop(self):
        class Minimal(StrategyBase):
            @property
            def name(self):
                return "minimal"

            def init(self, ctx):
                pass

            def on_bar(self, ctx):
                return None

        instance = Minimal()
        instance.on_tick({"close": 35000})  # should not raise

    def test_cleanup_default_noop(self):
        class Minimal(StrategyBase):
            @property
            def name(self):
                return "minimal"

            def init(self, ctx):
                pass

            def on_bar(self, ctx):
                return None

        instance = Minimal()
        instance.cleanup()  # should not raise

    def test_config_schema_default(self):
        class Minimal(StrategyBase):
            @property
            def name(self):
                return "minimal"

            def init(self, ctx):
                pass

            def on_bar(self, ctx):
                return None

        assert Minimal().config_schema is None
