# 2026-06-30 Dashboard 效能重構

## 修改摘要

### 1. 頁面 routing：st.tabs() → st.selectbox + session state
- **問題**: `st.tabs()` 不 lazy load，所有 tab 在每次 rerun 都執行完整 I/O + Plotly render
- **解決**: 
  - sidebar 頂部改用 `st.selectbox()` + `st.session_state` 做單頁 routing
  - 只有選中的頁面會執行對應的 rendering code
  - 5 個原本是死代碼的 `with tab_*:` 區塊改為正確的 `elif page == ...` chain
- **影響**: 每次 rerun 只走一個頁面的 I/O + render，不再 7 頁全跑

### 2. 自動刷新策略
- **問題**: 原本 `st_autorefresh(interval=60_000)` 定時整頁 rerun，無使用者控制
- **解決**:
  - 保留手動「🔄 重新載入資料」按鈕（清 cache + rerun）
  - 新增可選「⏱️ 自動更新間隔」下拉（關閉 / 15s / 30s / 60s / 120s / 300s）
  - 預設「關閉」，使用者自行決定何時啟用自動更新

### 3. Cache TTL 優化
- **問題**: 所有 `@st.cache_data(ttl=5)` 只有 5 秒有效期，類比沒有快取
- **解決**: 10 個 `ttl=5` 全部提升為 `ttl=30`（30 秒），減少重複 CSV I/O
  - `load_futures_indicators`、`load_futures_trades`
  - `load_options_indicators`、`load_options_ledger`、`load_options_equity`
  - `load_stock_trades`、`load_stock_orders`、`load_stock_indicators`

### 4. 設定頁面儲存反饋
- **問題**: 按下「儲存並重啟期貨/選擇權模組」後只顯示「設定已更新」，不知道寫入了哪個檔案
- **解決**: 成功訊息現在顯示實際檔案路徑（如 `config/futures.yaml`）+ 重啟提示

### 5. 其他修正
- `_TICKER` 提前讀取：在 sidebar 渲染前就從 config 讀取 ticker，避免 NameError
- `st_autorefresh` import 正確加回（僅在啟用自動更新時使用）
- 移除死代碼：`st.sidebar.radio` 重複定義清理

## 檔案
- `ui/dashboard.py` — 主要變更
