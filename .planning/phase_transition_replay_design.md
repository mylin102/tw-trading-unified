# Phase-Transition Replay — SINGLE_LEG release 研究成果審計（RESEARCH ONLY）

**狀態**: DESIGN + RED tests（codex 2026-08-08 授權新 workstream；先設計 +
RED，審查後才 implementation/run）
**硬限制**: **零 production 變更**（strategy/config/monitor/dashboard/
deploy/restart/push 全禁）。研究腳本只在 scripts/research/ 下。
**重用**: committed `scripts/research/exit_attribution/` 元件
（reconcile.py / classify.py / quoting.py / stats.py / pipeline.py /
manifest.py / fee.py / run_real.py）— **禁止 ad-hoc parser**。

## 1. 目標問題

SPREAD → SINGLE_LEG transition（Policy J release）到底該被阻止，還是保留
release、只重做 post-release risk controller？
→ 對每個 SINGLE_LEG release 做**因果分類**（predeclared, mutually
exclusive）+ 四臂反事實回放。

## 2. Candidate 契約（#24-1）

- candidate = reconciled 完整雙腿 ENTRY + **恰一個** release decision/event
  + matched fills
- unresolved/corrupt/partial → **censored with exclusion reason**
  （never drop silently）; 產出 censored 清單（含原因）
- **event time = release DECISION / ORDER_SUBMITTED timestamp**（非 fill）;
  同時記錄 decision ts 與 actual fill ts + skew
- reconciliation/quality tier 沿用 exit_attribution.reconcile（不另寫）

## 3. Per-candidate 記錄（#24-2）

pre_release_combined_net / released-leg locked PnL / remaining-leg UPL /
release leg+qty / post-release combined-net **peak** / MAE / exposure
duration / actual controller winner+reason / actual full net /
quality+reconciliation status。

## 4. 四臂反事實（#24-3 + #25-1/2）

| 臂 | 定義 |
|---|---|
| ACTUAL | 實際路徑（controller winner/reason 記錄） |
| ATOMIC | decision 當下 immediate atomic 雙腿合併 exit |
| REMAIN_SPREAD | 拒 release、維持 SPREAD — **clone point = release-decision
  event 前一刻**，deep-clone/hash 完整 strategy state（positions + policy
  peak/durable candidate/warmup/armed + ATR/reference prices + pending
  candidates/orders + quote freshness + controller/lifecycle/release/trail
  + cooldown + config/version）; missing/non-reconstructible →
  **NOT_AVAILABLE**（不合成 default）; persist clone schema/version/hash +
  missing fields 清單 |
| RELEASE_MANAGED | release + 專屬 SINGLE_LEG combined-net/time-stop
  controllers |

- 四臂消費**同一 immutable、globally-ordered market-event stream** +
  clock contract; artifact 每 event 記錄 source_event_seq /
  exchange_ts / recv_ts / replay_seq / stream hash / ordering key
- derived bars 由同一 stream 產生（**不得** BBO 用一臂、bar-close 用另一臂）
- sequence/clock integrity 缺失 → stateful arm **NOT_AVAILABLE**
- 無 look-ahead; remain-SPREAD 不得沿用 actual remaining-leg exit ts

## 5. Admission threshold sweep（#24-4 + #25-3）

- combined net <= 0 / -100 / -200 TWD（decision 當下）— preregistered
  sensitivity sweep over **overlapping subsets**（非獨立策略）
- 回報 N overlap matrix + distribution/paired deltas; **禁 winner-by-max
  PnL**; 需 robustness 判讀（plateau vs isolated threshold）
- **不改 Policy J SPREAD activation=200**; 不稱 admission counterfactual
  為 live policy recommendation

## 6. Execution 價格契約（#24-5）

- executable BBO **僅當**雙腿 last bid/ask ts <= decision 且在 documented
  staleness/skew bounds 內; close LONG @ bid − ≥1 adverse tick,
  SHORT @ ask + ≥1 adverse tick
- tick-only 值 = **BOUNDED_TICK_PROXY**，永不混入 executable stats;
  無可成交 BBO → NOT_AVAILABLE（「無法證明」是有效結果）

## 7. 因果分類（#25-5 + final refinement, predeclared, mutually exclusive）

**Interval dominance（freeze-classifier amendment 2026-08-08）**:
- 每個 Yi **NET of path-specific deterministic fees/tax**; interval
  [Li,Ui] = **residual execution uncertainty ONLY**（成本不重複計入）
- F_N = [max(L1,L2), max(U1,U2)]; F_R = [max(L0,L3), max(U0,U3)]
- HARMFUL iff `lower(F_N) − upper(F_R) > M_economic`; BENEFICIAL iff 反向;
  overlap → neutral
- MANAGEMENT_BAD（conservative）: `lower(Y3) − upper(Y0) > M_economic`
  AND `lower(Y3) >= upper(F_N) − M_economic`
- **M_economic = preregistered minimum economic decision benefit**
  （fees/tax 已在 Yi 內, 不重加）
- pairwise U_δ(i,j) 保留（M_ij = max(M_economic, U_δ); shared costs 相消）

**Frozen precedence**:
1. evidence gate → INDETERMINATE（最優先, 壓過一切經濟分類）
2. MANAGEMENT_BAD（保守式, 見上）
3. HARMFUL（F_N/F_R interval dominance）
4. BENEFICIAL（反向）
5. INCONCLUSIVE_NEUTRAL（overlap / ≤ M — 強制類別）

Y0=atomic, Y1=remain-SPREAD, Y2=actual, Y3=release+managed。
RED boundary tests（獨立 collection, 不 skip）: shared cost 完全相消 /
單一路徑多一腿成本 / family winner 在 intervals 下翻轉 / threshold 相等
→ neutral / management+beneficial overlap precedence / evidence gate
壓過一切。

## 8. Conditional time stops（#25-4）

- time stops 30/120/300: **僅當** horizon 當下 `current_combined_net <
  combined_net_at_release` 才觸發（記錄 recovery condition/value）
- 無條件 horizon mark 單獨回報（與 triggered exits 分開）

## 9. 輸出

expectancy / median / left-tail / MAE / recovery rate; artifact manifest
（input hashes + clone hash + stream hash）; censored 清單; NOT_AVAILABLE
理由。**No production change** 直到 SINGLE_LEG observations 累積足夠 +
admission-gate hypothesis 過 replay。

## 10. 交付順序

1. 本設計 + RED tests（本輪）→ codex 審查 → 2. implementation（研究
   scripts only）→ 3. run + artifact → 4. 報告。
