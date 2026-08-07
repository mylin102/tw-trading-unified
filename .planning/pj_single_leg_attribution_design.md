# 唯讀歷史單腿 Policy J 歸因稽核 — 設計文件 v6

**狀態**: DESIGN v6（codex 審查中；通過後才實作 committed script + artifact）
**範圍**: 僅 docs。無 production/ledger/dashboard 修改、無 deploy/restart。
**v6 變更**(對 v5，極小澄清，無範圍擴張)：① TEST 移除省略號 — TEST 為排除污染；若 TEST 行
帶非空 trade_id → 該 trade **整筆**排除於候選並計 `TEST_TRADE_CONTAMINATION`；TEST 的 side
enum 只記錄，永不使健康快照 UNREADABLE；② COMBINED_EXIT* wildcard 改為**顯式有限 allowlist**
（COMBINED_EXIT / COMBINED_EXIT_NEAR / COMBINED_EXIT_FAR / COMBINED_EXIT_COMPLETED /
COMBINED_EXIT_SETTLED）；其他新 prefix/型別 → UNREADABLE；OK 快照的 rejected_candidates
移除 UNKNOWN_TYPE（未知型別在 schema 層已中止）；③ 分類規則明確化 — 候選若有 naive /
mixed offset / missing / 語意不確定的 timestamp → **NOT_PROVABLE 且 eligibility_consistent=null**；
UTC+8 解析僅供顯示/排序，**永不**用以確立 INFERRED_ELIGIBLE；per-trade 證據事件缺 trade_id →
排除出證據 + 計入 manifest，不污染全域驗證。
**v4/v5 的因果限制（SUPPORTED=0 / CONTRADICTED=0 by design）不變。**
**v5 變更**(對 v4)：① `event`+`ts` 全域必備；`trade_id` 全域**可選**、僅 per-trade 證據事件型別
必備 — 全域事件缺 trade_id（實測 2,914 筆）計入 manifest，**永不 UNREADABLE**；② `TEST`
為已知非候選/測試污染型別 — 排除於候選、計數回報，**不靜默忽略、不使快照 UNREADABLE**；
未知 NEW enum → UNREADABLE；③ COMBINED_EXIT* 為已知排除型別，allowed side ∈
{BUY,SELL,NONE,""}（實測 BUY/SELL）— 不限制為 settlement-only；含此類的 trade 一律
拒絕為候選；④ manifest 記錄**由快照觀察到的 enum-by-fill-type 對映** + 顯式 allowlist，
只對 allowlist 外值失敗；⑤ timestamp 用 **timezone-aware ISO parse**（非 raw lexical）；
naive/mixed-offset/missing → 相關候選 NOT_PROVABLE（或 parser schema 無法確立時間語意時
整份 UNREADABLE）；offset 分布入 manifest。
**v4 的因果限制（SUPPORTED=0 / CONTRADICTED=0 by design）已獲 codex 接受，不變。**

## 1. 目標與誠實底線

歷史單腿出場（一腿 RELEASE + 另一腿 EXIT）是否為 Policy J（combined-UPL giveback）觸發。

**因果結論（v4 已接受）**：現行 ledger schema 下，**無法對任何歷史 trade 給出
「Policy J 觸發」的 PROVEN 判定，也無法給出「非 Policy J」的 CONTRADICTED 判定**：
- 事件型別中**無**「指名 Policy J 觸發/勝出」的事件（僅 GUARD_BASELINE/GUARD_QUARANTINE/
  PEAK_CANDIDATE/PEAK_CONFIRMED/PEAK_REJECTED/TRIGGER_SUPPRESSED）
- RELEASE_*_SUBMITTED 事件 trade_id=None（實測）→ 無法 per-trade join，亦無 cause 欄位
- 本稽核的產出：① schema 驗證、② eligibility 對照、③ provenance-gap 報告、④ EXIT_LOG 統計

**唯讀**：不回寫任何 ledger/state/event。

## 2. 候選選取（完整性契約 + enum-by-fill-type 對映）

對 fills ledger 每筆記錄建立 `(trade_id, leg) → fill 序列`（同 trade 內全部 fills 聚總）。

