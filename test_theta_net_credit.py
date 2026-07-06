import sys
sys.path.append('.')
from strategies.options.theta_gang import ThetaGangManager
import math

# 模擬Black-Scholes函數
def mock_bs_fn(spot, strike, dte_years, r, iv, side):
    # 簡單模擬：返回固定權利金
    return {"price": 50.0}

# 創建ThetaGangManager
cfg = {
    "theta_gang": {
        "strategy": "iron_condor",
        "min_credit": 30,
        "wing_width": 200,
        "otm_offset": 200
    }
}

tg = ThetaGangManager(cfg, mock_bs_fn, strike_rounding=100)

# 測試evaluate_entry
spot = 37000
iv = 0.25
dte_years = 0.1  # 約36天
squeeze_on = True

entry_info = tg.evaluate_entry(spot, iv, dte_years, squeeze_on)
if entry_info:
    print(f"✅ evaluate_entry返回: net_credit={entry_info.get('net_credit')}")
    print(f"   策略: {entry_info.get('strategy')}")
    print(f"   legs數量: {len(entry_info.get('legs', []))}")
else:
    print("❌ evaluate_entry返回None")
    
# 測試open_position
if entry_info:
    pos = tg.open_position(entry_info)
    print(f"✅ open_position返回: net_credit={pos.net_credit}")
    print(f"   is_open: {pos.is_open}")
else:
    print("❌ 無法測試open_position，因為entry_info為None")
