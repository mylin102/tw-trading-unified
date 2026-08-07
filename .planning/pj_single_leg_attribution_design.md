# 唯讀歷史單腿 Policy J 歸因稽核 — 設計文件 v4

**狀態**: DESIGN v4（codex 審查中；通過後才實作 committed script + artifact）
**範圍**: 僅 docs。無 production/ledger/dashboard 修改、無 deploy/restart。
**v4 變更**(對 v3)：① SUPPORTED/PROVEN 必須有**明確指名 Policy J 觸發/勝出**的同 trade
pre-submit 事件 — 已驗證現行 ledger schema **不存在**此類事件 → **SUPPORTED=0 by design**；
② CONTRADICTED 必須有同 trade 最終決策標記（指名非 Policy J 勝出）— RELEASE_*_SUBMITTED
無 trade_id、無 cause 欄位 → **CONTRADICTED=0 by design**，不做負面因果結論；③ fills side 為
混用 enum（BUY/SELL/LONG/SHORT/NONE/""）— 執行前先做 byte 快照的 schema 驗證，意外 schema →
**UNREADABLE**，不做逐候選拒絕；④ 參數無法溯源時**不得**用現行值算 eligibility →
`eligibility_consistent=null` + NOT_PROVABLE；⑤ **任一** malformed/torn JSONL 行 →
整份快照 SNAPSHOT_MALFORMED/UNREADABLE，不產出任何分類；⑥ manifest 加 source-schema
版本/觀察到的 keys、script 檔案 sha256 + commit SHA、git dirty 狀態。

## 1. 目標與誠實底線

歷史單腿出場（一腿 RELEASE + 另一腿 EXIT）是否為 Policy J（combined-UPL giveback）觸發。

**因果結論（v4 核心立場）**：現行 ledger schema 下，**無法對任何歷史 trade 給出
「Policy J 觸發」的 PROVEN 判定，也無法給出「非 Policy J」的 CONTRADICTED 判定**：
- 已驗證事件型別中**無**「指名 Policy J 觸發/勝出」的事件（僅有
  POLICY_J_GUARD_BASELINE / GUARD_QUARANTINE / PEAK_CANDIDATE / PEAK_CONFIRMED /
  PEAK_REJECTED / TRIGGER_SUPPRESSED；TRIGGER_SUPPRESSED 只是 per-tick 抑制日誌）
- RELEASE_*_SUBMITTED 事件 **trade_id=None**（實測）→ 無法 per-trade join，亦無 cause 欄位
- 因此本稽核的產出是：① schema 驗證、② **eligibility 對照**（條件一致/不一致/無法判定）、
  ③ **provenance-gap 報告**（為什麼 SUPPORTED=0 / CONTRADICTED=0，供 P1-B+ 加事件型別），
  ④ EXIT_LOG 全量統計對照

**唯讀**：不回寫任何 ledger/state/event。

## 2. 候選選取（完整性契約 + side enum 對映）

對 fills ledger 每筆記錄建立 `(trade_id, leg) → fill 序列`（同 trade 內**全部** fills 聚總）：

```
side enum 契約（按 fill_type 對映，schema-map 先行驗證，見 §6）：
  ENTRY   → side ∈ {LONG, SHORT}（持倉方向）
  EXIT/RELEASE → side ∈ {BUY, SELL}（收單方向）
  COMBINED_EXIT_* / settlement 列 → side ∈ {NONE, ""}，無方向，只做計數
  side 值不在上述集合 → schema 異常 → §6 UNREADABLE（非逐候選拒絕）
  持倉方向另行推導：ENTRY LONG → +1 / SHORT → -1；EXIT/RELEASE 為收單方向

candidate 成立（同一 trade_id，全部必要）：
  1. 兩腿各有完整 ENTRY：NEAR 與 FAR 各存在 fill_type=="ENTRY"，qty=1，
     side ∈ {LONG,SHORT} 且 price > 0
  2. 恰一腿 release：leg A 存在 fill_type=="RELEASE" 且 qty=1（>1 → 拒絕）
  3. 對側腿 exit：leg B 存在 fill_type=="EXIT" 且 qty=1（>1 → 拒絕）
  4. 數量對帳：leg A ENTRY qty == RELEASE qty；leg B ENTRY qty == EXIT qty
  5. 無 COMBINED_EXIT 同 trade
  6. 拒絕條件（計入 rejected_candidates + reason）：partial（qty<=0 或非整數）、
     multi-event、未知 fill_type、side 缺失、price<=0、COMBINED_EXIT 混入
```

- join key 唯一：`trade_id`（fills 與 events 皆含；RELEASE_*_SUBMITTED 例外 → 不可用）
- v2 探查值 70 為舊規則；v4 以 §2 契約重新計算，`candidates_considered` 與
  `rejected_candidates`（含 reason 計數）皆入 artifact

## 3. 時序契約與時間假設

- 每筆候選取：ENTRY ts（兩腿）、RELEASE ts、EXIT ts — ts 欄位名依 §6 schema-map
  （fills 用 `timestamp`、events 用 `ts`；兩者皆 ISO-8601 字串）
