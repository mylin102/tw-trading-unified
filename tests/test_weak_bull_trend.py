"""
Test weak_bull_trend strategy — WEAK Regime 防守型多頭策略測試

測試重點:
1. 只在 regime=WEAK/CHOP + bias=BULLISH 時進場
2. 價格必須在 VWAP 之上
3. 溫和動能確認：3 <= mom_velo <= 25
4. EMA 多頭排列：close > ema_fast > ema_slow
5. 突破強度門檻
6. 嚴格止損：0.8 ATR
7. 時間止損：12 分鐘無獲利出場
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timedelta

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from strategies.plugins.futures.active.weak_bull_trend import WeakBullTrend
from core.strategy_context import PositionView, StrategyContext
from core.signal import Signal


class MockMarket:
    def __init__(self, bar):
        self.last_bar = bar
        self.timestamp = bar.get("timestamp") if bar else None
        self.regime = bar.get("regime", "UNKNOWN")
        self.df_5m = None


def create_test_bar(
    close: float,
    vwap: float,
    ema_fast: float,
    ema_slow: float,
    mom_velo: float,
    adx: float,
    volume_spike: float,
    regime: str,
    bias: str,
    atr: float = 50.0,
    high: float = None,
    low: float = None,
    breakout_strength: float = 0.5,
    body_size_atr: float = 0.5,
    bars_since_open: int = 10,
    timestamp: datetime = None,
) -> dict:
    """創建測試用 K 棒數據."""
    if high is None:
        high = close + 10
    if low is None:
        low = close - 10
    if timestamp is None:
        timestamp = datetime.now()
    
    return {
        "Close": close,
        "High": high,
        "Low": low,
        "vwap": vwap,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "mom_velo": mom_velo,
        "adx": adx,
        "volume_spike": volume_spike,
        "breakout_strength": breakout_strength,
        "body_size_atr": body_size_atr,
        "bars_since_open": bars_since_open,
        "regime": regime,
        "bias": bias,
        "atr": atr,
        "price_vs_vwap": (close - vwap) / vwap if vwap > 0 else 0,
        "timestamp": timestamp.isoformat(),
    }


def test_weak_bull_entry():
    """測試 1: 標準多頭進場情境."""
    print("\n=== Test 1: 標準多頭進場 ===")
    
    strategy = WeakBullTrend()
    
    # 模擬 WEAK + BULLISH + 價格 > VWAP + EMA 多頭排列
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,   # 溫和正向動能
        adx=18.0,        # WEAK regime
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
        atr=50.0,
        high=22180,
        low=22120,
        breakout_strength=0.5,
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal and signal.action == "BUY":
        print(f"✅ 進場信號：BUY @ {signal.stop_loss=}")
        print(f"   止損：{signal.stop_loss:.0f}, 止盈：{signal.target:.0f}")
        print(f"   信心：{signal.confidence:.2f}")
        return True
    else:
        print(f"❌ 預期 BUY 信號，實際：{signal}")
        if hasattr(strategy, 'last_eval') and strategy.last_eval:
            print(f"   skip_reason: {strategy.last_eval.skip_reason}")
        return False


def test_regime_block():
    """測試 2: TREND regime 應該阻止進場."""
    print("\n=== Test 2: TREND regime 阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="TREND",  # 不允許
        bias="BULLISH",
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：TREND regime 不進場")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_bias_block():
    """測試 3: SHORT bias 應該阻止進場."""
    print("\n=== Test 3: SHORT bias 阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="SHORT",  # 不允許，需要 BULLISH
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：SHORT bias 不進場")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_below_vwap_block():
    """測試 4: 價格低於 VWAP 應該阻止進場."""
    print("\n=== Test 4: 低於 VWAP 阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22100,  # 低於 VWAP
        vwap=22150,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：低於 VWAP")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_momentum_too_weak():
    """測試 5: 動能太弱 (mom_velo < 3) 應該阻止."""
    print("\n=== Test 5: 動能太弱阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=1.0,  # 動能不足
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：動能太弱")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_momentum_overheated():
    """測試 6: 動能過熱 (mom_velo > 25) 應該阻止."""
    print("\n=== Test 6: 動能過熱阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=30.0,  # 過熱
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：動能過熱")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_ema_not_bullish():
    """測試 7: EMA 非多頭排列應該阻止."""
    print("\n=== Test 7: EMA 非多頭排列阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22050,
        vwap=22000,
        ema_fast=22100,  # close < ema_fast, 非多頭排列
        ema_slow=22080,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：EMA 非多頭排列")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_breakout_too_weak():
    """測試 8: 突破強度不足應該阻止."""
    print("\n=== Test 8: 突破強度不足阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
        breakout_strength=0.1,  # 太弱
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：突破強度不足")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_shadow_mode():
    """測試 9: Shadow mode 應該返回 HOLD 而不是 BUY."""
    print("\n=== Test 9: Shadow mode ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
    )
    
    context = StrategyContext(
        config={
            "params": {
                "shadow_mode": True,  # 開啟 shadow mode
            }
        },
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal and signal.action == "HOLD" and "SHADOW" in signal.reason:
        print(f"✅ Shadow mode 正確：HOLD, reason={signal.reason}")
        return True
    else:
        print(f"❌ 預期 HOLD (shadow), 實際：{signal}")
        return False


def test_too_far_from_vwap_chase():
    """測試 10: 距離 VWAP 太遠 (anti-chase) 應該阻止."""
    print("\n=== Test 10: 距離 VWAP 太遠阻止 ===")
    
    strategy = WeakBullTrend()
    
    # close 比 vwap 高 100 點，atr=50 → dist=2.0 > 1.2 (上限)
    bar = create_test_bar(
        close=22200,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
        atr=50.0,
        breakout_strength=0.5,
        body_size_atr=0.3,
        bars_since_open=10,
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print(f"✅ 正確阻止：距離 VWAP 太遠 (dist={(22200-22100)/50:.1f} ATR > 1.2)")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_prior_bar_too_large():
    """測試 11: 前一根 K 棒實體過大 (anti-chase) 應該阻止."""
    print("\n=== Test 11: 前一根 K 棒實體過大阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
        atr=50.0,
        breakout_strength=0.5,
        body_size_atr=2.0,  # 實體 2.0 ATR > 1.5 上限
        bars_since_open=10,
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：前一根 K 棒實體過大")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def test_too_early_after_open():
    """測試 12: 開盤初期不進場 (anti-chase) 應該阻止."""
    print("\n=== Test 12: 開盤初期不進場阻止 ===")
    
    strategy = WeakBullTrend()
    
    bar = create_test_bar(
        close=22150,
        vwap=22100,
        ema_fast=22080,
        ema_slow=22050,
        mom_velo=8.0,
        adx=18.0,
        volume_spike=1.1,
        regime="WEAK",
        bias="BULLISH",
        atr=50.0,
        breakout_strength=0.5,
        body_size_atr=0.3,
        bars_since_open=1,  # 只有 1 根 bar，需要 >= 3
    )
    
    context = StrategyContext(
        config={"params": {"shadow_mode": False}},
        market=MockMarket(bar),
        position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    )
    
    signal = strategy.on_bar(context)
    
    if signal is None:
        print("✅ 正確阻止：開盤初期不進場")
        return True
    else:
        print(f"❌ 預期無信號，實際：{signal}")
        return False


def run_all_tests():
    """執行所有測試."""
    print("=" * 60)
    print("weak_bull_trend 策略測試")
    print("=" * 60)
    
    results = []
    results.append(("標準多頭進場", test_weak_bull_entry()))
    results.append(("TREND regime 阻止", test_regime_block()))
    results.append(("SHORT bias 阻止", test_bias_block()))
    results.append(("低於 VWAP 阻止", test_below_vwap_block()))
    results.append(("動能太弱阻止", test_momentum_too_weak()))
    results.append(("動能過熱阻止", test_momentum_overheated()))
    results.append(("EMA 非多頭排列阻止", test_ema_not_bullish()))
    results.append(("突破強度不足阻止", test_breakout_too_weak()))
    results.append(("Shadow mode", test_shadow_mode()))
    results.append(("距離 VWAP 太遠 (anti-chase)", test_too_far_from_vwap_chase()))
    results.append(("前一根 K 棒實體過大 (anti-chase)", test_prior_bar_too_large()))
    results.append(("開盤初期不進場 (anti-chase)", test_too_early_after_open()))
    
    print("\n" + "=" * 60)
    print("測試結果總結")
    print("=" * 60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print(f"\n總計：{passed}/{total} 通過")
    
    if passed == total:
        print("🎉 所有測試通過！")
        return 0
    else:
        print("⚠️  有測試失敗，請檢查邏輯")
        return 1


if __name__ == "__main__":
    exit(run_all_tests())
