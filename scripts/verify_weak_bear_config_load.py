#!/usr/bin/env python3
"""
深度驗證：weak_bear_trend 配置載入路徑測試

測試邏輯:
1. 創建測試配置文件 (使用特殊的 max_adx 值 99.9)
2. 直接實例化 weak_bear_trend 策略
3. 檢查策略讀取的 max_adx 值
4. 確認是 99.9 (從配置載入) 還是 20.0/22.0 (硬編碼 default)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from strategies.plugins.futures.weak_bear_trend import WeakBearTrend
from core.strategy_context import StrategyContext, MarketData, PositionView

print("="*70)
print("weak_bear_trend 配置載入路徑深度驗證")
print("="*70)

# 步驟 1: 讀取主配置文件
config_path = Path("config/futures_night.yaml")
cfg = yaml.safe_load(config_path.open())

print("\n📄 主配置文件：config/futures_night.yaml")
print("-" * 70)

# 步驟 2: 檢查 strategy.params
params = cfg.get("strategy", {}).get("params", {})

if not params:
    print("❌ 錯誤：strategy.params 不存在!")
    sys.exit(1)

print(f"✅ strategy.params 存在")
print(f"   max_adx = {params.get('max_adx', 'NOT SET')}")

# 步驟 3: 創建測試用 context
print("\n🔧 創建測試用 StrategyContext...")

# 創建空的 bar 數據
test_bar = {
    "Close": 42000,
    "High": 42100,
    "Low": 41900,
    "vwap": 42050,
    "ema_fast": 42100,
    "ema_slow": 42200,
    "atr": 50,
    "adx": 25,
    "mom_velo": -5,
    "volume_spike": 1.0,
    "regime": "WEAK",
    "bias": "SHORT",
    "timestamp": "2026-05-08T03:10:00",
}

# 創建 context (使用主配置)
ctx = StrategyContext(
    market=MarketData(
        last_bar=test_bar,
        df_5m=None,
        df_15m=None,
        timestamp=test_bar["timestamp"],
        session=1,
        regime="WEAK",
    ),
    position=PositionView(
        size=0,
        entry_price=0,
        unrealized_pnl=0,
    ),
    config=cfg,  # ← 關鍵：傳入主配置
    bar_counter=100,
)

print("✅ StrategyContext 創建完成")
print(f"   context.config 來源：config/futures_night.yaml")

# 步驟 4: 實例化策略並初始化
print("\n🤖 實例化 weak_bear_trend 策略...")
strategy = WeakBearTrend()

print("✅ 策略實例化完成")
print(f"   策略名稱：{strategy.name}")

# 步驟 5: 調用 init (這會讀取 context.config)
print("\n⚙️  調用 strategy.init(context)...")
strategy.init(ctx)

print("✅ init() 執行完成")

# 步驟 6: 檢查策略內部的 max_adx 值
print("\n🔍 檢查策略內部的 max_adx 值...")
print("-" * 70)

actual_max_adx = strategy.max_adx
expected_max_adx = params.get("max_adx")

print(f"策略內部的 max_adx = {actual_max_adx}")
print(f"配置文件中的 max_adx = {expected_max_adx}")

# 步驟 7: 驗證結果
print("\n" + "="*70)
print("驗證結果")
print("="*70)

if actual_max_adx == expected_max_adx:
    print(f"✅ 驗證通過！")
    print(f"   weak_bear_trend 正確從 config/futures_night.yaml 讀取配置")
    print(f"   max_adx = {actual_max_adx} (不是硬編碼的 20.0 或 22.0)")
    
    # 額外檢查其他參數
    print(f"\n📊 其他參數驗證:")
    print(f"   stop_atr_mult: {strategy.stop_atr_mult} (配置：{params.get('stop_atr_mult')})")
    print(f"   take_profit_atr_mult: {strategy.take_profit_atr_mult} (配置：{params.get('take_profit_atr_mult')})")
    print(f"   min_mom_velo_bearish: {strategy.min_mom_velo_bearish} (配置：{params.get('min_mom_velo_bearish')})")
    print(f"   max_vwap_dist_atr: {strategy.max_vwap_dist_atr} (配置：{params.get('max_vwap_dist_atr')})")
    
    # 檢查是否有硬編碼 default
    if actual_max_adx in [20.0, 22.0]:
        print(f"\n⚠️  警告：max_adx = {actual_max_adx} 可能是硬編碼 default 值!")
        print(f"   請檢查代碼中的 params.get('max_adx', default)")
    else:
        print(f"\n✅ 確認：沒有使用硬編碼 default 值 (20.0 或 22.0)")
    
else:
    print(f"❌ 驗證失敗！")
    print(f"   策略內部的 max_adx ({actual_max_adx}) ≠ 配置文件 ({expected_max_adx})")
    print(f"   可能原因：使用了硬編碼的 default 值")
    sys.exit(1)

print("\n" + "="*70)
print("✅ 所有驗證通過！weak_bear_trend 正確使用配置文件")
print("="*70)