- 時序契約（違反者 `source_limit=ORDER_VIOLATION`，歸 NOT_PROVABLE）：
  `ENTRY_A <= ENTRY_B < RELEASE_A < EXIT_B`
- 時間假設（入 artifact parser_assumptions）：ISO-8601、台灣本地時間（UTC+8）、字串序可比較；
  out-log 日期 prefix 已知 ±1 日偏移且**永不作為證據**；同 trade 內 ts 反序 →
  `TS_OUT_OF_ORDER` → NOT_PROVABLE

## 4. 防誤連（EXIT_LOG 無 trade_id）

- `EXIT_LOG` 事件**不含 trade_id** → **永不**作為個別證據；只用於全量對照統計
  （EXIT_LOG 總數 vs PEAK_CONFIRMED 總數），artifact 標 `evidence=STATISTICS_ONLY`
- per-trade 判定只使用含 trade_id 的記錄：PEAK_CONFIRMED/PEAK_REJECTED/
  TRIGGER_SUPPRESSED、fills

## 5. 分類規則（v4：三類 × attribution_strength）

### SUPPORTED（attribution_strength=PROVEN）— 現行 schema 下 by design = 0
必要證據：同 trade、pre-submit、**明確指名 Policy J 觸發/勝出**的事件
（例如 `POLICY_J_SINGLE_LEG_TRIGGERED` / `POLICY_J_TRIGGERED` 帶 trade_id，或
legacy 等價欄位）。**已驗證現行 events ledger 不存在此類事件** →
- 若 schema probe 未發現任何 trigger-named 事件型別 → 全部候選 SUPPORTED 不成立，
  `summary.reason="NO_TRIGGER_NAMED_EVENT_IN_SCHEMA"`，`SUPPORTED.PROVEN=0`
- 若未來 schema 含此類事件，才套用：事件 ts < EXIT fill ts、事件指名該 trade、
  `attribution_strength=PROVEN`
- **absence-of-competing-cause 不得升級 provenance**（PEAK_CONFIRMED 只 arm peak，
  不代表 giveback 觸發或贏過 native TRAIL — 明確寫死）

### CONTRADICTED — 現行 schema 下 by design = 0
必要證據：同 trade 的**最終決策標記**，指名「此 exit 非 Policy J 觸發」
（例如帶 cause=trail/release_threshold 且 trade_id 的 submission 事件）。
- RELEASE_*_SUBMITTED 實測 trade_id=None 且無 cause → **不可用**
- PEAK_REJECTED / TRIGGER_SUPPRESSED **不構成** counterfactual（log 可能稀疏、
  抑制可與其他控制動作並存）→ 不得推負面結論
- schema 無此標記 → `CONTRADICTED=0` + `summary.reason="NO_FINAL_DECISION_CAUSE_EVENT"`

### INSUFFICIENT_EVIDENCE（其餘全部）
- **INFERRED_ELIGIBLE**（僅當 §7 參數可溯源）：無 trigger-named 事件，但以
  **已解析的 deployed 參數**重建 `durable_peak >= activation` 且 giveback 條件在
  [RELEASE_ts, EXIT_ts] 成立 → `eligibility_consistent=true`、
  `source_limit=NO_DECISION_PROVENANCE`（語意：條件一致，因果不可歸因）
  - 參數不可溯源 → **不得**以現行值計算 → `eligibility_consistent=null`、NOT_PROVABLE
    （現行 config 值只進 artifact `params_current_reference` 供參考，不進分類）
- **NOT_PROVABLE**：其餘全部（無 peak、缺價格、時序違反、§2 拒絕、
  eligibility 無法計算）

## 6. 輸入快照與 schema 驗證（v4：byte 快照先行）

```
輸入 1:  {runtime}/logs/mts_trade_fills.jsonl
輸入 2:  {runtime}/logs/mts_spread_events.jsonl
（runtime 根：/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime）
```

1. **每個輸入只讀一次**：`open(path,"rb").read()` → 不可變 bytes；sha256 對「實際被
   parse 的 bytes」計算；parse 一律從記憶體 bytes 進行
2. **schema 驗證（先於任何候選處理）**：從快照抽樣（前 N 行 + 全 enum 掃描）驗證：
   - fills 必須含 keys：`trade_id/timestamp/leg/contract/side/fill_type/qty/price`
   - events 必須含 keys：`ts/event/trade_id`
   - side enum ⊆ {BUY,SELL,LONG,SHORT,NONE,""}；fill_type enum ⊆
     {ENTRY,EXIT,RELEASE,COMBINED_EXIT,COMBINED_EXIT_NEAR,COMBINED_EXIT_FAR,
     COMBINED_EXIT_COMPLETED,COMBINED_EXIT_SETTLED}
   - 任一不符 → `status=UNREADABLE` + `schema_mismatch` 細節，**不產出任何分類**
3. **malformed/torn 處理**：任一 JSONL 行無法 parse、或檔尾不完整行 →
   **整份快照 `status=SNAPSHOT_MALFORMED`/UNREADABLE，不產出任何 trade 分類**
   （無法得知壞行屬於哪個 trade；部分分類會是假的完整）
