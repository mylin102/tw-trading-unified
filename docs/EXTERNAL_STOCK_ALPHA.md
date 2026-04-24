# External Stock Alpha Integration

**tw-canslim-web → tw-trading-unified** — Data contract and integration rules for daily high-alpha stock leaders.

---

## 1. Purpose

Establish a stable, version-controlled data exchange mechanism:

- **tw-canslim-web** → Daily Alpha Provider
- **tw-trading-unified** → Execution Consumer

Design constraints:
- Schema changes must not crash the consumer
- Network / GitHub availability must not block trading
- All data flow must be traceable and debuggable

---

## 2. Architecture

```
tw-canslim-web
  └─ GitHub Actions (daily batch)
       └─ data/leaders.json
            │
            ▼ (GitHub Raw)
tw-trading-unified
  └─ external_alpha_provider
       ├─ fetch from GitHub Raw
       ├─ store in local cache
       └─ consumed by decision layer
```

---

## 3. Data Format (Contract)

### 3.1 `leaders.json`

```json
{
  "schema_version": 1,
  "date": "2026-04-19",
  "generated_at": "2026-04-19T06:30:00+08:00",
  "universe": [
    {
      "symbol": "2330",
      "name": "TSMC",
      "rs_rating": 92,
      "i_rating": 88,
      "breakout_score": 0.81,
      "volume_score": 0.73,
      "composite_score": 0.87,
      "industry_rank": 4,
      "tags": ["leader", "breakout_candidate"]
    }
  ]
}
```

### 3.2 Field Definitions

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | int | Data contract version |
| `date` | str | YYYY-MM-DD |
| `generated_at` | str | ISO 8601 timestamp |
| `symbol` | str | Taiwan stock ticker (4-6 digits) |
| `name` | str | Company name |
| `rs_rating` | int | Relative strength rating (0-99) |
| `i_rating` | int | Institutional sponsorship rating (0-99) |
| `breakout_score` | float | Breakout probability (0.0-1.0) |
| `volume_score` | float | Volume confirmation score (0.0-1.0) |
| `composite_score` | float | Composite alpha score (0.0-1.0) |
| `industry_rank` | int | Industry group rank |
| `tags` | list[str] | Strategy classification tags |

---

## 4. Update Rules

### Producer (tw-canslim-web)

- Run once daily (post-close or pre-market)
- Overwrite `data/leaders.json` atomically
- Maintain backward compatibility on schema changes
- Bump `schema_version` on any non-backward-compatible change

### Consumer (tw-trading-unified)

Startup / refresh sequence:
1. Attempt to download from GitHub Raw
2. On success → write to local cache
3. On failure → use existing cache (never abort trading)
4. Cache TTL: one trading day

Cache paths:
```
cache/external_alpha/latest.json
cache/external_alpha/leaders_YYYY-MM-DD.json
```

---

## 5. Decision Layer Integration

### 5.1 Universe Filter

```python
if symbol not in leaders_universe:
    skip_trade()
```

### 5.2 Edge Modifier

```python
edge += leader_bias.get(symbol, 0.0)
```

### 5.3 Position Sizing Boost

```python
position_size *= (1.0 + composite_score * 0.2)
```

---

## 6. Safety & Fault Tolerance

### 6.1 Mandatory Rules

- Never depend on real-time GitHub availability for trade decisions
- Always read from local cache; treat fetch as an async refresh
- JSON parsing failure → fallback to previous cache
- Missing fields → use safe default values

### 6.2 Prohibited Patterns

```python
# ❌ NOT ALLOWED: raw HTTP response into decision path
data = requests.get("https://raw.githubusercontent.com/...").json()
```

---

## 7. Schema Evolution

### Backward Compatibility

```python
if "breakout_score" not in row:
    row["breakout_score"] = 0.5  # default
```

### Version Gate

```python
if schema_version > SUPPORTED_VERSION:
    log_warning("Schema version %d exceeds supported %d — degrading", schema_version, SUPPORTED_VERSION)
    degrade_mode()
```

---

## 8. Update Frequency

| Layer | Frequency |
|-------|-----------|
| Canslim output | Daily (post-close) |
| Trading fetch | On startup + once per session |
| Cache read | Real-time (every decision tick) |

---

## 9. Testing Requirements

All integration tests must cover:
- JSON schema validation (required vs optional fields)
- Missing field tolerance (default value fallback)
- Network failure fallback (cache hit on fetch failure)
- Duplicate symbol handling (dedup by symbol, last-wins)
- Stale cache behavior (TTL exceeded, graceful degrade)

---

## 10. Design Principles

1. **External Alpha = Soft Signal** — never replaces core decision logic
2. **Non-blocking** — fetch failure must not affect trade execution
3. **Traceable** — every decision must log its alpha source (cache date, scores)
4. **Toggle-able** — external alpha must be disable-able via feature flag

---

## 11. Future Extensions

Additional data feeds can be added via the same pattern:
- `breakout_candidates.json` — pre-screened breakout setups
- `industry_rank.json` — sector rotation signals
- `market_breadth.json` — overall market health

Each new feed must:
- Have its own independent schema (with `schema_version`)
- Not break existing consumers
- Support the same cache-and-fallback pattern

---

## 12. Key Takeaway

**Canslim provides the daily alpha distribution; Trading handles real-time execution.** The two systems are decoupled through a stable data contract with versioned schema, local caching, and graceful degradation on failure.
