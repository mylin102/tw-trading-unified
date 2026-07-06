import pandas as pd
from pathlib import Path

# --- 路徑設定 ---
# 根據偵查結果，歷史資料主要存放在 Qwen-squeeze-strategy 或 squeeze-backtest 相關路徑
SOURCE_DIRS = [
    Path("/Users/mylin/Documents/mylin102/Qwen-squeeze-strategy/backtests/tw_data"),
    Path("/Users/mylin/Documents/mylin102/squeeze-backtest/exports/market_data"),
]
TARGET_DIR = Path("/Users/mylin/Documents/mylin102/tw-trading-unified/data/taifex_raw")

def migrate():
    if not TARGET_DIR.exists():
        TARGET_DIR.mkdir(parents=True)
        print(f"📁 已建立目標目錄: {TARGET_DIR}")

    count = 0
    for src in SOURCE_DIRS:
        if not src.exists():
            continue
        
        print(f"🔍 掃描來源: {src}")
        # 尋找所有 .csv 檔案
        for f in src.glob("**/*.csv"):
            # 處理檔名，例如 "2330_TW.csv" 或 "2330.csv" -> "STOCK_2330_5m.csv"
            ticker = f.stem.replace("_TW", "").replace("TMF_", "")
            
            # 過濾掉非標的檔案 (例如含有 report, plan 等字眼的)
            if any(x in ticker.lower() for x in ["report", "plan", "ledger", "tracking", "results"]):
                continue
            
            # 只有純數字或帶有市場標誌的才搬移
            if not ticker.isalnum():
                continue

            target_name = f"STOCK_{ticker}_5m.csv"
            target_path = TARGET_DIR / target_name
            
            # 執行標準化搬移
            try:
                # 讀取並檢查格式 (V-Model 標準化)
                df = pd.read_csv(f)
                # 確保欄位大小寫統一 (Open, High, Low, Close, Volume)
                col_map = {c: c.capitalize() for c in df.columns if c.lower() in ["open", "high", "low", "close", "volume"]}
                df = df.rename(columns=col_map)
                
                # 存入新位置
                df.to_csv(target_path, index=False)
                print(f"✅ 已遷移: {f.name} -> {target_name}")
                count += 1
            except Exception as e:
                print(f"❌ 遷移失敗 {f.name}: {e}")

    print(f"\n✨ 任務完成！共成功遷移 {count} 檔標的歷史資料。")
    print("🚀 您現在可以在 8501 Dashboard 的 Stock Optimizer 中輸入這些代碼進行回測了。")

if __name__ == "__main__":
    migrate()
