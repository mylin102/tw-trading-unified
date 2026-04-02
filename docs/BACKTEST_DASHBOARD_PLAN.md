# Backtest Dashboard — 設計計畫

## 目標

獨立的 Streamlit Dashboard（port 8501），不影響盤中 monitor（port 8500）。
可在盤中或盤後執行回測，支援策略比較、參數掃描、可視化分析。

---

## 架構

```
port 8500 — Trading Dashboard（現有，盤中監控）
port 8501 — Backtest Dashboard（新增，回測分析）

兩者完全獨立，不共享 process/state。
回測 Dashboard 只讀取 CSV/歷史數據，不碰 Shioaji API。
```

```
ui/backtest_dashboard.py
├── Tab 1: 📊 單策略回測
│   ├── 選擇策略（8 種期貨 + 5 種選擇權）
│   ├── 選擇數據（今日 / 歷史 / 自訂日期範圍）
│   ├── 調整參數（slider）
│   ├── 執行回測 → 結果表格 + 圖表
│   └── 匯出 CSV
│
├── Tab 2: 🔄 策略比較
│   ├── 勾選多個策略（最多 4 個）
│   ├── 同一數據、同一時段
│   ├── 並排 equity curve + 績效指標
│   └── 熱力圖（策略 × 參數）
│
├── Tab 3: 🔬 參數掃描
│   ├── 選擇策略 + 掃描參數（grid）
│   ├── ATR mult × Entry Score × VWAP exit
│   ├── 熱力圖 + 最佳參數高亮
│   └── 匯出完整 sweep 結果
│
└── Tab 4: 📈 歷史績效
    ├── 每日 PnL 曲線（從 indicator CSV 累計）
    ├── 月報 / 週報統計
    ├── Drawdown 分析
    └── 策略切換時間軸
```

---

## 數據來源

| 來源 | 路徑 | 用途 |
|------|------|------|
| 今日 indicator | `logs/market_data/TMF_{date}_PAPER_indicators.csv` | 當日回測 |
| 歷史 indicator | `logs/market_data/TMF_*_indicators.csv` | 多日回測 |
| TAIFEX 原始數據 | `data/taifex_raw/TMF_5m_taifex.csv` | Q1 完整回測 |
| 交易紀錄 | `exports/trades/TMF_*_trades.csv` | 歷史績效分析 |
| 選擇權 indicator | `strategies/options/logs/paper_trading/OPTIONS_*_indicators.csv` | 選擇權回測 |

---

## Tab 1: 單策略回測 — UI 設計

```
┌─ Sidebar ──────────────────────┐  ┌─ Main ──────────────────────────────┐
│                                │  │                                     │
│ 📅 數據選擇                    │  │  績效摘要                            │
│ ○ 今日夜盤                     │  │  ┌────┬────┬────┬────┬────┐         │
│ ○ 指定日期 [____]              │  │  │ PnL│Win%│ PF │ DD │Trd │         │
│ ○ 日期範圍 [__]~[__]           │  │  │+13K│8.3%│2.21│-11%│ 12 │         │
│ ○ 完整 Q1 數據                 │  │  └────┴────┴────┴────┴────┘         │
│                                │  │                                     │
│ 🎯 策略                        │  │  Equity Curve                       │
│ [squeeze_breakout        ▼]    │  │  ┌─────────────────────────────┐    │
│                                │  │  │    ╱╲    ╱╲                 │    │
│ ⚙️ 參數                        │  │  │   ╱  ╲  ╱  ╲    ╱╲         │    │
│ ATR Mult: [===●===] 1.5       │  │  │  ╱    ╲╱    ╲  ╱  ╲        │    │
│ Entry Score: [===●===] 20     │  │  │ ╱              ╲╱    ╲╱     │    │
│ VWAP Exit: [✓]                │  │  └─────────────────────────────┘    │
│ Lots: [===●===] 2             │  │                                     │
│                                │  │  交易明細                            │
│ [▶ 執行回測]                   │  │  ┌──────┬────┬────┬─────┬─────┐    │
│                                │  │  │ Time │ Dir│Price│ PnL │Reason│   │
│ [📥 匯出 CSV]                  │  │  │20:00 │ S  │32335│  +40│SYNRGY│   │
│                                │  │  │20:30 │ S  │32378│  +40│SQEEZ │   │
└────────────────────────────────┘  │  └──────┴────┴────┴─────┴─────┘    │
                                    └─────────────────────────────────────┘
```

---

## Tab 2: 策略比較 — UI 設計

