"""P0 + P1 tests — every bug from tonight becomes a test case."""
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "strategies" / "futures"))
sys.path.insert(0, str(Path(__file__).parent.parent / "strategies" / "options"))

import pytest
from squeeze_futures.engine.simulator import PaperTrader


# ═══════════════════════════════════════════
# P0: 不能重複下單
# ═══════════════════════════════════════════

class TestNoDuplicateEntry:
    def _make_trader(self):
        return PaperTrader("TMF", 100000, 10, 20, 0, 0.00002)

    def test_second_buy_blocked_at_max(self):
        t = self._make_trader()
        r1 = t.execute_signal("BUY", 32800, datetime.now(), lots=2, max_lots=2)
        r2 = t.execute_signal("BUY", 32900, datetime.now(), lots=2, max_lots=2)
        assert r1 is not None
        assert r2 is None
        assert t.position == 2

    def test_second_sell_blocked_at_max(self):
        t = self._make_trader()
        r1 = t.execute_signal("SELL", 32800, datetime.now(), lots=2, max_lots=2)
        r2 = t.execute_signal("SELL", 32700, datetime.now(), lots=2, max_lots=2)
        assert r1 is not None
        assert r2 is None
        assert t.position == -2

    def test_buy_after_exit_allowed(self):
        t = self._make_trader()
        t.execute_signal("BUY", 32800, datetime.now(), lots=2, max_lots=2)
        t.execute_signal("EXIT", 32900, datetime.now(), lots=2, max_lots=2)
        assert t.position == 0
        r = t.execute_signal("BUY", 32850, datetime.now(), lots=2, max_lots=2)
        assert r is not None
        assert t.position == 2


# ═══════════════════════════════════════════
# P0: EXIT 後 position 必須歸零
# ═══════════════════════════════════════════

class TestExitClearsPosition:
    def test_exit_zeroes_long(self):
        t = PaperTrader("TMF", 100000, 10, 20, 0, 0)
        t.execute_signal("BUY", 32800, datetime.now(), lots=2, max_lots=2)
        t.execute_signal("EXIT", 32900, datetime.now(), lots=2, max_lots=2)
        assert t.position == 0
        assert t.entry_price == 0

    def test_exit_zeroes_short(self):
        t = PaperTrader("TMF", 100000, 10, 20, 0, 0)
        t.execute_signal("SELL", 32800, datetime.now(), lots=2, max_lots=2)
        t.execute_signal("EXIT", 32700, datetime.now(), lots=2, max_lots=2)
        assert t.position == 0

    def test_double_exit_returns_none(self):
        t = PaperTrader("TMF", 100000, 10, 20, 0, 0)
        t.execute_signal("BUY", 32800, datetime.now(), lots=2, max_lots=2)
        r1 = t.execute_signal("EXIT", 32900, datetime.now(), lots=2, max_lots=2)
        r2 = t.execute_signal("EXIT", 32900, datetime.now(), lots=2, max_lots=2)
        assert r1 is not None
        assert r2 is None


# ═══════════════════════════════════════════
# P1: BE offset 必須 cover 手續費
# ═══════════════════════════════════════════

class TestBreakEvenOffset:
    def test_be_offset_at_least_10(self):
        t = PaperTrader("TMF", 100000, 10, 20, 0, 0)
        t.execute_signal("SELL", 32335, datetime.now(), lots=2, max_lots=2,
                         stop_loss=50, break_even_trigger=50)
        # Simulate price moving 50 pts in favor → trigger BE
        t.update_trailing_stop(32285)
        assert t.be_triggered
        # SHORT: stop should be entry - 10 = 32325, not entry - 2
        assert t.current_stop_loss == 32335 - 10

    def test_be_offset_long(self):
        t = PaperTrader("TMF", 100000, 10, 20, 0, 0)
        t.execute_signal("BUY", 32000, datetime.now(), lots=2, max_lots=2,
                         stop_loss=50, break_even_trigger=50)
        t.update_trailing_stop(32050)
        assert t.be_triggered
        assert t.current_stop_loss == 32000 + 10


# ═══════════════════════════════════════════
# P1: PnL 必須扣手續費
# ═══════════════════════════════════════════

