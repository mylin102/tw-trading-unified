# CANSLIM leaders.json 產出邏輯改善計畫

## Goal

改善 `tw-canslim-web` 端 `leaders.json` 的品質，讓 composite_score 真正能區分強弱勢股，並排除不該在 leader 清單中的 ETF/低品質個股。

## 已完成的前置步驟

**B 端已改完：** `tw-trading-unified/core/external_feature_provider.py`
- 新增 `_is_valid_leader()` 過濾：ETF、rs_rating<=0、industry_rank>=999
- 排序改為：industry_rank ASC → rs_rating DESC → composite_score DESC
- 測試：5 passed

現在處理 **A 端** — `tw-canslim-web/export_canslim.py` 的 `_export_leaders_json()`

---

## 根因分析

### 問題1：ETF 進入 leader list

`final_universe_symbols = sorted(list(core_set.union(excel_symbols)))`

`core_symbols` 包含 `etf_symbols` bucket（0050/0056/00878）。B 端已擋掉，但 A 端根本該排除。

### 問題2：composite_score 無區辨力

行 446-449：
```python
blended_score = 0.7 * (canslim_score / 100.0) + 0.3 * (revenue_score / 6.0)
```

多數個股 canslim_score=70, revenue_score=6 → **blended=0.79**，所有人擠在同一個值。
完全沒用到 `rs_rating`，而 rs_rating 反而是最有區辨力的指標。

### 問題3：excel fallback 給 industry_rank=999

行 419-428：當個股只在 excel 中有資料但 batch 還沒輪到，`industry_rank` 直接給 999（無產業排名）。B 端會過濾掉這些股，但 A 端根本不該把缺失資料的個股列入 leader。

### 問題4：沒分 tier

211 檔全部 tagged `leader`。事實上應該分 tier 或至少讓 composite_score 能分級。

---

## 實作狀態 (2026-05-04)

### ✅ A 端 — export_canslim.py 修改完成

**檔案：** `tw-canslim-web/export_canslim.py`

| Task | 改動 | 行數 |
|------|------|------|
| 1. 排除 ETF | 在 `final_universe_symbols` 後加 `s.startswith("00")` 過濾 + log | ~405 |
| 2. 改善 composite_score | 舊公式 `0.7*C + 0.3*R` → 新公式 `0.4*C + 0.3*RS + 0.3*R`，另加 rs_rating<=0 時打 0.7 折 | ~460 |
| 3. excel fallback industry_rank | 從 `ticker_info` 查找 industry，預設值 500(非 999)不被 B 端過濾 | ~435 |
| 4. summary log | 輸出 `avg_rs`、`industry_ranked 比例`、`composite_range[min, max]` | ~500 |

**測試 (RED-GREEN-REFACTOR)：**
- `test_export_revenue.py`: 舊預期值 (0.86, 0.56) → 新預期值 (0.791, 0.491) → **2 passed**
- `test_export_schema.py::test_validate_artifact_payload_accepts_leaders_contract`: **passed**
- `test_core_selection.py` + `test_revenue_selection.py`: 全部 **passed**
- 唯一 1 FAIL 是既有 `test_validate_resume_stock_entry_rejects_missing_nested_contract_fields`，與本次改動無關

### ✅ B 端強化 — external_feature_provider.py (Phase 2)

| 強化 | 實作 | 位置 |
|------|------|------|
| Drop reason stats | `_is_valid_leader` 回傳 `tuple[bool, str]` + `drop_stats` 追蹤 etf/rs_zero/no_industry | ~113, ~195 |
| Score drift guard | composite_score range < 0.15 → WARNING, out of [0,1] → ERROR | ~203 |
| 產業集中度限制 | `MAX_PER_INDUSTRY=5`，同產業最多取 5 檔 | ~215 |
| 下限保護 | 過濾後 < 5 檔 → fallback 到未過濾排序 | ~231 |

**B 端測試：** `test_external_feature_provider.py` → 5 passed

## 計畫原文（已實作）

### Task 1：排除 ETF

**檔案：** `export_canslim.py` 第 402-403 行

```python
# 改前：
final_universe_symbols = sorted(list(core_set.union(excel_symbols)))

# 改後：
final_universe_symbols = sorted(
    list(core_set.union(excel_symbols))
)
final_universe_symbols = [
    s for s in final_universe_symbols
    if not s.startswith("00")
]
```

- 避免 0050/0056/00878 進入 leader list
- 對齊 B 端過濾規則

### Task 2：改善 composite_score 公式

**檔案：** `export_canslim.py` 第 446-449 行

