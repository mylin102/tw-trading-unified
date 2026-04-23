import pandas as pd
import time
import sys
from pathlib import Path

# --- 路徑設定 ---
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from core.shioaji_session import get_api, logout

# --- 設定 ---
CHIPS_DIR = Path("data/chips")
CHIPS_DIR.mkdir(parents=True, exist_ok=True)

def fetch_it_chips(ticker: str, days: int = 60):
    """
    抓取指定標的的投信買賣超數據。
    """
    api = get_api()
    target_path = CHIPS_DIR / f"{ticker}_it.csv"
    
    print(f"[*] Fetching IT chips for {ticker} (last {days} days)...")
    
    try:
        contract = api.Contracts.Stocks[ticker]
        # 使用 api.Chips.inst_investors_trading 獲取籌碼
        # 注意: 這是盤後數據，通常在 15:00 後更新
        chips = api.Chips.inst_investors_trading(contract)
        
        if not chips:
            print(f"⚠️ No chip data found for {ticker}")
            return
            
        df = pd.DataFrame({**chips})
        df['Date'] = pd.to_datetime(df['date'])
        
        # 篩選出投信買賣超 (it_buy) 相關欄位
        # Shioaji 的返回欄位包含: buy, sell, net_buy
        # 我們關心的是 net_buy
        df = df[['Date', 'buy', 'sell', 'net_buy']]
        df.rename(columns={'net_buy': 'it_net_buy'}, inplace=True)
        
        # 計算連三買指標
        df['it_buy_rolling_3_min'] = df['it_net_buy'].rolling(3).min()
        
        df.to_csv(target_path, index=False)
        print(f"✅ Saved to {target_path}")
        
    except Exception as e:
        print(f"❌ Failed to fetch chips for {ticker}: {e}")

if __name__ == "__main__":
    # 範例：抓取 Watchlist
    import yaml
    with open("config/stocks.yaml", "r") as f:
        cfg = yaml.safe_load(f)
    
    watchlist = cfg.get("stocks", {}).get("watchlist", ["2330", "2317", "2454"])
    
    try:
        for ticker in watchlist:
            fetch_it_chips(ticker)
            time.sleep(1) # 避開 Rate Limit
    finally:
        logout()
