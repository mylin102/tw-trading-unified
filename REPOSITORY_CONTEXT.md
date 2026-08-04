# tw-trading-unified-git — Repository Context for Codex CLI

**Last Updated:** 2026-08-04 (afternoon — on_tick correction + runtime evidence)

---

# Purpose

This repository implements a production futures calendar-spread trading system.

The system has two distinct goals:

1. Production trading (paper/live)
2. Research / counterfactual analysis

Those two paths MUST remain isolated.

---

# High Level Architecture

Broker (Shioaji)
        │
        ▼
Quote callback (production)
        │
        ▼
Global callback adapter (main.py:123 on_tick)
        │
        ▼
Monitor (f_mon.on_tick — monitor.py:1686)
        │
        ├──────── Strategy Runtime
        │
        ├──────── Dashboard state
        │
        ├──────── Policy J
        │
        ├──────── Release logic
        │
        ├──────── Renko
        │
        │
        └──────── Shadow collectors
                  (Model C / Shadow F)

Only the strategy runtime may submit orders.

Shadow collectors are STRICTLY READ ONLY.

---

# Current Major Components

## Strategy

tmf_spread.py

Contains:

- entry logic
- release logic
- Policy J
- combined exit
- lifecycle
- PnL calculation

This file is production-critical.

---

## Policy J

Purpose:

Protect combined PnL using

activation
+
peak tracking
+
giveback

Current production:

activation:
200 TWD

giveback:
50 TWD

Policy J must NEVER mutate execution unless explicitly enabled.

**2026-08-04 fix (VERIFIED):** entry gate used nonexistent attributes
(`_near_entry_avg` / `_near_open_qty`) plus a bare `"SPREAD"` phase string
while the runtime passes `PositionPhase.SPREAD` — this suppressed Policy J
for ALL trades (`ENTRY_NOT_SETTLED`, 164k events). Fixed to real attrs
(`_near_entry` / `_far_entry`) and suffix phase match. Verified via a real
COMBINED_EXIT trigger (trade mts-auto-120709-656, +340 TWD).

---

## Release Logic

Release converts

Spread Position

↓

Single Leg

↓

Trailing Exit

Current production release threshold:

max(
    release_stop_points,
    ATR × atr_multiplier_stop
)

Current config:

release_stop_points = 88

atr_multiplier_stop = 0.6

---

## Combined Exit

Atomic exit of both legs.

Must NOT leave naked exposure.

Used by:

- Policy J
- Emergency exit
- Manual close

---

## Model C

Purpose:

Executable BBO evaluation.

Uses:

Near:

- Ask when buying
- Bid when selling

Far:

same rule

Model C is NOT allowed to submit orders.

---

## Shadow F

Counterfactual strategy.

Purpose:

Compare

Current Release

vs

Immediate Atomic Combined Exit

Shadow only.

Execution influence:

FALSE

---

# Runtime Invariants

These invariants MUST NEVER be violated.

## 1.

Shadow collectors

MUST NOT

- submit orders
- mutate lifecycle
- change controller winner
- modify pending order
- suppress strategy logic

---

## 2.

Exactly-once outcome.

Outcome identity:

(trade_id,
 position_generation,
 entry_order_ids)

NOT

settlement timestamp.

---

## 3.

Production truth

Production runtime is source of truth.

Shadow must TAP.

Shadow must NOT reconstruct production decision.

---

## 4.

Atomic spread exits

Combined exit must exit

Near

AND

Far

together.

Never intentionally leave naked exposure.

---

## 5.

One controller owns execution.

Shadow controllers never participate.

---

# Current Research

Research question:

Release

↓

Single Leg

↓

Trail

vs

Immediate Combined Exit

Replay results (bar-based):

Release path consistently worst.

Evidence level:

SIMULATOR_RELATIVE

NOT production proof.

Model C executable replay is still required.

---

# ADR-026

Status:

PROPOSED

