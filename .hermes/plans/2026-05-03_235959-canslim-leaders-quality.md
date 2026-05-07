# CANSLIM 領頭羊資料品質改善計畫

## Goal

解決 CANSLIM watchlist 同步後出現大量傳產股（非強勢產業領頭羊）的問題。確保 watchlist 中的股票來自強勢產業且有足夠的相對強度。

## Current Context

### 資料流（已確認）
```
tw-canslim-web (GitHub Actions, daily)
  └─ export_canslim.py._export_leaders_json()
       └─ data/leaders.json ── GitHub Raw ──▶ tw-trading-unified
                                                 └─ external_feature_provider.py
                                                      └─ sync_external_watchlist.py
                                                           └─ config/stocks.yaml
```

### 根因

**A. tw-canslim-web 端 (export_canslim.py)**

`_export_leaders_json()` 第 446-449 行 composite_score 公式：
```python
blended_score = 0.7 * (canslim_score / 100.0) + 0.3 * (revenue_score / 6.0)
```
- 多數個股 CANSLIM score=70, revenue_score=6/6 → blended=0.79，缺乏區辨力
- `core_symbols` 包含 ETF (00開頭)、watchlist 舊股，全部灌進 leader list
- 沒有過濾 ETF 或 rs_rating=0 的低品質個股
- `api/ranking.json` 和 `api/stock_features.json` 目前是空的 `{}`

**B. tw-trading-unified 端 (external_feature_provider.py)**

- `_normalize_snapshot()` 第 164-167 行：當 ranking 為空時，直接從 leaders.json 取 top N by composite_score
- **沒有任何過濾**：ETF、rs_rating=0、industry_rank=999 的個股全部原樣進入 watchlist
- composite_score 無法區分強弱 → watchlist 中傳產和 ETF 擠在前面

### 現行篩選 (sync_watchlist.py)
- `score>=70 AND conditions>=5/7` → 從 local data.json 篩選
- 但 watchlist 已被 sync_external_watchlist.py 覆蓋 (直接替換)

## 受影響的檔案

| File | Repo | Change Type |
|------|------|-------------|
| `core/external_feature_provider.py` | tw-trading-unified | **修改** — 加過濾邏輯 |
| `scripts/sync/sync_watchlist.py` | tw-trading-unified | **修改** — 改用產業分層篩選 |
| `export_canslim.py` (optional) | tw-canslim-web | **修改** — 改善 leaders.json 品質 |
| `tests/test_external_feature_provider.py` | tw-trading-unified | 新增測試 |

## 實作狀態 (2026-05-04)

### ✅ Step 1 完成 — external_feature_provider.py 已修改

**位置：** `core/external_feature_provider.py`

**加的函數：** `_is_valid_leader(row)` 第 113-133 行
- 排除 ETF (symbol 開頭 00)
- 排除 rs_rating <= 0
- 排除 industry_rank >= 999

**改的邏輯：** `_normalize_snapshot()` 第 186-206 行
- 先過濾 → 再按 (industry_rank ASC, rs_rating DESC, composite_score DESC) 排序
- 取 top N (max_watchlist_size)
- 輸出 log 顯示 filter before/after/removed

**測試：** `pytest tests/test_external_feature_provider.py` → 5 passed，無回歸

### Step 1: 修改 external_feature_provider.py（短期見效）

在 `_normalize_snapshot()` 中，當 ranking 為空、使用 leaders 時，加過濾條件：

1. **過濾 ETF**：排除 symbol 以 `00` 開頭
2. **過濾低 RS**：排除 `rs_rating == 0` 的個股
3. **過濾無產業排名**：排除 `industry_rank == 999`（可選，太嚴格）
4. **按 composite_score 排序後取 top N**（保留現有邏輯，但輸入已過濾）

位置：`core/external_feature_provider.py` 第 164-168 行

### Step 2: 修改 sync_watchlist.py（雙管齊下）

從 local `data.json` 改用**產業強度排名過濾**取代單純的 score>=70 取前10：

- 從 `industry_strength` 取得產業排名
- 在 top 10 產業中尋找 score>=50 的個股
- 每產業最多取 2 檔，確保產業分散
- 排除 ETF
- 總數維持 10 檔
- 保留與 sync_external_watchlist 並存的互補機制

### Step 3: 改善 leaders.json（長期、可選）

修改 `export_canslim.py` 的 `_export_leaders_json()`：

1. 排除 ETF 進入 leaders list
2. 改善 composite_score 計算：加入 `rs_rating` 權重，讓分數有區辨力
3. 對無 excel_ratings 的個股降權

### Step 4: 驗證

1. 跑 `sync_external_watchlist.py` → 確認 watchlist 不含 ETF
2. 跑 `sync_watchlist.py` → 確認產業分散 + 真領頭羊
3. `pytest tests/test_external_feature_provider.py -v`
4. 檢查 `config/stocks.yaml` 結果

## 時程

| Step | 預估時間 | 依賴 |
|------|---------|------|
| 1. external_feature_provider.py | 15min | 無 |
| 2. sync_watchlist.py | 20min | Step 1 完成 |
| 3. export_canslim.py | 30min | 熟悉產出流程 |
| 4. 驗證 | 15min | Step 1-3 |

## 風險與注意事項

- **sync_watchlist.py vs sync_external_watchlist.py 衝突**：兩者可能互相覆蓋 watchlist。確認目前哪個才是主流程。
- **Data freshness**：5/3 的 data.json 是週日產出，週一開盤前應該還行
- **export_canslim.py 改動需在 tw-canslim-web 端**：需要確認 GitHub Actions 自動化流程
- **Step 3 修改可能影響 dashboard 顯示**：composite_score 變動可能影響已有使用該分數的 UI

## 開放問題

1. 是否要統一用 `sync_external_watchlist.py` (GitHub feed) 還是 `sync_watchlist.py` (local data.json)？
   - 目前兩者都有，後者覆蓋前者。建議定一個為主要管線。
2. export_canslim.py 的修改要先做，還是等 Step 1&2 確定有效再進行？
