#!/usr/bin/env python3
"""
Debug: 檢查 context.config 的實際結構
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from strategies.plugins.futures.weak_bear_trend import WeakBearTrend
from core.strategy_context import StrategyContext, MarketData, PositionView

# 載入主配置
config_path = Path("config/futures_night.yaml")
cfg = yaml.safe_load(config_path.open())

print("="*70)
print("Debug: context.config 結構檢查")
print("="*70)

# 檢查 cfg 的結構
print("\n📄 config/futures_night.yaml 顶层 keys:")
for key in cfg.keys():
    print(f"  - {key}")

print("\n📄 strategy 區塊:")
strategy = cfg.get("strategy", {})
for key in strategy.keys():
    print(f"  - {key}")

print("\n📄 strategy.params 區塊:")
params = strategy.get("params", {})
if params:
    for key, value in params.items():
        print(f"  - {key}: {value}")
else:
    print("  ❌ params 不存在或為空!")

# 創建 context
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

ctx = StrategyContext(
    market=MarketData(
        last_bar=test_bar,
        df_5m=None,
        df_15m=None,
        timestamp=test_bar["timestamp"],
        session=1,
        regime="WEAK",
    ),
    position=PositionView(size=0, entry_price=0, unrealized_pnl=0),
    config=cfg,
    bar_counter=100,
)

# 檢查 context.config
print("\n🔍 context.config 的結構:")
print(f"  type: {type(ctx.config)}")
print(f"  keys: {list(ctx.config.keys()) if isinstance(ctx.config, dict) else 'N/A'}")

# 檢查 context.config.get("params")
params_from_ctx = ctx.config.get("params", {})
print(f"\n  context.config.get('params'):")
if params_from_ctx:
    for key, value in params_from_ctx.items():
        print(f"    - {key}: {value}")
else:
    print(f"    ❌ 為空或不存!")

# 檢查 context.config.get("strategy", {}).get("params")
params_from_strategy = ctx.config.get("strategy", {}).get("params", {})
print(f"\n  context.config.get('strategy', {{}}).get('params'):")
if params_from_strategy:
    for key, value in params_from_strategy.items():
        print(f"    - {key}: {value}")
else:
    print(f"    ❌ 為空或不存!")

print("\n" + "="*70)
print("結論")
print("="*70)
print("問題：weak_bear_trend 使用 context.config.get('params')")
print("但 params 在 strategy 區塊內：context.config.get('strategy', {}).get('params')")
print("\n解決方案：修改代碼或調整配置結構")