**fill_type allowlist（由快照觀察值 + 此 allowlist 驗證，見 §6）：**
```
已知型別與其 allowed side enum（實測對映）：
  ENTRY                → side ∈ {LONG, SHORT}（持倉方向）
  EXIT / RELEASE       → side ∈ {BUY, SELL}（收單方向）
  COMBINED_EXIT*（NEAR/FAR/COMPLETED/SETTLED）→ side ∈ {BUY, SELL, NONE, ""}（排除型別）
  TEST                 → side enum 只記錄（任何觀察值），永不使快照 UNREADABLE；
                         已知非候選/測試污染：行排除 + 計數；若 TEST 行帶非空 trade_id →
                         該 trade 整筆排除於候選並計 TEST_TRADE_CONTAMINATION
  COMBINED_EXIT / COMBINED_EXIT_NEAR / COMBINED_EXIT_FAR /
  COMBINED_EXIT_COMPLETED / COMBINED_EXIT_SETTLED → side ∈ {BUY, SELL, NONE, ""}（排除型別）
任何不在 allowlist 的 fill_type 或 side → 快照 UNREADABLE（§6），非逐候選拒絕
（allowlist 為顯式有限集合；其他新 prefix/型別一律 UNREADABLE，無 wildcard）
持倉方向另行推導：ENTRY LONG → +1 / SHORT → -1；EXIT/RELEASE 為收單方向
```

```
candidate 成立（同一 trade_id，全部必要）：
  1. 兩腿各有完整 ENTRY：NEAR 與 FAR 各存在 fill_type=="ENTRY"，qty=1，
     side ∈ {LONG,SHORT} 且 price > 0
  2. 恰一腿 release：leg A 存在 fill_type=="RELEASE" 且 qty=1（>1 → 拒絕）
  3. 對側腿 exit：leg B 存在 fill_type=="EXIT" 且 qty=1（>1 → 拒絕）
  4. 數量對帳：leg A ENTRY qty == RELEASE qty；leg B ENTRY qty == EXIT qty
  5. 無 COMBINED_EXIT / COMBINED_EXIT_NEAR / COMBINED_EXIT_FAR /
     COMBINED_EXIT_COMPLETED / COMBINED_EXIT_SETTLED 同 trade（排除型別；含之 → 拒絕）
  6. TEST 型別行不參與候選建構；帶非空 trade_id 的 TEST 行 → 該 trade 整筆排除 +
     計 TEST_TRADE_CONTAMINATION
  7. 拒絕條件（計入 rejected_candidates + reason）：partial（qty<=0 或非整數）、
     multi-event、side 缺失、price<=0、COMBINED_EXIT 混入
     （註：未知 fill_type 在 schema 層即 UNREADABLE，不入 rejected_candidates — §6）
```

- join key 唯一：`trade_id`（RELEASE_*_SUBMITTED 例外 → 不可用，§4）
- `candidates_considered` 與 `rejected_candidates`（含 reason 計數）入 artifact

## 3. 時序契約與時間假設（v5：timezone-aware ISO parse）

- 每筆候選取：ENTRY ts（兩腿）、RELEASE ts、EXIT ts — ts 欄位名依 §6 schema-map
  （fills 用 `timestamp`、events 用 `ts`）
- **時間解析（v6）**：一律 `datetime.fromisoformat`（timezone-aware 優先）。
  分類規則明確化：候選若為 naive / mixed offset / missing / 時間語意不確定 →
  **NOT_PROVABLE 且 eligibility_consistent=null**（source_limit 分別為
  TS_NAIVE / TS_SEMANTICS_UNKNOWN / TS_MISSING）。
  UTC+8（naive_ts_mean_utc8）解析**僅供顯示/排序**，永不用以確立 INFERRED_ELIGIBLE。
  若 parser 層完全無法確立時間語意（全檔無 offset 且無本地時區聲明可依）→ 整份 UNREADABLE。
  offset 分布（每輸入一個 Counter：+08:00/naive/其他）入 manifest。
- 時序契約（違反者 `source_limit=ORDER_VIOLATION`，歸 NOT_PROVABLE）：
  `ENTRY_A <= ENTRY_B < RELEASE_A < EXIT_B`
- out-log 日期 prefix 已知 ±1 日偏移且**永不作為證據**

## 4. 防誤連（EXIT_LOG 無 trade_id）

- `EXIT_LOG` 事件**不含 trade_id** → **永不**作為個別證據；只用於全量對照統計，
  artifact 標 `evidence=STATISTICS_ONLY`
- per-trade 判定只使用含 trade_id 的記錄：PEAK_CONFIRMED/PEAK_REJECTED/TRIGGER_SUPPRESSED、fills
- **per-trade 證據事件缺 trade_id → 排除出證據 + 計入 manifest**
  （`per_trade_evidence_missing_trade_id` 計數），不污染全域驗證

## 5. 分類規則（v6 — v4/v5 因果限制不變）

