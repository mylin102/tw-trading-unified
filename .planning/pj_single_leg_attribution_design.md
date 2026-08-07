# 唯讀歷史單腿 Policy J 歸因稽核 — 設計文件 v3

**狀態**: DESIGN v3（codex 審查中；通過後才實作 committed script + artifact）
**範圍**: 僅 docs。無 production/ledger/dashboard 修改、無 deploy/restart。
**v3 變更**(對 v2)：① 分類改 `attribution_strength=PROVEN|INFERRED_ELIGIBLE|NOT_PROVABLE`，
SUPPORTED 必須有同 trade、pre-submit 的顯式決策證據；② CONTRADICTED 只用決策當下的
timestamped 反證，不得用 EXIT fill 價推論；③ 候選完整性收緊（雙腿完整 ENTRY、數量對帳、
單一 release/exit、拒絕 partial/overclose/unknown）；④ 參數溯源（deployed config 可解才當史實，
否則 PARAMETER_VERSION_UNKNOWN）；⑤ 輸入以不可變 byte 快照讀取+hash、torn/malformed 顯式標記；
⑥ artifact manifest 含 script commit SHA、快照 hash、分類計數，且不宣稱 gitignore 是安全邊界。

## 1. 目標

歷史單腿出場（一腿 RELEASE + 另一腿 EXIT）是否為 Policy J（combined-UPL giveback）觸發。
P1-B 之前的單腿出場沒有逐 trade 的 submission-cause 記錄，本稽核以既有記錄做三分類可歸因性
判定。**唯讀**：不回寫任何 ledger/state/event。誠實底線：舊資料多數結果會是
INFERRED_ELIGIBLE 或 NOT_PROVABLE —— 稽核的價值在於「哪些能 PROVEN、哪些只能說 eligibility
一致」，而非硬給 SUPPORTED/CONTRADICTED 計數。

## 2. 候選選取（v3：完整性契約）

對 fills ledger 每筆記錄建立 `(trade_id, leg) → fill 序列`（同 trade 內**全部** fills 聚總，非任一筆）：

```
candidate 成立（同一 trade_id，全部必要）：
  1. 兩腿各有完整 ENTRY：NEAR 與 FAR 各存在 fill_type=="ENTRY"，qty=1，side ∈ {LONG,SHORT} 且非空，price > 0
  2. 恰一腿 release：leg A 存在 fill_type=="RELEASE" 且 qty=1（超過一次 → 拒絕）
  3. 對側腿 exit：leg B（B != A）存在 fill_type=="EXIT" 且 qty=1（超過一次 → 拒絕）
  4. 數量對帳：leg A ENTRY qty == RELEASE qty；leg B ENTRY qty == EXIT qty（
     不等 → overclose/partial → 拒絕，source_limit=QTY_MISMATCH）
  5. 無 COMBINED_EXIT 同 trade（雙腿同時出場不算單腿候選）
  6. 拒絕條件（各帶 source_limit，計入 rejected_candidates 清單）：
     partial fill（qty 非整數或 0）、multi-event（>1 RELEASE / >1 EXIT）、
     未知 fill_type、side 缺失或非 LONG/SHORT、price<=0、COMBINED_EXIT 混入
```

- join key 唯一：`trade_id`
- v2 的探查值 70 為舊規則（「任一腿 ENTRY 存在」）；v3 以 §2 完整契約重新計算，
  `candidates_considered` 與 `rejected_candidates`（含 reject reason 計數）皆入 artifact

## 3. 時序契約與時間假設

- 每筆候選取：ENTRY ts（兩腿）、RELEASE ts（leg A）、EXIT ts（leg B）
- 時序契約（違反者 `source_limit=ORDER_VIOLATION`，歸 NOT_PROVABLE）：
  `ENTRY_A <= ENTRY_B < RELEASE_A < EXIT_B`（同 trade_id 內比較）
- **時間假設（記錄於 artifact parser_assumptions）**：
  - events/fills 的 `ts` 為 ISO-8601、台灣本地時間（UTC+8），字串序可比較
  - out-log 的日期 prefix 已知與 events ledger 存在 ±1 日偏移 — **永不作為證據**，
    只用 events ledger / fills ledger 的 ts
  - 若發現同 trade 內 ts 反序（亂序寫入）→ 該候選 `source_limit=TS_OUT_OF_ORDER` 歸 NOT_PROVABLE

## 4. 防誤連（EXIT_LOG 無 trade_id）

- `EXIT_LOG` 事件**不含 trade_id** → **永不**作為任何候選的個別證據；
  只用於全量對照統計（EXIT_LOG 總數 vs PEAK_CONFIRMED 總數），artifact 標 `evidence=STATISTICS_ONLY`