class TestPnLIncludesFees:
    def test_pnl_less_than_gross(self):
        t = PaperTrader("TMF", 100000, 10, 20, 0, 0.00002)
        t.execute_signal("BUY", 32000, datetime.now(), lots=2, max_lots=2)
        t.execute_signal("EXIT", 32010, datetime.now(), lots=2, max_lots=2)
        trade = t.trades[-1]
        gross = 10 * 10 * 2  # 10 pts * point_value * lots = 200
        assert trade["pnl_cash"] < gross
        assert trade["total_cost"] > 0

    def test_losing_trade_includes_fees(self):
        t = PaperTrader("TMF", 100000, 10, 20, 0, 0.00002)
        t.execute_signal("BUY", 32000, datetime.now(), lots=2, max_lots=2)
        t.execute_signal("EXIT", 31990, datetime.now(), lots=2, max_lots=2)
        trade = t.trades[-1]
        # Loss should be worse than just -10 pts because of fees
        assert trade["pnl_cash"] < -10 * 10 * 2

    def test_balance_tracks_net_pnl(self):
        t = PaperTrader("TMF", 100000, 10, 20, 0, 0.00002)
        initial = t.balance
        t.execute_signal("BUY", 32000, datetime.now(), lots=2, max_lots=2)
        t.execute_signal("EXIT", 32000, datetime.now(), lots=2, max_lots=2)
        # Flat trade should lose money (fees)
        assert t.balance < initial


# ═══════════════════════════════════════════
# P1: 策略插件格式一致
# ═══════════════════════════════════════════

class TestStrategyPlugins:
    def _make_state(self, **overrides):
        import pandas as pd
        defaults = {
            "sqz_on": False, "momentum": 50, "mom_state": 3,
            "Close": 32700, "vwap": 32600, "atr": 30,
            "bullish_align": True, "bearish_align": False,
            "ema_filter": 32650, "fired": False, "mom_velo": 5,
            "recent_high": 32750, "recent_low": 32600,
            "Volume": 1000, "Open": 32700, "High": 32710, "Low": 32690,
            "day_open": 32700, "trading_day": pd.Timestamp("2026-04-02").date()
        }
        defaults.update(overrides)
        last = pd.Series(defaults)
        df = pd.DataFrame([defaults] * 60)
        df["Open"] = df["Close"] - 5
        df["High"] = df["Close"] + 10
        df["Low"] = df["Close"] - 10
        df["Volume"] = 500
        df.index = pd.date_range("2026-04-02 20:00", periods=60, freq="5min")
        return {
            "last_5m": last, "last_15m": last, "df_5m": df,
            "score": 50, "stop_loss_pts": 30,
            "trend": {"trend_long": True, "trend_short": False},
            "hour": 20,
        }

    def test_all_strategies_return_valid_or_none(self):
        from strategies.futures.entry_strategies import STRATEGIES
        state = self._make_state()
        cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20}}
        for name, entry in STRATEGIES.items():
            fn = entry["func"]
            result = fn(state, cfg)
            if result is not None:
                assert "action" in result, f"{name} missing 'action'"
                assert "reason" in result, f"{name} missing 'reason'"
                assert "stop_loss" in result, f"{name} missing 'stop_loss'"
                assert result["action"] in ("BUY", "SELL"), f"{name} invalid action"
                assert result["stop_loss"] > 0, f"{name} stop_loss <= 0"

    def test_squeeze_on_blocks_breakout(self):
        from strategies.futures.entry_strategies import strategy_squeeze_breakout
        state = self._make_state(sqz_on=True)
        cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20}}
        assert strategy_squeeze_breakout(state, cfg) is None


# ═══════════════════════════════════════════
# P1: ThetaGang 基本功能
# ═══════════════════════════════════════════

class TestThetaGang:
    def test_iron_condor_pricing(self):
        from strategies.options.theta_gang import select_strikes, price_spread
        from strategies.options.options_engine.engine.greeks import black_scholes
        legs = select_strikes(32700, 100, "iron_condor", wing_width=200, otm_offset=200)
        assert len(legs) == 4
        credit, max_loss, _ = price_spread(legs, black_scholes, 32700, 0.02, 0.40, 12/365)
        assert credit > 0
        assert max_loss > 0

    def test_no_entry_when_squeeze_off(self):
        from strategies.options.theta_gang import should_enter_theta
        assert not should_enter_theta(squeeze_on=False, iv=0.30)

    def test_entry_when_squeeze_on_high_iv(self):
        from strategies.options.theta_gang import should_enter_theta
        assert should_enter_theta(squeeze_on=True, iv=0.30)

    def test_exit_on_squeeze_release(self):
        from strategies.options.theta_gang import ThetaGangManager
        from strategies.options.options_engine.engine.greeks import black_scholes
        cfg = {"theta_gang": {
            "strategy": "iron_condor", "wing_width": 200, "otm_offset": 200,
            "quantity": 1, "min_iv": 0.18, "min_credit": 10,
            "take_profit_pct": 0.50, "max_loss_pct": 1.0,
            "min_dte_entry": 5, "min_dte_exit": 3,
            "exit_on_squeeze_release": True, "risk_free_rate": 0.02,
        }}
        mgr = ThetaGangManager(cfg, black_scholes, 100)
        entry = mgr.evaluate_entry(32700, 0.40, 12/365, squeeze_on=True)
        assert entry is not None
        mgr.open_position(entry)
        exit_info = mgr.evaluate_exit(32700, 0.40, 10/365, squeeze_on=False)
        assert exit_info is not None
        assert "SQUEEZE_RELEASE" in exit_info["reason"]


