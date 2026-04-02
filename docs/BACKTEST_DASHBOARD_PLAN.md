# Backtest Dashboard — 設計計畫 (v2)

> v2 修訂：整合 Qwen 審查報告的可用建議，移除過度設計部分

## 目標

獨立的 Streamlit Dashboard（port 8501），不影響盤中 monitor（port 8500）。
可在盤中或盤後執行回測，支援策略比較、參數掃描、可視化分析。

---

## 架構

```
port 8500 — Trading Dashboard（現有，盤中監控）
port 8501 — Backtest Dashboard（新增，回測分析）

兩者完全獨立，不共享 process/state。
回測 Dashboard 只讀取本地 CSV，不碰 Shioaji API。
```

```
ui/backtest_dashboard.py
├── Tab 1: 📊 單策略回測
│   ├── 選擇策略（8 種期貨 + 5 種選擇權）
│   ├── 選擇數據（今日 / 歷史日期 / Q1 完整）
│   ├── 調整參數（slider）
│   ├── 執行回測 → 結果表格 + 圖表
│   └── 匯出 CSV
│
├── Tab 2: 🔄 策略比較
│   ├── 勾選多個策略（最多 4 個）
│   ├── 同一數據、同一時段
│   └── 並排 equity curve + 績效指標
│
├── Tab 3: 🔬 參數掃描
│   ├── 選擇策略 + 掃描參數（grid）
│   ├── 熱力圖 + 最佳參數高亮
│   ├── 鄰近參數穩定性檢查（過擬合警告）
│   └── 「套用到 PAPER」按鈕
│
└── Tab 4: 📈 歷史績效
    ├── 每日 PnL 曲線（從 indicator CSV 累計）
    ├── Drawdown 分析
    └── 策略切換時間軸
```

---

## 數據來源（已有，不需新增 loader）

| 來源 | 路徑 | 內容 | 用途 |
|------|------|------|------|
| 今日 indicator | `logs/market_data/TMF_{date}_PAPER_indicators.csv` | 5m OHLCV + squeeze 指標 | 當日回測 |
| 歷史 indicator | `logs/market_data/TMF_*_indicators.csv` | 同上，多日 | 多日回測 |
| **Q1 完整數據** | `data/taifex_raw/TMF_5m_taifex.csv` (375KB) | 5m OHLCV | 長期回測 |
| 交易紀錄 | `exports/trades/TMF_*_trades.csv` | 歷史交易 | 績效分析 |

數據載入降級策略（簡化版）：
```
用戶選「今日」→ logs/market_data/TMF_{today}_indicators.csv
用戶選「指定日期」→ logs/market_data/TMF_{date}_indicators.csv
用戶選「Q1 完整」→ data/taifex_raw/TMF_5m_taifex.csv
找不到 → 顯示錯誤提示（不用合成數據）
```

---

## 信號轉換層（新增，Qwen 審查建議）

`entry_strategies.py` 回傳 dict，`vectorized.py` 需要 boolean array，需要轉換：

```python
# backtest/signal_generator.py

def generate_signals(df, strategy_name, cfg):
    """策略 dict → boolean arrays，逐 bar 呼叫策略函數"""
    strategy_fn = get_strategy(strategy_name)
    n = len(df)
    long_signals = np.zeros(n, dtype=bool)
    short_signals = np.zeros(n, dtype=bool)

    for i in range(60, n):  # 需要 60 bars warmup
        state = build_state(df, i)
        result = strategy_fn(state, cfg)
        if result:
            if result["action"] == "BUY": long_signals[i] = True
            elif result["action"] == "SELL": short_signals[i] = True

    return long_signals, short_signals
```

---

## Tab 1: 單策略回測 — UI 設計

