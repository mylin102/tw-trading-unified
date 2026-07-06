# ETF Regime Engine v1 — Implementation Plan

## Goal

Build a lightweight market regime classifier using ETF relative strength. Output `data/etf_regime.json` from tw-canslim-web daily, consumed by tw-trading-unified to adjust ORB/VWAP thresholds and position sizing.

## Phases

| Phase | What | Who |
|-------|------|-----|
| **1** | tw-canslim-web produces `etf_regime.json` daily | This session |
| 2 | tw-trading-unified reads & caches etf_regime.json | Next |
| 3 | ORB / VWAP threshold adjustment | Next |
| 4 | Position sizing modifier | Future |

---

## Phase 1: Producer (tw-canslim-web)

### New file: `export_etf_regime.py`

Location: `tw-canslim-web/export_etf_regime.py`

### ETF Groups (from spec)

```python
ETF_GROUPS = {
    "market_proxy": ["0050", "006208"],
    "growth": ["00881", "00927"],
    "dividend_defensive": ["0056", "00878", "00919"],
    "small_mid": ["0051", "00733"],
    "inverse": ["00632R"],
    "bond": ["00679B", "00720B"],
}
```

### Features computed daily

All from 5-day returns:

| Feature | Formula |
|---------|---------|
| `market_momentum` | ret_0050_5d |
| `growth_vs_defensive` | avg_ret(growth)_5d - avg_ret(defensive)_5d |
| `small_vs_large` | avg_ret(small_mid)_5d - ret_0050_5d |
| `hedge_demand` | ret_00632R_3d |
| `bond_bid` | avg_ret(bond)_5d |

### Regime Classification

Priority order (first match wins):

1. **RISK_ON**: market_momentum > 0 AND growth_vs_defensive > 0 AND small_vs_large > 0 AND hedge_demand < 0
2. **RISK_OFF**: hedge_demand > 0 OR bond_bid > 0.01
3. **DEFENSIVE**: growth_vs_defensive < 0 AND bond_bid >= 0
4. **CHOP**: default

### Output Schema (`data/etf_regime.json`)

```json
{
  "schema_version": 1,
  "date": "2026-05-04",
  "regime": "RISK_ON",
  "confidence": 0.72,
  "features": {
    "market_momentum": 0.018,
    "growth_vs_defensive": 0.012,
    "small_vs_large": 0.006,
    "hedge_demand": -0.011,
    "bond_bid": -0.003
  }
}
```

Confidence = how many features support the regime decision (simplified: ratio of aligned features to total features).

### Data Source

Reuse existing `yfinance_provider.py` to fetch 5-day OHLC for each ETF symbol. The CANSLIM engine already has `get_price_history_with_policy()` for price data.

### Integration into publish flow

Add `_export_etf_regime()` to `CanslimEngine` class in `export_canslim.py`, called alongside `_export_leaders_json()`.

Register artifact kind in `publish_safety.py`: `"etf_regime.json": "etf_regime"`

---

## Files Changed

| File | Change |
|------|--------|
| `tw-canslim-web/export_canslim.py` | Add `_export_etf_regime()` method, call in publish |
| `tw-canslim-web/export_etf_regime.py` | New — ETF regime classifier logic |
| `tw-canslim-web/publish_safety.py` | Register `etf_regime.json` artifact kind |
| `tw-canslim-web/tests/test_etf_regime.py` | New — tests for regime classification |

## Open Questions

1. Price data source: use existing `yfinance_provider` or Shioaji kbars? YFinance is simpler for 5d returns.
2. Should the exporter also handle missing ETF data gracefully (e.g., 00632R delisted)?
3. Confidence formula: simple ratio of aligned features, or weighted by feature magnitude?
