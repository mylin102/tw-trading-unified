# Stock 上 Live 推廣計畫（v1.3 草案）

- 日期：2026-09-15（v1.3：**修正 P1-10 零股交易規格**，經 TWSE 官方規格查證）
- 狀態：**草案 v1.3，待核定後派工**
- 角色：Hermes（auditor）／ codex（implementer）
- 適用範圍：`strategies/stocks/`（台灣股票，PAPER → LIVE）

---

## 0. 現況（實測，2026-09-15）

| 項目 | 實況 |
|---|---|
| 交易總開關 | `config/stocks.yaml` → `live_trading: false`（PAPER） |
| 執行路徑 | `strategies/stocks/monitor.py:950, 1011` **直接呼叫 `self.api.place_order(contract, order)`** —— 無 gateway / intent / authorization / audit 層 |
| 既有防護 | `monitor.py:452` 每輪重讀 config：若 `live_trading=false` 即強制降級（kill-switch 基礎） |
| 候選名單 | `config/stocks.yaml` watchlist；**每日 sync cron 目前失敗（macOS TCC）且寫錯樹（git vs release16）→ 名單停在 2026-09-08** |
| 目前模式 | PAPER（`STOCK_*_PAPER_orders.json`）；今日 0 筆 |
| 風控參數（已存在） | capital_per_trade 100000／total_portfolio_budget 300000／stop_loss_pct 0.02／trailing_stop_pct 0.01／take_profit_pct 0.125／bear_defense{enabled, max_positions 1, max_daily_loss 3000, max_consecutive_losses 3}／normal max_positions 3 |

---

## 1. 已核定決策（USER，2026-09-15）

### D1｜策略版本（正式定案）
```yaml
strategy_version: mean_reversion_enhanced
```
硬性要求：
1. **config / runtime / paper / shadow / live 使用同一版本**（單一來源）
2. 啟動 log 必須記錄 `strategy_version` **與 code SHA**
3. **plain `mean_reversion` 不得作為 fallback**（查無版本即失敗，不得靜默退回）
4. 版本不一致 → **`PROMOTION_HOLD`**（fail-closed）

**角色界定**：`CANSLIM`（leaders.json）＝ **watchlist／候選股來源**；`mean_reversion_enhanced` ＝ **進場策略與 regime filter**。除非程式確實實作且有測試，**不得宣稱「CANSLIM alpha」是 enhanced 的交易邏輯**。

### D2｜治理深度（已修正架構矛盾）
> 採用**單一 chokepoint 函式**，**不導入** futures 式多層 authorization 狀態機；但 chokepoint **必須執行單層、可追蹤、fail-closed 的 pre-trade authorization**。

### D3｜資金配置
**維持**：capital_per_trade 100000／total_portfolio_budget 300000／normal max_positions 3／bear max_positions 1。

### D4｜零股
**納入**（每筆 10 萬上限下，高價股一手動輒超過 10 萬；不支援零股則 30 萬總額用不到）。

---

## 2. 目標架構（唯一管線）

```text
strategy signal (mean_reversion_enhanced)
→ stock_order_chokepoint          ← 唯一入單入口
   ├─ intent durable persist      （時間/symbol/side/qty/價/策略版本/理由/decision id/idempotency key）
   ├─ authorization / risk checks （單層、可追蹤、fail-closed）
   ├─ idempotency                 （decision/bar 去重 + 本地未結單 + 券商持倉）
   └─ broker place_order
→ broker receipt / callback
→ durable ledger                  （不變量：broker terminal fill ⇒ ledger row）
→ reconciliation
```

### 必要（不可省）
1. **單一送單咽喉**：含單層 authorization + intent durable persist + idempotency
2. **broker truth 對帳 + durable ledger**：不變量「broker terminal fill ⇒ ledger row（同 trade_id）」
3. **kill-switch + cancel-all SOP**

### 明確排除
- futures 式**多層** authorization 狀態機、exit-only capability、attestation、quarantine
- 組合單／雙腿生命週期／到期換月
- **排除的是多層狀態機與部署儀式，不是基本授權檢查。**

---

## 3. Phase 1：上 live 硬條件（逐項驗收）

| # | 項目 | 驗收標準 |
|---|---|---|
| P1-1 | 候選名單 sync 修復 + config parity | cron tick 實證 SUCCESS；運行樹 stocks.yaml 更新；monitor 重載；新鮮度 ≤ 24h |
| P1-2 | **單一送單咽喉** | 全 repo **僅 1 個**入單函式（內含單層 authorization → intent persist → idempotency → place_order）；未授權／超限／重複 → 拒絕（附測試）|
| P1-3 | pre-trade 檢查逐條生效 | 每筆上限、總額、單日虧損、最大檔數、bear 模式、可交易時段、漲跌停、零股規則 —— 各一條測試 |
| P1-4 | broker truth 對帳 + 不變量 | periodic read-only detector；分歧 → 事件 + fail-closed；不變量測試（含 incident replay） |
| P1-5 | fill receipt | deal callback → ledger（dedupe／exactly-once）；漏收有修復路徑；查詢不可用 → UNAVAILABLE（非空） |
| P1-6 | ledger / settlement | 成交→部位→成本→出場結算（手續費×折扣、證交稅）；T+2 交割正確 |
| P1-7 | kill-switch 演練 | 關 flag → 新單被擋；在途取消；對帳一致 |
| P1-8 | 部署治理 | config 進凍結樹、`LRC_RELEASE_SHA` pin、三 app 一致、gate PASS |
| P1-9 | paper/live parity | 滑價、費稅、部分成交、漲跌停、零股時段、時段差異各一項證據 |
| **P1-10** | **零股（odd lot）交易支援** | 見 §3.1（規格已按 TWSE 官方查證修正）|
| **P1-11** | **策略版本治理（D1）** | 單一來源；啟動 log 含版本+code SHA；plain 不得 fallback；不一致 → `PROMOTION_HOLD`；各一條測試 |

