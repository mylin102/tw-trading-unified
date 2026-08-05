from pathlib import Path


def test_trading_system_pm2_uses_single_futures_ssot_config():
    source = Path("ecosystem.config.js").read_text()

    assert "main.py --config futures`" in source
    assert "main.py --config futures,futures_mtx" not in source
    assert "comma-separated config list creates multiple monitors" in source
