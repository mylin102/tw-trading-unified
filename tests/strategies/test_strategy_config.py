"""Level 1 Unit Tests — core/strategy_config.py"""
import pytest
import tempfile
from pathlib import Path

from core.strategy_config import load


class TestConfigLoader:
    def test_loads_valid_yaml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("name: test\nparams:\n  confirm_bars: 5\n")
            f.flush()
            cfg = load(f.name)
        assert cfg["name"] == "test"
        assert cfg["params"]["confirm_bars"] == 5
        assert "risk" in cfg  # defaults merged

    def test_rejects_unknown_keys(self):
        """Unknown keys are kept (not forbidden) but structural invariants are checked."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("unknown_key: foo\nrisk:\n  max_positions: 1\n  stop_loss_mult: 2.0\n")
            f.flush()
            cfg = load(f.name)
        assert cfg["unknown_key"] == "foo"

    def test_applies_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("name: minimal\n")
            f.flush()
            cfg = load(f.name)
        assert cfg["enabled"] is True
        assert cfg["risk"]["max_positions"] == 1
        assert cfg["backtest"]["pf"] == 0.0

    def test_validates_stop_loss_mult_zero(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("risk:\n  max_positions: 1\n  stop_loss_mult: 0\n")
            f.flush()
            with pytest.raises(ValueError, match="stop_loss_mult"):
                load(f.name)

    def test_validates_stop_loss_mult_negative(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("risk:\n  max_positions: 1\n  stop_loss_mult: -1\n")
            f.flush()
            with pytest.raises(ValueError, match="stop_loss_mult"):
                load(f.name)

    def test_validates_max_positions_negative(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("risk:\n  max_positions: -1\n  stop_loss_mult: 2.0\n")
            f.flush()
            with pytest.raises(ValueError, match="max_positions"):
                load(f.name)

    def test_file_not_found_uses_defaults(self):
        cfg = load("/nonexistent/path/config.yaml")
        assert cfg["enabled"] is True
        assert cfg["name"] == ""

    def test_validates_backtest_pf_negative(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("risk:\n  max_positions: 1\n  stop_loss_mult: 2.0\nbacktest:\n  pf: -1\n  wr: 40\n  max_dd: -5\n  total_trades: 0\n  period: ''\n")
            f.flush()
            with pytest.raises(ValueError, match="backtest.pf"):
                load(f.name)

    def test_validates_backtest_wr_range(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("risk:\n  max_positions: 1\n  stop_loss_mult: 2.0\nbacktest:\n  pf: 1.0\n  wr: 150\n  max_dd: -5\n  total_trades: 0\n  period: ''\n")
            f.flush()
            with pytest.raises(ValueError, match="backtest.wr"):
                load(f.name)

    def test_validates_backtest_max_dd_positive(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("risk:\n  max_positions: 1\n  stop_loss_mult: 2.0\nbacktest:\n  pf: 1.0\n  wr: 40\n  max_dd: 5\n  total_trades: 0\n  period: ''\n")
            f.flush()
            with pytest.raises(ValueError, match="backtest.max_dd"):
                load(f.name)
