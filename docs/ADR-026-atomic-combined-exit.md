# ADR-026: Release Policy Transition — Single-Leg Release → Atomic Combined Exit

**Status**: PROPOSED (2026-08-04) — simulator-relative evidence; pending tick/BBO (Model C) replay before production promotion.
**Type**: Architecture (risk-control structure)
**Supersedes**: default single-leg release at `max(88, ATR×0.6)` per-leg stop

## Decision

Transition the default MTS release policy from single-leg release (leaving a
naked remaining leg on ATR trail) to **atomic combined exit at first leg
breach**: when either leg's adverse move crosses the release threshold, exit
both legs atomically (combined order path), instead of releasing the losing
leg and trailing the survivor.

Hold-horizon variants (hold 1–20m then combined exit) are **not** adopted:
no stable plateau was found.

## Rationale

1. **Naked directional exposure**: single-leg release converts a spread
   position into a naked directional position (the surviving leg), exposing
   the book to unhedged moves — the dominant loss structure in the
   simulator (baseline A total −3,240 vs immediate combined exit +5,740).
2. **Consistent simulator superiority**: across all compared architectures,
   every no-naked-leg variant (immediate combined exit, hold 1–20m, combined
   stop, combined trail, session-end) beats release+trail on the same
   close-based simulator. Immediate combined exit captures ~87% of the
   improvement without any fixed horizon.
3. **No stable hold plateau**: paired median deltas vs immediate are +0~+30
   TWD across 1–20m; totals fail the pre-fixed plateau rule 3 (within 10%
   of best) → NO_STABLE_HORIZON_PLATEAU. Fixed waiting time adds no robust
   value.
4. **Threshold robustness**: immediate combined exit is insensitive to the
   release threshold across floor 60–132 (totals +5,740~+6,660, ±14%);
   multiplier is largely inert (floor dominates, ATR×mult < floor for most
   trades; 16 param sets collapse to 9 equivalent groups). Current 88 floor
   already sits inside the stable region — no parameter change needed.
5. **Lowest-risk canary candidate**: before tick-level replay exists,
   immediate atomic combined exit is the simplest, lowest-exposure
   alternative.

## Evidence Level

```
EVIDENCE_LEVEL = SIMULATOR_RELATIVE
REPLAY_BASIS = BAR_CLOSE
BASELINE_TRIGGER_FIDELITY = 9/24   (scheme A reproduces 9/24 actual RELEASE
  triggers within 120s + same leg; matched subset PnL median |err| 67 TWD,
  sign 100%)
PRODUCTION_PROMOTION_ALLOWED = FALSE (until tick/BBO replay)
```

## Key simulator results (2026-08-03, 63 trades)

| architecture | total | vs baseline |
|---|---|---|
| A release+trail (current) | −3,240 | base |
| immediate combined exit | +5,740 | +8,980 |
| hold 10m then combined | +6,830 | +10,070 (not adopted — no plateau) |
| combined trail | +4,210 | +7,450 |

## Constraints

- Do not tune activation/giveback.
- Do not promote to real until Model C tick/BBO replay confirms
  (a) executable breach timing, (b) combined-exit fill realism.
- If a conservative canary is needed before tick replay, evaluate
  immediate atomic combined exit (F) — it removes naked-leg exposure and
  has no fixed-horizon dependency.

## Related

- ADR-025 (Policy J combined UPL trail)
- docs/MODEL_C_CANARY.md (tick/BBO source for promotion evidence)
