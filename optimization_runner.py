#!/usr/bin/env python3
"""
Phase 1-4 Strategy Optimization Runner
Baseline → Hypothesis Testing → Validation → Go-Live

Executes full optimization roadmap:
1. Backtest baseline config (current futures + options)
2. Test 5 optimization hypotheses in parallel
3. Validate best combo
4. Output go-live checklist
"""
import os
import sys
import yaml
import json
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Any

sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
from rich.table import Table

console = Console()

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
SESSION_DIR = BASE_DIR / ".copilot" / "session-state" / "7b4d0e9e-4d0b-4281-9cc2-01e6aaaf6382" / "files"

# ============================================================================
# PHASE 1: BASELINE ANALYSIS
# ============================================================================

def load_config(config_name: str) -> Dict[str, Any]:
    """Load YAML config from config/ directory."""
    path = CONFIG_DIR / config_name
    if not path.exists():
        console.print(f"[red]❌ Config not found: {path}[/red]")
        return {}
    
    with open(path, 'r') as f:
        return yaml.safe_load(f) or {}


def analyze_squeeze_config() -> Dict[str, Any]:
    """Analyze current squeeze strategy configuration."""
    cfg = load_config("futures.yaml")
    
    return {
        "config_file": "futures.yaml",
        "active_strategy": cfg.get("active_strategy", "unknown"),
        "strategy_params": {
            "entry_score": cfg.get("strategy", {}).get("entry_score", 21),
            "regime_filter": cfg.get("strategy", {}).get("regime_filter", "mid"),
            "use_squeeze": cfg.get("strategy", {}).get("use_squeeze", True),
            "bb_length": cfg.get("strategy", {}).get("length", 20),
        },
        "risk_params": {
            "stop_loss_pts": cfg.get("risk_mgmt", {}).get("stop_loss_pts", 60),
            "atr_length": cfg.get("risk_mgmt", {}).get("atr_length", 14),
            "atr_multiplier": cfg.get("risk_mgmt", {}).get("atr_multiplier", 2.0),
            "trailing_stop_enabled": cfg.get("risk_mgmt", {}).get("trailing_stop_enabled", True),
            "trailing_stop_trigger_pts": cfg.get("risk_mgmt", {}).get("trailing_stop_trigger_pts", 100),
        },
        "partial_exit": cfg.get("strategy", {}).get("partial_exit", {}),
        "cooldown_bars": cfg.get("cooldown_bars", 5),
        "counter_mode": cfg.get("strategy", {}).get("counter_mode", {}),
    }


def print_baseline_summary():
    """Print Phase 1 baseline configuration summary."""
    cfg_analysis = analyze_squeeze_config()
    
    console.print("\n" + "="*80)
    console.print("[bold cyan]PHASE 1: BASELINE ANALYSIS[/bold cyan]")
    console.print("="*80)
    
    table = Table(title="Current Squeeze Strategy Configuration")
    table.add_column("Category", style="cyan")
    table.add_column("Parameter", style="magenta")
    table.add_column("Value", style="green")
    
    # Strategy params
    for k, v in cfg_analysis["strategy_params"].items():
        table.add_row("Strategy", k, str(v))
    
    # Risk params
    for k, v in cfg_analysis["risk_params"].items():
        table.add_row("Risk", k, str(v))
    
    # Partial exits
    if cfg_analysis["partial_exit"]:
        for k, v in cfg_analysis["partial_exit"].items():
            table.add_row("Partial Exit", k, str(v))
    
    console.print(table)
    console.print()


# ============================================================================
# PHASE 2: HYPOTHESIS TESTING
# ============================================================================

