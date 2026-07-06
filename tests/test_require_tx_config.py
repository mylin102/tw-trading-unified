import yaml
from strategies.futures.monitor import FuturesMonitor


def test_require_tx_config(tmp_path):
    cfg = {"monitoring": {"require_tx": False}}
    p = tmp_path / "futures_test.yaml"
    p.write_text(yaml.dump(cfg), encoding="utf-8")
    fm = FuturesMonitor(api=None, config_path=str(p), dry_run=True)
    assert fm.MONITOR.get("require_tx", True) is False