```
┌─ 選擇策略（勾選最多 4 個）─────────────────────────────────────────┐
│ ☑ squeeze_breakout  ☑ trend_follow  ☐ vwap_bounce  ☐ momentum_burst │
│ ☐ night_short_only  ☑ volume_reversal  ☐ psar_breakout  ☐ cum_delta │
└────────────────────────────────────────────────────────────────────┘

┌─ 並排績效 ─────────────────────────────────────────────────────────┐
│ Strategy          │  PnL    │ Win% │  PF  │ MaxDD │ Trades │ Sharpe│
│ squeeze_breakout  │ +13,180 │ 8.3% │ 2.21 │ -11%  │   12   │  0.45 │
│ trend_follow      │  +8,200 │ 12%  │ 1.85 │  -8%  │    8   │  0.62 │
│ volume_reversal   │  -2,100 │ 33%  │ 0.78 │  -5%  │    6   │ -0.15 │
└────────────────────────────────────────────────────────────────────┘

┌─ Equity Curves（疊加）─────────────────────────────────────────────┐
│  🔵 squeeze ─── 📈 trend ─── 📊 volume                            │
│      ╱╲                                                            │
│     ╱  ╲───────╱╲                                                  │
│    ╱    ╲     ╱  ╲────                                             │
│   ╱      ╲───╱                                                     │
│  ╱        ╲╱                                                       │
└────────────────────────────────────────────────────────────────────┘
```

---

## Tab 3: 參數掃描 — UI 設計

```
┌─ 設定 ─────────────────────────┐  ┌─ 熱力圖 ──────────────────────┐
│ 策略: [squeeze_breakout ▼]     │  │                                │
│                                │  │  ATR Mult                      │
│ 掃描參數 X: [ATR Mult    ▼]   │  │       1.5   2.0   3.0   4.0   │
│   範圍: 1.0 ~ 4.0  步長: 0.5  │  │  10 │ -5K │ +2K │+13K │ +8K  │
│                                │  │  20 │ -3K │ +5K │+15K │+10K  │
│ 掃描參數 Y: [Entry Score ▼]   │  │  30 │ -8K │ +1K │ +9K │ +6K  │
│   範圍: 10 ~ 40  步長: 10     │  │  40 │-12K │ -4K │ +3K │ +1K  │
│                                │  │                                │
│ [▶ 開始掃描]                   │  │  ★ Best: ATR=3.0 Score=20     │
│                                │  │    PF=2.21 PnL=+15,180        │
└────────────────────────────────┘  └────────────────────────────────┘
```

---

## 技術實作

### 核心引擎（已有）
```python
# 直接複用現有的 vectorized simulator
from squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics
from strategies.futures.entry_strategies import STRATEGIES, get_strategy
```

### 數據載入
```python
def load_backtest_data(source, date_range=None):
    """
    source: "today" | "historical" | "taifex"
    統一回傳 DataFrame with columns: Open, High, Low, Close, Volume, 
    + squeeze indicators (sqz_on, momentum, vwap, atr, etc.)
    """
```

### 策略信號產生
```python
def generate_signals(df, strategy_name, cfg):
    """
    用 entry_strategies.py 的插件產生 long/short signal arrays
    回傳 (long_signals, short_signals) boolean arrays
    """
    strategy_fn = get_strategy(strategy_name)
    # 逐 bar 呼叫 strategy_fn，收集信號
```

### 啟動方式
```bash
# 盤中（不影響 monitor）
python3 -m streamlit run ui/backtest_dashboard.py --server.port 8501

# 或加到 autostart.sh
tmux new-window -t unified:2 -n "backtest" \
  "cd $UNIFIED_DIR && python3 -m streamlit run ui/backtest_dashboard.py --server.port 8501"
```

---

## 實作順序

| Phase | 內容 | 預估 |
|-------|------|------|
| 1 | Tab 1 單策略回測（今日數據 + 基本圖表） | 1 session |
| 2 | Tab 3 參數掃描（熱力圖） | 0.5 session |
| 3 | Tab 2 策略比較（多策略疊加） | 0.5 session |
| 4 | Tab 4 歷史績效（多日累計） | 0.5 session |
| 5 | 選擇權回測整合 | 1 session |

---

## 與現有系統的關係

```
Trading Dashboard (8500)          Backtest Dashboard (8501)
├── 即時監控                       ├── 歷史回測
├── 策略切換 → 寫 config           ├── 策略比較（不寫 config）
├── 參數調整 → 寫 config           ├── 參數掃描（不寫 config）
├── 讀 indicator CSV (tail)        ├── 讀 indicator CSV (full)
└── 需要 monitor 運行              └── 獨立運行，不需 monitor

共用：
├── config/*.yaml（只讀，顯示當前設定）
├── strategies/futures/entry_strategies.py（策略邏輯）
├── squeeze_futures/engine/vectorized.py（回測引擎）
└── logs/market_data/*.csv（歷史數據）
```
