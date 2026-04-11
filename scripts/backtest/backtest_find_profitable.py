import os
import sys
import pandas as pd
import numpy as np
import yaml
from pathlib import Path
from datetime import datetime

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.sweep_engine import run_multi_asset_backtest
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

DATA_DIR = ROOT / "data" / "taifex_raw"
REPORT_DIR = ROOT / "exports" / "backtests"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

def generate_html_report(summary, ledger, stk_cfg):
    """產出專業級回測 HTML 報告 (修正亂碼 & 增加診斷)"""
    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"stock_backtest_report_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    report_path = REPORT_DIR / filename
    
    # 績效統計
    total_pnl = summary["pnl"].sum()
    win_rate = summary["win_rate"].mean()
    total_trades = summary["trades"].sum()
    profitable_ratio = (len(summary[summary["pnl"] > 0]) / len(summary)) * 100 if not summary.empty else 0
    
    # 🕵️ 診斷：按出場原因統計損益
    reason_stats = ledger.groupby("原因")["損益"].agg(["sum", "count"]).reset_index()
    reason_stats.columns = ["原因", "累計損益", "發生次數"]
    reason_stats_html = reason_stats.to_html(classes='table table-bordered text-center', index=False)
    
    summary_html = summary.sort_values("pnl", ascending=False).to_html(classes='table table-striped', index=False)
    ledger_html = ledger.sort_values("時間", ascending=False).head(100).to_html(classes='table table-sm', index=False)
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <meta charset="UTF-8">
        <title>Quant Lab Backtest Report</title>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
        <style>
            body {{ background-color: #f8f9fa; padding: 40px; font-family: 'Inter', 'PingFang TC', sans-serif; }}
            .card {{ border-radius: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 30px; border: none; }}
            .metric {{ font-size: 24px; font-weight: bold; color: #2563eb; }}
            .positive {{ color: #10b981; }}
            .negative {{ color: #ef4444; }}
            h1 {{ color: #1e293b; font-weight: 800; }}
            .reason-card {{ border-left: 5px solid #2563eb; }}
            th {{ background-color: #f1f5f9 !important; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🍎 台股偵察兵：投資組合回測報告</h1>
            <p class="text-muted">產出時間: {report_time} | 模式: 零股模擬 (Portfolio Analysis)</p>
            
            <div class="row">
                <div class="col-md-3">
                    <div class="card p-3">
                        <div class="text-muted">總盈虧 (PnL)</div>
                        <div class="metric {'positive' if total_pnl > 0 else 'negative'}">{total_pnl:+,.0f} TWD</div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card p-3">
                        <div class="text-muted">標的獲利率</div>
                        <div class="metric">{profitable_ratio:.1f}%</div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card p-3">
                        <div class="text-muted">總交易筆數</div>
                        <div class="metric">{int(total_trades)}</div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card p-3">
                        <div class="text-muted">移動停損設定</div>
                        <div class="metric">{stk_cfg.get('trailing_stop_pct', 0.03)*100:.1f}%</div>
                    </div>
                </div>
            </div>

            <div class="card p-4 reason-card">
                <h3>🕵️ 損益歸因診斷 (PnL Attribution)</h3>
                <p class="text-muted">分析各類出場方式對總損益的貢獻。</p>
                {reason_stats_html}
            </div>

            <div class="card p-4">
                <h3>🏆 標的排行榜 (Ranking)</h3>
                {summary_html}
            </div>

            <div class="card p-4">
                <h3>📋 近期交易明細 (Last 100 Trades)</h3>
                {ledger_html}
            </div>
            
            <div class="mt-4 text-center text-muted">
                Powered by tw-trading-unified v2.0 | Quant Lab Institutional
            </div>
        </div>
    </body>
    </html>
    """
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    return report_path

def run_batch_backtest():
    # 1. 載入配置並優化參數
    with open(ROOT / "config" / "stocks.yaml", "r") as f:
        full_cfg = yaml.safe_load(f)
    
    stk_cfg = full_cfg.get("stocks", {})
    # 💡 關鍵：手動將移動停損放寬至 3.0% 以改善負績效
    stk_cfg["trailing_stop_pct"] = 0.03 
    full_cfg["trailing_stop_pct"] = 0.03 # 雙重確保注入
    
    # 2. 準備數據
    all_dfs = {}
    tickers = [f.stem.split("_")[1] for f in DATA_DIR.glob("STOCK_*_5m.csv")]
    
    print(f"🔍 Found {len(tickers)} tickers. Loading data...")
    for t in tickers:
        path = DATA_DIR / f"STOCK_{t}_5m.csv"
        df = pd.read_csv(path)
        date_col = "Date" if "Date" in df.columns else "timestamp"
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col)
        df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
        
        if len(df) > 50:
            all_dfs[t] = calculate_futures_squeeze(df)

    # 3. 執行回測
    print(f"🚀 Running portfolio backtest with TS=3.0%...")
    summary, ledger = run_multi_asset_backtest(all_dfs, stk_cfg["strategy"], full_cfg, capital_per_trade=stk_cfg["capital_per_trade"])
    
    # 4. 產出報告
    if not summary.empty:
        report_path = generate_html_report(summary, ledger, stk_cfg)
        print(f"✨ Report generated: {report_path}")
    else:
        print("❌ No trades triggered.")

if __name__ == "__main__":
    run_batch_backtest()
