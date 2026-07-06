
# KBAR Feature-Based Auto Trading Strategy

## Strategy concept
This strategy treats your exported KBAR table as a **feature layer** that has already encoded:
- trend alignment (`bull_align`, `bear_align`, `bullish_align`, `bearish_align`)
- momentum (`macd_hist`, `macd_line`, `macd_rising`, `momentum`, `mom_state`, `mom_velo`)
- intraday structure (`day_open`, `day_max`, `day_min`, `recent_high`, `recent_low`, `session`)
- volatility / squeeze (`atr`, `bb_lower`, `bb_upper`, `sqz_on`)
- participation (`volume`, `volume_spike`, `vwap`, `price_vs_vwap`)
- environment / regime (`regime`, `adx`, `ema_200_up`, `score`)

So the best way to use these fields is **not** to let any single feature fire a trade by itself.
Instead, split them into 4 layers:

1. **Regime filter**: decide whether trading is allowed.
2. **Direction filter**: only long or only short.
3. **Trigger**: the actual entry event.
4. **Risk/exit**: stop, target, trailing, and no-trade conditions.

---

## Recommended first strategy: pullback continuation + breakout confirmation

This is a good first production strategy because it is:
- easy to explain
- compatible with your current feature set
- less noisy than pure breakout chasing
- easy to extend later into direction-aware cross-regime logic

### Long setup
Trade only when the market is already constructive, then wait for pullback recovery and momentum re-expansion.

#### 1) Long regime filter
Allow longs only when:
- `ema_200_up == True`
- `bull_align == True`
- `bullish_align == True`
- `regime in ['NORMAL', 'STRONG']`
- `adx >= 18`

#### 2) Long pullback condition
Need evidence of a controlled pullback, not random chop:
- `in_bull_pb_zone == True` **or** `close <= ema_fast + 0.25 * atr`
- `price_vs_vwap > -0.003`
- `close >= vwap`

#### 3) Long trigger
Enter only when momentum turns back up:
- `macd_rising == True`
- `macd_hist > 0`
- `mom_velo > 0`
- `close > open`
- one of:
  - `is_new_high == True`
  - `close > recent_high`
  - `breakout_strength >= 1`

#### 4) Long quality score
Use a score gate to avoid weak setups:
- base requirement: `score >= 20`
- stronger mode: `score >= 40`

---

### Short setup
Symmetric to long, but use it more conservatively because short-side intraday behavior is often faster and noisier.

#### 1) Short regime filter
Allow shorts only when:
- `ema_200_up == False`
- `bear_align == True`
- `bearish_align == True`
- `regime in ['WEAK', 'NORMAL']`
- `adx >= 18`

#### 2) Short pullback condition
- `in_bear_pb_zone == True` **or** `close >= ema_fast - 0.25 * atr`
- `price_vs_vwap < 0.003`
- `close <= vwap`

#### 3) Short trigger
- `macd_rising == False`
- `macd_hist < 0`
- `mom_velo < 0`
- `close < open`
- one of:
  - `is_new_low == True`
  - `close < recent_low`
  - `breakout_strength <= -1`

#### 4) Short quality score
- base requirement: `score <= -20`
- stronger mode: `score <= -40`

---

## Entry timing
Do **not** trade every bar.
Use the features to suppress low-quality entries.

### Suggested entry timing rules
- Skip session open noise: avoid first 2-3 bars unless breakout strength is very high.
- Avoid dead tape: no trade if `adx < 15`.
- Avoid squeeze drift unless breakout confirms:
  - if `sqz_on == True`, require either `abs(macd_hist)` expansion or `is_new_high / is_new_low`.
- Avoid fading VWAP too aggressively:
  - long must be near or above VWAP
  - short must be near or below VWAP

---

## Exit rules
Do not rely on one exit only.
Use layered exits.

### Hard stop
- Long: `entry_price - 1.2 * atr`
- Short: `entry_price + 1.2 * atr`

### Initial target
- Long: `entry_price + 2.0 * atr`
- Short: `entry_price - 2.0 * atr`

### Trailing logic
After unrealized PnL reaches `+1.0 * atr`:
- move stop to breakeven or `entry ± 0.2 * atr`

After unrealized PnL reaches `+1.5 * atr`:
- trail by `1.0 * atr`
- or exit if `macd_hist` reverses for 2 consecutive bars

### Momentum failure exit
Exit early if:
- long position and `macd_hist < 0` while `close < vwap`
- short position and `macd_hist > 0` while `close > vwap`

### Time stop
- if trade does not move at least `0.5 * atr` in your favor within 3-5 bars, reduce or exit
- for intraday systems, force flat near session close

---

## Position sizing
Use ATR-based sizing, not fixed share/contract size.

Position size formula:

`size = risk_budget / stop_distance`

For example:
- account risk per trade = 0.5% of equity
- stop distance = `1.2 * atr`
- contracts = `floor(risk_budget / stop_distance)`

This keeps high-volatility bars from getting oversized.

---

## Practical mapping of your columns

### Best columns for regime filter
- `ema_200_up`
- `bull_align`, `bear_align`
- `bullish_align`, `bearish_align`
- `regime`
- `adx`

### Best columns for trigger
- `macd_hist`
- `macd_rising`
- `mom_velo`
- `momentum`
- `is_new_high`, `is_new_low`
- `breakout_strength`

### Best columns for structure / location
- `vwap`
- `price_vs_vwap`
- `recent_high`, `recent_low`
- `day_open`, `day_max`, `day_min`
- `in_bull_pb_zone`, `in_bear_pb_zone`

### Best columns for risk
- `atr`
- `bb_upper`, `bb_lower`
- `sqz_on`

### Best columns for ranking / throttling
- `score`
- `volume_spike`
- `session`

---

## Important issue found in your sample
In the uploaded sample:
- `volume_spike` is always `1`
- `breakout_strength` is always `0`
- `trend_strength_raw` is always `0`
- almost all rows are `WEAK` + `bear_align=True` + `bearish_align=True`

That means in this sample slice, those fields are **not adding much discrimination**.
So in live strategy design:
- keep them in the code
- but do not rely on them as primary triggers until you verify they vary meaningfully over a larger dataset

---

## Recommended first production version
If you want only one version to deploy first, use this:

### v1: short-side continuation strategy
Because your sample is mostly bearish, the cleanest first version is:
- trade only shorts
- require `bear_align and bearish_align`
- require `close <= vwap`
- require `macd_hist < 0`
- require `mom_velo < 0`
- trigger on `close < recent_low` or `is_new_low`
- stop = `1.2 * atr`
- target = `2.0 * atr`
- early exit if price reclaims VWAP and MACD turns up

This is simpler, easier to debug, and matches the structure seen in your current feature export.

---

## Next step after v1
After validating logs and fills, split into 3 separate strategies:

1. `trend_pullback_long`
2. `trend_pullback_short`
3. `squeeze_breakout_expansion`

Do **not** cram everything into one mega-rule at the beginning.

