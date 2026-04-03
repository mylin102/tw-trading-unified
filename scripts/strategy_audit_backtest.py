import pandas as pd
import numpy as np
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
from strategies.futures.squeeze_futures.engine.vectorized import VectorizedSimulator, SimulatorConfig

def run_audit():
    DATA = "data/taifex_raw/TMF_5m_taifex.csv"
    if not Path(DATA).exists():
        print(f"❌ Data file {DATA} not found.")
        return

    print("Loading data and calculating indicators...")
    df_raw = pd.read_csv(DATA, parse_dates=["ts"], index_col="ts")
    df_5m = calculate_futures_squeeze(df_raw)
    
    # 建立一個簡單的模擬分數 (真實情況由多週期決定)
    df_5m["score"] = np.where(df_5m["momentum"] > 0, 40, -40)
    
    config = SimulatorConfig(point_value=10, slippage=1.0)
    simulator = VectorizedSimulator(df_5m, config)

    # 參數網格搜索
    results = []
    atr_mult_range = [2.0, 3.0]
    tp_range = [40, 80]
    velo_range = [0.0, 0.5, 1.0, 2.0] # 測試不同斜率門檻

    print(f"Starting Final Audit (Total combinations: {len(atr_mult_range)*len(tp_range)*len(velo_range)})...")
    
    for mult in atr_mult_range:
        for tp in tp_range:
            for velo in velo_range:
                res = simulator.run(
                    stop_loss_pts=0, 
                    atr_mult=mult, 
                    tp1_pts=tp, 
                    velo_thresh=velo
                )
                m = res["metrics"]
                results.append({
                    "ATR_Mult": mult, "TP": tp, "Velo": velo,
                    "PF": m["profit_factor"], 
                    "Win%": m["win_rate"], 
                    "PnL": m["total_pnl"],
                    "Trades": m["total_trades"]
                })

    df_results = pd.DataFrame(results)
    print("\n=== Final Audit Results (Top 10) ===")
    print(df_results.sort_values("PF", ascending=False).head(10).to_string(index=False))
    
    # 找出漏洞分析
    print("\n🔍 漏洞分析報告：")
    
    # 1. 檢查過度交易
    high_trades = df_results[df_results["Trades"] > df_results["Trades"].median()]
    if high_trades["PF"].mean() < 1.0:
        print("⚠️ 漏洞發現：交易次數過多時獲利能力劇降。這代表策略在盤整期極度脆弱，易受滑價磨損。")
    
    # 2. 檢查停損點數
    small_sl = df_results[df_results["SL"] <= 30]
    large_sl = df_results[df_results["SL"] > 30]
    if small_sl["Win Rate"].mean() < large_sl["Win Rate"].mean() - 5:
        print("⚠️ 漏洞發現：緊湊停損 (<=30pts) 的勝率顯著低於寬鬆停損。市場雜訊可能頻繁觸發無謂的停損。")

    # 3. 檢查跳空影響
    # (這部分會從 simulator.run 返回的 exit_reasons 中分析，若 reason=0 且成交價 != sl_price 則為跳空)
    
if __name__ == "__main__":
    run_audit()
