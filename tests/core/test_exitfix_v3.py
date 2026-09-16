from datetime import datetime, timedelta
from pathlib import Path


SRC = Path(__file__).resolve().parents[2] / "strategies/stocks/monitor.py"


def _run_source():
    s = SRC.read_text()
    return s[s.index("    def run_iteration"):s.index("    def check_risk")]


def _monitor():
    from strategies.stocks.monitor import StockMonitor
    m = StockMonitor.__new__(StockMonitor)
    m.positions = {}
    m.total_budget = 300000
    m.capital_per_trade = 150000
    m.strat_name = "mean_reversion"
    m.mode_tag = "PAPER"
    m.daily_pnl = 0
    m.consecutive_losses = 0
    m.ledger_path = Path("/tmp/no-exitfix-ledger.csv")
    m._log_trade = lambda *a, **k: None
    m._save_position_state_to_disk = lambda: None
    m.get_current_exposure = lambda: sum(
        p["qty"] * p["entry_price"] for p in m.positions.values())
    return m


def test_t1_held_position_risk_is_before_entry_gate():
    s = _run_source()
    assert s.index("self.check_risk(ticker") < s.index("should_trade, tf_details")


def test_t2_cache_still_reaches_exit():
    s = _run_source()
    assert s.index("self.check_risk(ticker") < s.rindex('if data_source == "cache"')
    assert "if ticker in self.positions:" in s


def test_t3_cache_has_no_entry_path():
    s = _run_source()
    assert s.index('if data_source == "cache"') < s.index("should_trade, tf_details")


def test_t4_false_regime_cannot_enter():
    s = _run_source()
    assert "if not should_trade:" in s


def test_t5_exit_check_is_not_gated_by_cache():
    s = _run_source()
    assert s.index("self.check_risk(ticker") < s.rindex('if data_source == "cache"')


def test_t6_same_day_odd_lot_is_silent():
    m = _monitor()
    m.positions["2330"] = {"qty": 500, "entry_price": 100,
                            "entry_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    m.execute_trade("2330", "SELL", 99, "ALL", "STOP")
    assert "2330" in m.positions


def test_t7_next_day_odd_lot_exit_allowed():
    m = _monitor()
    m.positions["2330"] = {"qty": 500, "entry_price": 100,
                            "entry_ts": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")}
    m.execute_trade("2330", "SELL", 99, "ALL", "STOP")
    assert "2330" not in m.positions


def test_t8_whole_lot_intraday_exit_regression():
    m = _monitor()
    m.positions["2330"] = {"qty": 1000, "entry_price": 100,
                            "entry_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    m.execute_trade("2330", "SELL", 99, "ALL", "STOP")
    assert "2330" not in m.positions


def test_t9_day_strategy_entry_is_whole_lot():
    m = _monitor()
    m.execute_trade("2330", "BUY", 10, "SCOUT", "SIGNAL")
    assert m.positions["2330"]["qty"] == 1000


def test_t10_unaffordable_day_entry_does_not_downgrade_to_odd():
    m = _monitor()
    m.execute_trade("2330", "BUY", 200, "SCOUT", "SIGNAL")
    assert "2330" not in m.positions


def test_t11_f7_cap_total_and_two_holdings():
    m = _monitor()
    assert m.capital_per_trade == 150000 and m.total_budget == 300000
    m.execute_trade("2330", "BUY", 10, "SCOUT", "SIGNAL")
    m.execute_trade("2317", "BUY", 10, "SCOUT", "SIGNAL")
    m.execute_trade("2454", "BUY", 10, "SCOUT", "SIGNAL")
    assert len(m.positions) == 2
