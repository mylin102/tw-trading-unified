#!/usr/bin/env python3
"""
驗證 weak_bear_trend 配置載入
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

# 載入主配置
config_path = Path("config/futures_night.yaml")
cfg = yaml.safe_load(config_path.open())

print("="*60)
print("weak_bear_trend 配置驗證")
print("="*60)

# 檢查 params
params = cfg.get("strategy", {}).get("params", {})

if not params:
    print("❌ 錯誤：strategy.params 不存在!")
    print("\n當前 strategy 配置:")
    print(cfg.get("strategy", {}))
    sys.exit(1)

print("\n✅ 配置已載入:")
print(f"  max_adx: {params.get('max_adx', 'NOT SET')}")
print(f"  stop_atr_mult: {params.get('stop_atr_mult', 'NOT SET')}")
print(f"  take_profit_atr_mult: {params.get('take_profit_atr_mult', 'NOT SET')}")
print(f"  min_mom_velo_bearish: {params.get('min_mom_velo_bearish', 'NOT SET')}")
print(f"  max_vwap_dist_atr: {params.get('max_vwap_dist_atr', 'NOT SET')}")
print(f"  time_stop_minutes: {params.get('time_stop_minutes', 'NOT SET')}")
print(f"  shadow_mode: {params.get('shadow_mode', 'NOT SET')}")

# 驗證關鍵值
expected_max_adx = 35.0
actual_max_adx = params.get('max_adx')

if actual_max_adx == expected_max_adx:
    print(f"\n✅ max_adx = {actual_max_adx} (正確！)")
else:
    print(f"\n❌ max_adx = {actual_max_adx} (預期：{expected_max_adx})")
    sys.exit(1)

print("\n✅ 所有配置驗證通過！")
print("\n下一步：訪問 Dashboard 查看 weak_bear_trend 是否正常評估")