```
┌─ Sidebar ──────────────────────┐  ┌─ Main ──────────────────────────────┐
│                                │  │                                     │
│ 📅 數據選擇                    │  │  績效摘要                            │
│ ○ 今日夜盤                     │  │  ┌────┬────┬────┬────┬────┐         │
│ ○ 指定日期 [____]              │  │  │ PnL│Win%│ PF │ DD │Trd │         │
│ ○ Q1 完整數據                  │  │  │+13K│8.3%│2.21│-11%│ 12 │         │
│                                │  │  └────┴────┴────┴────┴────┘         │
│ 🎯 策略                        │  │  數據來源: TMF_20260402 (879 bars)   │
│ [squeeze_breakout        ▼]    │  │                                     │
│ ℹ️ 策略說明...                  │  │  Equity Curve                       │
│                                │  │  ┌─────────────────────────────┐    │
│ ⚙️ 參數                        │  │  │    ╱╲    ╱╲                 │    │
│ ATR Mult: [===●===] 1.5       │  │  │   ╱  ╲  ╱  ╲    ╱╲         │    │
│ Entry Score: [===●===] 20     │  │  │  ╱    ╲╱    ╲  ╱  ╲        │    │
│ VWAP Exit: [✓]                │  │  │ ╱              ╲╱    ╲╱     │    │
│ Lots: [===●===] 2             │  │  └─────────────────────────────┘    │
│                                │  │                                     │
│ [▶ 執行回測]                   │  │  交易明細                            │
│ [📥 匯出 CSV]                  │  │  ┌──────┬────┬────┬─────┬─────┐    │
│                                │  │  │ Time │ Dir│Price│ PnL │Reason│   │
└────────────────────────────────┘  └─────────────────────────────────────┘
```

---

## Tab 3: 參數掃描 + 套用

```
┌─ 熱力圖 ──────────────────────────────────────────────┐
│  ATR Mult                                              │
│       1.5    2.0    3.0    4.0                         │
│  10 │ -5K │  +2K │ +13K │  +8K                        │
│  20 │ -3K │  +5K │★+15K │ +10K   ← 點擊              │
│  30 │ -8K │  +1K │  +9K │  +6K                        │
└────────────────────────────────────────────────────────┘

點擊 ★ 後：
┌─ 套用設定 ─────────────────────────────────────────────┐
│  策略: squeeze_breakout                                │
│  ATR Mult: 3.0  (現有: 1.5) ⚠️                        │
│  Entry Score: 20  (現有: 20) ✓                         │
│                                                        │
│  鄰近參數穩定性:                                        │
│  ATR 2.0→PF=1.5  ATR 3.0→PF=2.2  ATR 4.0→PF=1.8     │
│  ✅ 鄰近表現穩定                                        │
│                                                        │
│  ⚠️ 回測期間僅 1 天，建議至少 5 天                       │
│                                                        │
│  [✅ 套用到 PAPER]  [↩️ 還原上次設定]                    │
└────────────────────────────────────────────────────────┘
```

### 套用安全機制

| 機制 | 說明 |
|------|------|
| 差異對比 | 顯示「現有 vs 新值」 |
| 過擬合警告 | 回測 < 5 天顯示警告 |
| 鄰近穩定性 | 最佳參數的上下格 PnL 差異 > 50% → 警告「可能過擬合」 |
| Config 備份 | 套用前自動備份 `futures.yaml.backup.{timestamp}` |
| 一鍵還原 | 從最近的 backup 還原 |
| 變更日誌 | 記錄到 `logs/param_changes.csv` |
| PAPER 優先 | 預設只套用到 PAPER，LIVE 需二次確認 |

---

## SDD: 介面契約

```python
@dataclass
class BacktestRequest:
    strategy: str                    # "squeeze_breakout" | "trend_follow" | ...
    data_source: str                 # "today" | "date:{YYYYMMDD}" | "taifex_q1"
    params: dict                     # {"atr_mult": 3.0, "entry_score": 20, ...}
    lots: int = 2
    initial_balance: float = 100000

@dataclass
class BacktestResult:
    trades: pd.DataFrame             # timestamp, direction, price, pnl, reason
    equity_curve: pd.Series          # cumulative equity
    metrics: dict                    # pnl, win_rate, pf, max_dd, sharpe, trades
    params: dict                     # echo back params used
    data_info: dict                  # {"source": "...", "bars": N, "period": "..."}

def run_backtest(req: BacktestRequest) -> BacktestResult:
    """
    Preconditions:
      - req.strategy in STRATEGIES
      - data_source 對應的 CSV 存在
    Postconditions:
      - result.equity_curve.iloc[0] == initial_balance
      - result.trades PnL includes fees
    Invariants:
      - 不寫入任何檔案
      - 不呼叫 Shioaji API
    """

def run_grid_sweep(req, sweep_params) -> pd.DataFrame:
    """每個 combo 獨立執行，結果可 pivot 成熱力圖"""

def run_comparison(strategies: list[str], req) -> dict[str, BacktestResult]:
    """同一數據，多策略並行，結果互相獨立"""

def apply_params(strategy, params) -> Path:
    """備份 config → 寫入新參數 → 記錄變更 → 回傳 backup path"""

def rollback_params(backup_path: Path):
    """從 backup 還原 config"""
```

### 數據流

