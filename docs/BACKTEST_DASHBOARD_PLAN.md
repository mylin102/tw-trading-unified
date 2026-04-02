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

---

## SDD: Backtest Dashboard 架構設計

### 模組職責

```
ui/backtest_dashboard.py          ← Streamlit UI (port 8501)
│  職責：使用者互動、圖表渲染
│  不做：策略邏輯、指標計算
│
├── backtest/runner.py            ← 回測執行器（新增）
│   職責：載入數據 → 產生信號 → 呼叫 vectorized simulator → 回傳結果
│   不做：UI 渲染、檔案寫入
│
├── strategies/futures/entry_strategies.py  ← 策略信號（現有）
│   職責：接收 market state → 回傳 signal dict 或 None
│
├── squeeze_futures/engine/vectorized.py   ← 向量化模擬器（現有）
│   職責：接收信號 array → 模擬交易 → 回傳 PnL array
│
└── squeeze_futures/engine/indicators.py   ← 指標計算（現有）
    職責：原始 OHLCV → squeeze/atr/vwap/ema 指標
```

### 介面契約

```python
# backtest/runner.py

@dataclass
class BacktestRequest:
    strategy: str                    # "squeeze_breakout" | "trend_follow" | ...
    data_source: str                 # "today" | "date_range" | "taifex"
    date_from: Optional[str]         # "20260402"
    date_to: Optional[str]           # "20260402"
    params: dict                     # {"atr_mult": 3.0, "entry_score": 20, ...}
    lots: int = 2
    initial_balance: float = 100000

@dataclass
class BacktestResult:
    trades: pd.DataFrame             # timestamp, direction, price, pnl, reason
    equity_curve: pd.Series          # cumulative equity
    metrics: dict                    # pnl, win_rate, pf, max_dd, sharpe, trades
    signals: pd.DataFrame            # all signals (for chart overlay)
    params: dict                     # echo back params used

def run_backtest(req: BacktestRequest) -> BacktestResult:
    """
    Preconditions:
      - req.strategy in STRATEGIES
      - data_source has valid data
      - params values > 0
    
    Postconditions:
      - result.equity_curve.iloc[0] == initial_balance
      - result.metrics["total_trades"] == len(result.trades)
      - result.trades PnL includes fees
    
    Invariants:
      - 不寫入任何檔案（純計算）
      - 不影響 monitor 的任何狀態
      - 不呼叫 Shioaji API
    """

def run_grid_sweep(req: BacktestRequest, sweep_params: dict) -> pd.DataFrame:
    """
    sweep_params: {"atr_mult": [1.5, 2.0, 3.0], "entry_score": [10, 20, 30]}
    回傳: DataFrame with all param combos + metrics
    
    Invariants:
      - 每個 combo 獨立執行，不共享狀態
      - 結果可直接 pivot 成熱力圖
    """

def run_comparison(strategies: list[str], req: BacktestRequest) -> dict[str, BacktestResult]:
    """
    同一數據、同一時段，多策略並行回測
    
    Invariants:
      - 所有策略用完全相同的 OHLCV 數據
      - 各策略結果互相獨立
    """
```

### 數據流

```
User selects params in UI
        │
        ▼
BacktestRequest
        │
        ▼
load_data(source, date_range)
        │ → DataFrame [Open, High, Low, Close, Volume]
        ▼
calculate_futures_squeeze(df)
        │ → DataFrame + [sqz_on, momentum, vwap, atr, ema, ...]
        ▼
generate_signals(df, strategy_fn, cfg)
        │ → (long_signals[], short_signals[])
        ▼
simulate_trades_vectorized(...)
        │ → (entries, exits, positions, pnl, reasons)
        ▼
calculate_metrics(pnl, ...)
        │ → {pnl, win_rate, pf, max_dd, sharpe}
        ▼
BacktestResult
        │
        ▼
UI renders: equity curve + trade table + metrics
```

### 隔離保證

