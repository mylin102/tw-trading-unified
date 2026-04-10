"""Level 1 Unit Tests — core/strategy_registry.py"""
import pytest
import tempfile
from pathlib import Path

from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.strategy_registry import StrategyRegistry


def _make_ctx() -> StrategyContext:
    return StrategyContext(
        market=MarketData(last_bar={"Close": 35000.0}),
        position=PositionView(),
        config={},
    )


class TestStrategyRegistry:
    def test_get_unknown_returns_none(self):
        reg = StrategyRegistry()
        assert reg.get("nonexistent") is None

    def test_list_all_empty(self):
        reg = StrategyRegistry()
        assert reg.list_all() == []

    def test_manual_register_via_discovery(self):
        """Create a temp plugin and verify discovery."""
        with tempfile.TemporaryDirectory() as tmpdir:
            futures_dir = Path(tmpdir) / "futures"
            futures_dir.mkdir()

            plugin_file = futures_dir / "test_dummy.py"
            plugin_file.write_text(
                "from core.strategy_base import StrategyBase\n"
                "from core.strategy_context import StrategyContext\n"
                "\n"
                "class TestDummy(StrategyBase):\n"
                "    @property\n"
                "    def name(self):\n"
                "        return 'test_dummy'\n"
                "\n"
                "    def init(self, ctx):\n"
                "        pass\n"
                "\n"
                "    def on_bar(self, ctx):\n"
                "        return None\n"
            )

            reg = StrategyRegistry(plugin_root=tmpdir)
            reg.discover()

            assert reg.get("test_dummy") is not None
            assert len(reg.errors) == 0

    def test_import_error_handled(self):
        """A plugin that raises ImportError on import should be logged, not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            futures_dir = Path(tmpdir) / "futures"
            futures_dir.mkdir()

            broken_file = futures_dir / "broken.py"
            broken_file.write_text("raise ImportError('deliberate break')\n")

            reg = StrategyRegistry(plugin_root=tmpdir)
            reg.discover()

            assert reg.get("broken") is None
            assert "broken" in reg.errors

    def test_no_strategybase_subclass_warned(self, caplog):
        """A .py file with no StrategyBase subclass should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            futures_dir = Path(tmpdir) / "futures"
            futures_dir.mkdir()

            (futures_dir / "no_class.py").write_text("X = 42\n")

            reg = StrategyRegistry(plugin_root=tmpdir)
            reg.discover()

            assert len(reg._plugins) == 0
            # Should have logged a warning about no class found

    def test_list_all_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            futures_dir = Path(tmpdir) / "futures"
            futures_dir.mkdir()
            (futures_dir / "bad.py").write_text("raise ImportError('oops')\n")

            reg = StrategyRegistry(plugin_root=tmpdir)
            reg.discover()

            items = reg.list_all()
            bad = [i for i in items if i.get("name") == "bad"]
            assert len(bad) == 1
            assert bad[0]["available"] is False
            assert "oops" in bad[0]["error"]
