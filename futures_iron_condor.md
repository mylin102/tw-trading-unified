# MXF "Futures Iron Condor" Research Note

## Reality Check

This is **not** a true options Iron Condor.

If we insist on trading only micro index futures (`MXF`) and avoid options, the closest analog is a
**calendar spread / near-far month mean-reversion strategy**:

- short near / long far when the market looks overheated
- long near / short far when the market looks over-sold

The goal is not to earn theta. The goal is to capture **spread normalization** and **basis mean
reversion** while using the far-month leg to reduce outright directional exposure.

That also means the main failure mode is different:

- **range / mean-reverting days**: often favorable
- **strong trend / regime-shift days**: can be very painful

So this document should be treated as a **research + backtest spec**, not a live-trading playbook.

---

## Strategy Thesis

The basic idea:

1. The near month reacts more aggressively to spot and short-term order flow.
2. The far month usually moves in the same direction, but less violently.
3. When price is stretched relative to VWAP **and** the near-far spread is stretched relative to its
   own history, the spread may revert.

This is the part that matters most:

> **Do not trade on VWAP stretch alone.**
>
> A market can stay above VWAP +2 sigma for a long time in a trend day.
> The setup is more credible only when **price is stretched** and **spread is also stretched**.

---

## What This Strategy Is Actually Trading

| Topic | Options Iron Condor | MXF Calendar-Spread Analog |
|---|---|---|
| Profit source | Theta / vol decay | Spread normalization / basis mean reversion |
| Risk cap | Long wings define max loss | Far leg reduces risk but does not hard-cap it like options |
| Main edge | Time decay in range | Near-far mispricing in range |
| Worst regime | Large directional move / vol expansion | Persistent directional trend / spread expansion |
| Fill model | Broker combo possible | Often two independent futures orders unless broker spread product is confirmed |

So calling this "futures Iron Condor" is fine as a shortcut for intuition, but **engineering and risk
should treat it as a calendar spread strategy**.

---

## Market Regime Assumption

This strategy should be treated as a **range-day-only** strategy.

It should be disabled or heavily filtered when:

- VWAP slope is strongly one-directional
- ATR is expanding rapidly
- opening drive is directional and persistent
- major macro event / settlement / expiration distortion is present
- spread behavior is abnormal or illiquid

If the day is trend-dominant, the spread can keep widening and the far leg may not protect enough.

---

## Signal Framework

Use two layers together:

1. **VWAP deviation on the near month**
2. **Spread Bollinger / z-score on near - far**

### Core Variables

- `near_close`: near-month close
- `far_close`: far-month close
- `spread = near_close - far_close`
- `vwap`: near-month VWAP
- `vwap_std`: rolling std of near-month deviation from VWAP
- `vwap_z = (near_close - vwap) / vwap_std`
- `spread_ma`: rolling mean of spread
- `spread_std`: rolling std of spread
- `spread_z = (spread - spread_ma) / spread_std`

### Entry Bias

**Fade overheated upside**

- market price is stretched high: `vwap_z >= entry_vwap_z`
- spread is also stretched high: `spread_z >= entry_spread_z`
- regime filter still says "range / mean reversion allowed"

Action:

- sell near month
- buy far month

**Fade over-sold downside**

- market price is stretched low: `vwap_z <= -entry_vwap_z`
- spread is also stretched low: `spread_z <= -entry_spread_z`
- regime filter still says "range / mean reversion allowed"

Action:

- buy near month
- sell far month

### Recommended Starting Thresholds for Backtest

These are starting points, not production defaults:

- `entry_vwap_z = 2.0`
- `entry_spread_z = 2.0`
- `take_profit_vwap_z = 0.5`
- `max_holding_bars = 6` to `12` on 5m bars

Do not optimize these aggressively before the stop-loss framework is stable.

---

## Stop-Loss Framework

This strategy lives or dies on stop rules.

Focus on **loss containment first**, not win rate.