### 3.1 P1-10 零股交易規格（**已修正**，依 TWSE 官方）

**盤中零股**
- 委託時間：**09:00–13:30**（限價、當日有效；僅電子交易，專業機構投資人除外）
- 撮合：**09:10 第一次撮合，其後每 5 秒以集合競價撮合**（113/12/02 起由 1 分鐘縮短為 5 秒）
- **價格穩定措施**：第一次撮合成交後至 13:25，如試算成交價較前次成交價漲跌逾 **±3.5%** → **該次撮合延緩 2 分鐘**（延緩期間續接受申報、取消及變更；每 10 秒揭露模擬撮合資訊）
- **未成交之盤中零股委託不保留至盤後零股**

**盤後零股**
- 委託時間：**13:40–14:30**（限價、當日有效）
- 撮合：**14:30 一次集合競價**（價格優先；同價位以電腦隨機排列決定順序）

**共通規則**
- 數量：**1–999 股**（整股為 1000 股之倍數，1–499 張）
- 零股**不得**融資融券、借券賣出、當沖
- 零股申報及成交之價格**不作為**當日開盤、收盤、最高、最低行情之依據
- **零股與整股為兩個獨立市場**：報價、成交、可用額度**不得混算**

**實作要求（硬性）**
- **不得硬編碼撮合間隔**（「每 3 分鐘」為舊制、已廢；且 **115/12/07 起**盤中零股申報時間將改為 08:30–13:30、09:00 第一次撮合）→ 一律**依交易所規格與實際券商 API 實作並測試**
- 參考：[TWSE 集中市場交易制度](https://www.twse.com.tw/zh/products/system/trading.html)、[TWSE 盤中零股交易制度](https://www.twse.com.tw/downloads/zh/trading/introduce/introduce4-1.pdf)

**必要測試**
- 時段判定（盤中/盤後/非交易時段）
- **未成交取消**（時點與條件明確）
- **取消後重掛**：允許與否明確；重掛使用**新的 idempotency key**
- **收盤清盤**：收盤前未成交委託的明確處置（不得留殭屍單）
- **部分成交**：剩餘數量不得被誤認為已成交；部位/成本/出場處理正確
- **零股最低手續費**
- **零股／整股市場分離**：報價、成交、**可用額度不得合併計算**

---

## 4. Phase 2：上線授權與首日 Protocol

1. **形式授權**：使用者明確下令 + 正式 promotion gate（fail-closed，不繞過）
2. **首日最小規模**：1 檔 × 最小單位（零股可）；盤中逐筆對帳；隨時可 kill
3. **緊急 SOP**：手動平倉程序 + 通報路徑 + 事後 RCA

---

## 5. Phase 3：維運

- 對帳 detector + 每日報告/告警
- **log rotation**（`pm2-stock-out.log` 已 564MB）
- watchlist 新鮮度 SLO（> 24h → 告警）

---

## 6. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 多個送單點 | P1-2 單一 chokepoint |
| 策略 bug 直達券商 | chokepoint 單層 authorization + 硬性上限 + 異常檢查 |
| 重複送單 | chokepoint idempotency（decision/bar + 券商持倉） |
| 無審計鏈 | intent durable persist |
| kill-switch 邊界 | SOP：關 flag + cancel-all + 對帳 |
| 對帳缺口 | P1-4 detector + 不變量 |
| 零股流動性薄／部分成交 | P1-10 委託生命週期 + 部分成交處理 |
| 零股＋整股混算 | P1-10 明文分開計算 |
| 零股規格誤寫（舊制） | P1-10 依官方規格 + 禁硬編碼 + 測試 |
| 策略版本漂移 | P1-11 單一來源 + PROMOTION_HOLD |
| 股票特有 | T+2 交割、漲跌停、費稅（P1-3/P1-6/P1-9） |

---

## 7. 與 futures 的對照

| 教訓（futures 本週實證） | 股票版落實 |
|---|---|
| 真實成交被多層閘門靜默丟棄 | P1-5：成交先落帳；hold 只擋新單 |
| absence-based 推論 | P1-5：查詢失敗 → UNAVAILABLE，嚴禁推論 |
| 樂觀前移 | P1-6：部位/成本等 broker terminal 才動 |
| 驗收只做路徑單測 | P1-4/P1-9：不變量 + incident replay + parity |
| 部署儀式過重 | P1-8：identity 檢查為主 |

---

## 8. 派工硬性要求（原文，不得改寫）

> Stock 採單一 order chokepoint，不建立 futures 式多層 authorization/quarantine 狀態機；但 chokepoint 必須具備單層 fail-closed authorization、intent durable persist、idempotency、broker receipt 與 reconciliation。策略正式使用 `mean_reversion_enhanced`，禁止 plain fallback。CANSLIM 僅作候選名單來源，除非另有實作證據，不得宣稱為 enhanced 的交易 alpha。P1-10 必須涵蓋零股部分成交、最低手續費、取消與重掛。上述測試未完成前，維持 `live_trading: false`。

> **零股規格修正（原文）**：修正 P1-10 的零股交易規格：不得使用「每 3 分鐘撮合」或「盤後 14:00–14:30」。依台灣證交所與實際券商 API 的當前規格實作盤中零股集合競價、盤後零股 13:40–14:30 委託及 14:30 撮合；盤中未成交委託不得自動延續至盤後。補上時段、取消、收盤清盤、部分成交及零股／整股市場分離測試。