### SUPPORTED（attribution_strength=PROVEN）— 現行 schema 下 by design = 0
必要證據：同 trade、pre-submit、**明確指名 Policy J 觸發/勝出**的事件（例如
`POLICY_J_SINGLE_LEG_TRIGGERED` / `POLICY_J_TRIGGERED` 帶 trade_id，或 legacy 等價欄位）。
已驗證現行 events ledger 不存在 → 全部候選 SUPPORTED 不成立，
`summary.reasons=["NO_TRIGGER_NAMED_EVENT_IN_SCHEMA"]`，`SUPPORTED.PROVEN=0`。
若未來 schema 含此類事件才套用：事件 ts < EXIT fill ts、事件指名該 trade。
**absence-of-competing-cause 不得升級 provenance**（PEAK_CONFIRMED 只 arm peak，
不代表 giveback 觸發或贏過 native TRAIL — 明確寫死）。

### CONTRADICTED — 現行 schema 下 by design = 0
必要證據：同 trade 的**最終決策標記**，指名「此 exit 非 Policy J 觸發」（帶
cause=trail/release_threshold 且 trade_id）。RELEASE_*_SUBMITTED trade_id=None 且無 cause
→ 不可用；PEAK_REJECTED/TRIGGER_SUPPRESSED 不構成 counterfactual。
schema 無此標記 → `CONTRADICTED=0` + `summary.reasons=["NO_FINAL_DECISION_CAUSE_EVENT"]`。

### INSUFFICIENT_EVIDENCE（其餘全部）
- **INFERRED_ELIGIBLE**（僅當 §7 參數可溯源）：無 trigger-named 事件，但以**已解析的
  deployed 參數**重建 `durable_peak >= activation` 且 giveback 條件在 [RELEASE_ts, EXIT_ts]
  成立 → `eligibility_consistent=true`、`source_limit=NO_DECISION_PROVENANCE`
  （語意：條件一致，因果不可歸因）
  - 前置條件：該候選時間語意必須**乾淨**（無 TS_NAIVE / TS_SEMANTICS_UNKNOWN /
    TS_MISSING）— 任何時間不確定 → 直接 NOT_PROVABLE + eligibility_consistent=null
    （UTC+8 假設不得用以建立 eligibility）
  - 參數不可溯源 → 不得以現行值計算 → `eligibility_consistent=null`、NOT_PROVABLE
    （現行 config 值只進 `params_current_reference` 供參考）
- **NOT_PROVABLE**：其餘全部（無 peak、缺價格、時序違反、TS 語意問題、§2 拒絕、
  eligibility 無法計算）

## 6. 輸入快照與 schema 驗證（v5：全域/型別分層）

```
輸入 1:  {runtime}/logs/mts_trade_fills.jsonl
輸入 2:  {runtime}/logs/mts_spread_events.jsonl
（runtime 根：/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime）
```

1. **每輸入只讀一次**：`open(path,"rb").read()` → 不可變 bytes；sha256 對「實際被 parse 的
   bytes」；parse 一律從記憶體 bytes 進行
2. **全域 schema 驗證（先於任何候選處理）**：
   - fills 必須含 keys：`trade_id/timestamp/leg/contract/side/fill_type/qty/price`
   - events 必須含 keys：`event`、`ts`（**全域必備**）；`trade_id` **全域可選** —
     僅「per-trade 證據事件型別」（PEAK_CONFIRMED / PEAK_REJECTED / TRIGGER_SUPPRESSED）
     **必須**有 trade_id；全域事件（無 trade_id，實測 2,914 筆）計入 manifest
     `global_events_without_trade_id`，**永不 UNREADABLE**
   - 任一核心 key 缺失 → `status=UNREADABLE` + `schema_mismatch` 細節
3. **enum 驗證（由快照觀察 + allowlist 對照）**：
   - 從快照建 `observed_enum_by_fill_type`（每個 fill_type → 實際 side 值集合）與
     `observed_event_types`（每個 event → 計數）
   - 與 §2 顯式 allowlist（有限集合）對照：fill_type/side 在 allowlist 內 → 正常；
     `TEST` → 已知非候選型別：行排除 + 計數（`test_rows`）；帶非空 trade_id 的 TEST 行 →
     該 trade 整筆排除 + 計 `TEST_TRADE_CONTAMINATION`，**不 UNREADABLE**；
     allowlist 外值（含 COMBINED_EXIT 新 prefix/未知型別）→ `status=UNREADABLE` + 該值細節
   - COMBINED_EXIT 五型別（顯式清單）以**排除型別**處理（含其 trade 拒絕為候選），
     allowed side 為 {BUY,SELL,NONE,""}（實測），不做 settlement-only 限制
