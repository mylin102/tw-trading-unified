# Dashboard UI 操作邏輯設計

## 1. 參數即時調整

### 設計原則
- Dashboard 上可調整的參數限定在**安全範圍**內，防止誤操作
- 修改後寫入 YAML config，策略 loop 下一輪自動 reload（hot-reload）
- 有持倉時，停損/停利參數修改需額外確認

### 可調參數與範圍

| 參數 | 期貨範圍 | 選擇權範圍 | 即時生效 |
|------|----------|------------|----------|
| entry_score | 10 ~ 100 | 50 ~ 100 | ✅ 下一棒 |
| stop_loss_pts / stop_loss_pct | 20 ~ 200 pts / 0.05 ~ 0.50 | 同左 | ✅ 下一棒 |
| tp1_pts / tp1_pct | 20 ~ 200 pts / 0.3 ~ 3.0 | 同左 | ✅ 下一棒 |
| lots_per_trade | 1 ~ 5 | 1 ~ 3 | ✅ 下次進場 |
| max_positions | 1 ~ 5 | 1 ~ 3 | ✅ 下次進場 |
| force_close_at_end | on/off | on/off | ✅ 即時 |

### 不可調參數（需重啟）
- weights（多週期權重）
- pricing_model
- active_mode（day/night）

### UI 元件
```
st.slider("Entry Score", min=50, max=100, value=90)
st.number_input("Stop Loss %", min=0.05, max=0.50, step=0.05)
```
修改後顯示 diff 預覽 → 按「套用」→ 寫入 YAML → toast 通知

---

## 2. 模擬 ↔ 真實交易切換

### 切換流程（Double Confirm）

```
用戶點擊 [切換至 LIVE] 按鈕
    ↓
第一層確認：st.warning 彈出
    "即將切換至真實交易，所有訂單將送出至永豐。確定？"
    [確認] [取消]
    ↓
第二層確認：輸入確認碼
    "請輸入 CONFIRM-LIVE 以確認"
    ↓ 輸入正確
寫入 config: live_trading: true
策略 hot-reload 生效
    ↓
Dashboard 頂部顯示紅色橫幅：🔴 LIVE TRADING ACTIVE
```

### 安全規則
- 有持倉時**禁止切換**（必須先平倉）
- LIVE → PAPER 切換只需單次確認（降風險方向）
- 切換後 30 秒內可一鍵 rollback

### UI 狀態指示
- PAPER：綠色 badge `📝 PAPER`
- LIVE：紅色 badge `🔴 LIVE`，頂部常駐紅色橫幅

---

## 3. 真實資金管理（共用帳戶）

### 核心問題
期貨和選擇權共用同一個永豐期權帳戶（`futopt_account`），保證金互相排擠。

### 資金讀取
```python
# 透過共用 session 讀取
margin_info = api.get_account_margin()
available = margin_info.available_margin      # 可用保證金
equity = margin_info.equity                   # 權益數
maintenance = margin_info.maintenance_margin  # 維持保證金
```
Dashboard 頂部即時顯示，每 30 秒更新。

### 資金分配機制

採用**預留制**，在 config 中設定各策略的資金上限：

```yaml
# config/risk_global.yaml
account:
  total_equity_source: "api"        # 從 API 讀取真實權益數
  margin_reserve_pct: 0.20          # 保留 20% 不動用（安全墊）

allocation:
  futures:
    max_margin_pct: 0.40            # 最多用 40% 權益做期貨
    max_lots: 3
  options:
    max_margin_pct: 0.40            # 最多用 40% 權益做選擇權
    max_lots: 2
```

### 下單前檢查流程

```
策略觸發進場訊號
    ↓
RiskGate 檢查：
  1. 讀取 api.get_account_margin()
  2. 計算該策略已用保證金
  3. 已用 + 本次所需 > max_margin_pct × equity？
     → YES: 拒絕下單，log "margin exceeded"
     → NO: 放行
  4. 全帳戶可用保證金 < reserve_pct × equity？
     → YES: 拒絕下單，log "reserve breached"
     → NO: 放行
    ↓
送出訂單
```

### 停損互相排擠的解法

