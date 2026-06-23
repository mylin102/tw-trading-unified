import ast
from pathlib import Path
from types import SimpleNamespace
import pandas as pd

from core.live_readiness import get_readiness_items, get_readiness_summary
from core.dashboard_positions import describe_options_order_truth, summarize_combo_legs


def test_get_readiness_items_handles_check_all_tuple():
    check_output = (
        False,
        {
            "Environment": SimpleNamespace(passed=False, message="Missing env vars"),
            "Directories": SimpleNamespace(passed=True, message="OK"),
        },
    )

    items = get_readiness_items(check_output)
    status, passed, total = get_readiness_summary(check_output)

    assert [(item.name, item.passed, item.detail) for item in items] == [
        ("Environment", False, "Missing env vars"),
        ("Directories", True, "OK"),
    ]
    assert (status, passed, total) == ("DEGRADED", 1, 2)


def test_dashboard_uses_readiness_items_helper():
    src = Path("ui/dashboard.py").read_text()

    assert "get_readiness_items" in src
    assert "for r in readiness_items:" in src
    # 2026-06-23 Gemini CLI: Updated assertion to match current status label in ui/dashboard.py
    assert "DEGRADED: STALE DATA" in src


def test_autostart_no_longer_manages_main_py():
    src = Path("autostart.sh").read_text()

    assert 'graceful_kill "main.py"' not in src
    assert '"$UNIFIED_DIR/main.py"' not in src
    assert 'MAIN_PIDS=' not in src
    assert '[❌期貨停]' not in src
    assert '[✅期貨]' not in src


def test_autostart_still_launches_dashboards_and_tracks_minutes():
    src = Path("autostart.sh").read_text()

    assert 'scripts/restart_dashboard.sh' in src
    assert 'streamlit run ui/backtest_dashboard.py' in src
    assert 'MM=$(date +%M)' in src


def test_restart_dashboard_script_has_preflight_and_health_check():
    src = Path("scripts/restart_dashboard.sh").read_text()

    assert "build_stock_orders_from_trades" in src
    assert "resolve_stock_orders_file" in src
    assert "latest_indicator_close" in src
    assert "curl -fsS" in src
    assert "lsof -ti tcp:$PORT" in src


def test_pm2_keeps_trading_system_only():
    src = Path("ecosystem.config.js").read_text()

    assert 'name: "trading-system"' in src
    assert 'script: "main.py"' in src
    assert 'name: "trading-dashboard"' not in src
    assert 'name: "backtest-dashboard"' not in src


def test_main_keeps_single_instance_pid_lock():
    src = Path("main.py").read_text()

    assert 'lock_file = "/tmp/tw_trading_unified.pid"' in src
    assert 'Another main.py instance is running' in src
    assert "ensure_single_instance()" in src


def test_options_monitor_initializes_exchange_fee_per_side():
    src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
    mod = ast.parse(src)
    cls = next(
        node for node in mod.body
        if isinstance(node, ast.ClassDef) and node.name == "ShioajiOptionsSmartMonitor"
    )
    init_fn = next(
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )

    assigned_attrs = {
        target.attr
        for node in ast.walk(init_fn)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }

    assert "exchange_fee_per_side" in assigned_attrs


def test_dashboard_combo_truth_metadata_runtime_path_uses_sample_export():
    sample_orders = pd.DataFrame(
        [
            {
                "order_id": "ORD-COMBO-001",
                "status": "filled",
                "truth_source": "broker_combo",
                "combo_strategy": "bull_put_spread",
                "combo_legs": [
                    {"action": "SELL", "side": "P", "strike": 22800},
                    {"action": "BUY", "side": "P", "strike": 22600},
                ],
            }
        ]
    )

    truth_results = sample_orders.apply(
        lambda row: describe_options_order_truth(row, orders_rebuilt_from_ledger=False),
        axis=1,
    )
    sample_orders["truth_source"] = truth_results.apply(lambda result: result["truth_source"])
    sample_orders["真實來源"] = truth_results.apply(lambda result: result["badge"])
    sample_orders["組合腿摘要"] = sample_orders["combo_legs"].apply(summarize_combo_legs)

    src = Path("ui/dashboard.py").read_text()

    assert sample_orders.loc[0, "truth_source"] == "broker_combo"
    assert sample_orders.loc[0, "真實來源"] == "✅ broker_combo"
    assert sample_orders.loc[0, "組合腿摘要"] == "SELL P22800 | BUY P22600"
    assert "describe_options_order_truth" in src
    assert "summarize_combo_legs" in src
    assert "broker_combo 為券商複式單真實來源" in src
