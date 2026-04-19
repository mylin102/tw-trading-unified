import requests
import yaml
import json
from pathlib import Path
import os
from rich.console import Console

# --- 路徑設定 ---
ROOT = Path(__file__).parent.parent.parent
STOCKS_CFG_PATH = ROOT / "config" / "stocks.yaml"
LEADERS_URL = "https://raw.githubusercontent.com/mylin102/tw-canslim-web/master/data/leaders.json"

console = Console()

def sync():
    console.print(f"[cyan]🌐 Fetching external watchlist from {LEADERS_URL}...[/cyan]")
    
    # 1. 抓取外部資料
    try:
        response = requests.get(LEADERS_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # 2. 提取 symbol
        new_tickers = [item["symbol"] for item in data.get("universe", [])]
        
        if not new_tickers:
            console.print("[yellow]⚠️ 外部名單為空，取消同步。[/yellow]")
            return

        console.print(f"[green]✅ 成功抓取 {len(new_tickers)} 檔股票。[/green]")

    except Exception as e:
        console.print(f"[red]❌ 抓取外部資料失敗: {e}[/red]")
        return

    # 3. 讀取現有設定
    try:
        with open(STOCKS_CFG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        console.print(f"[red]❌ 讀取設定檔失敗: {e}[/red]")
        return

    # 4. 更新 Watchlist
    old_watchlist = cfg.get("stocks", {}).get("watchlist", [])
    
    # 直接替換或是合併？
    # 根據用戶指令「從raw.githubusercontent.com抓清單當sotck的名單使用」，這裡採直接替換
    cfg["stocks"]["watchlist"] = new_tickers
    
    # 5. 寫回設定
    try:
        with open(STOCKS_CFG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        console.print(f"[bold green]✨ Watchlist 同步完成！已更新為 {len(new_tickers)} 檔股票。[/bold green]")
    except Exception as e:
        console.print(f"[red]❌ 寫入設定檔失敗: {e}[/red]")
        return

    # 顯示部分名單
    console.print(f"前 10 檔監控股票: {new_tickers[:10]}...")
    
    # 6. 觸發重啟 (如果 monitor 正在跑)
    restart_flag = ROOT / ".restart"
    restart_flag.touch()
    console.print("[dim]🔄 已標記系統重啟...[/dim]")

if __name__ == "__main__":
    sync()
