# R-004 Entry Episode Analysis — Final

**Status:** COMPLETE (v1 frozen)  
**Date:** 2026-07-22  
**Dataset:** TMF 2026-07-22 (night + day session)  

---

## Findings

### F1 — Entries cluster in episodes, not independent trials

34 entries across the trading date map to **4 significant episodes**, with a 94.1% attribution coverage. Entries are not IID samples.

### F2 — Repeated same-direction entries within episodes are common

Average **2.5 entries per episode** among attributed entries. At least one episode had 4 same-direction entries.

### F3 — Entry signal has no protection against cluster entries

The current Z-score threshold entry fires repeatedly as long as the spread remains extreme. Within an expanding episode, this produces multiple entries before the previous hypothesis (mean reversion) is invalidated.

### F4 — Episode-level reasoning is a more appropriate abstraction

Episode metrics (duration, expansion distance, entry count, release rate) are more informative than individual trade metrics for understanding systematic losses.

---

## Dataset Limitations

| Issue | Impact | Status |
|-------|--------|--------|
| Far-leg coverage (194 vs 688 bars) | Episode timestamps use partial data | Documented |
| 2 unmatched entries (94.1% coverage) | Marginal bias possible, not systematic | Documented |
| Release attribution | Not yet available (needs deal_id linking) | RI-001 |
| Single trading date | Small sample (4 episodes) | Awaiting accumulation |
| Episode 14 excluded | Invalid spread (-19) likely data artifact | Excluded |

---

## Threats to Validity

1. **Single-day sample** — observations may not generalize to other market regimes or volatility environments.
2. **Partial far-leg data** — episode boundaries may shift with complete data.
3. **Z-score computed at bar level** — strategy uses tick-level Z-score which may trigger earlier.
4. **Episode definition parameters** (entry_z=2.0, reset_z=0.5) affect segmentation and are not independently validated.

---

## Recommendations

### Production
- Add `episode_id` and `entry_sequence` to order records for automatic data accumulation.

### Research
- Continue daily episode dataset accumulation (target: 50+ episodes before R-005).
- Decouple trade lifecycle linking as infrastructure task (RI-001), not research prerequisite.
- Postpone R-005 until episode count supports statistical inference.

---

## Artifacts

- `scratch/r004_v4.py` — Episode analysis script (v4, final)
- Episode Dataset v1 — embedded in analysis outputs
