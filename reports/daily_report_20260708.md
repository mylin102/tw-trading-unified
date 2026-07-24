# 📊 MTS Calendar Spread Daily Performance Review
**Date:** `2026-07-08` | **Generated At:** `2026-07-08 11:06:14`

## 📈 Performance Summary
| Metric | Value | Description |
|---|---|---|
| **Daily Net PnL** | 🟢 **$+356.6 TWD** | Net realized profit/loss after fees |
| **Completed Trades** | **2** | Total spread loops closed today |
| **Win Rate** | **50.0%** | (1 Wins / 1 Losses) |
| **Profit Factor** | **2.57** | Ratio of gross wins to gross losses |
| **Average Net / Trade** | **$+178.3 TWD** | Mean net payout per round-trip |

## 📝 Closed Trades Details
| Trade ID | Direction | Entry Time | Exit Time | Exit Reason | Release Leg UPL | Exit Leg UPL | Total Net PnL | Risk Mode |
|---|---|---|---|---|---|---|---|---|
| `mts-auto-090244-130` | SELL Near / BUY Far | 09:02:44 | 09:47:43 | `TRAIL` | $-2,638.4 | $+3,221.7 | **$+583.3** | `ATR_DYNAMIC` |
| `mts-auto-095417-769` | SELL Near / BUY Far | 09:54:17 | 10:50:55 | `TRAIL` | $-1,808.3 | $+1,581.6 | **$-226.7** | `FIXED_FALLBACK` |

## ⏳ Currently Active / Open Positions
| Trade ID | Direction | Entry Time | Near Entry | Far Entry | Spread Z | ATR |
|---|---|---|---|---|---|---|
| `mts-auto-105607-780` | BUY Near / SELL Far | 10:56:07 | 45701 | 45986 | 3.00 | N/A |

## 💡 Operational Checklist & Notes
- [ ] **Execution Quality:** Check for any slippage between expected entries and fill prices.
- [ ] **Bollinger Band Gating:** Confirm if any release stop triggers were delayed/optimized by the Bollinger Band filter.
