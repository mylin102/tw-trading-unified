import yaml
import sys
import os
from pathlib import Path
from rich.console import Console

# --- 路徑設定 ---
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from core.external_feature_provider import get_external_feature_provider, load_stock_config
STOCKS_CFG_PATH = ROOT / "config" / "stocks.yaml"
console = Console()

def sync():
    cfg = load_stock_config(STOCKS_CFG_PATH)
    provider = get_external_feature_provider(cfg)
    console.print("[cyan]🌐 Fetching external watchlist via external feature provider...[/cyan]")

    try:
        snapshot = provider.get_snapshot(prefer_refresh=True)
        new_tickers = snapshot.get("watchlist_symbols", [])
        if not new_tickers:
            console.print("[yellow]⚠️ 外部名單為空，取消同步。[/yellow]")
            return

        if snapshot.get("degraded"):
            console.print(f"[yellow]⚠️ Using degraded feature snapshot: {snapshot.get('degraded_reason', '')}[/yellow]")
        console.print(f"[green]✅ 成功取得 {len(new_tickers)} 檔股票。[/green]")

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
    # 直接替換 watchlist，保持既有腳本行為
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
