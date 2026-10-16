# MTS Ledger Authority Projection — Forensic & Design Proposal

**狀態**: PROPOSAL（read-only forensic；module `strategies/futures/mts_ledger_authority.py`
與其測試**已凍結** — 不 stage / 不 commit / 不接 monitor / 不部署 / 不再編輯，直到本 proposal
經 codex 審查並有獨立 RED 設計）
**範圍**: 僅本文件。2026-08-06 codex audit round 2 verdict item 2 的交付物。

## 1. 凍結背景與觸發事實

`capture_flat.py`（唯讀 baseline）以現行 projection 掃描真實 fills ledger，發現 3 個
「anomaly trades」（`mts-auto-153858-398` / `mts-auto-094226-675` / `mts-auto-180039-642`），
逐筆追蹤後證實**皆為已平倉** trade：

| trade | 事實 |
|---|---|
| 153858-398 (8/3) | NEAR ENTRY SHORT → COMBINED_EXIT side=**SELL**（正確平倉應為 BUY）；FAR ENTRY LONG → COMBINED_EXIT side=**BUY**（正確應為 SELL）— **v1 符號 bug 汙染** |
| 094226-675 (8/4) | NEAR RELEASE SELL（正確）+ FAR EXIT BUY ×**2**（442µs 間隔，同 leg/side/qty/price — **重複 deal**） |
| 180039-642 (8/4) | NEAR ENTRY SHORT → COMBINED_EXIT BUY ✓；FAR ENTRY LONG → COMBINED_EXIT **BUY**（正確應為 SELL）— 遠月側汙染 |

現行（凍結中的）projection 以「exit 型 fill 依 side 累加」→ 上述 trade 被判 OPEN；
已做 reduce-toward-zero + overclose clamp 修正（**未 commit**，隨本 proposal 一併凍結）。

## 2. 各 fill_type 的「應有」side 語意（intended semantics）

以 plugin 送單路徑為準（`tmf_spread.py` 訂單建構 + fills ledger 寫入）：

| fill_type | 應有 side | 語意 | 歷史實測 | 可信度 |
|---|---|---|---|---|
| ENTRY | LONG / SHORT | 持倉方向（BUY_NEAR_SELL_FAR → near LONG / far SHORT） | 實測 LONG/SHORT 各 373 | **可信** |
| RELEASE | BUY / SELL | 收單方向（釋放腿：LONG 持倉 → SELL） | RELEASE 71 筆 | 需抽樣核對 |
| EXIT | BUY / SELL | 收單方向 | EXIT 90 筆 | 需抽樣核對 |
| COMBINED_EXIT | BUY / SELL | 收單方向（**應**與 ENTRY 對稱） | **實測被 v1 bug 汙染**（153858/180039 反側） | **不可信** |
| COMBINED_EXIT_NEAR/FAR | BUY / SELL | 同 COMBINED_EXIT | 少量 | 不可信 |
| COMBINED_EXIT_COMPLETED/SETTLED | NONE / "" | 結算標記（qty=0，無方向） | 實測 NONE/"" | 非方向行 |
| TEST | 任意 | 測試污染 | 1 筆 | 排除 |

**結論**：COMBINED_EXIT 家族（v1 時期）的 side 不可作為方向依據；projection 對
「已平倉判定」不能依賴 exit fill 的 side，只能依賴 **qty 對帳 + trade 生命週期完整性**。

## 3. trade / contract / qty 關聯（correlation）

- 每個 trade_id 的 legs = {NEAR, FAR}（fills 的 `leg` 欄位）；`contract` 欄位為
  實際代碼（TMFH6/TMFI6）— 需驗證：同 trade 內 NEAR leg 的 contract 恆為近月代碼、
  FAR 恆為遠月代碼，且與 events 的 contract 一致（**snapshot code consistency**）
- qty 關聯：trade 內 ENTRY/RELEASE/EXIT 的 qty 必須逐腿對帳
  （entry_qty == release_qty / entry_qty == exit_qty）；COMBINED_EXIT 兩腿各 1
- 現行 fills 的 qty 全為 1（實測），但 projection 必須以一般 qty 設計（qty=2 情境）

