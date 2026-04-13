# 2026-04-13 Trading Session Review

## Summary: No Real Trades (All PAPER, All Entry-Blocked)

| Instrument | Trades | PnL | Status |
|-----------|--------|-----|--------|
| **Stocks (PAPER)** | 4 BUY, 1 SELL | -23 TWD | Simulated only |
| **Futures (PAPER)** | 1 SELL, 1 EXIT | -234 TWD | Night session only |
| **Options (PAPER)** | 0 entries | 0 | Score too low all day |

---

## 1. Futures: 1 Trade, Night Session Only

### What happened
- **15:05** SELL SHORT @ 35505 (UPTHRUST signal, score 6.7/10)
- **15:10** EXIT @ 35523 (VWAP stop)
- **PnL: -18 pts × 200 × 1 lot = -234 TWD** (friction cost 54 TWD)
- Session: **Night (夜盤)** — started 15:00, not day session

### Why only 1 trade in day session?
**ENTRY_BLOCKED 77 times** — all blocked by `low_volume`:
```
vol=126 avg=742 thresh=0.3  → 126/742 = 17% < 30% threshold
```
Volume filter too strict for today's thin market. The strategy is `counter_vwap` which requires volume confirmation.

### Why did the night trade lose?
- UPTHRUST signal fired but price continued rising (35505 → 35523)
- VWAP trailing stop hit at +18 pts against
- This is the expected behavior of counter-VWAP: it's trying to catch reversals, sometimes wrong

---

## 2. Options: 0 Trades — Score Filter Blocked Everything

### Configuration
| Setting | Value | Effect |
|---------|-------|--------|
| `entry_score` | 30 | Minimum score to enter |
| `require_fire` | True | Must have squeeze fire |
| `require_align` | True | Must have timeframe alignment |

### Reality
| Metric | Value |
|--------|-------|
| Max score today | **20.0** |
| Mean score | **-70.2** |
| Median score | **-66.7** |
| Bars with score ≥ 30 | **0** |
| Bars with score ≥ 60 | **0** |
| Total bars scanned | 1369 |

### Root cause
**Max score was 20 — 10 points below the 30 entry threshold.** The scoring weights (5m:0.2, 15m:0.4, 1h:0.4) produced deeply negative scores all day because:
- Market was range-bound (35205–35651, range < 1.3%)
- No squeeze fire signals met all 3 timeframe requirements
- `require_fire=true` + `require_align=true` = triple filter, extremely strict

### "fired=True" bars (32 of them)
All from **4/10 night session**, score -60 to -93 — they fired on squeeze but scored too low.

---

## 3. Stocks: 4 Paper Entries, 1 Paper Exit

All PAPER (simulated) — no real money:
- 2207 @ 503 (9 shares)
- 3149 @ 41.4 (120 shares)  
- 1708 @ 42.0 (118 shares)
- 3030 @ 308 → sold @ 310 (net -23 TWD after fees)

Strategy: `mean_reversion_enhanced` — BB lower bounce. These are all "catching falling knives" in a down market.

---

## Diagnosis

### Futures: Volume filter too strict
`vol=126 avg=742 thresh=0.3` — today's volume was 17% of average, well below the 30% threshold. On thin days, counter-VWAP never gets a chance to fire during day session.

**Fix:** Lower volume threshold to 0.15 or disable for day session.

### Options: Score system produces negative scores all day
The scoring formula generates -66 to -100 on normal range-bound days. The `entry_score=30` threshold is unreachable. This was likely tuned during a high-volatility squeeze event and never adjusted for normal conditions.

**Fix:** Either:
1. Lower `entry_score` from 30 → 10 (still blocks noise, allows weaker signals)
2. Or recalibrate the scoring weights to produce more positive scores on normal days
3. Or add a "regime-adaptive threshold" (lower score requirement in strong trends)

### Both: Day session dead, night session only active
The day session (09:00–13:30) generated almost no signals. The market was too range-bound. All action was in the night session (15:00+) when US markets opened.

---

## Action Items

| Priority | Action | Details |
|----------|--------|---------|
| P1 | Lower futures volume threshold | `volume_threshold: 0.3 → 0.15` in futures.yaml |
| P2 | Lower options entry score | `entry_score: 30 → 10` or recalibrate scoring |
| P3 | Review options scoring formula | Max score 20 on a normal day = formula may be broken |
| P4 | Check if mean_reversion strategy is too aggressive | 4 entries in one day = too many for mean reversion |