```
User selects params
    │
    ▼
load_data(source)  ← 讀本地 CSV，不呼叫 API
    │
    ▼
calculate_futures_squeeze(df)  ← 現有指標計算
    │
    ▼
generate_signals(df, strategy_fn)  ← 新增：dict → boolean array
    │
    ▼
simulate_trades_vectorized(...)  ← 現有回測引擎
    │
    ▼
BacktestResult → UI 渲染
```

---

## V-Model 測試計畫

### Level 1: 單元測試

```python
class TestDataLoading:
    def test_load_today():
        """今日 CSV 載入正確"""
    def test_load_specific_date():
        """指定日期 CSV 載入正確"""
    def test_load_taifex_q1():
        """Q1 完整數據載入正確"""
    def test_missing_date_shows_error():
        """不存在的日期回傳空 + 錯誤訊息"""

class TestSignalGeneration:
    def test_signals_match_strategy():
        """信號與手動計算一致"""
    def test_no_simultaneous_long_short():
        """同一 bar 不能同時 long + short"""
    def test_warmup_period_no_signals():
        """前 60 bars 不產生信號"""

class TestBacktestResult:
    def test_equity_starts_at_initial():
        """Equity 起始 = initial_balance"""
    def test_pnl_includes_fees():
        """PnL 扣手續費"""
    def test_no_side_effects():
        """回測不寫入任何檔案"""

class TestGridSweep:
    def test_covers_all_combos():
        """涵蓋所有參數組合"""
    def test_results_independent():
        """每個 combo 結果獨立"""

class TestApplyParams:
    def test_backup_created():
        """套用前建立 backup"""
    def test_rollback_restores():
        """還原後 config 與 backup 一致"""
    def test_short_period_warning():
        """回測 < 5 天觸發警告"""
```

### Level 2: 整合測試

```python
def test_full_flow():
    """選數據 → 選策略 → 跑回測 → 看結果 → 套用參數"""

def test_comparison_same_data():
    """比較模式用完全相同的數據"""
```

### Level 3: 系統測試

```python
def test_no_impact_on_monitor():
    """回測不影響 Trading Dashboard"""

def test_concurrent_csv_read():
    """monitor 寫 CSV 同時 backtest 讀 CSV 不衝突"""
```

### Level 4: 驗收 Checklist

- [ ] port 8501 啟動正常
- [ ] 選今日數據 + squeeze_breakout → 結果合理
- [ ] 策略比較：2 策略並排，equity curve 不同
- [ ] 參數掃描：熱力圖正確，最佳參數高亮
- [ ] 鄰近穩定性檢查顯示正確
- [ ] 套用參數 → config 更新 + backup 建立
- [ ] 還原 → config 恢復
- [ ] `python3 -m pytest tests/test_backtest_runner.py -v` 全過

---

## 實作順序

| Phase | 內容 | 預估 |
|-------|------|------|
| 1 | `backtest/signal_generator.py` + `backtest/runner.py` + Tab 1 單策略回測 | 1 session |
| 2 | Tab 3 參數掃描 + 熱力圖 + 穩定性檢查 + 套用/還原 | 0.5 session |
| 3 | Tab 2 策略比較（多策略 equity curve 疊加） | 0.5 session |
| 4 | Tab 4 歷史績效（多日 PnL + Drawdown） | 0.5 session |
| 5 | 選擇權簡化版回測（ThetaGang spread pricing） | 1 session |

---

## 與現有系統的關係

```
Trading Dashboard (8500)          Backtest Dashboard (8501)
├── 即時監控                       ├── 歷史回測
├── 策略切換 → 寫 config           ├── 策略比較（不寫 config）
├── 參數調整 → 寫 config           ├── 參數掃描 → 可選「套用」
├── 讀 indicator CSV (tail)        ├── 讀 indicator CSV (full)
└── 需要 monitor 運行              └── 獨立運行，不需 monitor

共用（只讀）：
├── config/*.yaml（顯示當前設定，套用時才寫）
├── strategies/futures/entry_strategies.py（策略邏輯）
├── squeeze_futures/engine/vectorized.py（回測引擎）
├── squeeze_futures/engine/indicators.py（指標計算）
└── logs/market_data/*.csv + data/taifex_raw/*.csv（歷史數據）
```

---

## 啟動方式

```bash
# 獨立啟動（不影響 monitor）
python3 -m streamlit run ui/backtest_dashboard.py --server.port 8501 --server.headless true

# 或加到 autostart.sh
tmux new-window -t unified:2 -n "backtest" \
  "cd $UNIFIED_DIR && python3 -m streamlit run ui/backtest_dashboard.py --server.port 8501"
```
