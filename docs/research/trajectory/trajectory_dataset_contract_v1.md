# Trajectory Dataset Contract v1 (軌跡數據集契約)

---

## 1. Schema Specifications (欄位規格)
軌跡重播數據集由一系列強型別的交易與市場事件序列組成。在儲存上，事件記錄可採用 JSON Lines (JSONL) 或 Parquet 格式。每個事件必須完全包含以下欄位：

| 欄位名稱 (Field) | 資料型態 (Type) | 說明 |
| :--- | :--- | :--- |
| `event_id` | `VARCHAR(36)` | 唯一事件 UUID v4。 |
| `event_type` | `VARCHAR(32)` | 事件類別（如 `MARKET_TICK`, `BROKER_ACK`, `LIFECYCLE_TRANSITION`）。 |
| `event_time` | `BIGINT` | 事件發生的納秒時間戳（UTC 時代以來的 Unix 納秒數）。 |
| `receive_time` | `BIGINT` | 系統接收並記錄該事件的納秒時間戳。 |
| `source` | `VARCHAR(32)` | 事件產生來源（例如 `exchange`, `shioaji_broker`, `strategy_router`）。 |
| `source_sequence` | `BIGINT` | 來源端之單調遞增序號（用於解決同 timestamp 排序）。若來源不支援則為 `0`。 |
| `trade_id` | `VARCHAR(32)` | 關聯之交易 ID (若有)，無關聯填 `null`。 |
| `session_id` | `VARCHAR(32)` | 交易交易日 Session ID (格式：`s-YYYYMMDD-[day|night]`)。 |
| `origin` | `VARCHAR(16)` | `OBSERVED`, `DERIVED`, `RECONSTRUCTED`, `COUNTERFACTUAL`。 |
| `authority` | `VARCHAR(20)` | `EXCHANGE`, `BROKER`, `PRODUCTION_ENGINE`, `REPLAY_ENGINE`。 |
| `causality` | `VARCHAR(12)` | `EXOGENOUS`, `ENDOGENOUS`。 |
| `mutability` | `VARCHAR(12)` | `IMMUTABLE`, `REPLACEABLE`。 |
| `payload_schema_version` | `VARCHAR(10)` | 承載資料（Payload）結構版本，預設為 `v1.0.0`。 |
| `payload` | `JSON` | 依據 `event_type` 變動的事件承載詳細資料（如 Tick 價格、Fills 數量等）。 |
| `quality_flags` | `INTEGER` | 資料品質標籤（用二進位 Bitmask 表示，如 `0` 代表正常，`1` 代表延遲，`2` 代表重複，`4` 代表重填）。 |

---

## 2. Event Payload Contracts (事件 Payload 承載契約)

### `MARKET_TICK` Payload
```json
{
  "symbol": "TMF_NEAR",
  "bid_price": 14250,
  "bid_qty": 5,
  "ask_price": 14252,
  "ask_qty": 3,
  "last_price": 14251,
  "last_qty": 1,
  "volume": 28452
}
```

### `BROKER_FILL` Payload
```json
{
  "order_id": "ord-20260717-0915",
  "symbol": "TMF_NEAR",
  "side": "BUY",
  "price": 14250.0,
  "quantity": 1,
  "fill_id": "fill-948274",
  "fee": 20.0,
  "tax": 10.0
}
```

### `LIFECYCLE_TRANSITION` Payload
```json
{
  "strategy_name": "TMF_Calendar_Spread",
  "state_before": "IDLE",
  "state_after": "ARMED",
  "reason": "Z-score crossed trigger threshold",
  "context_snapshot": {
    "zscore": 2.14,
    "position": 0
  }
}
```

---

## 3. Data Integrity & Validation Gates (資料完整性與品質關卡)
當軌跡數據集載入時，必須通過以下嚴格驗證，否則拒絕重播：

1. **唯一性校驗 (Identity Check)**：
   * 所有事件的 `event_id` 不得有任何重複。
2. **時間單調性校驗 (Time Monotonicity Check)**：
   * 排除 late-arriving 事件，依 `event_time` 排序後，整體時間軸不得出現逆流。
3. **無外生權威偽造 (No Exogenous Authority Forgery)**：
   * 所有 `Causality == EXOGENOUS` 的事件，其 `authority` 必須為 `EXCHANGE` 或 `PRODUCTION_ENGINE`。
4. **邊界完備性校驗 (Boundary Completeness Check)**：
   * 當存在 `SYSTEM_RESTART` 或 `BROKER_DISCONNECT` 等基礎建設事件時，必須包含明確的 `STATE_RESTORED` 與狀態重新對齊回報。