### 1. Spread Stop

After entry, if spread keeps moving against the position beyond a defined threshold, exit both legs.

Suggested starting point for backtest:

- stop when adverse spread move exceeds `1.0 x spread_ATR`

Where `spread_ATR` is ATR-like movement measured on the spread series itself, not just the near contract.

### 2. Time Stop

This is a mean-reversion trade. If mean reversion does not begin quickly, edge is probably weak.

Suggested starting point:

- if no meaningful reversion within `30-60 minutes` or `N bars`, flatten both legs

### 3. Regime-Break Stop

If the market transitions from "stretch" to "trend continuation", exit immediately.

Examples:

- VWAP slope steepens further in trade direction
- ATR expands beyond a threshold
- spread keeps widening while price also continues trend

### 4. Session / Event Stop

Disable or flatten before:

- monthly settlement risk windows
- low-liquidity session edges
- known major event releases

---

## Why VWAP Alone Is Not Enough

VWAP helps, but it does **not** solve trend-day risk by itself.

VWAP improves:

- entry timing
- overextension detection
- structured exits

VWAP does **not** guarantee:

- fast reversion
- bounded drawdown
- spread normalization during a regime shift

That is why the spread filter is mandatory.

The better framing is:

> VWAP answers: "Is price stretched?"
>
> Spread filter answers: "Is near-month richness/cheapness also stretched?"

You want **both**.

---

## Data and Backtest Requirements

Before any live design, the strategy should pass a proper backtest with at least:

1. **Near-month and far-month aligned bars**
   - same timestamps
   - same session handling
   - same missing-bar policy

2. **Spread-aware transaction model**
   - fees for both legs
   - slippage for both legs
   - realistic entry/exit delay

3. **Regime split analysis**
   - range days
   - trend days
   - day session vs night session
   - settlement-adjacent days

4. **Risk-first metrics**
   - max drawdown
   - worst day
   - worst trade
   - consecutive losses
   - time-in-trade

5. **No look-ahead bias**
   - rolling VWAP std / spread stats must use only past data

---

## Minimum Research Questions

Before implementation, answer these:

1. How often does spread revert after both `VWAP_z` and `spread_z` exceed thresholds?
2. How long does mean reversion usually take?
3. What does the loss distribution look like on strong trend days?
4. Does the strategy survive after double-leg fees and slippage?
5. Is night session behavior materially worse than day session?
6. Does settlement week distort spread behavior enough to require a hard block?

If these are not answered, this strategy is not ready.

---

## Engineering Reality in This Repo

The current futures system is built mainly around **single-contract logic**.

To trade this strategy safely, the codebase would need dedicated support for:

1. near/far contract discovery and rollover
2. dual-leg quote subscriptions
3. spread position state
4. spread PnL and cost accounting
5. two-leg order submission and recovery
6. leg-risk handling when one side fills and the other does not
7. settlement-day and liquidity guards

So even if the idea is valid, it is **not** a small config-only change.

---

## Live Trading Caveat

Do **not** assume:

- two `place_order()` calls == safe combo execution
- far-month leg fully caps risk
- broker margin discount exists without confirmation
- a profitable manual experience automatically generalizes

If the broker does not provide atomic spread execution for the target product, you still have **leg risk**.

That makes this strategy much more dangerous in fast markets than the document title suggests.

---

## Critical Live-Execution Constraints

These points must be designed explicitly before any live rollout.

### 1. Async Placement and Leg Recovery

Using `api.place_order()` twice means the two legs are submitted **independently**.

In a fast market:

- near month may fill
- far month may slip or remain unfilled
- the account can be left holding a dangerous one-sided futures position

So any live version must include **Leg Recovery** logic.

Minimum requirement:

1. submit both legs
2. monitor fill state for both legs immediately
3. if the hedge leg is still unfilled after a short timeout window (for example `5 seconds`):
   - either market-chase the missing leg, or
   - flatten the filled leg immediately