| 情境 | 問題 | 解法 |
|------|------|------|
| 期貨停損觸發需要保證金 | 選擇權佔用太多 | 選擇權 max_margin_pct 限制 + reserve 保護 |
| 兩邊同時停損 | 保證金不足 | reserve_pct 20% 確保至少能平倉 |
| 選擇權買方 | 不需保證金（已付權利金） | 只佔用權利金額度，不影響期貨保證金 |

> 選擇權買方（目前策略）只付權利金，不佔保證金。所以實際排擠主要是：
> - 期貨保證金 vs 選擇權權利金
> - 兩者加總不超過 80% 權益（留 20% reserve）

---

## 4. 模擬交易 Reset

### Reset 範圍
- 清空該策略的 trade ledger CSV
- 重置 equity curve CSV
- 重置 PnL 統計

### 期初資金

**模擬交易的期初資金各自獨立**，不共用：

```yaml
# config/futures.yaml
execution:
  initial_balance: 100000    # 期貨模擬期初資金

# config/options_strategy.yaml（新增）
paper_trading:
  initial_balance: 40000     # 選擇權模擬期初資金
```

理由：
- 真實交易時資金是共用的（同一帳戶），由 API 讀取
- 模擬交易時各自獨立，方便分別評估策略績效
- Reset 只影響該策略，不影響另一個

### UI 操作
```
[🔄 重置期貨模擬] → 確認 → 清空期貨 ledger，重設期初資金
[🔄 重置選擇權模擬] → 確認 → 清空選擇權 ledger，重設期初資金
期初資金輸入框（各自獨立）
```

---

## 5. 資金與指數變化 Chart

### 總覽 Tab — 雙軸圖

```
左 Y 軸：帳戶權益（TWD）
右 Y 軸：台指期 / MTX 價格
X 軸：時間

圖層：
  - 權益曲線（面積圖，綠色）
  - MTX 價格（線圖，灰色）
  - 進場點（▲ 綠色三角）
  - 出場點（▼ 紅色三角）
```

### 資料來源

| 模式 | 權益來源 | 更新頻率 |
|------|----------|----------|
| LIVE | `api.get_account_margin().equity` | 每 30 秒 |
| PAPER | 累計 PnL + initial_balance | 每棒更新 |

### 各策略 Tab — 獨立績效圖

```
期貨 Tab：
  - 期貨 PnL 曲線
  - TMF 價格走勢
  - 進出場標記

選擇權 Tab：
  - 選擇權 PnL 曲線
  - MTX 價格走勢
  - 進出場標記 + Greeks 面板
```

### 合併權益圖（LIVE 模式）

LIVE 模式下，總覽 Tab 額外顯示：
- 帳戶總權益（API 讀取）
- 期貨已實現 PnL（從 ledger 累計）
- 選擇權已實現 PnL（從 ledger 累計）
- 三條線疊在同一張圖上，看各策略對總權益的貢獻

---

## 6. Dashboard 頁面結構總結

```
┌─────────────────────────────────────────────────┐
│ 🔴 LIVE / 📝 PAPER    權益: $XXX,XXX   可用: $XXX,XXX │  ← 頂部狀態列
├─────────────────────────────────────────────────┤
│ [📈 總覽] [🔵 期貨] [🟠 選擇權] [⚙️ 設定]      │  ← Tab 切換
├─────────────────────────────────────────────────┤
│                                                 │
│  總覽 Tab:                                       │
│    - 權益 + 指數雙軸圖                            │
│    - 期貨/選擇權即時指標卡片                       │
│    - 今日交易摘要                                 │
│                                                 │
│  期貨 Tab:                                       │
│    - TMF 價格 + Score 走勢                        │
│    - 交易記錄表                                   │
│    - 期貨 PnL 曲線                               │
│                                                 │
│  選擇權 Tab:                                     │
│    - MTX 價格 + Score 走勢                        │
│    - Trade Ledger                                │
│    - Greeks 面板                                  │
│                                                 │
│  設定 Tab:                                       │
│    - 參數滑桿（即時調整）                          │
│    - LIVE/PAPER 切換（double confirm）            │
│    - 資金分配設定                                 │
│    - 模擬 Reset                                  │
│                                                 │
├─────────────────────────────────────────────────┤
│ Sidebar: 策略參數摘要 / 模式狀態 / 刷新設定        │
└─────────────────────────────────────────────────┘
```
