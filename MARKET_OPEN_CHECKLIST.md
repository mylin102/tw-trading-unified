# MARKET OPEN PREPARATION CHECKLIST
## Monday, April 13, 2026

### ✅ SYSTEM STATUS (as of Sun Apr 12 18:00 CST 2026)
- [x] All tests passing (275/275)
- [x] Dashboard running on port 8500
- [x] Auto-focus on password field implemented
- [x] Data files available (tmf_full_2026.csv)
- [x] Paper mode enabled (PAPER_MODE=true)
- [x] Paper capital limit set (40,000 TWD)

### 📊 TRADING STRATEGIES CONFIGURED

#### 1. FUTURES TRADING
**Active Strategy:** Counter VWAP
- **Day Session:** 08:45-13:45
- **Night Session:** 15:00-05:00
- **Risk Management:**
  - Stop loss (day): 60 pts
  - Stop loss (night): 80 pts  
  - ATR multiplier (day): 1.8
  - ATR multiplier (night): 2.2
  - Break-even offset: 100 pts
  - Max positions: 2
  - Lots per trade: 1
- **Strategy Parameters:**
  - Confirm bars (day): 7
  - Confirm bars (night): 10
  - Min momentum (day): 30.0
  - Min momentum (night): 50.0
  - Use squeeze filter: ✓
  - Partial exit enabled: ✓
  - TP1 points: 200 pts
- **Execution:**
  - Order type: MKP
  - Broker fee: 20 TWD/side
  - Tax rate: 0.002%
  - Initial balance: 100,000 TWD
  - Live trading: ❌ (Paper mode)

#### 2. OPTIONS TRADING
**Active Mode:** V2 (Swing)
- **Strategy Parameters:**
  - Entry score: 15 (was 30 → lowered, was 10 → raised)
  - Score floor: 15
  - Regime filter: mid
  - Require fire: ✗ (disabled — was blocking all signals)
  - Require align: ✗ (disabled — triple filter too strict)
  - Fire score threshold: 60
  - Use opening logic: ✓
- **Risk Management:**
  - Stop loss: 15% (was 20% → tighter)
  - Max positions: 2
  - Initial capital: 40,000 TWD
  - Max daily loss: 2%
  - Max holding days: 7
  - **Min DTE entry: 7 days** (was 0.5 → DTE too short causes IV crush)
  - Expiry floor: 7 days
- **Exit Strategy:**
  - TP1: 2.0% (was 0.5% → too small to matter)
  - Trailing stop: 1.5% (was 0.15% → way too tight)
  - Reversal threshold: entry_score × 1.5 = 22.5 (was × 0.67 = 10 → too sensitive)
- **Theta Gang Strategy:**
  - Enabled: ✓
  - Strategy: Iron Condor
  - Min IV: 0.18
  - Min DTE entry: 7 days
  - Take profit: 50%

#### 3. STOCKS TRADING
**Strategy:** Mean Reversion
- **Watchlist:** 15 stocks (1590, 2049, 2059, 2207, 2233, 2360, 2368, 3711, 4583, 4768, 1216, 1525, 1560, 1707, 2330)
- **Risk Management:**
  - Total portfolio budget: 100,000 TWD
  - Capital per trade: 20,000 TWD
  - Stop loss: 5%
  - Take profit: 15%
  - Max positions (normal): 3
  - Max positions (bear): 1
  - Max daily loss: 3,000 TWD

### 🔧 PRE-MARKET CHECKS (Tomorrow Morning)

#### BEFORE MARKET OPEN (08:30)
1. **Run System Tests:**
   ```bash
   python3 -m pytest tests/ -v
   ```

2. **Verify Data Files:**
   ```bash
   head -5 data/tmf_full_2026.csv
   # Should show 'timestamp' column
   ```

3. **Check Dashboard:**
   - Access http://localhost:8500
   - Verify password field auto-focus works
   - Check all tabs load correctly

4. **Verify Paper Mode:**
   ```bash
   grep -i "paper" .env
   # Should show PAPER_MODE=true and PAPER_CAPITAL_LIMIT=40000
   ```

5. **Check Logs Directory:**
   ```bash
   ls -la logs/
   # Ensure write permissions
   ```

#### AT MARKET OPEN (08:45)
1. **Monitor Initial Data Flow:**
   - Check dashboard for real-time updates
   - Verify timestamp synchronization

2. **Strategy Activation:**
   - Futures: Counter VWAP active
   - Options: V2 mode with theta gang
   - Stocks: Mean reversion

3. **Risk Limits Monitoring:**
   - Paper capital limit: 40,000 TWD
   - Stop loss offsets: ≥10 pts
   - Position limits enforced

### ⚠️ CRITICAL RULES REMINDER

1. **Paper Mode Safety:**
   - All trades are paper trades with real financial implications if bugs exist
   - Capital limit: 40,000 TWD maximum exposure

2. **Position Management:**
   - `PaperTrader.position` is single source of truth
   - Zero position BEFORE logging any exit
   - Pass explicit quantity on all exits

3. **PnL Calculation:**
   - ALL PnL must include broker fees + exchange fees + tax
   - Stop loss offset must be ≥10 pts (round-trip cost ~8 pts for TMF)

4. **Entry Validation:**
   - Check position == 0
   - Verify margin sufficient
   - Confirm price > 0
   - Not same bar as previous trade

5. **Strategy Plugins:**
   - Must return {"action", "reason", "stop_loss"} or None
   - Side effects ONLY after operation succeeds

### 📈 DATA COLLECTION FOR STRATEGY REVIEW

**Collect during trading day:**
1. **Trade Logs:** All entries/exits with timestamps
2. **PnL Tracking:** Net PnL including all fees
3. **Strategy Performance:** Win rate, avg win/loss
4. **Risk Metrics:** Max drawdown, Sharpe ratio
5. **Execution Quality:** Slippage, fill rates

**Post-market analysis:**
1. **Review all trades** against strategy rules
2. **Calculate actual PnL** vs expected
3. **Identify optimization opportunities**
4. **Update strategy parameters** if needed

### 🚨 EMERGENCY PROCEDURES

**If system malfunctions:**
1. **Immediate halt:** Stop all trading activity
2. **Check logs:** `tail -f logs/trading.log`
3. **Restart if needed:** `python3 main.py --paper`
4. **Manual override:** Use dashboard to close positions

**If data feed fails:**
1. **Fallback to file data:** `data/tmf_full_2026.csv`
2. **Check Shioaji connection:** Verify API keys
3. **Monitor dashboard alerts:** Stale data warnings

### ✅ FINAL VERIFICATION

**System Ready:**
- [x] Tests passing
- [x] Dashboard accessible
- [x] Paper mode enabled
- [x] Data files available
- [x] Strategies configured
- [x] Risk limits set

**Ready for Market Open: 08:45, Monday, April 13, 2026**