# Event Snapshot Source Adapter — 設計（research-only, design + RED first）

**狀態**: DESIGN + RED tests only（codex 2026-08-08 — runtime schema 檢查
發現實際來源非 builder-native, 需 adapter; 未接受前不實作/不讀 runtime）
**Scope**: scripts/research/event_snapshot/** + tests/research/**

## 1. 實際 runtime schemas（adapter 要顯式轉換）
- **fills**: `fill_type` / `timestamp` / `leg` / `contract` / `side` /
  `trade_id`
- **spread events**: `event` / `ts` / `trade_id` / `order_id`
  （RELEASE_DECISION / SUBMISSION 為 legal anchor kinds）
- **BBO telemetry**: `event_type=BBO_UPDATE` / `leg` / `contract_code` /
  `exchange_ts_ms` / `receive_ts_ms` / `source` / `bid` / `ask`
  （**source=shioaji_bidask 為唯一 allowlist**）

## 2. Adapter 契約
1. **read-once + hash**: 每來源讀恰好一次; sha256 綁定 parsed bytes
2. **provenance 保留**: source path/hash + record_no + byte_offset +
   **原始 timestamp 文字** + parsed ts/unit（fills=timestamp,
   events=ts, BBO=exchange_ts_ms/receive_ts_ms）
3. **legal anchors**: 只 map same-trade RELEASE_DECISION/SUBMISSION
   （join 該 trade 的 release fills）; 非 legal / ambiguous →
   NOT_AVAILABLE
4. **fills join**: anchor 的 trade 必須 join 全部 required release
   fills + 由此推 **per-leg pre-decision position**（side）
5. **BBO**: 只 source=shioaji_bidask + **exact contract+leg** +
   quote exchange_ts_ms <= decision（no lookahead）; bid/ask 直接取
   **無 last-price/OHLC 啟發式轉換**
6. **拒絕**: ambiguous event/fill join / unsupported raw tick CSV /
   malformed/torn record / no-BBO 期間（typed NOT_AVAILABLE /
   REFUSED, 永不靜默）
7. **輸出**: normalized snapshot（builder-native 格式）+ manifest
   （sources hashes + event_map + provenance）— **no-replace atomic
   writes**（同 builder/runner 政策）


## 5. v2 修訂（critical source review — real runtime shapes）

(1) **JSONL 解析**: 來源是 JSONL（一行一 record, 非單一 JSON list）;
    讀 bytes 一次 → 分行保留**實際 byte offset**（line-start byte
    位置, 非 record index）; torn/malformed line → 整份 REFUSED
(2) **fill_type enum**: {ENTRY, RELEASE, EXIT, COMBINED_EXIT} 已知;
    **ENTRY sides 是 LONG/SHORT**（非 Buy/Sell）; per-leg positions 由
    ENTRY LONG/SHORT 推導 + **qty/side validation**（qty<=0 拒）
(3) **Contract mapping（永不硬編碼）**: fills.contract = NEAR/FAR
    **標籤**; BBO contract_code = 實際碼（TMFH6/TMFI6）— **永不直接
    比較兩者**; mapping 由 authoritative event/order records（entry/
    order contract codes + delivery metadata）或**顯式 versioned
    mapping input**（validity windows）解析, **per decision timestamp**
(4) **Roll 處理（P0）**: TMFH6/TMFI6 near/far 碼在月度結算/roll 後
    變更; 每個 settlement window 一組 mapping; 解析依
    decision_ts 落在哪個 window; roll boundary 上 ambiguous →
    NOT_AVAILABLE（不猜）; **BBO join 用 per-event mapping + 同
    validity window — 無 cross-roll reuse**; 錯 period 的 BBO code
    拒絕
(5) **Manifest**: 記錄 contract mapping evidence / version / hash
    （+ 既有 sources hashes + event_map + provenance）

## 3. 輸出生態
adapter → normalized snapshot → builder（既有）→ events.json +
manifest → runner --input

## 4. RED matrix 對應
adapt_fill / adapt_spread_event / adapt_bbo / normalize_sources /
join_anchor / join_positions / build_normalized_snapshot —
全 skeletal（NotImplementedError at intended point）