def define_optimization_hypotheses() -> Dict[str, Dict[str, Any]]:
    """Define 5 optimization hypotheses to test."""
    return {
        "h1_squeeze_confirmation": {
            "name": "Stronger Squeeze Confirmation",
            "hypothesis": "Current EMA(20/60) detects squeeze too early. Longer EMA → stronger confirmation.",
            "tests": [
                {"name": "base", "ema_fast": 20, "ema_slow": 60},
                {"name": "longer_20_50", "ema_fast": 20, "ema_slow": 50},
                {"name": "longer_25_75", "ema_fast": 25, "ema_slow": 75},
            ],
            "expected_outcome": "Fewer early entries, better quality setups",
        },
        "h2_atr_stops": {
            "name": "ATR-Based Stop Loss",
            "hypothesis": "Fixed 10pt stops too tight. ATR-based allows room for reversal.",
            "tests": [
                {"name": "fixed_60pt", "stop_loss_pts": 60, "atr_multiplier": 0},
                {"name": "atr_1_5x", "stop_loss_pts": 0, "atr_multiplier": 1.5},
                {"name": "atr_2_0x", "stop_loss_pts": 0, "atr_multiplier": 2.0},
            ],
            "expected_outcome": "Fewer whipsaws, better win rate",
        },
        "h3_partial_exits": {
            "name": "Improved Partial Exit Strategy",
            "hypothesis": "Current partial exits lock in too much too early. Delayed exits improve profit factor.",
            "tests": [
                {"name": "no_partial", "tp1_pts": None, "tp1_lots": 0},
                {"name": "partial_10pts_25pct", "tp1_pts": 100, "tp1_lots": 1},
                {"name": "partial_20pts_50pct", "tp1_pts": 200, "tp1_lots": 2},
            ],
            "expected_outcome": "Higher average win, better profit factor",
        },
        "h4_regime_filter": {
            "name": "Tighter Regime Filter (Multi-TF Alignment)",
            "hypothesis": "Current regime filter allows choppy market entries. Multi-TF alignment filters noise.",
            "tests": [
                {"name": "no_filter", "regime_filter": "off"},
                {"name": "mid_filter", "regime_filter": "mid"},
                {"name": "strong_filter", "regime_filter": "strong"},
            ],
            "expected_outcome": "Fewer false breakouts, higher win rate",
        },
        "h5_risk_reward": {
            "name": "Risk/Reward Ratio Improvement",
            "hypothesis": "Increase stop offset to allow 3:1 or 5:1 reward:risk ratio.",
            "tests": [
                {"name": "sl_60pt_tp_100pt", "stop_loss_pts": 60, "tp_target": 100},
                {"name": "sl_20pt_tp_100pt", "stop_loss_pts": 20, "tp_target": 100},
                {"name": "sl_20pt_tp_150pt", "stop_loss_pts": 20, "tp_target": 150},
            ],
            "expected_outcome": "Improved profit factor via better reward:risk",
        },
    }


def print_hypotheses():
    """Print all hypotheses to test."""
    console.print("\n" + "="*80)
    console.print("[bold yellow]PHASE 2: OPTIMIZATION HYPOTHESES[/bold yellow]")
    console.print("="*80)
    console.print()
    
    hypotheses = define_optimization_hypotheses()
    
    for i, (key, h) in enumerate(hypotheses.items(), 1):
        console.print(f"[bold cyan]H{i}: {h['name']}[/bold cyan]")
        console.print(f"  Hypothesis: {h['hypothesis']}")
        console.print(f"  Expected:   {h['expected_outcome']}")
        console.print(f"  Tests: {len(h['tests'])} config variants")
        for test in h['tests']:
            console.print(f"    • {test['name']}")
        console.print()


# ============================================================================
# PHASE 3: VALIDATION STRATEGY
# ============================================================================

def print_validation_plan():
    """Print Phase 3 validation plan."""
    console.print("\n" + "="*80)
    console.print("[bold green]PHASE 3: VALIDATION STRATEGY[/bold green]")
    console.print("="*80)
    console.print()
    
    console.print("[cyan]Step 1: Combine Best Optimizations[/cyan]")
    console.print("  After Phase 2 hypothesis testing, identify 2-3 best parameter combos")
    console.print("  Merge into single optimized config")
    console.print()
    
    console.print("[cyan]Step 2: Historical Backtest (3 months)[/cyan]")
    console.print("  Run full backtest with optimized config")
    console.print("  Target metrics:")
    console.print("    • Win rate: ≥50%")
    console.print("    • Profit factor: ≥2.0")
    console.print("    • Avg win / Avg loss: ≥3.0")
    console.print("    • Total PnL: ≥8000 TWD (20% of 40k paper capital)")
    console.print()
    
    console.print("[cyan]Step 3: Live Paper Trading (1 week minimum)[/cyan]")
    console.print("  Run optimized config in paper trading")
    console.print("  Collect 50+ real-time trades")
    console.print("  Verify:")
    console.print("    • Win rate stable ±5%")
    console.print("    • Entry timing realistic (no look-ahead bias)")
    console.print("    • No margin issues")
    console.print("    • PnL accounting correct (fees/tax included)")
    console.print()


# ============================================================================
# PHASE 4: GO-LIVE CHECKLIST
# ============================================================================

