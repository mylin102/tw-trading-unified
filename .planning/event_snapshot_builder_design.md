# Reconciled Event Snapshot Builder — 設計（research-only, test-first）

**狀態**: DESIGN + RED tests only（codex 2026-08-08 單一前置任務）
**硬限制**: 零 production code; 零 runtime 讀取/執行; 零歷史 replay;
零 deploy/restart/push。Scope: scripts/research/** + tests/research/**。

## 1. 用途
phase_transition runner 的 input 前置建構器 — 把**明確命名的不可變
byte snapshots**（fills / events / quotes）對帳成單一 reconciled event
list（run_replay schema 相容）+ 版本化 manifest。

## 2. 輸入契約（只接受這三類, 明確命名, 唯讀）
- fills snapshot（成交）
- events snapshot（訊號/決策事件）
- quotes snapshot（逐筆 BBO）
- 每個輸入檔: **讀取恰好一次** → sha256 對**同一份 parsed bytes** 計算
  （TOCTOU 契約與 runner 一致）; hash 後不依 pathname reopen

## 3. 輸出契約
- **events.json**: versioned JSON list（每 event 帶
  source_event_seq / exchange_ts / recv_ts / decision_ts_ms +
  quotes{near,far}: bid/ask/age_s/close_action/quote_exchange_ts/
  quote_source）— run_replay 直接可食
- **manifest.json**: schema_version（event-snapshot-v1）/
  sources[{path, sha256}] / event_map（每個 output event →
  event 來源 record id + quote 來源 records + 各來源 hash）

## 4. 核心契約（RED 鎖定）
1. **順序**: events 依 (exchange_ts, source_event_seq) 全序排列
2. **duplicate**: 同 (exchange_ts, source_event_seq) 重複 →
   deterministic 拒絕並標 reason（不靜默去重）
3. **out-of-order**: 輸入序與 (exchange_ts, seq) 序不符 → 標記
   reordered + reason（排序仍確定）
4. **missing/invalid BBO → 顯式 censored/NOT_AVAILABLE record**:
   **絕不推測 last-price BBO** — 缺腿 / bid|ask 無效 / age 異常 /
   quote ts 晚於 event exchange_ts / pair skew 超界 → 該 event 標
   quotes=N/A + reason（runner 的 censoring 會承接）
5. **malformed/torn input**: 任一來源檔無法 parse → 整份 REFUSED
   （非零 + 零 output）
6. **no lookahead**: 每 event 的 quotes 只來自 exchange_ts <= 該
   event 的 quote records — 未來 quote 永不附著（→ censored）
7. **read-once + hash binding**: 每來源讀一次; hash = parsed bytes

## 5. 測試覆蓋（RED matrix 對應）
- ordering / duplicate / out-of-order / missing BBO / malformed-torn /
  no-lookahead / read-once-hash / manifest event_map 完整性
- 全部 skeletal（NotImplementedError at intended point）— 獨立
  collection, 無 skip

## 6. 交付順序
design + RED → codex 審查 → 實作授權（research-only）→ runner 對接
（--input 吃 builder 產物）→ dry-run evidence packet
