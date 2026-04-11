import pandas as pd
import yaml
from pathlib import Path
import os

# --- 路徑設定 ---
ROOT = Path(__file__).parent.parent.parent
RECOMMENDATIONS_PATH = Path("/Users/mylin/Documents/mylin102/squeeze-tw-screener/recommendations.csv")
STOCKS_CFG_PATH = ROOT / "config" / "stocks.yaml"

def sync():
    if not RECOMMENDATIONS_PATH.exists():
        print("❌ 找不到選股清單：recommendations.csv")
        return

    # 1. 讀取最新選股
    df = pd.read_csv(RECOMMENDATIONS_PATH)
    # 過濾出正在 Squeeze 或剛 Fired 的買入信號
    buy_signals = df[df["status"] == "tracking"] # 根據內容選取追蹤中的
    
    # 取前 10 檔 (按動能排序，如果有的話)
    buy_signals = buy_signals.head(10)
    
    new_tickers = []
    for t in buy_signals["ticker"]:
        # 移除 .TW 或 .TWO 後綴
        clean_t = t.split(".")[0]
        new_tickers.append(clean_t)

    if not new_tickers:
        print("⚠️ 今日無推薦買入標的。")
        return

    # 2. 讀取現有設定
    with open(STOCKS_CFG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 3. 更新 Watchlist
    old_watchlist = cfg.get("stocks", {}).get("watchlist", [])
    # 合併新舊名單並去重
    updated_watchlist = list(dict.fromkeys(new_tickers + old_watchlist))[:15] # 限制最大監控 15 檔
    
    cfg["stocks"]["watchlist"] = updated_watchlist
    
    # 4. 寫回設定
    with open(STOCKS_CFG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

    print(f"✅ 同步完成！目前監控名單: {updated_watchlist}")
    
    # 5. 觸發重啟 (如果 monitor 正在跑)
    restart_flag = ROOT / ".restart"
    restart_flag.touch()

if __name__ == "__main__":
    sync()
