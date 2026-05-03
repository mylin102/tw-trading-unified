# External Stock Alpha — Pipeline Contract

Defines the `tw-canslim-web` → `tw-trading-unified` data pipeline: producer quality rules, consumer defense filters, and observability. This is the single source of truth for the external alpha integration.

---

## 1. Architecture

```
tw-canslim-web                     tw-trading-unified
  (Producer)                          (Consumer)
  ┌──────────────────┐              ┌──────────────────────┐
  │ export_canslim.py │──leaders.json──▶ external_feature_  │
  │ _export_leaders   │  (GitHub Raw) │ provider.py          │
  │   _json()         │              │ _normalize_snapshot() │
  │                   │              │ get_snapshot()        │
  │ A端: 品質提升      │              │ B端: 防禦性過濾       │
  └──────────────────┘              └──────────────────────┘
                                            │
                                            ▼
                                      config/stocks.yaml
                                      (watchlist)
```

### A端 (Producer) — `tw-canslim-web/export_canslim.py`

負責產出乾淨、可排序的 `leaders.json`。

### B端 (Consumer) — `tw-trading-unified/core/external_feature_provider.py`

負責從 GitHub Raw 抓取、防禦性過濾、排序、快取。

---

## 2. Data Contract — `leaders.json`

### Format

```json
{
  "schema_version": 1,
  "date": "2026-05-03",
  "generated_at": "2026-05-03T07:48:09Z",
  "universe": [
    {
      "symbol": "2330",
      "name": "TSMC",
      "rs_rating": 86,
      "breakout_score": 0.5,
      "volume_score": 0.5,
      "composite_score": 0.791,
      "industry_rank": 1,
      "tags": ["leader", "breakout_candidate"]
    }
  ]
}
```

### Fields

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `symbol` | str | 4-6 digits | Taiwan stock code |
| `name` | str | — | Company name |
| `rs_rating` | int | 0-99 | Relative strength (0 = unknown) |
| `breakout_score` | float | 0.0-1.0 | 1.0 if N (new-high) is true, else 0.5 |
| `volume_score` | float | 0.0-1.0 | 1.0 if S (supply/demand) is true, else 0.5 |
| `composite_score` | float | 0.0-1.0 | **Three-factor blend** (CANSLIM + RS + Revenue) |
| `industry_rank` | int | 1-999 | 1 = strongest industry; 500 = unknown; 999 = none |
| `tags` | list[str] | — | `leader`, `breakout_candidate`, `rev_acc`, `rev_strong`, `verified`, `from_excel` |

### Composite Score Formula (A端, export_canslim.py ~line 462)

```python
rs_weight = min(1.0, max(0.0, rs_rating / 100.0))
blended_score = (
    0.4 * (canslim_score / 100.0)   # CANSLIM基本面
    + 0.3 * rs_weight               # 相對強度 (主要區辨力)
    + 0.3 * (revenue_score / 6.0)   # 營收品質
)
if rs_rating <= 0:
    blended_score *= 0.7  # 無RS折扣
```

預期分布：0.64–0.90（視 rs_rating 而定）。

---

## 3. A端: Quality Rules (`export_canslim.py`)

Applied during `_export_leaders_json()`, before writing to file.

### 3.1 ETF Filter (~line 405)

```python
final_universe_symbols = [s for s in final_universe_symbols if not s.startswith("00")]
```
Excludes 0050, 0056, 00878 and any ETF/warrant-like codes.

### 3.2 Excel Fallback Industry Rank (~line 435)

For stocks in Excel but not yet batch-processed: use `ticker_info` to resolve industry. Default to **500** (neutral), never 999 (which would be filtered by B端).

### 3.3 Score Drift Guard (~line 511)

```python
if cmax - cmin < 0.15:
    logger.warning("[SCORE DRIFT] composite_score range too flat ...")
if cmax > 1.0 or cmin < 0.0:
    logger.error("[SCORE DRIFT] composite_score out of [0,1] range ...")
```

### 3.4 Summary Log (~line 519)

```
Leaders export complete: N leaders, avg_rs=XX.X industry_ranked=N/N composite_range=[X.XXX, X.XXX]
```

---

## 4. B端: Defense Filters (`external_feature_provider.py`)

Applied in `_normalize_snapshot()` when `ranking.json` is empty (current normal state) and data comes from `leaders.json`.

### 4.1 `_is_valid_leader(row)` (~line 113)

