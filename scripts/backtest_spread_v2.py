#!/usr/bin/env python3
"""
Unified Spread Backtester V2
Tests multiple spread strategies using pre-computed calendar spread data.
"""

import os
import sys
import glob
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# Add project root to path
sys.path.append('.')

# 2026-06-25 Hermes Agent: Isolate backtests from production state and trade logs
os.environ["MTS_FILL_LOG_PATH"] = "/tmp/backtest_mts_trade_fills.jsonl"
os.environ["MTS_EVENT_LOG_PATH"] = "/tmp/backtest_mts_spread_events.jsonl"
os.environ["MTS_STATE_PATH"] = "/tmp/backtest_mts_position_state.json"
# 2026-06-25 Gemini CLI: Enable backtest flag to disable state recovery and disk I/O in strategy
os.environ["MTS_BACKTEST"] = "1"

from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, MarketData, PositionView
from core.signal import Signal

# ==================== Configuration ====================
DATA_PATTERN = "data/mxf_calendar_spread_*.csv"
INITIAL_CAPITAL = 100_000
MULTIPLIER = 10.0  # TMF (Micro)
FEE_PER_SIDE = 10.0 # Estimate for micro
TAX_RATE = 0.00002

# ==================== Backtest Engine ====================

class SpreadBacktester:
    def __init__(self, strategy_name: str, config: Dict[str, Any] = None):
        self.strategy_name = strategy_name
        self.config = config or {}
        
        reg = StrategyRegistry()
        reg.discover()
        self.strategy = reg.get(strategy_name)
        if not self.strategy:
            raise ValueError(f"Strategy {strategy_name} not found in registry")
        
        self.reset()

    def reset(self):
        self.cash = INITIAL_CAPITAL
        self.position_size = 0  
        self.trades = []
        self.equity_history = []
        self.current_entry_price_near = 0.0
        self.current_entry_price_far = 0.0
        self.released_leg = None 
        self.release_price = 0.0
        self.current_trade_id = None
        
        # Performance tracking
        self.total_gross = 0.0
        self.total_fees = 0.0
        self.total_taxes = 0.0
        
        # For StrategyContext
        self.bar_counter = 0
        
    def _calculate_cost(self, near_price: float, far_price: float, qty: int, is_full: bool = True) -> Dict[str, float]:
        """Calculate breakdown of fees + taxes."""
        num_legs = 2 if is_full else 1
        fees = FEE_PER_SIDE * num_legs * abs(qty)
        # Tax: based on notional value
        total_notional = 0.0
        if is_full:
            total_notional = (near_price + far_price) * abs(qty) * MULTIPLIER
        else:
            total_notional = (near_price or far_price) * abs(qty) * MULTIPLIER
        
        tax = total_notional * TAX_RATE
        return {"fees": fees, "tax": tax, "total": fees + tax}

    def run_on_df(self, df: pd.DataFrame):
        """Run backtest on a single day's dataframe."""
        self.strategy.init(StrategyContext(
            market=MarketData(last_bar={}, df_5m=pd.DataFrame()),
            position=PositionView(),
            config=self.config
        ))
        
        for i in range(len(df)):
            row = df.iloc[i]
            ts = row.name
            
            # Construct bar dict for strategy
            bar = row.to_dict()
            bar['timestamp'] = ts
            bar['Close'] = row['Close_near']
            bar['near_close'] = row['Close_near']
            bar['far_close'] = row['Close_far']
            bar['spread_z'] = row.get('spread_z', 0.0)
            bar['spread_age_minutes'] = 0 
            
            # Indicators
            bar['vwap_z'] = row.get('vwap_z', 0.0)
            bar['spread_std'] = row.get('spread_std', 5.0)
            # Add a mock ATR if missing (use spread_std as proxy for now)
            bar['atr'] = row.get('atr', row.get('spread_std', 5.0))
            
            bar.setdefault("regime", self.config.get("params", {}).get("regime", "WEAK"))
            bar.setdefault("adx", 15.0)
            bar.setdefault("breakout_strength", 0.0)
            bar.setdefault("volume_spike", 1.0)
            
            ctx = StrategyContext(
                market=MarketData(last_bar=bar, df_5m=df.iloc[:i+1]),
                position=PositionView(size=self.position_size, entry_price=self.current_entry_price_near),
                config=self.config,
                bar_counter=i
            )
            
            signal = self.strategy.on_bar(ctx)
            
            if signal:
                self._handle_signal(signal, bar, ts)
            
            # Record equity
            unrealized = 0
            if self.position_size != 0:
                if self.released_leg is None:
                    pnl_near = (row['Close_near'] - self.current_entry_price_near) * self.position_size
                    pnl_far = (self.current_entry_price_far - row['Close_far']) * self.position_size
                    unrealized = (pnl_near + pnl_far) * MULTIPLIER
                elif self.released_leg == 'near':
                    pnl_far = (self.current_entry_price_far - row['Close_far']) * self.position_size
                    unrealized = pnl_far * MULTIPLIER
                elif self.released_leg == 'far':
                    pnl_near = (row['Close_near'] - self.current_entry_price_near) * self.position_size
                    unrealized = pnl_near * MULTIPLIER
                
            self.equity_history.append(self.cash + unrealized)

    def _handle_signal(self, signal: Signal, bar: Dict[str, Any], ts: Any):
        # 2026-06-25 Hermes Agent: Convert ts to pandas Timestamp to prevent strftime errors on raw/numpy types
        if not isinstance(ts, pd.Timestamp):
            try:
                if isinstance(ts, (int, float, np.integer)):
                    if ts > 1e11:
                        ts = pd.to_datetime(ts, unit='ns')
                    else:
                        ts = pd.to_datetime(ts, unit='s')
                else:
                    ts = pd.to_datetime(ts)
            except:
                ts = pd.Timestamp.now()

        near_price = bar['Close_near']
        far_price = bar['Close_far']
        
        is_entry = signal.action in ["BUY", "SELL", "SELL_NEAR_BUY_FAR", "BUY_NEAR_SELL_FAR"]
        
        if is_entry and self.position_size == 0:
            qty = getattr(signal, "quantity", 1)
            side = 1 if signal.action in ["BUY", "BUY_NEAR_SELL_FAR"] else -1
            
            cost_details = self._calculate_cost(near_price, far_price, qty)
            cost = cost_details["total"]
            
            if self.cash >= cost:
                self.cash -= cost
                self.total_fees += cost_details["fees"]
                self.total_taxes += cost_details["tax"]
                
                self.position_size = side * qty
                self.current_entry_price_near = near_price
                self.current_entry_price_far = far_price
                self.released_leg = None
                self.current_trade_id = f"T-{ts.strftime('%Y%m%d-%H%M%S')}"
                
                # 2026-06-25 Gemini CLI: Sync strategy state passing historical entry_ts for grace period tracking
                self.strategy.sync_position(
                    trade_id=self.current_trade_id,
                    side="LONG" if side == 1 else "SHORT",
                    near_entry=near_price,
                    far_entry=far_price,
                    entry_spread_z=bar.get('spread_z', 0.0),
                    entry_ts=ts
                )
                
                self.trades.append({
                    "ts": ts, "action": "ENTRY", "near": near_price, "far": far_price,
                    "qty": self.position_size, "cost": cost, "reason": signal.reason
                })
                
        elif signal.action in ["EXIT", "CLOSE"] and self.position_size != 0:
            qty = abs(self.position_size)
            
            gross_pnl = 0.0
            if self.released_leg is None:
                pnl_near = (near_price - self.current_entry_price_near) * self.position_size
                pnl_far = (self.current_entry_price_far - far_price) * self.position_size
                gross_pnl = (pnl_near + pnl_far) * MULTIPLIER
            elif self.released_leg == 'near':
                gross_pnl = (self.current_entry_price_far - far_price) * self.position_size * MULTIPLIER
            else: # far released
                gross_pnl = (near_price - self.current_entry_price_near) * self.position_size * MULTIPLIER
            
            cost_details = self._calculate_cost(near_price if self.released_leg != 'near' else 0,
                                               far_price if self.released_leg != 'far' else 0,
                                               qty, is_full=False)
            cost = cost_details["total"]
            
            self.total_gross += gross_pnl
            self.total_fees += cost_details["fees"]
            self.total_taxes += cost_details["tax"]
            self.cash += (gross_pnl - cost)
            
            self.trades.append({
                "ts": ts, "action": "EXIT", "near": near_price, "far": far_price,
                "pnl": gross_pnl - cost, "gross": gross_pnl, "cost": cost, "reason": signal.reason
            })
            
            self.position_size = 0
            self.current_entry_price_near = 0
            self.current_entry_price_far = 0
            self.released_leg = None
            
            # 2026-06-25 Gemini CLI: Reset strategy state with exit_ts to prevent cooldown lock in backtests
            self.strategy._reset(reason="backtest_exit", exit_ts=ts)
            
        elif signal.action == "PARTIAL_EXIT" and self.position_size != 0 and self.released_leg is None:
            qty = abs(self.position_size)
            if "NEAR" in signal.reason:
                self.released_leg = "near"
                gross_pnl = (near_price - self.current_entry_price_near) * self.position_size * MULTIPLIER
                cost_details = self._calculate_cost(near_price, 0, qty, is_full=False)
                # 2026-06-25 Hermes Agent: Sync strategy partial exit release (remaining price is far_price)
                self.strategy.sync_release(leg="near", price=far_price)
            else:
                self.released_leg = "far"
                gross_pnl = (self.current_entry_price_far - far_price) * self.position_size * MULTIPLIER
                cost_details = self._calculate_cost(0, far_price, qty, is_full=False)
                # 2026-06-25 Hermes Agent: Sync strategy partial exit release (remaining price is near_price)
                self.strategy.sync_release(leg="far", price=near_price)
            
            cost = cost_details["total"]
            self.total_gross += gross_pnl
            self.total_fees += cost_details["fees"]
            self.total_taxes += cost_details["tax"]
            self.cash += (gross_pnl - cost)
            
            self.trades.append({
                "ts": ts, "action": "PARTIAL_EXIT", "near": near_price, "far": far_price,
                "pnl": gross_pnl - cost, "gross": gross_pnl, "cost": cost, "reason": signal.reason
            })

    def get_metrics(self) -> Dict[str, Any]:
        if not self.trades:
            return {}
        
        exits = [t for t in self.trades if t['action'] == "EXIT"]
        if not exits:
            return {"trade_count": 0}
            
        total_net = self.cash - INITIAL_CAPITAL
        total_costs = self.total_fees + self.total_taxes
        
        # Win rate based on realized legs
        all_pnls = [t.get('pnl', 0.0) for t in self.trades if 'pnl' in t]
        win_count = sum(1 for p in all_pnls if p > 0)
        win_rate = win_count / len(all_pnls) if all_pnls else 0
        
        return {
            "strategy": self.strategy_name,
            "total_net": total_net,
            "total_gross": self.total_gross,
            "total_fees": self.total_fees,
            "total_taxes": self.total_taxes,
            "trade_count": len(exits),
            "win_rate": win_rate,
            "profit_factor": self.total_gross / total_costs if total_costs > 0 else 99.0,
            "avg_net": total_net / len(exits) if exits else 0
        }