- 任何需要 trade 層級證據的判定只使用含 trade_id 的記錄：
  `POLICY_J_PEAK_CONFIRMED`、`POLICY_J_PEAK_REJECTED`、`POLICY_J_TRIGGER_SUPPRESSED`、
  `RELEASE_*_SUBMITTED`、fills

## 5. 分類規則（v3：三類 × attribution_strength）

「決策當下」定義：該 trade 在 EXIT fill ts 之前的**最後一筆** Policy J 評估事件
（依 ts 排序，候選事件型別 = PEAK_CONFIRMED / PEAK_REJECTED / TRIGGER_SUPPRESSED）。
以決策當下事件為準 — 不用 EXIT fill 價推論觸發與否（fill 是 post-submit 證據）。

### SUPPORTED（attribution_strength=PROVEN）— 同 trade、pre-submit 顯式決策證據，缺一不可
1. 同 trade_id 在 EXIT fill ts 之前存在 `POLICY_J_PEAK_CONFIRMED`（= 決策當下事件）
2. 該 CONFIRMED 的 `durable_peak >= activation`（activation 依 §7 溯源，不可解時本類不成立
   → 降為 INFERRED_ELIGIBLE + `param_source=PARAMETER_VERSION_UNKNOWN`）
3. CONFIRMED ts 與 EXIT fill ts 之間無同 trade 的競爭原因證據：
   同 trade 存在 `RELEASE_*_SUBMITTED`/EXIT submission 事件但**無** Policy J 決策事件在
   submission 之前緊鄰 → `source_limit=COMPETING_CAUSE` 降為 INFERRED_ELIGIBLE
4. §2 候選完整性全過
   → 可歸因：決策事件存在、決策先於出場、且決策當下條件成立

### CONTRADICTED — 只有同 trade、timestamped 的「決策當下」反證
1. 該 trade 在 EXIT fill ts 之前的決策當下事件為 `POLICY_J_PEAK_REJECTED`
   或 `POLICY_J_TRIGGER_SUPPRESSED`（即最後一筆評估結論 = 不觸發）
2. 且該反證事件之後到 EXIT fill 之間**無** PEAK_CONFIRMED
   → 可證明「出場決策當下 Policy J 未觸發」
- 明確禁止：以 EXIT fill 價重建的 current_net 反推矛盾（post-decision 證據）
- TRIGGER_SUPPRESSED 為高頻事件（每 tick），取「最後一筆」而非任意一筆

### INSUFFICIENT_EVIDENCE（其餘全部）— 內分兩級
- **INFERRED_ELIGIBLE**：無 pre-submit 決策事件，但由事件 + fills 重建的
  `durable_peak >= activation` 且 giveback 條件在 [RELEASE_ts, EXIT_ts] 區間內成立
  → `source_limit=NO_DECISION_PROVENANCE`、`eligibility_consistent=true`；
  語意：條件一致但不可歸因（native TRAIL 可能先贏，fill 價無法分辨）
- **NOT_PROVABLE**：無 peak / 缺價格（entry/release/exit 任一 0 或缺）/
  時序違反 / §2 拒絕 / 決策當下事件缺失或混雜 — 全部歸此

## 6. 輸入快照（v3：不可變 byte 快照）

```
輸入 1:  {runtime}/logs/mts_trade_fills.jsonl
輸入 2:  {runtime}/logs/mts_spread_events.jsonl
（runtime 根：/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime）
```

- **每個輸入只讀一次**：`open(path, "rb").read()` → 記憶體中的不可變 bytes；
  `sha256` 對「實際被 parse 的那份 bytes」計算；parse 一律從該 bytes 進行（不重新開檔）
- runtime ledgers 在稽核期間仍會被 append → 快照 = 讀取當下的 byte image；
  artifact 記錄 `snapshot_read_ts`；重跑以相同 byte image 可重現相同 hash
- malformed/torn 處理（**永不靜默跳過**）：
  - 檔尾不完整 JSON 行 → `snapshot_torn=true` + 該行計入 `torn_lines`，受影響候選
    `source_limit=PARSE_ERROR` 歸 NOT_PROVABLE
  - 檔中 malformed 行（非 JSON）→ 同上，計入 `malformed_lines`，不靜默略過
  - 任一個輸入檔案無法讀取（不存在/權限）→ 整個稽核 `status=UNREADABLE` 並中止，
    不產出部分結論
- parser 假設（入 artifact）：JSONL、UTF-8、逐行 json.loads、ts 為 ISO-8601 字串

## 7. 參數溯源（v3）