Returns `(is_valid: bool, reason: str)`. Three rejection rules:

| Reason | Rule |
|--------|------|
| `etf` | Symbol starts with `00` |
| `rs_zero` | `rs_rating <= 0` |
| `no_industry` | `industry_rank >= 999` |

### 4.2 Drop Stats (~line 195)

```python
drop_stats = {"etf": 0, "rs_zero": 0, "no_industry": 0}
```
Logged in every sync:
```
[ExternalAlpha] leaders filter: before=X after=X removed=X → top X
  | drop: etf=X rs_zero=X no_industry=X | industry_cap=5/per
```

### 4.3 Score Drift Guard (~line 203)

Duplicate of A端 check. Applied to filtered leaders before sorting:
- Range < 0.15 → WARNING
- Out of [0,1] → ERROR

### 4.4 Industry Concentration Cap (~line 215)

```python
MAX_PER_INDUSTRY = 5
```
Limits how many stocks from the same `industry_rank` can enter the watchlist. Prevents single-sector dominance.

### 4.5 Floor Guard (~line 231)

```python
MIN_REQUIRED = 5
if len(sorted_leaders) < MIN_REQUIRED:
    # Fall back to unfiltered sort
```
Prevents "empty universe" scenarios when filters are too aggressive.

### 4.6 Sort Order (~line 240)

```python
key=lambda r: (
    int(r.get("industry_rank") or 999),    # lower = stronger industry
    -float(r.get("rs_rating") or 0),        # higher = stronger stock
    -float(r.get("composite_score") or 0),  # higher = better blend
)
```
Then take `top N` (configurable via `max_watchlist_size`, default 20).

---

## 5. Data Freshness

Three layers of freshness checking:

| Layer | Method | Location |
|-------|--------|----------|
| HTTP header | `curl -sI leaders.json` → `date` header | Manual |
| `generated_at` field | ISO timestamp in payload | `leaders.json` field |
| `age_minutes` | Computed by `apply_snapshot_health()` | `external_feature_provider.py` ~line 202 |

The `get_snapshot()` return value includes `generated_at`, `age_minutes`, and `is_stale` flags for the decision layer.

Quick check:
```bash
curl -s "https://raw.githubusercontent.com/mylin102/tw-canslim-web/master/data/leaders.json" \
| python3 -c "
import json,sys;from datetime import datetime,timezone
d=json.load(sys.stdin)
dt=datetime.fromisoformat(d['generated_at'].replace('Z','+00:00'))
age=(datetime.now(timezone.utc)-dt).total_seconds()/3600
print(f'產出: {d[\"generated_at\"]} | 距今: {age:.1f}h | {\"🟢\" if age<24 else \"🟡\" if age<48 else \"🔴\"}')"
```

---

## 6. Cache Paths

```
cache/external_alpha/latest.json               # Latest successful fetch
cache/external_alpha/leaders_YYYY-MM-DD.json    # Dated snapshot
```

---

## 7. Alpha Usage in Trading (SCOUT vs SCALE)

CANSLIM alpha is **not** used to size the first entry.

| Phase | Sizing Rule | CANSLIM Influence |
|-------|------------|-------------------|
| **SCOUT** (signal validation) | `fixed_scout_cap` | Universe filter only (non-leader = no scout) |
| **SCALE** (conviction add) | `base_size * (0.6*momentum + 0.4*canslim_alpha)` | Scale bias + risk cap |

CANSLIM does three things:
1. **Universe filter** — skip stocks not in leaders.json
2. **Scale bias** — higher composite_score → larger scale size
3. **Risk cap** — higher ranking allows higher single-position limit

Momentum signals determine entry timing. CanSlim determines whether it's worth scaling. Risk engine caps total exposure.

---

## 8. Safety & Failover

- Never block trading on fetch failure (use cache)
- Never treat stale data as hard truth (is_stale → degrade signals)
- Every decision logs its alpha source (cache date, scores)
- External alpha is toggle-able via feature flag (`stocks.external_features.enabled`)

---

## 9. Deprecated Files

| File | Status | Reason |
|------|--------|--------|
| `docs/VAN_FEATURE_INTEGRATION.md` | Deprecated | Superseded by this doc (`api/ranking.json` and `api/stock_features.json` are empty) |
| `docs/ALPHA_TRADING_STRATEGY.md` | Deprecated | Strategy-level content merged into Section 7 above |

These files remain for search compatibility but should not be referenced for new development.
