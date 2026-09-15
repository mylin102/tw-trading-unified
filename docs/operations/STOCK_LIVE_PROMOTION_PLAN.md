# Stock 上 Live 推廣計畫（v1.2 草案）

- 日期：2026-09-15（v1.2：修正 D2 架構矛盾、D1 正式定案、P1-10 補委託生命週期）
- 狀態：**草案 v1.2，待核定後派工**
- 角色：Hermes（auditor）／ codex（implementer）
- 適用範圍：`strategies/stocks/`（台灣股票，PAPER → LIVE）

---

## 0. 現況（實測，2026-09-15）

| 項目 | 實況 |
|---|---|
| 交易總開關 | `config/stocks.yaml` → `live_trading: false`（PAPER） |
| 執行路徑 | `strategies/stocks/monitor.py:950, 1011` **直接呼叫 `self.api.place_order(contract, order)`** —— 無 gateway / intent / authorization / audit 層 |
| 既有防護 | `monitor.py:452` 每輪重讀 config：若 `live_trading=false` 即強制降級（kill-switch 基礎） |
| 候選名單 | `config/stocks.yaml` watchlist；**每日 sync cron 目前失敗（macOS TCC／`failed to make path absolute`）且寫錯樹（git tree vs release16）→ 名單停在 2026-09-08** |
| 目前模式 | PAPER（`STOCK_*_PAPER_orders.json`）；今日 0 筆 |
| 風控參數（已存在） | capital_per_trade 100000／total_portfolio_budget 300000／stop_loss_pct 0.02／trailing_stop_pct 0.01／take_profit_pct 0.125／bear_defense{enabled, max_positions 1, max_daily_loss 3000, max_consecutive_losses 3}／normal max_positions 3 |

---

## 1. 已核定決策（USER，2026-09-15）

### D1｜策略版本（正式定案）
```yaml
strategy_version: mean_reversion_enhanced
```
硬性要求：
1. **config / runtime / paper / shadow / live 使用同一版本**（單一來源，不得各處不同）
2. 啟動 log 必須記錄 `strategy_version` **與 code SHA**
3. **plain `mean_reversion` 不得作為 fallback**（查無版本即失敗，不得靜默退回）
4. 版本不一致 → **`PROMOTION_HOLD`**（fail-closed，不得繼續）

**角色界定**：`CANSLIM`（leaders.json）＝ **watchlist／候選股來源**；`mean_reversion_enhanced` ＝ **進場策略與 regime filter**。除非程式確實實作且有測試，**不得宣稱「CANSLIM alpha」是 enhanced 的交易邏輯**。

### D2｜治理深度（已修正架構矛盾）
> 採用**單一 chokepoint 函式**，**不導入** futures 式多層 authorization 狀態機；但 chokepoint **必須執行單層、可追蹤、fail-closed 的 pre-trade authorization**。

（前一版寫「不收 authorization」是錯的 —— 那會讓 P1-2 的「未授權拒絕」沒有實作位置。）

### D3｜資金配置
**維持**：capital_per_trade 100000／total_portfolio_budget 300000／normal max_positions 3／bear max_positions 1。

### D4｜零股
**納入**。理由（USER）：每筆 10 萬上限下，高價股一手（1000 股）動輒超過 10 萬，**不支援零股則 30 萬總額根本用不到**。

---

## 2. 目標架構（唯一管線）

```text
strategy signal (mean_reversion_enhanced)
→ stock_order_chokepoint          ← 唯一入單入口
   ├─ intent durable persist      （結構化：時間/symbol/side/qty/價/策略/理由/decision id）
   ├─ authorization / risk checks （單層、可追蹤、fail-closed）
   ├─ idempotency                 （decision/bar 去重 + 本地未結單 + 券商持倉）
   └─ broker place_order
→ broker receipt / callback
→ durable ledger                  （不變量：broker terminal fill ⇒ ledger row）
→ reconciliation
```

### 必要（不可省 —— 便宜的 80% 價值）
1. **單一送單咽喉（chokepoint）**：含**單層 authorization** + intent durable persist + idempotency（見上圖）
2. **broker truth 對帳 + durable ledger**：不變量「**broker terminal fill ⇒ ledger row（同 trade_id）**」，任何 hold/異常都不得阻止落帳
3. **kill-switch + cancel-all SOP**：flag 只擋新單；在途/已掛單要有明確處置程序

### 明確排除
- futures 式**多層** authorization 狀態機、exit-only capability、attestation、quarantine
- 組合單／雙腿生命週期／到期換月
- **排除的是多層狀態機與部署儀式，不是基本授權檢查。**

**理由**：futures 的 authorization 之所以「麻煩」，主因是組合單＋雙腿生命週期＋到期換月；股票無腿、無 release、現金交割、隨時可出，套用同一套狀態機只會產生誤報與摩擦（futures 本週已實證）。

---

## 3. Phase 1：上 live 硬條件（逐項驗收）

