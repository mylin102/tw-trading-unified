import yaml
import sys
from pathlib import Path
from rich.console import Console

# --- 路徑設定 ---
ROOT = Path(__file__).resolve().parent.parent.parent
STOCKS_CFG_PATH = ROOT / "config" / "stocks.yaml"
console = Console()

def update_config():
    try:
        with open(STOCKS_CFG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        
        # 1. 確保策略是 mean_reversion_enhanced (目前用戶設定的)
        # 或是考慮建議用戶切換到 scout_strategy
        # 這裡我們先保持用戶的設定，但確保一些關鍵參數是合理的
        
        # 2. 增加一些強勢標的到 watchlist (如果不在的話)
        # 例如近期熱門股，增加交易機會
        hot_stocks = ["2330", "2317", "2454", "2382", "2308", "3037", "3711", "2357"]
        current_watchlist = cfg["stocks"].get("watchlist", [])
        
        # 確保 hot_stocks 在名單前列
        new_watchlist = list(dict.fromkeys(hot_stocks + current_watchlist))
        cfg["stocks"]["watchlist"] = new_watchlist
        
        # 3. 調整 P0 濾網參數 (如果太嚴格)
        # 如果用戶同意，我們可以把 entry_score 調低
        # cfg["stocks"]["entry_score"] = 10 
        
        with open(STOCKS_CFG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        console.print("[green]✅ Watchlist updated with some liquid stocks.[/green]")
        
    except Exception as e:
        console.print(f"[red]❌ Update config failed: {e}[/red]")

if __name__ == "__main__":
    update_config()