4. **malformed/torn 處理**：任一 JSONL 行無法 parse、或檔尾不完整行 → 整份快照
   `status=SNAPSHOT_MALFORMED`/UNREADABLE，不產出任何 trade 分類
   （無法得知壞行屬於哪個 trade；部分分類會是假的完整）
5. **timestamp 語意（§3 規則）**：parser 層無法確立時間語意時整份 UNREADABLE；
   可確立時逐候選套用 NOT_PROVABLE 條件
6. runtime ledgers 稽核期間仍會 append → 快照 = 讀取當下 byte image；`snapshot_read_ts`
   入 manifest；同 byte image 重跑可重現相同 hash

## 7. 參數溯源（v5 不變）

- activation/giveback 有效值以該 trade 日期的 deployed config 為準：
  1. `git log --follow --format=%H -- config/futures.yaml` + release 部署時間線 → 對齊該日期
     的 config commit → `param_source=DEPLOYED_CONFIG_<sha>`，以該檔值計算
  2. 不可對齊 → `param_source=PARAMETER_VERSION_UNKNOWN` → 該候選
     `eligibility_consistent=null`、NOT_PROVABLE
  3. 現行 futures.yaml 值一律另存 `params_current_reference`（僅參考，不進分類）
- mult(10)/friction(92)：以 §7.1 對應 commit 的 plugin 碼為準；不可解 → 同上 UNKNOWN

## 8. artifact schema / 輸出位置 / manifest（v5）

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
  "git_dirty": "<git status --porcelain 輸出>",
  "manifest": {
    "inputs": {
      "fills":  {"sha256": "...", "bytes": n, "snapshot_read_ts": iso,
                 "source_schema": {
                   "keys": [...],
                   "observed_enum_by_fill_type": {"ENTRY": ["LONG","SHORT"], "EXIT": ["BUY","SELL"],
                                                  "COMBINED_EXIT": ["BUY","SELL","NONE",""],
                                                  "COMBINED_EXIT_COMPLETED": ["NONE"],
                                                  "COMBINED_EXIT_SETTLED": ["NONE"], "TEST": ["SELL"]},
                   "allowlist_ok": true,
                   "test_rows": n,
                   "test_trade_contamination": n,
                   "timestamp_offsets": {"+08:00": n, "naive": n, "other": n}}},
      "events": {"sha256": "...", "bytes": n, "snapshot_read_ts": iso,
                 "source_schema": {
                   "keys": [...],
                   "event_types": {"POLICY_J_PEAK_CONFIRMED": n, ...},
                   "global_events_without_trade_id": n,
                   "per_trade_evidence_missing_trade_id": {"PEAK_CONFIRMED": n, ...},
                   "timestamp_offsets": {"+08:00": n, "naive": n, "other": n}}}
    },
    "parser_assumptions": ["jsonl", "utf-8", "iso8601_timezone_aware", "naive_ts_mean_utc8",
                           "out_log_prefix_never_evidence"],
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
  "rejected_candidates": {"QTY_MISMATCH": n, "MULTI_EVENT": n,
                          "BAD_SIDE": n, "BAD_PRICE": n, "COMBINED_EXIT": n,
                          "TEST_TRADE_CONTAMINATION": n},
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
      "source_limits": ["NO_DECISION_PROVENANCE", "ORDER_VIOLATION", "TS_NAIVE",
                        "TS_SEMANTICS_UNKNOWN", "TS_MISSING", ...]
    }
  ],
  "statistics_only": {"exit_log_count": n, "peak_confirmed_total": n, "trigger_suppressed_total": n}
}
```

## 9. raw_ticks 截止 7/28 的限制（不變）

- raw_ticks 僅涵蓋 2026-07-23 ~ 07-28 → 之後候選無 tick/bar 重播；peak 只能以事件記錄為準
- 每筆標 `tick_availability: NONE`（全期間）；不嘗試用 tick 補證據

## 10. 交付物順序（v5）

1. 本設計文件 v5（committed，僅 docs）
2. codex 核准 v5 → committed script `scripts/research/pj_single_leg_attribution/audit.py`
   （含 §6 快照+schema 驗證、§3 timezone-aware parse、§7 溯源、§5 分類；
   script file sha256 + commit SHA 入 manifest）
3. 執行 → artifact（含 manifest）→ 送 codex 審查
4. 任何 production 變更（含 P1 接線）於 v5 審查通過後另行排程 — 本稽核全程不觸碰