## 4. 重複事件識別（duplicate identity）

- 094226 案例：同 leg/side/qty/price、ts 差 442µs — 重複寫入（同一 deal 的二次落盤）
- 建議 dedup 鍵：`(trade_id, leg, side, qty, price, timestamp)` — ts 有微秒可區分真事件；
  但**重複可能 ts 完全相同**（同批次寫兩行）→ dedup 鍵需含「來源行序」或
  deal_id（fills 無 deal_id 欄位 — 需與 events 的 deal 記錄 join 才能拿 deal_id）
- **open question（待設計）**：dedup 應在「寫入端」（有 deal_id，消除源頭）還是在
  「投影端」（防呆）？現行 frozen code 是投影端 seen-set（deque 5000）

## 5. partial / cancel / reject / out-of-order callbacks

| 情境 | 應有行為 | 現行 frozen code | 缺口 |
|---|---|---|---|
| partial fill（qty 分次） | 逐次累積到 target qty | qty 累加 ✓ | 需 target 對帳（order qty） |
| cancel（無 fill） | 不影響投影（無 fill 行） | ✓（只吃 fills） | events cancel 與 fills 的對帳未做 |
| reject | 不影響投影（無 fill 行） | ✓ | 同上 |
| out-of-order 寫入 | ts 反序 → 標 TS_OUT_OF_ORDER | snapshot 層有 flag 概念（audit）；projection 無 | projection 需同規則 |
| 重複 deal | dedup（§4） | seen-set（時間窗） | 需 deal_id 或全鍵 |

## 6. UNKNOWN vs clamp 政策（核心設計決策）

**codex 立場**：reduce-toward-zero + clamp 可能把「真正的重複/超平倉」靜默抹平 —
一個真正 OPEN 的部位被誤判 FLAT，風險是**該管理而未管理**（比誤判 OPEN 更危險）。

**proposal（待審查）**：
1. **投影永不 clamp 成 FLAT**：任何 overclose（exit qty > 剩餘 qty）或
   同 trade 內對帳失敗 → 該 trade 標 **UNKNOWN**（不是 clamp 到 0）
2. UNKNOWN 的 authority 語意：該 trade 不能當 OPEN 保護（避免誤保護），
   也不能當 FLAT 清掉（避免誤 reset）→ **UNKNOWN trade 使整個 snapshot 的
   current-trade 判定降為 UNKNOWN**（fail-closed：不 reset、不 RECONSTRUCT、
   後續接線的 pre-signal gate 對 UNKNOWN 只 PASS）
3. 判定順序（每 trade）：lifecycle 完整性（entry 兩腿）→ qty 對帳 →
   無 overclose → 才算有效 OPEN/FLAT；任一不滿足 → trade 級 UNKNOWN
4. **測試要求（不靜默抹平真 OPEN）**：
   - 真 OPEN trade + 一條多餘 exit 行 → 必須 UNKNOWN（不是 FLAT）
   - entry qty=2 + exit qty=1 → OPEN(qty=1)（partial 正常）
   - entry qty=1 + exit qty=2 → UNKNOWN（overclose，非 clamp FLAT）
   - 重複 exit（同鍵）→ dedup 後正常 FLAT；不同鍵的重複（ts 微差）→
     依 §4 政策（deal_id 不可得時 → UNKNOWN）
   - COMBINED_EXIT side 汙染（§2）→ 以 qty 對帳判定 closed（153858/180039 應 FLAT），
     但**若同時有 qty 異常 → UNKNOWN**

## 7. 交付順序（proposal 後）

1. 本 proposal 審查 → 2. 獨立 RED 設計（UNKNOWN 三態含 overclose 語意）→
3. RED tests → 4. 實作 → 5. review → 6. commit → 7. monitor 接線另案。
不與 historical audit、Live Route Certification 混同。

## 8. 詞彙（terminology）

**VERIFIED_FLAT（baseline 20260808_031249）= paper fills ledger/state projection 為 FLAT**；
**不是** live-broker flat 證明。任何引用 baseline 的文件/報告必須明示此界線。
