# R-005：MTS HVWAP 提早釋放 A/B（Shadow 對照）規格

狀態：`DRAFT — 未實作、未部署`
作者：Hermes（唯讀分析 2026-10-28）
資料窗口：2026-10-05 ~ 2026-10-28（PAPER ledger）

---

## 1. 目的

驗證一個假設：**HVWAP 提早釋放（release）正在把「本來會回來的腿」提前實現成虧損**，若抑制該釋放、讓既有 Policy J 合併出場管理整個價差部位，淨 PnL 是否更好。

本文件只定義「如何量測」，不授權任何行為變更。

## 2. 現況證據（唯讀，已核對）

資料來源：`$TRADING_RUNTIME_DIR/paper/logs/mts_trade_fills.jsonl`、`mts_spread_events.jsonl`（snapshot mtime 2026-10-28 09:36）。

### 2.1 拆腿 vs 合併出場

| 出場模式 | 筆數 | 淨 PnL (TWD) |
|---|---:|---:|
| 拆腿（RELEASE + 單腿 EXIT） | 54 | **-7,798.7** |
| COMBINED_EXIT | 170 | +13,502.4 |
| 合計 | 224 | +5,703.7 |

拆腿細分：

| 釋放腿 | n | 釋放腿本身 | 留下的腿 | 合計 |
|---|---:|---:|---:|---:|
| NEAR | 34 | -4,906.9 | -148.9 | -5,055.8 |
| FAR | 20 | -7,222.0 | +4,479.1 | -2,742.9 |

### 2.2 釋放時序

- 釋放全部由 `HVWAP_RELEASE_INTENT` 觸發（`gate_mode=PAPER_ACTIVE`，paper-only）。
- 進場→釋放：**22/54 在 10 秒內**；NEAR 中位數 12 秒。
- <60 秒釋放：28 筆，**-4,821.1**（佔拆腿虧損 62%，平均 -172/筆）。
- ≥60 秒釋放：26 筆，-2,977.6（平均 -114/筆）。

### 2.3 釋放當下的 HVWAP 欄位

- `consecutive_confirmed_bars`：28 筆 = 0、20 筆 = 1，**從未 ≥ 2**。
- 依 `mts_hvwap_candidate.py:840-841` 註解，5m 確認「is only a confirmation modifier and **never has independent veto authority**」——即 0 確認仍可釋放，屬現行設計而非 bug。

### 2.4 反事實 replay（已做，非本 spec 的驗收依據）

方法：用 `HVWAP_CANDIDATE` 事件的雙腿 `price` 路徑，模擬「不釋放、兩腿抱到 Policy J 合併出場(200/50)」，成本 117.8/組。

| 子集 | n | 實際 | 反事實 | 差 |
|---|---:|---:|---:|---:|
| Policy J 真的觸發（可部署） | 35 | -3,343.3 | -1,203.0 | **+2,140.3**（17/35 較好）|
| 從未觸發（mark-to-last，**不可部署**） | 14 | -4,325.8 | -449.2 | +3,876.6（不採用）|
| 無法 replay | 5 | — | — | — |

參數敏感度（含不可部署子集）：300/100 → +5,657；200/100 → +5,947。

**限制（必須保留在結論旁）**：全部為 `MARK_NOT_EXECUTABLE`（最後成交價，非可成交 bid/ask）；夜盤無 tick/BBO 存檔；14 筆缺替代出場規則。

## 3. 假設（可被推翻）

H1：抑制 HVWAP 釋放後，兩腿持有到 Policy J 合併出場，淨 PnL 優於現行提早釋放。
H0：無差異或更差。

## 4. 與 taishin-stock「主策略／影子策略」的關係

**同一個概念家族，機制不同。**

| 面向 | taishin-stock | 本 spec（MTS） |
|---|---|---|
| 影子跑什麼 | 另一個**策略**（`SHADOW_STRATEGY`），同 process、永遠 PAPER | 同一策略的**另一個出場政策**（不釋放 → Policy J 合併） |
| 影子是否有獨立部位 | 有，影子自己模擬成交（`SHADOW_PAPER`） | 不需要；同一筆進場的替代出場結果，屬**反事實**而非獨立部位 |
| 切換方式 | 改 `STRATEGY` / `SHADOW_STRATEGY` 環境變數（主/影互換） | 不改主行為；影子只記錄，達標後才改政策 |
| 共同點 | 影子永不送真單；以配對資料晉升 | 相同 |

結論：可以沿用 taishin 的「平行影子、不動主線、以證據晉升」治理模型，但**不能照抄其實作**——MTS 這題不需要第二個部位，只需要同一部位的第二個出場決策被記錄下來。