```python
# 改前：
canslim_score = float(canslim.get("score") or 0.0)
revenue_score = float(canslim.get("revenue_score") or 0.0)
blended_score = 0.7 * (canslim_score / 100.0) + 0.3 * (revenue_score / 6.0)

# 改後：
canslim_score = float(canslim.get("score") or 0.0)
revenue_score = float(canslim.get("revenue_score") or 0.0)

# Normalize rs_rating to 0-1 scale
rs_weight = min(1.0, max(0.0, rs_rating / 100.0))

# Three-factor blend: CANSLIM + RS + Revenue
# RS gives differentiation even when canslim_score plateaus at 70
blended_score = (
    0.4 * (canslim_score / 100.0)
    + 0.3 * rs_weight
    + 0.3 * (revenue_score / 6.0)
)
```

效果預期：
- 2330 (rs=86): 0.4×0.85 + 0.3×0.86 + 0.3×1.0 = **0.898** ✅
- 2308 (rs=70): 0.4×0.70 + 0.3×0.70 + 0.3×1.0 = **0.790**
- 一般股 (rs=50): 0.4×0.70 + 0.3×0.50 + 0.3×1.0 = **0.730**
- 低RS股 (rs=20): 0.4×0.70 + 0.3×0.20 + 0.3×1.0 = **0.640** ← 自然被排序淘汰

分數分布會從「全部 0.79」拉開到 **0.64~0.90**。

### Task 3：excel fallback 給合理 industry_rank

**檔案：** `export_canslim.py` 第 419-428 行

```python
# 改前：
entry = {
    "symbol": symbol,
    ...
    "industry_rank": 999,
    ...
}

# 改後：
# Try to get industry from stock_data or ticker_info
industry = None
if stock_data:
    industry = stock_data.get("industry")
elif symbol in self.ticker_info:
    industry = self.ticker_info[symbol].get("industry")
fallback_rank = industry_rank_map.get(industry, 999) if industry else 999

entry = {
    "symbol": symbol,
    ...
    "industry_rank": min(fallback_rank, 999),  # 999 still means unknown
    ...
}
```

### Task 4（可選）：加入新鮮度檢查 log

在 leaders.json 的 payload 中，`generated_at` 已經存在，但在 `_export_leaders_json` 結束時加一行 log 顯示摘要：

```python
logger.info(
    "Leaders export complete: %d leaders, "
    "avg_rs=%.1f industry_ranked=%d/%d composite_range=[%.3f, %.3f]",
    len(universe),
    sum(r.get("rs_rating",0) for r in universe) / max(len(universe),1),
    sum(1 for r in universe if r.get("industry_rank",999)<999),
    len(universe),
    min((r.get("composite_score",0) for r in universe), default=0),
    max((r.get("composite_score",0) for r in universe), default=0),
)
```

---

## 受影響檔案

| File | Change Type | Impact |
|------|-------------|--------|
| `tw-canslim-web/export_canslim.py` | **修改** (3 處) | leader 產出品質 |
| `tw-canslim-web/data/leaders.json` | 間接 | 下一次 publish 後自動更新 |

## 測試與驗證

1. **語法檢查：** `python3 -m py_compile export_canslim.py`
2. **現有測試：** `cd tw-canslim-web && python3 -m pytest tests/ -v --timeout=60`
3. **手動驗證：** 跑 `_export_leaders_json` 的 branch，看 composite_score 分布
4. **B 端驗證：** 同步一次 `sync_external_watchlist.py`，確認過濾後的名單改善

## 時程

| Task | 預估 | 備註 |
|------|------|------|
| 1. 排除 ETF | 2min | 一行過濾 |
| 2. 改善 score 公式 | 5min | 三因子權重調整 |
| 3. excel fallback industry | 5min | 從 ticker_info 找 industry |
| 4. 新鮮度 log | 3min | 訊息記錄 |
| 驗證 | 10min | 測試+手動檢查 |

## 風險

- **composite_score 改變可能衝擊已有依賴**：B 端 sync 已經改為用 industry_rank+rs+composite 三層排序，新的分數只會讓排序更合理，不會造成倒退
- **excel fallback 的 industry 可能不精準**：ticker_info 的 industry 可能比 batch-processed 的粗略，但至少比 999 好
- **測試環境可能缺少 parquet 檔案**：如果測試需要完整的 parquet 才能跑 `build_core_universe`，可能需要 mock

## 開放問題

1. target_size=300 是否太大？現在 core_symbols 有 300 檔，全部進 leaders.json。是否需要下修 target_size，或是在 export 時只取 top N（如 top 50）？
2. 下次 publish leaders.json 後，git commit 會觸發 GitHub Actions deploy，需要確認 deploy 流程會自動更新 raw URL 的 leaders.json