4. runtime ledgers 稽核期間仍會 append → 快照 = 讀取當下 byte image；
   `snapshot_read_ts` 入 manifest；同 byte image 重跑可重現相同 hash

## 7. 參數溯源（v4）

- activation/giveback 有效值以該 trade 日期的 deployed config 為準：
  1. `git log --follow --format=%H -- config/futures.yaml` + release 部署時間線 →
     對齊該日期的 config commit → `param_source=DEPLOYED_CONFIG_<sha>`，以該檔值計算
  2. 不可對齊 → `param_source=PARAMETER_VERSION_UNKNOWN` →
     **該候選 eligibility_consistent=null、NOT_PROVABLE**（不得以現行值標 eligibility）
  3. 現行 futures.yaml 值一律另存 `params_current_reference`（僅參考，不進分類）
- mult(10)/friction(92)：以 §7.1 對應 commit 的 plugin 碼為準；不可解 → 同上 UNKNOWN

## 8. artifact schema / 輸出位置 / manifest（v4）

```
輸出: {runtime}/exports/research/pj_single_leg_attribution_<YYYYmmdd_HHMMSS>.json
```
**安全邊界聲明**：runtime/exports 由 repo .gitignore 排除只是檔案管理慣例，**不是**安全邊界；
artifact 為外部可讀檔。此聲明寫入 artifact。

```json
{
  "status": "OK | UNREADABLE | SNAPSHOT_MALFORMED",
  "generated_at": iso,
  "script_commit_sha": "<git rev-parse 執行當下 script commit>",
  "script_file_sha256": "<script bytes sha256>",
  "git_dirty": "<git status --porcelain 輸出，未 commit 的變更清單>",
  "manifest": {
    "inputs": {
      "fills":  {"sha256": "...", "bytes": n, "snapshot_read_ts": iso,
                 "source_schema": {"keys": [...], "side_enum": [...], "fill_type_enum": [...]}},
      "events": {"sha256": "...", "bytes": n, "snapshot_read_ts": iso,
                 "source_schema": {"keys": [...], "event_enum_count": {...}}}
    },
    "parser_assumptions": ["jsonl", "utf-8", "iso8601_local_tw", "out_log_prefix_never_evidence"],
    "params": {"activation_twd": 200, "giveback_twd": 50, "mult": 10, "friction": 92,
               "param_source": "DEPLOYED_CONFIG_<sha> | PARAMETER_VERSION_UNKNOWN",
               "params_current_reference": {...}}
  },
  "summary": {
    "SUPPORTED": {"PROVEN": 0},
    "CONTRADICTED": 0,
    "INSUFFICIENT_EVIDENCE": {"INFERRED_ELIGIBLE": n, "NOT_PROVABLE": n},
    "reasons": ["NO_TRIGGER_NAMED_EVENT_IN_SCHEMA", "NO_FINAL_DECISION_CAUSE_EVENT", "..."]
  },
  "candidates_considered": n,
  "rejected_candidates": {"QTY_MISMATCH": n, "MULTI_EVENT": n, "UNKNOWN_TYPE": n,
                          "BAD_SIDE": n, "BAD_PRICE": n, "COMBINED_EXIT": n},
  "trades": [
    {
      "trade_id", "released_leg", "remaining_leg",
      "entry_ts", "release_ts", "exit_ts",
      "entry_prices": {"near": p, "far": p}, "release_price": p, "exit_price": p,
      "decision_event": {"type": "TRIGGER_NAMED|PEAK_CONFIRMED|PEAK_REJECTED|TRIGGER_SUPPRESSED|NONE",
                          "ts": iso, "durable_peak": x},
      "eligibility_consistent": true|false|null,
      "classification": "SUPPORTED|CONTRADICTED|INSUFFICIENT_EVIDENCE",
      "attribution_strength": "PROVEN|INFERRED_ELIGIBLE|NOT_PROVABLE",
      "evidence_keys": [...],
      "source_limits": ["NO_DECISION_PROVENANCE", "ORDER_VIOLATION", ...]
    }
  ],
  "statistics_only": {"exit_log_count": n, "peak_confirmed_total": n, "trigger_suppressed_total": n}
}
```

## 9. raw_ticks 截止 7/28 的限制（不變）

- raw_ticks 僅涵蓋 2026-07-23 ~ 07-28 → 之後候選無 tick/bar 重播；peak 只能以事件記錄為準
- 每筆標 `tick_availability: NONE`（全期間）；不嘗試用 tick 補證據

## 10. 交付物順序（v4）

1. 本設計文件 v4（committed，僅 docs）
2. codex 核准 v4 → committed script `scripts/research/pj_single_leg_attribution/audit.py`
   （含 §6 快照+schema 驗證、§7 溯源、§5 分類；script file sha256 + commit SHA 入 manifest）
3. 執行 → artifact（含 manifest）→ 送 codex 審查
4. 任何 production 變更（含 P1 接線）於 v4 審查通過後另行排程 — 本稽核全程不觸碰
