# Shioaji Futures Contract Handling (Near / Far Month)

## Overview

When using Shioaji for futures trading, symbols like:

TMFR1

are NOT fixed contracts.

They are rolling continuous contracts.

---

## Key Concept

TMFR1 = current front-month contract (auto-roll)

### This means:

- It changes every month
- It is NOT a stable instrument
- It should NOT be used for spread strategies

---

## Problem in Calendar Spread

If you do:

near = TMFR1  
far  = TMFR2

You may encounter:

- sudden spread jumps  
- rollover distortion  
- incorrect PnL  
- invalid backtest signals  

---

## Correct Approach

Use actual monthly contracts, e.g.:

TMF202406  
TMF202407  
TMF202408  

---

## Implementation (Python)

### Step 1: Get contracts

contracts = api.Contracts.Futures.TMF

### Step 2: Filter valid contracts

valid = [c for c in contracts if hasattr(c, "delivery_date") and c.delivery_date]

### Step 3: Sort by expiry

valid = sorted(valid, key=lambda c: c.delivery_date)

### Step 4: Select near / far

near = valid[0]  
far  = valid[1]

---

## Helper Function

def get_near_far_contracts(api, product="TMF"):
    contracts = getattr(api.Contracts.Futures, product)

    valid = [c for c in contracts if getattr(c, "delivery_date", None)]
    valid = sorted(valid, key=lambda c: c.delivery_date)

    if len(valid) < 2:
        raise ValueError("Not enough contracts")

    return valid[0], valid[1]

---

## Rollover Handling (Important)

Near expiry, liquidity shifts.

Add rule:

if (near.delivery_date - today).days < 3:
    near = valid[1]
    far  = valid[2]

---

## Best Practice

### Execution Layer
- Always use real contracts (e.g., TMF202406)
- Never use R1 for trading logic

### Data Layer
- Update near/far mapping daily
- Keep stable during session

### Strategy Layer
Provide:

near_close  
far_close  
spread  
spread_z  

---

## Final Rule

R1 is for viewing, not for trading.

Using correct contracts is critical for:

- spread stability  
- accurate signals  
- correct PnL  
