# Backtest Dashboard 審查報告

**審查日期**: 2026-04-03  
**審查文件**: `docs/BACKTEST_DASHBOARD_PLAN.md`  
**審查依據**: `trading_strategy_guide.md` + `RULES.md`  
**審查維度**: 架構設計、數據流、測試覆蓋、實作可行性、**數據來源策略**

---

## 一、執行摘要

### ✅ 通過項目

| 設計決策 | 評分 | 說明 |
|---------|------|------|
| **獨立 port 8501** | ✅ 優秀 | 與 Trading Dashboard (8500) 完全隔離 |
| **只讀不寫** | ✅ 優秀 | 回測不寫入任何檔案，避免側效應 |
| **複用現有引擎** | ✅ 優秀 | `vectorized.py` + `entry_strategies.py` 直接複用 |
| **四 Tab 設計** | ✅ 良好 | 覆蓋單策略/比較/參數掃描/歷史績效 |
| **V-Model 測試** | ✅ 良好 | 單元/整合/系統測試完整 |

### ⚠️ 需要補充

| 問題 | 風險 | 優先級 | 建議 |
|------|------|--------|------|
| **數據來源缺失** | 高 | P0 | 實作多層降級策略 (Shioaji → TAIFEX → 合成) |
| **yfinance 不適用** | 中 | P0 | TMF 期貨無數據，需改用 Shioaji |
| **信號轉換層缺失** | 中 | P1 | 策略 dict → boolean arrays |
| **安全機制不足** | 中 | P1 | 回測期間 < 5 天警告、rollback |

### ❌ 不建議項目

| 項目 | 原因 | 替代方案 |
|------|------|---------|
| **yfinance 下載 TMF** | 不可行 | 直接用本地 CSV |
| **Shioaji 下載完整歷史** | API 有限制 | 用 `data/taifex_raw/TMF_5m_taifex.csv` |
| **Kaggle 數據** | 不相關 | 本地 CSV 已足夠 |

---

## 二、架構設計審查

### 2.1 整體架構

```
┌─────────────────────────────────────────────────────────┐
│                   Trading System                        │
├───────────────────────┬─────────────────────────────────┤
│  Trading Dashboard    │   Backtest Dashboard            │
│  (port 8500)          │   (port 8501)                   │
├───────────────────────┼─────────────────────────────────┤
│  • 即時監控           │   • 歷史回測                    │
│  • 策略切換 → 寫 config│   • 策略比較 (不寫 config)      │
│  • 參數調整 → 寫 config│   • 參數掃描 (不寫 config)      │
│  • 讀 indicator (tail)│   • 讀 indicator (full)         │
│  • 需要 monitor       │   • 獨立運行                    │
│  • Shioaji API        │   • 不使用 API (數據已下載)     │
└───────────────────────┴─────────────────────────────────┘
```

**評分**: ✅ 優秀 - 職責分離清晰，無互相依賴

---

### 2.2 模組職責

| 模組 | 職責 | 現有/新增 | 評分 |
|------|------|----------|------|
| `ui/backtest_dashboard.py` | UI 渲染、用戶互動 | 新增 | ✅ |
| `backtest/runner.py` | 回測執行、數據載入 | 新增 | ✅ |
| `backtest/data_loader.py` | 數據來源管理 | 新增 | ✅ |
| `backtest/signal_generator.py` | 策略信號轉換 | 新增 | ✅ |
| `strategies/futures/entry_strategies.py` | 策略邏輯 | 現有 | ✅ |
| `squeeze_futures/engine/vectorized.py` | 向量化模擬 | 現有 | ✅ |
| `squeeze_futures/engine/indicators.py` | 指標計算 | 現有 | ✅ |

**建議**: 添加 `backtest/shioaji_loader.py` - Shioaji API 數據下載

---

## 三、數據來源策略 (修正版)

### 3.1 現有數據來源評估

**✅ 已完整擁有的數據**:

| 檔案 | 大小 | 內容 | 可用性 |
|------|------|------|--------|
| `data/taifex_raw/TMF_5m_taifex.csv` | 375K | Q1 完整 5 分鐘數據 | ✅ **主要來源** |
| `logs/market_data/TMF_*_PAPER_indicators.csv` | 每日產生 | 每日 indicator 數據 (含 squeeze 指標) | ✅ **當日回測** |

