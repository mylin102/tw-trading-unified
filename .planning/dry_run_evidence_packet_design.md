# Dry-run Evidence Packet — Design（research-only, 不讀 runtime input）

**用途**: 未來受限唯讀 dry-run 的產出規格 — 供 codex 審查, 不執行。

## 1. 輸入
- runtime 匯出事件流（reconciled via exit_attribution.reconcile_fill,
  同 phase-transition 契約）— **本設計不觸碰, 未讀任何 runtime input**
- 每 event: source_event_seq / exchange_ts / recv_ts / decision_ts_ms
  （epoch-ms）/ quotes{near,far}: bid/ask/age_s/close_action/
  quote_exchange_ts

## 2. 執行（僅 --dry-run; engine 未實作 → 非 dry-run REFUSED）
1. git provenance gate（HEAD/tracked/blob-match/dirty → 不可證 REFUSED）
2. --prereg 解析（M/fee/staleness/max_pair_skew_ms/timestamp_unit/
   validator v1）
3. input sha256 + stream hash（reproducible）
4. 逐 event: schema → epoch-ms → BBO tier（bid/ask/age/close-action/
   pair-skew）→ kept / censored_with_reason
5. 寫 manifest.json（見下）— 不計算 PnL, engine_run=False

## 3. Manifest 欄位
- manifest_version v3 / preregistration_id + sha / git_provenance
  （repo_head, runner+prereg sha, tracked, matches_head, dirty）
- parameters: m_economic / fee_assumption_id + assumptions /
  staleness / max_pair_skew_ms / **timestamp_unit=epoch_ms** /
  timestamp_validator_version=v1 / config_version / classifier
- input_path + input_sha256 / stream_hash / clock_contract
- n_events / n_kept / n_censored
- kept_records: event_seq / decision_ts_ms / near+far
  quote_exchange_ts / pair_skew_ms / max_pair_skew_ms / near+far age_s
- censored_reasons: event_seq + reason（schema:/epoch-ms/pair skew/
  age/stale/close_action/leg set）
- dry_run=true / engine_run=false / created_at（UTC ISO）

## 4. 證據強度
- 每 kept record 可回推: 該 decision 點有同步 BBO（雙腿 ts 同
  epoch-ms、skew<=bound、bid/ask valid、age fresh、close_action 顯式）
- 每 censored record 帶精確 reason — 零靜默丟棄
- 無 historical artifact 執行; 無 PnL; 無 order

## 5. 驗收
- codex 以 source diff + 精確測試 + fail-closed 行為逐項驗收
- AGY 獨立唯讀驗收（provenance / --authorize 拒絕 / JSON quote /
  schema/age/close-side/pair-skew censoring）
- artifact 授權維持 NOT AUTHORIZED — 本設計不改變該狀態