**關鍵發現：這個影子機制在 MTS 已經存在但沒有產出**。`tmf_spread.py:2706 _record_release_counterfactual()` 會寫 `RELEASE_COUNTERFACTUAL` 事件，實測整個 ledger 只有 **11 筆**，且 **11/11 `bbo_status=MISSING`、`cf_net_twd` 非 null 為 0 筆**（抽樣 `mts-auto-192006-253` 確認）。該函式要求 bar 提供 `near_bid/near_ask/far_bid/far_ask` 與 `*_bidask_age_ms` 並通過數值/stale 驗證，未通過即記 MISSING；具體是欄位缺失或型別不可用，需在 Phase 0 定位，本文件不預設原因。

因此本 spec 的第一步不是「新建影子」，而是**修好既有的影子記錄**。

## 5. 設計

### Phase 0（前置，必須先做）：讓影子記錄可用

- 在 HVWAP release 呼叫點（`tmf_spread.py:2900` 附近）把當前 BBO 傳入，使 `_record_release_counterfactual()` 能算出 `cf_gross_twd / cf_fee_twd / cf_tax_twd / cf_net_twd`。
- 若 BBO 缺失/stale：`bbo_status` 必須是 `MISSING`/`STALE`，**不得**以最後成交價冒充可成交價（沿用既有 fail-closed 語義）。
- 驗收：新產生的 `RELEASE_COUNTERFACTUAL` 事件 `bbo_status=OK` 且四個 `cf_*` 非 null；`MISSING` 時四個欄位維持 null。

### Phase 1（觀察，不改行為）：影子 arm

- 每一筆 HVWAP 釋放都記錄兩個數字：實際結果（現行）與替代結果（Policy J 合併，200/50）。
- **不改動任何送單路徑**，純 telemetry。
- 觀察期建議 10 個交易日或 ≥30 筆釋放（先到者為準）。

### Phase 2（決策）：晉升規則

只有在下列全部成立時，才建議把 HVWAP 釋放改為 Policy J 合併：

1. 配對樣本 ≥30 筆，且 `bbo_status=OK` 比例 ≥90%；
2. 替代 arm 的**中位數**每筆改善 > 0（不是只看總和，避免少數大逆轉主導）；
3. 替代 arm 在 **NEAR 與 FAR 兩個子群**都不為負；
4. 最壞單筆虧損不得比現行惡化超過 300 TWD；
5. 沒有「Policy J 從未觸發」的孤兒（見 §6 缺口）。

任一條件不成立 → 維持現行，寫成結案報告。

## 6. 必須先補的規格缺口

**孤兒部位規則**：抑制釋放後，若 Policy J 在特定期間內從未觸發，部位如何結束？本次 replay 有 14/49（29%）屬此類，目前只能用 mark-to-last，**不可部署**。

候選（需使用者裁決，本 spec 不定案）：
- (a) 收盤前強制平倉（日盤 13:40 / 夜盤 04:50）；
- (b) 沿用既有 ATR stop；
- (c) 時間停損（例如 N 分鐘）。

沒有這條規則，Phase 1 的樣本會持續產生不可判定項。

## 7. 必測矩陣

| # | 條件 | 測試類型 | 預期 |
|---|---|---|---|
| 1 | 釋放當下 BBO 完整 | 單元 | `cf_*` 四個欄位非 null，`bbo_status=OK` |
| 2 | BBO 缺失 | 單元 | 全 null + `bbo_status=MISSING`（不得回填 last-trade） |
| 3 | BBO stale（near>500ms / far>1500ms） | 單元 | `bbo_status=STALE`，全 null |
| 4 | 影子 arm 不送任何單 | 整合（mock order manager） | 訂單呼叫次數 = 0 |
| 5 | 同一 trade 只記一次 counterfactual | 單元 | 第二次呼叫 no-op（既有 guard） |
| 6 | Phase 2 門檻 | 單元（fixture） | 條件 2/3/4 任一不滿足 → 不晉升 |

## 8. 輸出契約

Phase 1 結束需產出：
- 配對表 CSV：`trade_id, ts, release_leg, actual_net, cf_net, delta, bbo_status, cohort`
- 摘要：n、`bbo_status` 分佈、中位數/平均 delta、NEAR/FAR 分群、孤兒筆數
- 明列 **未計入** 的筆數與原因
- 明確聲明 replay 基準（`MARK_NOT_EXECUTABLE` / `EXECUTABLE_BBO`）

## 9. 明確不做的事

- 不改 `monitor.py`、不改 order manager、不改 PM2/部署。
- 不啟用任何會影響實盤的行為變更（HVWAP 釋放本身已是 `PAPER_ACTIVE`）。
- 不以「總和改善」單獨作為晉升依據。
- 不以 mark-to-last 的數字對外宣稱效果。

## 10. 已知缺口（不得在報告中省略）

1. 夜盤無 tick/BBO 存檔；本次所有夜盤數字皆為最後成交價推算。
2. 5 筆無法 replay（缺 candidate 路徑或方向）。
3. `HVWAP_CANDIDATE` 全部 42,669 筆事件的 `release_action_emitted` 與 `baseline_pass_release` 恆為 False，**不可**用來判定釋放意圖；本分析改用 `RELEASE_*_SUBMITTED` 事件。
4. 樣本量小（可部署子集 35 筆），不足以下結論。