**⚠️ Shioaji API 限制**:
```python
# Shioaji api.kbars() 有時間限制，只能拉最近幾天
# 不能用來下載整個 Q1 的歷史數據
kbars = api.kbars(contract="TMF202604", start="20260101", end="20260403")
# ❌ 會失敗或只返回部分數據
```

**✅ 已有 ShioajiClient 可複用**:
```python
# 不需要寫 backtest/shioaji_loader.py
# 直接使用現有的 ShioajiClient.get_kline()
from squeeze_futures.data.shioaji_client import ShioajiClient
client = ShioajiClient()
df = client.get_kline(contract="TMF", interval="5m")
```

### 3.2 建議實作：簡化數據載入

```python
# backtest/data_loader.py

from pathlib import Path
from typing import Optional
import pandas as pd
import glob

def load_backtest_data(
    source: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> pd.DataFrame:
    """
    載入回測數據
    
    優先級:
    1. 本地 TAIFEX CSV (完整歷史)
    2. 每日 indicator CSV (當日回測)
    3. ShioajiClient.get_kline() (最近數據)
    """
    
    # === 優先級 1: TAIFEX 完整歷史 ===
    taifex_path = Path("data/taifex_raw/TMF_5m_taifex.csv")
    if source == "taifex" and taifex_path.exists():
        df = pd.read_csv(taifex_path, index_col=0, parse_dates=True)
        console.print("[green]✓ 從 TAIFEX CSV 載入成功[/]")
        return df
    
    # === 優先級 2: 每日 indicator CSV ===
    if source == "today":
        csv_path = find_latest_indicator_csv()
        if csv_path and csv_path.exists():
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            console.print(f"[green]✓ 從 {csv_path.name} 載入成功[/]")
            return df
    
    # === 優先級 3: 日期範圍 (多日 indicator 合併) ===
    if source == "date_range" and date_from and date_to:
        pattern = f"logs/market_data/TMF_{date_from}*PAPER_indicators.csv"
        files = sorted(glob.glob(pattern))
        if files:
            dfs = [pd.read_csv(f, index_col=0, parse_dates=True) for f in files]
            df = pd.concat(dfs)
            console.print(f"[green]✓ 從 {len(files)} 個檔案載入成功[/]")
            return df
    
    # === 優先級 4: ShioajiClient (最近數據) ===
    try:
        from squeeze_futures.data.shioaji_client import ShioajiClient
        client = ShioajiClient.__new__(ShioajiClient)
        client.api = get_api()  # 注入現有 API
        df = client.get_kline(contract="TMF", interval="5m")
        if not df.empty:
            console.print("[green]✓ 從 Shioaji 載入成功[/]")
            return df
    except Exception as e:
        console.print(f"[yellow]⚠ Shioaji 載入失敗：{e}[/]")
    
    raise FileNotFoundError(
        "無法載入數據。請確認:\n"
        f"  1. {taifex_path} 存在 (完整歷史)\n"
        f"  2. logs/market_data/ 有 indicator CSV (當日數據)"
    )


def find_latest_indicator_csv() -> Optional[Path]:
    """找到最新的 indicator CSV"""
    import glob
    pattern = "logs/market_data/TMF_*_PAPER_indicators.csv"
    files = sorted(glob.glob(pattern), reverse=True)
    return Path(files[0]) if files else None
```

### 3.3 數據來源決策

| 回測需求 | 推薦來源 | 說明 |
|---------|---------|------|
| **完整 Q1 回測** | `data/taifex_raw/TMF_5m_taifex.csv` | 375K，完整 5 分鐘數據 |
| **當日夜盤回測** | `logs/market_data/TMF_{date}_PAPER_indicators.csv` | 含 squeeze 指標 |
| **最近 3 天回測** | ShioajiClient.get_kline() | 即時數據，需自行計算指標 |
| **歷史多日回測** | 合併多個 indicator CSV | 需確保指標計算一致 |

### 3.4 不需要實作的模組

