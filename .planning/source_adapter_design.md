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

## 3. 輸出生態
adapter → normalized snapshot → builder（既有）→ events.json +
manifest → runner --input

## 4. RED matrix 對應
adapt_fill / adapt_spread_event / adapt_bbo / normalize_sources /
join_anchor / join_positions / build_normalized_snapshot —
全 skeletal（NotImplementedError at intended point）