- activation（200）/ giveback（50）的**有效值**以該 trade 日期的 deployed config 為準：
  1. 嘗試解析：`git log --follow --format=%H -- config/futures.yaml` +
     release 部署時間線（releases/<sha>/ 目錄 + pm2 deploy 記錄）→ 找到該日期生效的
     config commit → `param_source=DEPLOYED_CONFIG_<commit_sha>`，以該檔值計算
  2. 無法解析（無部署記錄/日期不可對齊）→ `param_source=PARAMETER_VERSION_UNKNOWN`，
     `params_assumed_from_current=true`：使用現行 futures.yaml 值僅為**假設**，
     artifact 中標記，**不宣稱是歷史真相**
- mult(10)/friction(92)：以 §7.1 解析出的 commit 對應的 plugin 碼為準；同樣不可解 →
  PARAMETER_VERSION_UNKNOWN
- 每個候選的 classification 依其解析到的參數版本計算；同一次執行可能混用
  DEPLOYED_CONFIG 與 PARAMETER_VERSION_UNKNOWN（不同 trade 日期），逐筆標記

## 8. artifact schema / 輸出位置 / manifest（v3）

```
輸出: {runtime}/exports/research/pj_single_leg_attribution_<YYYYmmdd_HHMMSS>.json
```

**安全邊界聲明**：runtime/exports 由 repo .gitignore 排除，這只是檔案管理慣例，
**不是**安全邊界；artifact 為外部可讀檔。此聲明寫入 artifact。

```json
{
  "status": "OK | UNREADABLE",
  "generated_at": iso,
  "script_commit_sha": "git rev-parse 執行當下 script 的 commit（script 必須已 commit 才執行）",
  "manifest": {
    "inputs": {
      "fills":  {"sha256": "...", "bytes": 123456, "snapshot_read_ts": iso, "torn_lines": 0, "malformed_lines": 0},
      "events": {"sha256": "...", "bytes": 234567, "snapshot_read_ts": iso, "torn_lines": 0, "malformed_lines": 0}
    },
    "parser_assumptions": ["jsonl", "utf-8", "iso8601_local_tw"],
    "params": {"activation_twd": 200, "giveback_twd": 50, "mult": 10, "friction": 92,
               "param_source": "DEPLOYED_CONFIG_<sha> | PARAMETER_VERSION_UNKNOWN",
               "params_assumed_from_current": true|false}
  },
  "classification_counts": {
    "SUPPORTED": {"PROVEN": n},
    "CONTRADICTED": {"CONTRADICTED": n},
    "INSUFFICIENT_EVIDENCE": {"INFERRED_ELIGIBLE": n, "NOT_PROVABLE": n}
  },
  "candidates_considered": n,
  "rejected_candidates": {"QTY_MISMATCH": n, "MULTI_EVENT": n, "UNKNOWN_TYPE": n, "BAD_SIDE": n, "BAD_PRICE": n, "COMBINED_EXIT": n},
  "trades": [
    {
      "trade_id", "released_leg", "remaining_leg",
      "entry_ts", "release_ts", "exit_ts",
      "entry_prices": {"near": p, "far": p}, "release_price": p, "exit_price": p,
      "decision_event": {"type": "PEAK_CONFIRMED|PEAK_REJECTED|TRIGGER_SUPPRESSED|NONE", "ts": iso,
                          "durable_peak": x, "param_source": "..."},
      "eligibility_consistent": true|false|null,
      "classification": "SUPPORTED|CONTRADICTED|INSUFFICIENT_EVIDENCE",
      "attribution_strength": "PROVEN|INFERRED_ELIGIBLE|NOT_PROVABLE",
      "evidence_keys": ["PEAK_CONFIRMED", "FILLS"],
      "source_limits": ["NO_DECISION_PROVENANCE", "ORDER_VIOLATION", "COMPETING_CAUSE", ...]
    }
  ],
  "statistics_only": {"exit_log_count": n, "peak_confirmed_total": n}
}
```

## 9. raw_ticks 截止 7/28 的限制（不變）

- `raw_ticks` 僅涵蓋 2026-07-23 ~ 07-28（tick 持久化缺口）→ 7/28 之後的候選無 tick/bar
  重播；peak 只能以 PEAK_CONFIRMED 記錄為準
- 7/23-7/28 期間亦無完整 BBO 序列 → 同以事件記錄為準
- artifact 每筆標 `tick_availability: NONE`（全期間）；不嘗試用 tick 補證據

## 10. 交付物順序（v3）

1. 本設計文件 v3（committed，僅 docs）
2. codex 核准 v3 → committed script `scripts/research/pj_single_leg_attribution/audit.py`
   （含 §6 快照、§7 溯源、§5 分類實作；script commit SHA 寫入 artifact）
3. 執行 → artifact（含 manifest）→ 送 codex 審查
4. 任何 production 變更（含 P1 接線）於 v3 審查通過後另行排程 — 本稽核全程不觸碰
