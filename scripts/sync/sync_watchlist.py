import pandas as pd
import yaml
import json
from pathlib import Path
import os

# --- 路徑設定 ---
ROOT = Path(__file__).parent.parent.parent
# 使用相對路徑，避免絕對路徑問題
CANSLIM_WEB_ROOT = ROOT.parent / "tw-canslim-web"
CANSLIM_DATA_PATH = CANSLIM_WEB_ROOT / "docs" / "data.json"
STOCKS_CFG_PATH = ROOT / "config" / "stocks.yaml"

def sync():
    if not CANSLIM_DATA_PATH.exists():
        print(f"❌ 找不到 CANSLIM 資料檔：{CANSLIM_DATA_PATH}")
        return

    # 1. 讀取 CANSLIM 資料
    with open(CANSLIM_DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    stocks_data = data.get("stocks", {})
    
    # 2. 過濾出符合 CANSLIM 條件的股票
    # 條件：CANSLIM 分數 >= 70 且至少 5 個條件符合
    qualified_stocks = []
    for symbol, stock_info in stocks_data.items():
        canslim_info = stock_info.get("canslim", {})
        score = canslim_info.get("score", 0)
        
        # 計算符合的條件數量
        conditions_met = sum([
            canslim_info.get("C", False),
            canslim_info.get("A", False),
            canslim_info.get("N", False),
            canslim_info.get("S", False),
            canslim_info.get("L", False),
            canslim_info.get("I", False),
            canslim_info.get("M", False)
        ])
        
        if score >= 70 and conditions_met >= 5:
            qualified_stocks.append({
                "symbol": symbol,
                "score": score,
                "conditions_met": conditions_met,
                "name": stock_info.get("name", "N/A")
            })
    
    # 3. 按分數排序，取前 10 檔
    qualified_stocks.sort(key=lambda x: x["score"], reverse=True)
    top_stocks = qualified_stocks[:10]
    
    new_tickers = [stock["symbol"] for stock in top_stocks]

    if not new_tickers:
        print("⚠️ 今日無符合 CANSLIM 條件的推薦標的。")
        return

    # 4. 讀取現有設定
    with open(STOCKS_CFG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 5. 更新 Watchlist
    old_watchlist = cfg.get("stocks", {}).get("watchlist", [])
    # 合併新舊名單並去重
    updated_watchlist = list(dict.fromkeys(new_tickers + old_watchlist))[:15] # 限制最大監控 15 檔
    
    cfg["stocks"]["watchlist"] = updated_watchlist
    
    # 6. 寫回設定
    with open(STOCKS_CFG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

    # 顯示詳細資訊
    print(f"✅ CANSLIM 同步完成！")
    print(f"   符合條件股票數: {len(qualified_stocks)}")
    print(f"   前 {len(top_stocks)} 檔推薦股票:")
    for i, stock in enumerate(top_stocks, 1):
        print(f"     {i:2d}. {stock['symbol']} - {stock['name']} (分數: {stock['score']}, 條件: {stock['conditions_met']}/7)")
    print(f"   目前監控名單: {updated_watchlist}")
    
    # 7. 觸發重啟 (如果 monitor 正在跑)
    restart_flag = ROOT / ".restart"
    restart_flag.touch()

if __name__ == "__main__":
    sync()