| # | 項目 | 驗收標準（須給證據） |
|---|---|---|
| P1-1 | 候選名單 sync 修復 + config parity | ① TCC 修正後**真實 cron tick** 產出 SUCCESS ② 運行樹 stocks.yaml 更新 ③ monitor 重載 ④ 名單新鮮度 ≤ 24h |
| P1-2 | **單一送單咽喉** | 全 repo **僅 1 個**入單函式；chokepoint 內含單層 authorization（intent persist → 檢查 → idempotency → place_order）；未授權／超限／重複 → **拒絕**（附測試）|
| P1-3 | pre-trade 檢查逐條生效 | 每筆上限、總額上限、單日虧損、最大檔數、bear 模式、可交易時段、漲跌停、**零股規則** —— 各一條測試 |
| P1-4 | broker truth 對帳 + 不變量 | periodic read-only detector；分歧 → 事件 + fail-closed；不變量測試（含 incident replay 形式） |
| P1-5 | fill receipt | deal callback → ledger（dedupe／exactly-once）；重複不重複入帳；漏收有修復路徑；查詢不可用 → **UNAVAILABLE（非空）** |
| P1-6 | ledger / settlement | 成交→部位→成本→出場結算（手續費公定價×折扣、證交稅）；**T+2 交割**正確 |
| P1-7 | kill-switch 演練 | 關 flag → 新單被擋；在途取消；對帳一致 |
| P1-8 | 部署治理 | config 進凍結樹、`LRC_RELEASE_SHA` pin、三 app 一致、gate PASS |
| P1-9 | paper/live parity | 滑價、手續費/稅、部分成交、漲跌停、零股時段、時段差異各一項證據 |
| **P1-10** | **零股（odd lot）交易支援** | ① 以**股**為單位（1–999）與整股（1000 倍數）分流 ② 零股時段判定（盤中 09:00–13:30 每 3 分鐘撮合／盤後 14:00–14:30）③ 零股撮合價/漲跌停/tick ④ **部分成交**處理 ⑤ **零股最低手續費** ⑥ **委託取消與重掛**（見下）⑦ 各項附測試 |
| **P1-11** | **策略版本治理（D1）** | ① 單一來源 `strategy_version` ② 啟動 log 含版本+code SHA ③ plain 不得 fallback ④ 不一致 → `PROMOTION_HOLD` ⑤ 各一條測試 |

### P1-10 委託生命週期（必要測試）
- 零股**未成交**時是否取消（時點與條件明確）
- **取消後是否允許重掛**；重掛是否使用**新的 idempotency key**
- **收盤前未成交委託**如何處理（明確、不得留殭屍單）
- **零股與整股不得錯誤合併**計算可用額度（額度/部位分開計算）
- **部分成交後，剩餘數量不得被誤認為已成交**

**必測邊界矩陣**：None/NaN/Inf 價格、缺 K 線、過期 artifact、第二 session、漲跌停、資金不足、重複 callback、券商查詢失敗（UNAVAILABLE）、零股部分成交、零股最低手續費、**取消/重掛 idempotency**。

---

## 4. Phase 2：上線授權與首日 Protocol

1. **形式授權**：使用者明確下令 + 正式 promotion gate（fail-closed，不繞過）
2. **首日最小規模**：1 檔 × 最小單位（零股可）；盤中逐筆對帳；隨時可 kill
3. **緊急 SOP**：手動平倉程序 + 通報路徑 + 事後 RCA

---

## 5. Phase 3：維運

- 對帳 detector + 每日報告/告警
- **log rotation**（`pm2-stock-out.log` 已 564MB，同類風險曾導致 disk 事件）
- watchlist 新鮮度 SLO（> 24h → 告警）

---

## 6. 風險與緩解

| 風險 | 為什麼 | 緩解 |
|---|---|---|
| 多個送單點 | 現有 ≥2 處 `place_order` | P1-2 收斂成單一 chokepoint |
| 策略 bug 直達券商 | 無閘門 | chokepoint 單層 authorization + 硬性上限 + 異常檢查 |
| 重複送單 | 無冪等鍵 | chokepoint idempotency（decision/bar + 券商持倉） |
| 無審計鏈 | 事後無法回答「誰/何時/為何」 | intent durable persist |
| kill-switch 邊界 | flag 只擋新單 | SOP：關 flag + cancel-all + 對帳 |
| 對帳缺口 | 本地/券商分歧隱形 | P1-4 detector + 不變量 |
| 零股流動性薄 | 部分成交常態、掛單可能不成 | P1-10 委託生命週期（取消/重掛/收盤處置） |
| 零股＋整股混算 | 額度/部位錯誤 | P1-10 明文分開計算 |
| 策略版本漂移 | paper/live 不同版本 | P1-11 單一來源 + PROMOTION_HOLD |
| 股票特有 | T+2 交割、漲跌停、費稅 | P1-3/P1-6/P1-9 |

---

## 7. 與 futures 的對照（避免重蹈覆轍）

| 教訓（本週 futures 實證） | 股票版的落實 |
|---|---|
| 真實成交被多層閘門靜默丟棄 | P1-5：成交先落帳；hold 只擋新單 |
| absence-based 推論（查不到＝沒有） | P1-5：查詢失敗 → UNAVAILABLE，嚴禁推論 |
| 樂觀前移（本地領先券商） | P1-6：部位/成本等 broker terminal 才動 |
| 驗收只做路徑單測 | P1-4/P1-9：不變量 + incident replay + parity |
| 部署儀式過重（每次數小時） | P1-8：identity 檢查為主，manifest 只在合約檔變更時做 |

---

## 8. 派工硬性要求（原文，不得改寫）

> Stock 採單一 order chokepoint，不建立 futures 式多層 authorization/quarantine 狀態機；但 chokepoint 必須具備單層 fail-closed authorization、intent durable persist、idempotency、broker receipt 與 reconciliation。策略正式使用 `mean_reversion_enhanced`，禁止 plain fallback。CANSLIM 僅作候選名單來源，除非另有實作證據，不得宣稱為 enhanced 的交易 alpha。P1-10 必須涵蓋零股部分成交、最低手續費、取消與重掛。上述測試未完成前，維持 `live_trading: false`。
