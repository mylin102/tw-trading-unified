# Dry-run Evidence Packet — Preflight Checklist（research-only）

**狀態**: docs only（v6.1 之後, codex 2026-08-08 指示）
**硬限制**: 禁讀 runtime input; 禁授權 artifact; 禁改 production。
本清單是未來受限唯讀 dry-run 的執行規格 — 現在只演練 synthetic input。

## 1. 唯一允許的 input snapshot（路徑 + 格式）
- **路徑**: `runtime/exports/` 目錄下的 reconciled event JSON snapshot
  （由 scripts/research/exit_attribution.reconcile.reconcile_fill 對帳
  產出之研究輸入; 執行時以唯讀開啟）
- **禁止**: 任何非 exports 的 runtime state/log/order 檔案; 任何
  production 路徑
- **格式**（JSON list; 每 event）:
  ```json
  {
    "source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
    "decision_ts_ms": 1700000100000,
    "quotes": {
      "near": {"bid": 50.0, "ask": 100.0, "age_s": 1,
               "close_action": "SHORT", "quote_exchange_ts": 1700000000000},
      "far":  {"bid": 25.0, "ask": 50.0, "age_s": 1,
               "close_action": "SHORT", "quote_exchange_ts": 1700000000050}
    }
  }
  ```
- 欄位契約: ordering fields（int>0）/ decision_ts_ms（epoch-ms
  [1.4e12, 2.5e12]）/ quotes near+far 各含 bid/ask/age_s/close_action/
  quote_exchange_ts（epoch-ms）

## 2. sha256 前後檢查（可重現性）
- **run 前**: `shasum -a 256 <input>` → 記錄於執行日誌
- **run 後**: 再算一次 → 必須等於 manifest.input_sha256
- 前後不符 → 產物作廢（視為 tampered, 不交付）

## 3. 確切 --dry-run command
```bash
cd /Users/myllin_mini/Documents/mylin102/tw-trading-unified-git
.venv/bin/python3 -B -m scripts.research.phase_transition_replay.run_replay \
  --input <SNAPSHOT> --out-dir <ISOLATED_OUT> \
  --prereg prereg-v1 --dry-run
```
- **演練（synthetic tmp input, 非 runtime 資料）**:
  `/tmp/dryrun_rehearsal/events.json`（2 events: 1 valid + 1
  skew>bound）→ 預期 exit 0 + manifest.json 產出（n_kept=1,
  n_censored=1, reason=pair skew）
- 拒絕路徑: 無 --dry-run → exit 3; 無 --prereg → exit 2（argparse）;
  provenance 不可證 → exit 5; 整檔 malformed → exit 4 — 全零 output

## 4. 預期 manifest schema + 拒絕/abort rules
- **schema**（manifest_version v3）:
  preregistration_id + preregistration_sha / git_provenance（repo_head,
  runner+prereg sha256, tracked, matches_head, dirty）/ parameters
  （m_economic, fee_assumption_id+assumptions, staleness,
  max_pair_skew_ms, timestamp_unit=epoch_ms, validator_version=v1,
  config_version, classifier）/ input_path + input_sha256 / stream_hash /
  clock_contract / n_events / n_kept / n_censored / kept_records
  （event_seq, decision_ts_ms, near+far quote_exchange_ts, pair_skew_ms,
  max_pair_skew_ms, near+far age_s）/ censored_reasons（event_seq +
  reason）/ dry_run=true / engine_run=false / created_at（UTC ISO）
- **abort rules**（REFUSED 非零 + 零 output）:
  engine 未實作非 dry-run（3）/ provenance 不可證（5）/ 整檔
  malformed（4）/ --prereg 缺失（2）/ input 不可讀
- **censor rules**（逐 event, 永不靜默）:
  schema 缺欄 / decision_ts_ms 非 epoch-ms / quote ts 非 epoch-ms /
  quote ts 晚於 decision / pair skew > bound / age 缺/NaN/負/超時 /
  bid|ask 缺/零/NaN / close_action 非 LONG|SHORT / leg set 不符

## 5. 產物寫入隔離目錄
- out-dir 必須為隔離目錄（`/tmp/dryrun_out_<ts>/` 或
  `runtime/research_out/` 新子目錄 — 與 runtime 資料目錄分離）
- 只寫 manifest.json; 永不寫入 input 路徑 / runtime state / logs
- 產物僅供唯讀審查

## 6. 驗收（外部）
- codex: source diff + 精確測試 + fail-closed 行為逐項驗收
- AGY: 最新 HEAD 獨立唯讀重驗（先前 151/155 為舊快照 — HEAD
  d866d18b 已 155/155）
- artifact 授權維持 NOT AUTHORIZED
