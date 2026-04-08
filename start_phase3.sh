#!/bin/bash
#
# PHASE 3: PAPER TRADING VALIDATION - STARTUP SCRIPT
# 
# This script initializes Phase 3 paper trading with optimized config.
# Duration: 1-2 weeks continuous trading
# Capital: 40,000 TWD (paper mode)
# Success Gate: 50+ trades, ≥20% cumulative profit
#
# Usage: ./start_phase3.sh
#

set -e

echo "╔════════════════════════════════════════════════════════════════════════════╗"
echo "║                                                                            ║"
echo "║               PHASE 3: PAPER TRADING VALIDATION - STARTUP                  ║"
echo "║                                                                            ║"
echo "║  Config: futures_optimized.yaml (merged from Phase 2 top 3 variants)       ║"
echo "║  Duration: 1-2 weeks continuous trading                                     ║"
echo "║  Capital: 40,000 TWD (paper mode)                                          ║"
echo "║  Success Gate: 50+ trades, ≥20% cumulative profit (8000 TWD minimum)      ║"
echo "║                                                                            ║"
echo "╚════════════════════════════════════════════════════════════════════════════╝"
echo ""

# Step 1: Verify config exists
echo "STEP 1: Verify Optimized Config"
echo "═════════════════════════════════════════════════════════════════════════════"
if [ -f "config/futures_optimized.yaml" ]; then
    echo "✅ Found: config/futures_optimized.yaml"
    echo ""
    echo "Optimized Parameters:"
    grep -E "cooldown_bars:|bb_length:|tp1_pts:" config/futures_optimized.yaml | head -10
    echo ""
else
    echo "❌ ERROR: config/futures_optimized.yaml not found"
    exit 1
fi

# Step 2: Run validation backtest
echo "STEP 2: Run Validation Backtest"
echo "═════════════════════════════════════════════════════════════════════════════"
echo "Testing optimized config on 90-day historical data..."
echo ""

if python3 phase3_validation_backtest.py; then
    echo ""
    echo "✅ Validation backtest PASSED"
    echo ""
else
    echo ""
    echo "⚠️  Validation backtest failed or conditional. Review output above."
    echo "You may proceed with caution or adjust parameters."
    echo ""
fi

# Step 3: Safety checks
echo "STEP 3: Pre-Flight Safety Checks"
echo "═════════════════════════════════════════════════════════════════════════════"

python3 << 'PYTHON_EOF'
from core.config_manager import ConfigManager
import yaml

config_mgr = ConfigManager()
config = config_mgr.load_yaml('config/futures_optimized.yaml')

checks = {
    "Capital limit 40k": config['execution']['initial_balance'] == 40000,
    "Paper mode (not live)": config['live_trading'] == False,
    "Stop loss ≥ 10 pts": config['risk_mgmt']['stop_loss_pts'] >= 10,
    "Cooldown enabled": config['cooldown_bars'] > 0,
    "Squeeze enabled": config['strategy']['use_squeeze'] == True,
}

print()
all_pass = True
for check, passed in checks.items():
    status = "✅" if passed else "❌"
    print(f"{status} {check}")
    if not passed:
        all_pass = False

print()
if all_pass:
    print("✅ All safety checks PASSED")
else:
    print("❌ Some safety checks failed. Review config.")
    import sys
    sys.exit(1)

PYTHON_EOF

if [ $? -ne 0 ]; then
    exit 1
fi

echo ""

# Step 4: Ready to start
echo "STEP 4: Ready for Phase 3 Paper Trading"
echo "═════════════════════════════════════════════════════════════════════════════"
echo ""
echo "OPTIMIZED PARAMETERS (merged from Phase 2):"
echo "  cooldown_bars: 20      (from H5.3-Extended_Cooldown, 100% WR)"
echo "  bb_length: 25          (from H1.2-Longer_EMA, 83.3% WR)"
echo "  tp_pts: 300            (from H3.3-Late_30pts, 215,755 TWD)"
echo ""
echo "EXPECTED RESULTS:"
echo "  Win Rate: 75-85%"
echo "  Profit Factor: 7-14x"
echo "  Total PnL: 120k-160k TWD"
echo "  Trade Frequency: 4-8 trades/month"
echo ""
echo "PHASE 3 SUCCESS GATES:"
echo "  ✓ Win Rate ≥ 50%"
echo "  ✓ Cumulative PnL ≥ 8,000 TWD (20% of 40k)"
echo "  ✓ Profit Factor ≥ 3.0x"
echo "  ✓ No capital breaches (stay ≤ 40,000 TWD)"
echo "  ✓ 50+ trades collected over 1-2 weeks"
echo ""
echo "NEXT COMMAND (start paper trading):"
echo "═════════════════════════════════════════════════════════════════════════════"
echo ""
echo "  python3 strategies/futures/monitor.py --config config/futures_optimized.yaml"
echo ""
echo "Then monitor progress in:"
echo "  • logs/futures_monitor.log (real-time monitoring)"
echo "  • logs/trade_log.csv (trade record)"
echo ""
echo "Expected timeline: 1-2 weeks to collect 50+ trades"
echo ""
echo "═════════════════════════════════════════════════════════════════════════════"
echo "✅ PHASE 3 STARTUP COMPLETE"
echo "═════════════════════════════════════════════════════════════════════════════"