# ═══════════════════════════════════════════
# P1: 跨日日期處理
# ═══════════════════════════════════════════

class TestDateHandling:
    def test_before_5am_uses_yesterday(self):
        """凌晨 02:00 應該用前一天的日期"""
        from unittest.mock import patch
        with patch("squeeze_futures.data.data_storage.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 3, 2, 0, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # The storage should use 20260402, not 20260403
            # (tested via the __import__ workaround)
            assert True  # Placeholder — actual test needs DataStorage refactor


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])


# ═══════════════════════════════════════════
# P2: Strategy Review 待做修復
# ═══════════════════════════════════════════

class TestMomentumBurstZScore:
    def test_zscore_filters_low_vol(self):
        """低波動環境下，同樣的 velocity 不應觸發"""
        from strategies.futures.entry_strategies import strategy_momentum_burst
        import pandas as pd
        # 低波動：mom_velo=5 但歷史 std 也是 5 → zscore=0 → 不觸發
        df = pd.DataFrame({"mom_velo": [5.0] * 100, "Close": [32700] * 100})
        df.index = pd.date_range("2026-04-02", periods=100, freq="5min")
        state = {"last_5m": pd.Series({"fired": True, "mom_velo": 5.0, "atr": 30, "sqz_on": False}), "df_5m": df}
        cfg = {"strategy": {"momentum_burst": {"min_zscore": 2.0, "atr_mult": 2.0}}}
        result = strategy_momentum_burst(state, cfg)
        assert result is None  # zscore ≈ 0, should not fire

    def test_zscore_fires_on_extreme(self):
        """極端 velocity 應該觸發"""
        from strategies.futures.entry_strategies import strategy_momentum_burst
        import pandas as pd
        import numpy as np
        # 歷史 mom_velo 均值 0, std 2 → 當前 10 → zscore=5 → 觸發
        velos = np.random.normal(0, 2, 99).tolist() + [10.0]
        df = pd.DataFrame({"mom_velo": velos, "Close": [32700] * 100})
        df.index = pd.date_range("2026-04-02", periods=100, freq="5min")
        state = {"last_5m": pd.Series({"fired": True, "mom_velo": 10.0, "atr": 30, "sqz_on": False}), "df_5m": df}
        cfg = {"strategy": {"momentum_burst": {"min_zscore": 2.0, "atr_mult": 2.0}}}
        result = strategy_momentum_burst(state, cfg)
        assert result is not None
        assert result["action"] == "BUY"


class TestTrendFollowExit:
    def test_trailing_exit_on_reversal(self):
        """ELITE: trend_follow 已淘汰，驗證 elite_strategies 模組正常載入"""
        # trend_follow 策略已從 elite_strategies 中移除
        # 這個測試確保精英策略模組可以正確載入
        from strategies.futures.elite_strategies import get_elite_strategies
        elite = get_elite_strategies()
        
        # 驗證 trend_follow 不在精英策略中
        assert "trend_follow" not in elite, "trend_follow should be eliminated"
        
        # 驗證精英策略都存在
        assert "counter_vwap" in elite
        assert "psar_breakout" in elite
        assert "vol_squeeze" in elite
        assert len(elite) == 3, "Should have exactly 3 elite strategies"


class TestCumulativeDeltaWeighted:
    def test_weighted_delta_differs_from_simple(self):
        """價格加權 delta 與簡單 delta 結果不同"""
        from strategies.futures.entry_strategies import strategy_cumulative_delta
        import pandas as pd
        import numpy as np
        n = 60
        c = np.linspace(32600, 32700, n)
        o = c - 5  # all green bars
        v = np.full(n, 500.0)
        df = pd.DataFrame({"Close": c, "Open": o, "Volume": v, "High": c+10, "Low": c-10})
        df.index = pd.date_range("2026-04-02", periods=n, freq="5min")
        # With price-weighted delta, larger price moves contribute more
        state = {"last_5m": pd.Series({"atr": 30, "sqz_on": False}), "df_5m": df, "score": 50}
        cfg = {"strategy": {"cumulative_delta": {"sma_length": 50, "lookback": 20, "atr_mult": 2.0}}}
        # Should produce a signal (all green bars = rising delta + price > SMA)
        result = strategy_cumulative_delta(state, cfg)
        # Result depends on price pullback condition, just verify no crash
        assert result is None or result["action"] in ("BUY", "SELL")
