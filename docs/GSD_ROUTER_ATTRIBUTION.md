# GSD Plan: Router Attribution + V-Model + Doc Sync

**GSTACK**: Search first → docs/router_attribution.py 已存在可直接用
**SDD**: SSOT (FuturesRouterDecision),  side effects after validation (attribution log)
**V-Model**: Unit → Integration → System → UAT

---

## Phase 1: 整合 AttributionRecorder 進 Router

### 1.1 核心實作 — `AttributionRecorder` 整合進 router

**檔案**:
- `core/futures_strategy_router.py` — 注入 recorder，在每個決策點記錄
- `docs/router_attribution.py` — 現成，直接 copy 到 core/

**實作要點**:
1. 將 `docs/router_attribution.py` 搬移到 `core/attribution_recorder.py`
   - 保持 API 不變 (`log_router_row`, `log_signal`, `log_trade`, `summarize_router`, `build_starvation_report`, `export_csv`)
2. 在 `route_futures_signal()` 簽名中加入 `recorder: AttributionRecorder | None = None`
   - 可選參數，不影響現有 caller
3. 在每個 candidate 評估點記錄：
   - `recorder.log_router_row(...)` — candidate / no_signal / winner / shadowed
4. 在 return FuturesRouterDecision 前記錄：
   - `recorder.log_signal(...)` — 最終訊號

**V-Model 驗證**:

| Level | Test Name | Precondition | Input | Expected |
|-------|-----------|-------------|-------|----------|
| 1.1.1 | test_recorder_logs_candidate | Router + recorder | WEAK regime, 3 candidates | 3 log_router_row calls with candidate status |
| 1.1.2 | test_recorder_logs_winner | Router + recorder, counter_vwap fires | Bar that triggers counter_vwap | winner logged with correct strategy name |
| 1.1.3 | test_recorder_logs_shadowed | Router + recorder, counter_vwap fires | 3 candidates, only first fires | Remaining 2 logged as shadowed |
| 1.1.4 | test_recorder_noop_when_none | Router, recorder=None | Any bar | No AttributeError, normal operation |
| 1.1.5 | test_starvation_index_zero | First bar, all strategies eval'd | 3 candidates, 1 winner | Starvation index = 0 for all |
| 1.1.6 | test_starvation_index_grows | kbar_feature shadowed 10x | 10 bars where counter_vwap wins | kbar_feature starvation = 10 |

### 1.2 匯出機制 — 定時寫入 CSV

**實作要點**:
- `AttributionRecorder.export_csv()` 每 N bars 或每秒 flush
- 寫入 `logs/attribution/` 目錄
- 檔案格式：`attribution_{date}.csv`

**V-Model 驗證**:

| Level | Test Name | Precondition | Input | Expected |
|-------|-----------|-------------|-------|----------|
| 1.2.1 | test_export_csv_creates_file | Recorder with 5 rows | export_csv() | File exists, 5 data rows + header |
| 1.2.2 | test_export_csv_column_names | Recorder with 1 row | export_csv() | Has ts, regime, candidate, status, strategy, reason |
| 1.2.3 | test_export_no_data | Empty recorder | export_csv() | File with header only, or no file created |

---

## Phase 2: Starvation Monitoring 與 Reporting

### 2.1 Starvation Report 腳本

**檔案**: `scripts/attribution_report.py`

**實作要點**:
- 讀取 `logs/attribution/` 下的 CSV
- 計算每個策略的：
  - `candidate_count`: 被當作 candidate 的次數
  - `eval_count`: 實際被評估的次數 (前面 winner 沒中斷)
  - `winner_count`: 成為 winner 的次數
  - `shadowed_count`: 被前面策略擋住的次數
  - `starvation_index`: shadowed / candidate
- 輸出表格到 terminal + `logs/attribution_report_{date}.csv`

**V-Model 驗證**:

| Level | Test Name | Precondition | Input | Expected |
|-------|-----------|-------------|-------|----------|
| 2.1.1 | test_report_counts_correct | 100 rows mock CSV | run_report() | counter_vwap: 40 candidates, 10 winners, 30 shadowed |
| 2.1.2 | test_starvation_index_formula | kbar_feature: 50 candidates, 45 shadowed | run_report() | starvation = 0.90 |
| 2.1.3 | test_empty_data | Empty CSV | run_report() | Empty table, no crash |

### 2.2 Dashboard 整合 (可選)

**實作要點**:
- 在 dashboard 加一個頁面/tab 顯示 attribution 摘要
- 即時 starvation index
- 每個策略的 winner/shadowed 比例

**V-Model 驗證**:

| Level | Test Name | Precondition | Input | Expected |
|-------|-----------|-------------|-------|----------|
| 2.2.1 | test_dashboard_attribution_renders | Flask test client | GET /attribution | 200 OK, contains strategy table |
| 2.2.2 | test_dashboard_realtime_update | Recorder with data | GET /attribution/api | JSON with current stats |

---

## Phase 3: 文件同步更新

### 3.1 Futures_Router_Flow.md

**檔案**: `docs/Futures_Router_Flow.md`

**變更**:
1. Regime 名稱統一：WEAK_DIRECTIONAL → WEAK, 移除 CHOP
2. Candidates 表格 match FuturesRouterConfig 實作
3. 移除 `strategy.supports_regime()` 段落
4. 加入 kbar_feature 已加入 weak_strategies 的狀態
5. 加入 AttributionRecorder 說明（記錄什麼、怎麼讀 starvation report）

### 3.2 V_MODEL_PLUGGABLE_STRATEGIES.md

**檔案**: `docs/V_MODEL_PLUGGABLE_STRATEGIES.md`

**變更**:
- 加入 Level 1.8: AttributionRecorder Unit Tests
- 加入 Level 2.5: Router + AttributionRecorder Integration Tests

### 3.3 router_attribution.py

**檔案**: `docs/router_attribution.py` → **搬移到** `core/attribution_recorder.py`
- 在 docs/ 留 symlink 或 README 說明

---

## 實作順序

```
Phase 1.1 (core/attribution_recorder.py + router 注入)
  ↓
Phase 1.1 V-Model tests (6 tests)
  ↓ 跑全部測試確認無回歸
Phase 1.2 (CSV 匯出 + 2 tests)
  ↓
Phase 2.1 (scripts/attribution_report.py + 3 tests)
  ↓
Phase 2.2 (dashboard 頁面 + 2 tests)
  ↓
Phase 3 (文件更新)
  ↓
Final: 全測試套件 + UAT 確認
```

## 預估測試總數

| Phase | Tests | 新增檔案 |
|-------|-------|---------|
| 1.1 | 6 | core/attribution_recorder.py |
| 1.2 | 3 | — |
| 2.1 | 3 | scripts/attribution_report.py |
| 2.2 | 2 | dashboard 修改 |
| **Total** | **14** | |

## Exit Criteria

1. 全測試套件通過 (599+14 = 613 passed)
2. router 在 production 中記錄 attribution data 到 logs/attribution/
3. `scripts/attribution_report.py` 可產出 starvation report
4. `docs/Futures_Router_Flow.md` match 實作
5. `docs/V_MODEL_PLUGGABLE_STRATEGIES.md` 包含新 tests
