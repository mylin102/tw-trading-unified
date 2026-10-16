# Replay Conclusion Gap — Step 1 Design: entries + release_leg in snapshot

**狀態**: DESIGN + RED only（送 codex 審查; 接受後才實作）
**根因**: snap_v5_retry events 缺 per-leg entry price/qty + release_leg
→ engine 全數 fail-closed INDETERMINATE_DATA_QUALITY, 無 Y0..Y3 結論

## 1. 資料來源（已存在於 mts_trade_fills.jsonl）
- ENTRY fill: {fill_type=ENTRY, leg=near/far, side=LONG/SHORT, price,
  qty, trade_id} — per-leg entry reference
- RELEASE fill: {fill_type=RELEASE, leg=near/far, trade_id} — 單腿釋放
  目標

## 2. Adapter 契約（source_adapter.py, research-only）
(1) **adapt_fill 保留 price**: normalized fill 增加 price 欄位
(2) **join_entries(fills, anchor_trade)** -> {near: {price, qty},
    far: {price, qty}}:
    - 從該 trade 的 ENTRY fills 依 leg join; 缺任一腿/price/qty ->
      ("NOT_AVAILABLE", reason) — 永不推測
(3) **release_leg(fills, anchor_trade)** -> "near"/"far":
    - 從該 trade 的 RELEASE fill 的 leg; 多筆 RELEASE fill 同 trade
      leg 不一致 -> ("NOT_AVAILABLE", reason); 無 RELEASE fill ->
      NOT_AVAILABLE
(4) **build_normalized_snapshot**: 每 event 帶
    entries + release_leg（join 成功才 emit; 失敗 -> per-candidate
    refused, 入 refused_candidates, 不 emit 半殘 event）

## 3. RED matrix
- join_entries per-leg 正確（price/qty 保留）
- join_entries 缺 ENTRY fill（trade 無 ENTRY）-> NOT_AVAILABLE
- join_entries 缺 price -> NOT_AVAILABLE
- release_leg 從 RELEASE fill leg（near/far）
- release_leg 多筆不一致 -> NOT_AVAILABLE
- release_leg 缺 RELEASE fill -> NOT_AVAILABLE
- 整合: build event 帶 entries + release_leg（fixture fills 加 price）

## 4. 之後（Step 2-5）
重建 snap_v6（同授權 inputs）-> dry-run -> authorize replay ->
真結論（Y0..Y3 / 6 deltas / evidence counts; BBO 覆蓋限制如常）