The system must never tolerate "temporary single-leg exposure" as a normal state.

### 2. Margin Discount Is Not Instant Funding

Calendar-spread margin discount may exist, but that does **not** mean the second leg is always safe to
submit.

Practical constraint:

- both legs must be in the same account
- the broker may only release excess margin **after both legs are filled**

So pre-trade checks must assume the worst-case entry path:

> The account may need enough available margin to survive **two independent initial positions**
> before any spread discount is released.

If the account cannot support that, the protective leg may be rejected and the strategy becomes a naked
directional futures trade.

### 3. VWAP Reset and Night-Session Filter

For Taiwan index futures, VWAP treatment matters:

- full-day VWAP and split-session VWAP are not equivalent
- night session often behaves differently from day session
- the first part of a new session often has unstable VWAP statistics

Research default should be:

1. compute VWAP separately for day and night sessions
2. avoid trusting `VWAP_z` immediately after a session opens
3. add a time filter near session open

Suggested starting rule:

- block new entries during the first `30 minutes` of a fresh day/night session

Without this, the Z-score can look precise while the VWAP anchor is still immature.

---

## Clean Research Prototype

The code below is a **research prototype** for signal generation only.
It is not production execution code, does not handle execution, and does not solve leg recovery.

```python
import pandas as pd
import numpy as np


def build_calendar_spread_frame(
    df_near: pd.DataFrame,
    df_far: pd.DataFrame,
    window: int = 20,
) -> pd.DataFrame:
    """
    Research helper only.

    Required columns:
      df_near: datetime, close, vwap, vwap_std
      df_far: datetime, close

    Notes:
      - Sort before rolling windows
      - Inner-join to avoid misaligned bars
      - Signal output is only a candidate signal; production logic still needs
        regime filters, liquidity guards, session filters, and execution controls
    """
    df_near = df_near.sort_values("datetime").copy()
    df_far = df_far.sort_values("datetime").copy()

    df = pd.merge(
        df_near[["datetime", "close", "vwap", "vwap_std"]],
        df_far[["datetime", "close"]],
        on="datetime",
        suffixes=("_near", "_far"),
        how="inner",
    )

    df["spread"] = df["close_near"] - df["close_far"]
    df["spread_ma"] = df["spread"].rolling(window=window, min_periods=window).mean()
    df["spread_std"] = df["spread"].rolling(window=window, min_periods=window).std()

    safe_vwap_std = df["vwap_std"].replace(0, np.nan)
    safe_spread_std = df["spread_std"].replace(0, np.nan)

    df["vwap_z"] = (df["close_near"] - df["vwap"]) / safe_vwap_std
    df["spread_z"] = (df["spread"] - df["spread_ma"]) / safe_spread_std

    conditions = [
        (df["vwap_z"] >= 2.0) & (df["spread_z"] >= 2.0),
        (df["vwap_z"] <= -2.0) & (df["spread_z"] <= -2.0),
    ]
    choices = [-1, 1]
    df["signal"] = np.select(conditions, choices, default=0)

    return df


def add_risk_metrics(df: pd.DataFrame, atr_window: int = 14) -> pd.DataFrame:
    """
    Research proxy for spread-volatility stop design.

    This is not a textbook ATR because the spread series does not have OHLC bars here.
    It is a rolling mean of absolute spread changes, useful for early stop modeling.
    """
    result = df.copy()
    result["spread_diff"] = result["spread"].diff().abs()
    result["spread_atr_proxy"] = result["spread_diff"].rolling(
        window=atr_window,
        min_periods=atr_window,
    ).mean()
    return result
```

---

## Suggested Next Step

The correct next step is:

1. implement this as a **backtest-only research strategy**
2. verify stop-loss robustness on trend days
3. verify whether the edge survives fees/slippage
4. only then discuss live execution design

If backtest cannot control drawdown on trend days, stop there.
