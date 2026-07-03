# MTS Spread ATR Source of Truth

**Last updated**: 2026-06-28

## The Question

> What ATR does the TMF/MTR spread strategy use for stop/trail calculations?

## One-Sentence Answer

> Spread stop/trail uses the **`atr` column on the 5-minute futures bar**, which is
> computed by `calculate_futures_squeeze()` from `strategies/futures/squeeze_futures/engine/indicators.py`,
> using True Range rolling(14), defaulting to `atr_60` (or `atr_20` when <60 bars exist).

## Full Data Trace

```
calculate_futures_squeeze(df)
  │
  ├── _true_range(df)                       → high-low, high-prev_close, low-prev_close
  ├── .rolling(window=kc_length).mean()     → kc_length = 14 (default)
  │
  ├── res["atr_5"]  = calculate_atr(df, length=5)
  ├── res["atr_10"] = calculate_atr(df, length=10)
  ├── res["atr_20"] = calculate_atr(df, length=20)
  ├── res["atr_60"] = calculate_atr(df, length=60)
  │
  └── res["atr"]   = res["atr_60"] if len(df) >= 60
                                         else res["atr_20"]
                                        │
                                        ▼
                        df_5m.iloc[-1].get("atr")            [monitor.py:5120]
                                        │
                        last_5m.to_dict()["atr"]             [monitor.py:5185]
                                        │
                        StrategyContext.market.last_bar["atr"] [monitor.py:2654]
                                        │
                        tmf_spread._get_thresholds(bar)
                          atr = bar.get("atr")               [tmf_spread.py:408]
                                        │
                          stop  = atr * _atr_mult_stop       (default 1.5)
                          trail = atr * trail_mult           (default 2.0)
                                        │
                          floor: max(10.0, stop), max(20.0, trail)  [tmf_spread.py:434]
```

## Key Detail: ATR Duration Switch

```python
# indicators.py:241
res["atr"] = res["atr_60"] if len(df) >= 60 else res["atr_20"]
```

| Condition | ATR used | Typical TMF value |
|---|---|---|
| < 60 bars in df (~5h trading) | ATR(20) | ~30-50 points |
| >= 60 bars | ATR(60) | ~40-80 points |

After 60 bars (about 5 trading hours), the stop/trail distance silently widens
because the ATR source switches from 20-bar to 60-bar lookback.

**Risk**: TMF 5m ATR(20) vs ATR(60) differs by ~40%. A stop tuned for one
regime behaves differently in the other.

## Mismatch Risk: Single-Leg ATR vs Spread

```
ATR   = single-leg futures price volatility   (TMF index)
Spread = near-far month price differential    (calendar spread)
```

**These are NOT the same distribution.** A calendar spread typically has
lower volatility than either leg individually because the legs are correlated.

| Metric | Single-leg TMF ATR(60) | Spread ATR (estimated) |
|---|---|---|
| Typical 5m range | ~40-80 points | ~10-25 points (estimated) |
| Stop = atr * 1.5 | 60-120 points | needs calibration |
| Trail = atr * 2.0 | 80-160 points | needs calibration |

**If** spread volatility is lower, applying single-leg ATR multipliers results
in stops/trails that are too wide for the spread strategy. The strategy appears
"stable" because stops rarely trigger, but it's actually under-managing risk.

## tmf_spread ATR Fallback Chain

```python
# tmf_spread.py:408-434
def _get_thresholds(self, bar: dict) -> tuple[float, float]:
    atr = bar.get("atr")                    # 1. try current bar's atr
    if atr and not pd.isna(atr) and atr > 0:
        self._last_atr = atr                # 2. cache for next bar
    else:
        atr = self._last_atr                # 3. fallback to cached

    if atr and not pd.isna(atr) and atr > 0:
        stop  = atr * self._atr_mult_stop   # default 1.5
        trail = atr * trail_mult            # default 2.0
        return max(10.0, stop), max(20.0, trail)

    # No ATR available → fixed fallback
    return self._release_stop_fixed, self._trail_dist_fixed
```

| Condition | Behaviour |
|---|---|
| bar has valid `atr` | Use ATR-based stop/trail |
| bar missing `atr` + `_last_atr` exists | Use cached ATR |
| No ATR ever (warm-up, missing indicator) | Fixed fallback (`release_stop_points`, `trail_distance_points`) |
| L409: `pd.isna(atr)` explicitly handled | NaN from divide-by-zero in indicator calc |

## ATR also serialized to state file

```python
# tmf_spread.py:293
"atr": round(atr, 2) if atr else existing.get("atr"),
```

The MTS state JSON carries the ATR value used at the time of each update.
This is for dashboard display and post-session audit.

## Suggested Audit Payload

If adding telemetry to `_get_thresholds()`:

```json
{
  "atr_source": "futures_squeeze_5m_bar.atr",
  "atr_value": 32.5,
  "atr_window": "rolling_tr_14",
  "atr_selected": "atr_60",
  "atr_bars_in_df": 120,
  "stop": 48.75,
  "trail": 65.0
}
```

## Future: Spread-Specific Vol

A dedicated spread ATR would be computed from the spread price series:

```python
spread_price = near_close - far_close
spread_tr = true_range(spread_price)
spread_atr = spread_tr.rolling(14).mean()
```

Configurable via:

```yaml
tmf_spread:
  threshold_mode: "spread_vol"   # legacy_atr | spread_vol | fixed
  spread_atr:
    length: 14
    mult_stop: 1.5
    mult_trail: 2.0
```

This is NOT yet implemented.