# ==================== Main ====================

def run_scenario(strat_name: str, files: List[str], config: Dict[str, Any]) -> Dict[str, Any]:
    tester = SpreadBacktester(strat_name, config=config)
    for f in files:
        df = pd.read_csv(f)
        if df.empty: continue
        ts_col = next((c for c in ["ts", "timestamp", "datetime"] if c in df.columns), None)
        if ts_col:
            df[ts_col] = pd.to_datetime(df[ts_col])
            df = df.set_index(ts_col)
            tester.run_on_df(df)
    return tester.get_metrics()

def main():
    files = sorted(glob.glob(DATA_PATTERN))
    if not files:
        print(f"No data files found matching {DATA_PATTERN}")
        return

    strat_name = "tmf_spread"
    results = []

    scenarios = [
        ("No Filter (Z=0, ATR=0)", 0.0, 0.0),
        ("Z-Gate Only (Z=2.5, ATR=0)", 2.5, 0.0),
        ("ATR-Gate Only (Z=0, ATR=10)", 0.0, 10.0),
        ("Optimized (Z=2.5, ATR=10)", 2.5, 10.0),
    ]

    for label, entry_z, min_atr in scenarios:
        print(f"Running {label}...")
        config = {
            "params": {
                "allow_night_session": True, "regime": "WEAK",
                "atr_multiplier_stop": 2.0, "atr_multiplier_trail": 3.5,
                "entry_z": entry_z,
                "min_atr": min_atr,
                "release_stop_points": 20, "trail_distance_points": 30
            }
        }
        metrics = run_scenario(strat_name, files, config)
        if metrics:
            metrics['label'] = label
            metrics['friction'] = (metrics['total_fees'] + metrics['total_taxes']) / abs(metrics['total_gross'] if metrics['total_gross'] != 0 else 1)
            results.append(metrics)

    print("\n" + "="*105)
    print(f"{'Scenario':<30} | {'Net PnL':>10} | {'Trades':>6} | {'Win%':>7} | {'PF':>5} | {'Avg Net':>8} | {'Friction':>8}")
    print("-" * 105)
    for r in results:
        print(f"{r['label']:<30} | ${r['total_net']:>9.0f} | {r['trade_count']:>6} | {r['win_rate']:>6.1%} | {r['profit_factor']:>5.2f} | ${r['avg_net']:>7.2f} | {r['friction']:>8.1%}")
    print("="*105)
    print("Note: ATR is proxied by spread_std from CSV.")

if __name__ == "__main__":
    # macOS Silicon optimization: Force main and spawned sub-processes to E-Cores
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