| 項目 | Trading Dashboard (8500) | Backtest Dashboard (8501) |
|------|--------------------------|---------------------------|
| Process | main.py + monitor threads | 獨立 streamlit process |
| State | PaperTrader.position (mutable) | 無持久狀態（每次重算） |
| Config | 讀寫 config/*.yaml | 只讀（顯示當前設定） |
| API | Shioaji session | 不使用 |
| Data | tail indicator CSV | 讀完整 CSV |
| Side effects | 寫 trades CSV/JSON | 無（純計算） |

---

## V-Model: Backtest Dashboard 測試計畫

### Level 1: 單元測試

```python
# tests/test_backtest_runner.py

class TestDataLoading:
    def test_load_today_returns_dataframe():
        """今日 indicator CSV 載入正確"""
        df = load_data("today")
        assert not df.empty
        assert "Close" in df.columns
        assert "sqz_on" in df.columns

    def test_load_date_range():
        """多日數據合併正確"""
        df = load_data("date_range", "20260401", "20260402")
        assert len(df.index.normalize().unique()) >= 2

    def test_missing_date_returns_empty():
        """不存在的日期回傳空 DataFrame"""
        df = load_data("date_range", "20200101", "20200101")
        assert df.empty

class TestSignalGeneration:
    def test_signals_match_strategy():
        """策略信號與手動計算一致"""
        df = load_test_data()
        long_sig, short_sig = generate_signals(df, "squeeze_breakout", default_cfg)
        # squeeze_breakout: not sqz_on & score >= 20 & mom_state >= 2
        expected_long = (~df["sqz_on"]) & (df["score"] >= 20) & (df["mom_state"] >= 2)
        assert (long_sig == expected_long).all()

    def test_no_simultaneous_long_short():
        """同一根 bar 不能同時有 long 和 short 信號"""
        for name in STRATEGIES:
            df = load_test_data()
            long_sig, short_sig = generate_signals(df, name, default_cfg)
            assert not (long_sig & short_sig).any(), f"{name} has simultaneous signals"

class TestBacktestResult:
    def test_equity_starts_at_initial():
        """Equity curve 起始值 = initial_balance"""
        result = run_backtest(BacktestRequest(
            strategy="squeeze_breakout", data_source="taifex",
            params={}, initial_balance=100000))
        assert result.equity_curve.iloc[0] == 100000

    def test_pnl_includes_fees():
        """回測 PnL 包含手續費"""
        result = run_backtest(BacktestRequest(
            strategy="squeeze_breakout", data_source="taifex", params={}))
        if result.metrics["total_trades"] > 0:
            # 至少有一筆 flat trade 應該是負的（手續費）
            assert result.metrics["total_pnl"] != result.trades["gross_pnl"].sum()

    def test_no_side_effects():
        """回測不寫入任何檔案"""
        import os
        before = set(os.listdir("exports/trades"))
        run_backtest(BacktestRequest(
            strategy="squeeze_breakout", data_source="taifex", params={}))
        after = set(os.listdir("exports/trades"))
        assert before == after

class TestGridSweep:
    def test_sweep_covers_all_combos():
        """Grid sweep 涵蓋所有參數組合"""
        sweep = {"atr_mult": [1.5, 3.0], "entry_score": [10, 20]}
        result = run_grid_sweep(base_req, sweep)
        assert len(result) == 2 * 2  # 4 combos

    def test_sweep_results_independent():
        """每個 combo 的結果互相獨立"""
        sweep = {"atr_mult": [1.5, 3.0]}
        result = run_grid_sweep(base_req, sweep)
        assert result.iloc[0]["PnL"] != result.iloc[1]["PnL"]

class TestComparison:
    def test_same_data_different_results():
        """不同策略用同一數據，結果不同"""
        results = run_comparison(
            ["squeeze_breakout", "trend_follow"], base_req)
        assert results["squeeze_breakout"].metrics != results["trend_follow"].metrics

    def test_comparison_uses_identical_data():
        """比較模式確保數據完全相同"""
        results = run_comparison(
            ["squeeze_breakout", "trend_follow"], base_req)
        eq1 = results["squeeze_breakout"].equity_curve
        eq2 = results["trend_follow"].equity_curve
        assert len(eq1) == len(eq2)  # 同樣長度
```

### Level 2: 整合測試

```python
class TestUIIntegration:
    def test_streamlit_renders_without_error():
        """Dashboard 啟動不 crash"""
        # streamlit run ui/backtest_dashboard.py --server.headless true
        # 檢查 process exit code == 0

    def test_backtest_button_produces_chart():
        """按下回測按鈕後產生圖表數據"""
        result = run_backtest(make_request("squeeze_breakout", "today"))
        assert len(result.equity_curve) > 0
        assert result.metrics["total_trades"] >= 0

    def test_export_csv_matches_result():
        """匯出的 CSV 與畫面顯示一致"""
        result = run_backtest(make_request("squeeze_breakout", "today"))
        csv_df = result.trades.to_csv()
        assert str(result.metrics["total_pnl"]) in csv_df or result.metrics["total_trades"] == 0
```

### Level 3: 系統測試

```python
class TestIsolation:
    def test_backtest_does_not_affect_monitor():
        """回測 Dashboard 不影響 Trading Dashboard"""
        # 1. 記錄 monitor 的 position
        # 2. 執行回測
        # 3. 確認 monitor position 不變

    def test_concurrent_access():
        """兩個 Dashboard 同時讀 CSV 不衝突"""
        # Trading Dashboard 在寫 indicator CSV
        # Backtest Dashboard 同時在讀
        # 不應該有 lock error
```

### Level 4: 驗收 Checklist

- [ ] port 8501 啟動正常，不影響 port 8500
- [ ] 選擇今日數據 + squeeze_breakout → 回測結果合理
- [ ] 策略比較：2 個策略並排，equity curve 不同
- [ ] 參數掃描：熱力圖正確渲染，最佳參數高亮
- [ ] 匯出 CSV 可下載
- [ ] 盤中執行回測不影響 monitor 交易
- [ ] `python3 -m pytest tests/test_backtest_runner.py -v` 全部通過