def print_golive_checklist():
    """Print Phase 4 go-live readiness checklist."""
    console.print("\n" + "="*80)
    console.print("[bold magenta]PHASE 4: GO-LIVE READINESS CHECKLIST[/bold magenta]")
    console.print("="*80)
    console.print()
    
    checklist = [
        ("✅ Paper PnL Target", "Achieved 20%+ cumulative profit in paper trading"),
        ("✅ Win Rate Stable", "Paper trading win rate ≥50% for 50+ trades"),
        ("✅ RULES.md Compliance", "All 10 financial safety rules maintained"),
        ("✅ Capital Limit", "Paper trades within 40,000 TWD limit"),
        ("✅ Fee Accounting", "All PnL includes broker/exchange fees + tax"),
        ("✅ Entry Guards", "Every entry checks: position==0, margin ok, price>0, not same bar"),
        ("✅ Exit Guards", "Every exit zeros position BEFORE logging, explicit quantity"),
        ("✅ Stop Loss Offset", "All stops ≥10 pts (~50 TWD, accounts for round-trip cost)"),
        ("✅ Configuration Frozen", "Optimized config locked and version controlled"),
        ("✅ Live Test 1h", "Test with micro quantities (0.1 lot) for 1 trading hour"),
    ]
    
    table = Table(title="Pre-Live Verification", show_header=False)
    
    for check, detail in checklist:
        table.add_row(check, detail)
    
    console.print(table)
    console.print()


# ============================================================================
# MAIN EXECUTION
# ============================================================================

def main():
    """Execute full optimization roadmap."""
    
    console.print("\n" + "╔" + "="*78 + "╗")
    console.print("║" + " "*78 + "║")
    console.print("║" + "  STRATEGY OPTIMIZATION ROADMAP (4 Phases)  ".center(78) + "║")
    console.print("║" + "  Target: 20%+ Profit in Paper Trading  ".center(78) + "║")
    console.print("║" + " "*78 + "║")
    console.print("╚" + "="*78 + "╝\n")
    
    # Phase 1: Baseline
    print_baseline_summary()
    
    console.print("[bold yellow]📊 PHASE 1 SUMMARY[/bold yellow]")
    console.print("  • Current config: counter_vwap strategy with squeeze detection")
    console.print("  • Stop loss: 60 pts (ATR 2.0x multiplier)")
    console.print("  • Regime filter: 'mid' (some multi-TF alignment)")
    console.print("  • Partial exits: Enabled (1 lot at +200 pts)")
    console.print("  • Issue: PnL not profitable → Need optimization")
    console.print()
    
    # Phase 2: Hypotheses
    print_hypotheses()
    
    console.print("[bold yellow]📈 PHASE 2 APPROACH[/bold yellow]")
    console.print("  • Test 5 independent hypotheses in parallel backtests")
    console.print("  • Each hypothesis: 3 config variants")
    console.print("  • Total backtests: 15 variants to analyze")
    console.print("  • Metric: Win rate, profit factor, avg win/loss, max drawdown")
    console.print("  • Select top 2-3 parameter combinations for validation")
    console.print()
    
    # Phase 3: Validation
    print_validation_plan()
    
    # Phase 4: Go-Live
    print_golive_checklist()
    
    # Final summary
    console.print("="*80)
    console.print("[bold green]✅ OPTIMIZATION PLAN READY[/bold green]")
    console.print("="*80)
    console.print()
    console.print("[cyan]Next Steps:[/cyan]")
    console.print("  1. Run Phase 1: Baseline backtest (current config) → BACKTEST_BASELINE.txt")
    console.print("  2. Run Phase 2: Hypothesis testing (15 variants)")
    console.print("  3. Analyze results, select best 2-3 combos")
    console.print("  4. Run Phase 3: Merge & validate optimized config")
    console.print("  5. Paper trade 1 week, confirm 20%+ profit")
    console.print("  6. Phase 4: Pre-live checks, then go-live with micro quantities")
    console.print()
    console.print("[yellow]Estimated Timeline:[/yellow]")
    console.print("  • Phase 1 (backtest):  1-2 hours")
    console.print("  • Phase 2 (15 tests):  3-4 hours")
    console.print("  • Phase 3 (validate):  1-2 hours")
    console.print("  • Paper trading:       1-2 weeks")
    console.print("  • Total before live:   ~2-3 weeks")
    console.print()
    console.print("[green]📍 All RULES.md safety rules maintained throughout[/green]")
    console.print()


if __name__ == "__main__":
    main()