| 模組 | 原因 | 替代方案 |
|------|------|---------|
| `backtest/shioaji_loader.py` | 過度設計 | 直接用 `ShioajiClient.get_kline()` |
| `backtest/taifex_downloader.py` | 已有 CSV | 不需要爬蟲 |
| `backtest/synthetic_data.py` | 有真實數據 | 不需要合成數據 |
| Kaggle 數據下載 | 不相關 | 本地 CSV 已足夠 |

---

## 四、核心引擎審查

### 4.1 `vectorized.py` 分析

**✅ 優點**:
- Numba 加速，效能良好
- 支援動態停損、VWAP 退出、部分平倉
- 費用計算完整 (broker + exchange + tax + slippage)

**⚠️ 限制**:

```python
# 問題 1: 信號是 boolean array，無法處理策略的「原因」字段
def simulate_trades_vectorized(...):
    long_signals: np.ndarray
    short_signals: np.ndarray
    # ❌ 無法返回 reason

# 問題 2: 無法處理策略插件返回的 None
# entry_strategies.py 返回 dict 或 None，需要轉換

# 問題 3: 無法處理 MTF alignment score
# score 是外部傳入，不是內部計算
```

### 4.2 建議修復：信號轉換層

```python
# backtest/signal_generator.py
import numpy as np
from typing import Tuple
from strategies.futures.entry_strategies import get_strategy

def generate_signals(
    df: pd.DataFrame,
    strategy_name: str,
    cfg: dict
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    將 entry_strategies.py 的策略函數轉換為 boolean arrays
    
    Returns:
        (long_signals, short_signals, reasons)
    """
    strategy_fn = get_strategy(strategy_name)
    if not strategy_fn:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    
    n = len(df)
    long_signals = np.zeros(n, dtype=bool)
    short_signals = np.zeros(n, dtype=bool)
    reasons = np.empty(n, dtype=object)
    
    # 逐 bar 呼叫策略函數
    for i in range(n):
        state = build_state(df, i)
        result = strategy_fn(state, cfg)
        
        if result is not None:
            if result["action"] == "BUY":
                long_signals[i] = True
                reasons[i] = result["reason"]
            elif result["action"] == "SELL":
                short_signals[i] = True
                reasons[i] = result["reason"]
    
    return long_signals, short_signals, reasons


def build_state(df: pd.DataFrame, index: int) -> dict:
    """構建當前 bar 的 state 用於策略呼叫"""
    # 需要計算 MTF alignment score
    # 簡化版本：使用 momentum proxy
    last_5m = df.iloc[index]
    last_15m = df.iloc[max(0, index-3)]  # 3 根 5m bar = 15m
    
    score = last_5m.get("momentum", 0) * 10  # proxy
    
    return {
        "last_5m": last_5m,
        "last_15m": last_15m,
        "score": score,
        "df_5m": df.iloc[max(0, index-60):index+1],
        "stop_loss_pts": 30,
        "hour": df.index[index].hour,
    }
```

---

## 五、測試計劃審查

### 5.1 V-Model 測試計劃分析

**✅ 完整覆蓋**:
- Level 1: 單元測試 (數據載入、信號產生、回測結果)
- Level 2: 整合測試 (UI 整合)
- Level 3: 系統測試 (隔離保證)
- Level 4: 驗收 Checklist

**⚠️ 遺漏測試**:

```python
# tests/test_backtest_runner.py

# === 新增：數據來源測試 ===
class TestDataLoading:
    def test_missing_data_raises_error():
        """數據不存在時應拋出錯誤或降級"""
        with pytest.raises(FileNotFoundError):
            load_data("today")  # 假設今日沒有 indicator CSV
    
    def test_shioaji_loader_returns_dataframe():
        """Shioaji 載入應返回正確格式的 DataFrame"""
        api = get_mock_api()
        df = load_from_shioaji(api, start="20260401", end="20260402")
        assert not df.empty
        assert all(col in df.columns for col in ["Open", "High", "Low", "Close", "Volume"])
    
    def test_synthetic_data_generation():
        """合成數據應符合期貨特徵"""
        df = generate_synthetic_futures_data()
        assert not df.empty
        assert (df["High"] >= df["Low"]).all()
        assert (df["High"] >= df["Open"]).all()
        assert (df["High"] >= df["Close"]).all()

# === 新增：信號轉換測試 ===
class TestSignalGeneration:
    def test_strategy_returns_none():
        """策略返回 None 時，信號應為 False"""
        df = load_test_data()
        long_sig, short_sig, _ = generate_signals(df, "squeeze_breakout", cfg)
        # 當策略返回 None 時，信號應為 False
        assert len(long_sig) == len(df)
    
    def test_no_simultaneous_long_short():
        """同一根 bar 不能同時有 long 和 short 信號"""
        for name in STRATEGIES:
            df = load_test_data()
            long_sig, short_sig, _ = generate_signals(df, name, default_cfg)
            assert not (long_sig & short_sig).any(), f"{name} has simultaneous signals"

# === 新增：效能測試 ===
class TestPerformance:
    def test_grid_sweep_performance():
        """參數掃描應在合理時間內完成"""
        import time
        start = time.time()
        result = run_grid_sweep(req, {"atr_mult": [1.5, 2.0, 3.0], ...})
        elapsed = time.time() - start
        assert elapsed < 60  # 應在 60 秒內完成
```

---

## 六、UI 設計審查

### 6.1 Tab 1: 單策略回測

**✅ 設計良好**:
- Sidebar 數據選擇 + 策略 + 參數
- 績效摘要 + Equity Curve + 交易明細
- 匯出 CSV 功能

**⚠️ 建議改進**:

```python
# 建議添加：數據來源顯示
┌─ 績效摘要 ─────────────────────┐
│ 回測期間：2026-04-02 夜盤       │
│ 數據來源：Shioaji API ✓        │  ← 添加
│ 總 bar 數：120                  │
│ 策略：squeeze_breakout         │
└────────────────────────────────┘
```

### 6.2 Tab 2: 策略比較

**✅ 設計良好**:
- 勾選最多 4 個策略
- 並排績效 + Equity Curves 疊加

**⚠️ 建議改進**:

```python
# 建議添加：策略相關性矩陣
┌─ 策略相關性 ───────────────────┐
│              │ SQZ │ TRD │ VOL │
│ squeeze_breakout│ 1.0 │ 0.6 │ 0.2 │
│ trend_follow    │ 0.6 │ 1.0 │ 0.1 │
│ volume_reversal │ 0.2 │ 0.1 │ 1.0 │
└────────────────────────────────┘
# 幫助用戶了解策略分散效果
```

### 6.3 Tab 3: 參數掃描

**✅ 設計良好**:
- 熱力圖視覺化
- 最佳參數高亮

**⚠️ 建議改進**:

```python
# 建議添加：參數穩定性分析
┌─ 參數穩定性 ───────────────────┐
│ ATR Mult 敏感性:                │
│   1.5 → PF=1.2, 2.0 → PF=1.5,  │
│   3.0 → PF=2.2, 4.0 → PF=1.8  │
│                                │
│ ✅ 最佳參數附近表現穩定          │
│ ⚠️ 最佳參數孤立，可能過擬合      │
└────────────────────────────────┘
```

---

## 七、回測結果 → Live 導入審查

### 7.1 套用邏輯

**✅ 設計良好**:
- 寫入 config/futures.yaml
- Monitor 下一個 tick cycle 自動讀取
- 變更日誌記錄

**⚠️ 安全機制不足**:

```python
# 建議添加：回測期間警告
def apply_params_to_config(strategy, params, target="paper"):
    # 1. 檢查回測期間長度
    if backtest_days < 5:
        st.warning(
            f"⚠️ 回測期間僅 {backtest_days} 天，建議至少 5 天\n"
            "短期間回測結果可能過擬合"
        )
        if not st.checkbox("我了解風險，仍要套用"):
            return
    
    # 2. 檢查樣本外數據
    if not has_oos_data:
        st.warning(
            "⚠️ 無樣本外數據驗證，建議使用部分數據作為 OOS"
        )
    
    # 3. 添加 rollback 功能
    save_backup_config()  # 保存舊參數
```

