# 唯讀歷史單腿 Policy J 歸因稽核 — 設計文件 v2

**狀態**: DESIGN（codex 審核中；通過後才實作 committed script + ignored artifact）
**範圍**: 僅 docs。無 production/ledger/dashboard 修改、無 deploy/restart。

## 1. 目標

歷史單腿出場（一腿 RELEASE + 另一腿 EXIT）是否為 Policy J（combined-UPL giveback）
觸發。P1-B 之前的單腿出場沒有 decision event，本稽核以既有記錄做三分類可歸因性
判定。**唯讀**：不回寫任何 ledger/state/event。

## 2. 候選選取 algorithm（trade identity / legs / qty）

對 fills ledger 每筆記錄建立 `(trade_id, leg) → fill_type 序列`：

```
candidate 定義（同一 trade_id 內）：
  1. 存在 leg A 的 fill_type == "RELEASE"（qty=1）
  2. 存在另一 leg B（B != A）的 fill_type == "EXIT"（qty=1）
  3. 任一腿 ENTRY qty=1 存在（trade 完整性）
  4. 無 COMBINED_EXIT 同 trade（雙腿同時出場不算單腿候選）
```

- join key 唯一：`trade_id`（fills/events 皆含）
- 候選清單 = 上述條件全成立者（探查值：70）
- 輸出逐筆 `trade_id / released_leg / remaining_leg / entry 價格 / release 價格 / exit 價格 / qty`

## 3. 時序與 join key

- 每筆候選取：ENTRY ts（兩腿）、RELEASE ts（leg A）、EXIT ts（leg B）
- 時序契約（違反者標 `source_limit=ORDER_VIOLATION`，歸 INSUFFICIENT）：
  `ENTRY_A <= ENTRY_B < RELEASE_A < EXIT_B`（同 trade_id 內比較）
- join：`POLICY_J_PEAK_CONFIRMED.trade_id == 候選 trade_id`；
  且 `PEAK_CONFIRMED.ts` 在 `[RELEASE_ts, EXIT_ts]` 區間內（時間順序證據）

## 4. 防誤連（EXIT_LOG 無 trade_id）

- `EXIT_LOG` 事件**不含 trade_id** → **永不**作為任何候選的個別證據；
  只用於全量對照統計（EXIT_LOG 總數 vs PEAK_CONFIRMED 總數），artifact 中標
  `evidence=STATISTICS_ONLY`
- 任何需要 trade 層級證據的判定只使用含 trade_id 的記錄：
  `POLICY_J_PEAK_CONFIRMED`、`POLICY_J_TRIGGER_SUPPRESSED`、fills

## 5. 分類規則（三類）

### SUPPORTED（必要證據，缺一不可）
1. 同一 trade_id 存在 `POLICY_J_PEAK_CONFIRMED`，且
   `durable_peak >= activation`（現行 config: 200）
2. 該 peak 的 ts 在該 trade 的 `[RELEASE_ts, EXIT_ts]` 內
3. released-leg realized + remaining-leg UPL（由 fills 價格重建，
   parity 公式 `(near_pnl_pts + far_pnl_pts) * 10 - 92`）→ `current_net_twd`
4. `current_net_twd <= durable_peak - giveback`（現行 config: 50）

### CONTRADICTED（必要證據）
1. 同一 trade_id 存在 `POLICY_J_PEAK_CONFIRMED`（ts 在 [RELEASE, EXIT] 內）但
   `durable_peak < activation` 或 `current_net_twd > durable_peak - giveback`
   （peak 有、但給回條件不成立 → 可證明「不是 Policy J 觸發」）

### INSUFFICIENT_EVIDENCE（其餘全部）
- 無 PEAK_CONFIRMED / 無該 trade 的 peak / 缺價格（entry/release/exit 任一為 0 或缺失）/
  時序違反 / 任何證據不足 — 全部歸此類

## 6. 輸入檔精確路徑 + sha256 manifest

```
runtime 根：/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime
輸入 1:  {runtime}/logs/mts_trade_fills.jsonl
輸入 2:  {runtime}/logs/mts_spread_events.jsonl
（activation/giveback 參數來源: repo futures.yaml mts.enable_combined_upl_trail 區塊）
```

artifact 首段記錄各輸入檔 `SHA-256`（`hashlib.sha256` 全檔）→ 重跑可對齊。

## 7. raw_ticks 截止 7/28 的限制

- `raw_ticks` 僅涵蓋 2026-07-23 ~ 07-28（P1-3 未做，tick 持久化缺）→
  **7/28 之後的候選無 tick/bar 重播**；peak 只能以 PEAK_CONFIRMED 記錄為準
- 7/23-7/28 期間的候選亦無完整 BBO 序列 → 同以事件記錄為準
- artifact 每筆標 `tick_availability`: `NONE`（全期間）；不嘗試用 tick 補證據

## 8. artifact schema / 輸出位置 / gitignore

```
輸出: {runtime}/exports/research/pj_single_leg_attribution_<YYYYmmdd_HHMMSS>.json
（runtime/exports = gitignored 由 repo 慣例；確認 .gitignore 含 exports/）

schema:
{
  "generated_at": iso,
  "input_hashes": {"fills_sha256": ..., "events_sha256": ...},
  "params": {"activation_twd": 200, "giveback_twd": 50, "mult": 10, "friction": 92},
  "candidates": 70,
  "summary": {"SUPPORTED": n, "CONTRADICTED": n, "INSUFFICIENT_EVIDENCE": n},
  "trades": [
    {
      "trade_id", "released_leg", "remaining_leg", "entry_ts", "release_ts", "exit_ts",
      "released_realized_twd", "remaining_upl_twd", "current_net_twd",
      "durable_peak", "peak_ts", "peak_in_window": bool,
      "classification": "SUPPORTED|CONTRADICTED|INSUFFICIENT_EVIDENCE",
      "evidence_keys": ["PEAK_CONFIRMED", "FILLS", ...],
      "source_limits": ["NO_TICK_DATA", "ORDER_VIOLATION", ...]
    }
  ],
  "statistics_only": {"exit_log_count": n, "peak_confirmed_total": n}
}
```

## 9. 交付物順序

1. 本設計文件（committed，僅 docs）
2. codex 核准 → committed script `scripts/research/pj_single_leg_attribution/audit.py`
3. 執行 → gitignored artifact（含 input sha256）→ 送 codex 審查
