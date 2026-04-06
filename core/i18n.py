import streamlit as st

TRANSLATIONS = {
    "en": {
        "nav_futures": "Futures (TMF)",
        "nav_stocks": "Stocks (Odd-Lot)",
        "nav_options": "Options (TXO)",
        "nav_system": "System & Analytics",
        
        "nav_single": "Single Backtest",
        "nav_sweep": "Optimization Sweep",
        "nav_stock_lab": "Portfolio Optimizer",
        "nav_leaderboard": "Strategy Ranking",
        "nav_history": "Trade History",
        
        "sidebar_lang": "Language",
        "lang_toggle": "繁體中文",
        "ticker_select": "Target Selection",
        "params": "Parameters",
        "results": "Results",
        "profit": "Total PnL",
        "win_rate": "Win Rate",
        "trades": "Trades",
        "equity_curve": "Equity Curve",
        "exit_dist": "Exit Reasons",
        "strategy_settings": "Strategy",
        "btn_run_single": "▶ Run Single Backtest",
        "btn_run_global": "🚀 Run Global Scan",
        "no_trades": "No trades executed."
    },
    "zh": {
        "nav_futures": "📈 期貨研究 (台指期)",
        "nav_stocks": "🍎 台股研究 (零股)",
        "nav_options": "🔮 選擇權研究",
        "nav_system": "⚙️ 系統與統計",
        
        "nav_single": "單一策略回測",
        "nav_sweep": "參數矩陣掃描",
        "nav_stock_lab": "多標的全域優化",
        "nav_leaderboard": "全策略排行榜",
        "nav_history": "歷史交易總帳",
        
        "sidebar_lang": "語系設定",
        "lang_toggle": "English",
        "ticker_select": "標的選擇",
        "params": "回測參數",
        "results": "績效總覽",
        "profit": "總盈虧",
        "win_rate": "勝率",
        "trades": "總交易筆數",
        "equity_curve": "權益曲線",
        "exit_dist": "出場原因分佈",
        "strategy_settings": "選擇策略",
        "btn_run_single": "▶ 執行單一回測",
        "btn_run_global": "🚀 全域掃描 (台股全標的)",
        "no_trades": "此參數下無任何交易紀錄。"
    }
}

def get_text(key, *args):
    lang = st.session_state.get("lang", "zh")
    text = TRANSLATIONS[lang].get(key, key)
    if args:
        return text.format(*args)
    return text