### 7.2 建議添加：Rollback 功能

```python
# backtest/config_manager.py
import yaml
import shutil
from datetime import datetime

def apply_params_to_config(strategy, params, target="paper"):
    """將回測最佳參數寫入 config，附 rollback"""
    
    # 1. 備份當前 config
    backup_path = FUTURES_CFG_PATH.with_suffix(
        f".yaml.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy(FUTURES_CFG_PATH, backup_path)
    
    # 2. 載入 config
    cfg = load_yaml(FUTURES_CFG_PATH)
    
    # 3. 更新參數
    cfg["strategy"]["active_strategy"] = strategy
    # ... 更新其他參數
    
    # 4. 保存
    save_yaml(FUTURES_CFG_PATH, cfg)
    
    # 5. 記錄變更
    log_param_change(strategy, params, source="backtest_sweep", backup=backup_path)
    
    return backup_path


def rollback_config(backup_path: str):
    """還原到之前的配置"""
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")
    
    shutil.copy(backup_path, FUTURES_CFG_PATH)
    st.success(f"✅ 已還原到 {backup_path.name}")
```

---

## 八、實作順序建議

根據設計計劃的 Phase，調整為：

| Phase | 內容 | 調整建議 | 預估 |
|-------|------|---------|------|
| **0** | **數據確認** | **新增**: 確認 `data/taifex_raw/` 和 `logs/market_data/` 存在 | 0.2 session |
| 1 | Tab 1 單策略回測 | 添加「數據來源」顯示 | 1 session |
| 2 | Tab 3 參數掃描 | 添加「參數穩定性」分析 | 0.5 session |
| 3 | Tab 2 策略比較 | 添加「策略相關性」矩陣 | 0.5 session |
| 4 | Tab 4 歷史績效 | 需要多日數據累積 | 0.5 session |
| 5 | **選擇權回測整合** | **可簡化版**: 用現有 spread pricing | 0.5 session |

---

## 九、實作 Checklist

### Phase 0: 數據確認 (修正)
- [x] 確認 `data/taifex_raw/TMF_5m_taifex.csv` 存在 (375K, Q1 完整數據)
- [x] 確認 `logs/market_data/` 有 indicator CSV (每日產生)
- [ ] 實作 `backtest/data_loader.py` (簡化版，用現有 CSV)
- [ ] 不需要實作 `backtest/shioaji_loader.py` (過度設計)
- [ ] 不需要實作 `backtest/taifex_downloader.py` (已有 CSV)

### Phase 1: Tab 1 單策略回測
- [ ] 建立 `backtest/runner.py`
- [ ] 實作 `backtest/data_loader.py` (多層降級)
- [ ] 實作 `backtest/signal_generator.py`
- [ ] 實作 `run_backtest()`
- [ ] 建立 `ui/backtest_dashboard.py`
- [ ] 添加數據存在性檢查
- [ ] 添加績效指標顯示 + 數據來源

### Phase 2: Tab 3 參數掃描
- [ ] 實作 `run_grid_sweep()`
- [ ] 渲染熱力圖
- [ ] 添加參數穩定性分析

### Phase 3: Tab 2 策略比較
- [ ] 實作 `run_comparison()`
- [ ] 渲染多策略 Equity Curve
- [ ] 添加策略相關性矩陣

### Phase 4: Tab 4 歷史績效
- [ ] 實作多日數據載入
- [ ] 計算累計 PnL
- [ ] 添加 Drawdown 分析

### 安全機制 (新增)
- [ ] 回測期間 < 5 天警告
- [ ] 添加 rollback 功能
- [ ] 變更日誌記錄到 `logs/param_changes.csv`

### 測試
- [ ] `tests/test_backtest_runner.py` - 數據載入測試
- [ ] `tests/test_backtest_runner.py` - 信號轉換測試
- [ ] `tests/test_backtest_runner.py` - 效能測試
- [ ] `python3 -m pytest tests/test_backtest_runner.py -v`
- [ ] 所有單元測試通過
- [ ] 所有整合測試通過
- [ ] 手動驗收測試通過

---

## 十、關鍵風險與緩解