Decision:

Replace

Release → Single Leg

with

Immediate Atomic Combined Exit

ONLY after shadow validation.

Promotion:

BLOCKED

---

# Shadow Validation Gates

Current runtime gates:

Gate B

Restart isolation

Gate C

Natural recovery dedupe

Gate D1

Settlement coverage

Gate D2

Candidate / outcome join

Gate 7 is only complete after:

B

+

D1

+

D2

---

# Position Identity

Canonical identity:

trade_id

position_generation

entry_order_ids

Never use:

callback timestamp

as unique identity.

---

# Production vs Shadow

Production:

may trade.

Shadow:

must never trade.

Shadow only records:

- candidate
- executable BBO
- counterfactual PnL
- actual outcome

---

# Quote Path — VERIFIED (2026-08-04 correction)

**Correction:** earlier note claimed `on_tick()` is NOT the production quote
callback. That is WRONG. Verified with runtime counters:

```
Shioaji subscribe (monitor.py:1460/1472 — api.quote.subscribe, tick)
→ main.py:123 on_tick(*args)        ← global callback adapter
   (registered: return on_tick at main.py:202)
→ main.py:191 f_mon.on_tick(exchange, tick)   ← FuturesMonitor dispatch
   (near/far routing via f_mon.far_contract.code — TICK_ROUTE print,
    main.py:188)
→ monitor.py:1686 on_tick           ← production handler
```

Invocation evidence:

- TICK_ROUTE printed 114,785 times in logs/pm2-trading-out.log
  (~40 min window — every far-month tick routes through on_tick)
- "futures tick err" count = 0

So `on_tick` (monitor.py:1686) IS the production tick path.

**Current state of Model C / F Shadow hooks:**

- Both are attached at the head of on_tick (monitor.py:1688+)
- Telemetry writes = 0 (2026-08-04)
- The 0-write cause is NOT a dead path — it is an unresolved exception /
  condition inside the hook (`_f_shadow()` fail-open swallows errors;
  candidate causes: runtime self.contract / evaluate conditions)
- Next step: add a side-effect-free probe counter (no restart during
  session) to localize the exception — DO NOT move the hooks before the
  probe. In particular do NOT assume `_mts_tick` (bar-level, close/last
  only — no executable bid/ask) is the correct place for Model C/F.

Correct architecture (planned, after probe):

```
Broker callback
↓
Normalized QuoteEnvelope
↓
Shared helper (process_executable_quote(contract, bid, ask, ts, seq))
↓
Model C
↓
Shadow F
```

Both production callback and test callback call the SAME helper.

---

# Current Technical Debt

Need runtime call graph (partially done — see Quote Path above):

Broker callback

↓

Router

↓

Monitor

↓

Quote normalization

↓

Strategy

↓

Shadow

Do NOT move hooks before the path is verified (it now IS — see above).

---

# Coding Rules

Never introduce:

- duplicated quote paths
- duplicated execution logic
- duplicated PnL calculation

Extract common helpers instead.

---

Never change production behavior while implementing shadow.

---

Never infer production runtime.

Always verify with:

- callback registration
- runtime counters
- invocation evidence

---

# Testing Philosophy

Mocks must use REAL strategy fields.

Do NOT invent attributes.

Example:

Correct:

self._near_entry

Incorrect:

self._near_entry_avg

The repository has previously suffered production failures caused by mock-only attributes.

---

# Commit Strategy

Production changes should remain small.

One logical change

=

One commit.

Shadow integration commits should remain independently revertible.

---

# Before Any Major Refactor

Verify:

1.

Is this production code?

2.

Is this research code?

3.

Does this change execution?

4.

Does this affect lifecycle?

5.

Can this be rolled back independently?

If any answer is unclear,

STOP.

Investigate first.

---

# Important Principle

The production trading engine is more important than the research system.

Research exists to justify production changes.

Production must never become dependent on unfinished research.