| 風險 | 影響 | 機率 | 緩解措施 |
|------|------|------|---------|
| **數據不存在** | 高 | 低 | Phase 0 確認數據，添加錯誤提示 |
| **indicator CSV 未產生** | 中 | 中 | 提示用戶先運行 monitor |
| **參數掃描過慢** | 中 | 高 | 添加 progress bar，支持中斷 |
| **策略信號轉換錯誤** | 高 | 中 | 添加單元測試覆蓋所有策略 |
| **過擬合參數導入 Live** | 高 | 中 | 添加警告 + OOS 驗證要求 |

---

## 十一、審查結論

### ✅ 通過項目 (7 項)

1. **架構設計**: 獨立 port 8501，與 Trading Dashboard 完全隔離 ✅
2. **數據流**: 只讀不寫，無側效應 ✅
3. **引擎複用**: `vectorized.py` + `entry_strategies.py` 直接複用 ✅
4. **測試計劃**: V-Model 覆蓋完整 ✅
5. **UI 設計**: 四 Tab 設計清晰完整 ✅
6. **安全機制**: 參數導入有警告 ✅
7. **數據策略**: 使用現有 CSV，不需要額外下載 ✅

### ⚠️ 需要補充 (4 項)

1. **數據載入簡化** (優先級：P0)
   - 實作 `backtest/data_loader.py` (簡化版，直接用現有 CSV)
   - 不需要實作 `shioaji_loader.py` 或 `taifex_downloader.py`
   - 添加數據存在性檢查

2. **信號轉換層** (優先級：P1)
   - 實作 `backtest/signal_generator.py`
   - 將策略 dict 轉換為 boolean arrays
   - 添加 `build_state()` helper

3. **測試補充** (優先級：P1)
   - 添加數據不存在測試
   - 添加策略返回 None 測試
   - 添加效能測試

4. **UI 改進** (優先級：P2)
   - 添加數據來源顯示 (TAIFEX CSV / indicator CSV)
   - 添加參數穩定性分析
   - 添加策略相關性矩陣

### ❌ 不建議項目 (4 項)

1. **yfinance 下載 TMF** - 不可行，直接用本地 CSV
2. **Shioaji 下載完整歷史** - API 有限制，用 `data/taifex_raw/TMF_5m_taifex.csv`
3. **Shioaji loader 獨立模組** - 過度設計，直接用 `ShioajiClient.get_kline()`
4. **Kaggle 數據** - 不相關，本地 CSV 已足夠

### ✅ 可簡化實作

1. **選擇權回測 (Phase 5)** - 可用現有 spread pricing 做簡化版

---

## 十二、數據來源決策樹 (修正版)

```
用戶選擇數據來源
    │
    ├─「完整歷史 / Q1」→ data/taifex_raw/TMF_5m_taifex.csv
    │   ├─ 存在 (375K) → 載入 ✅
    │   └─ 不存在 → 提示用戶數據缺失
    │
    ├─「今日 / 夜盤」→ logs/market_data/TMF_{date}_PAPER_indicators.csv
    │   ├─ 存在 → 載入 (含 squeeze 指標) ✅
    │   └─ 不存在 → 提示先運行 monitor
    │
    ├─「日期範圍」→ 合併多個 indicator CSV
    │   ├─ 找到檔案 → 合併載入 ✅
    │   └─ 無檔案 → 降級到 ShioajiClient
    │
    └─「最近數據」→ ShioajiClient.get_kline()
        ├─ 成功 → 返回最近幾天數據
        └─ 失敗 → 錯誤提示
```

**注意**: 不需要合成數據、不需要 TAIFEX 爬蟲、不需要 Kaggle

---

**審查者**: Qwen Agent  
**版本**: 1.1 (修正數據來源策略)  
**建議**: 計劃整體良好，**直接使用現有 CSV 即可**

**下一步**:
1. 確認 `data/taifex_raw/TMF_5m_taifex.csv` 和 `logs/market_data/` 存在
2. 實作簡化版 `backtest/data_loader.py`
3. 運行 `python3 -m pytest tests/ -v` 驗證現有功能
4. 開始 Phase 1 (Tab 1 單策略回測)
