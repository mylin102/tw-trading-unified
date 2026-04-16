"""
Unified Backtest Engine — Event-driven simulator for Stocks and Futures.
Aligned with PaperTrader logic for high-fidelity backtesting.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import MarketData, PositionView, StrategyContext


class AssetType(str, Enum):
    STOCK = "stock"
    FUTURES = "futures"
    OPTIONS = "options"


class AssetProfile(BaseModel):
    """Configuration for asset-specific trading math."""
    asset_type: AssetType
    point_value: float = 1.0
    margin_per_lot: float = 0.0
    fee_rate: float = 0.0 # Default 0 to force explicit config
    tax_rate: float = 0.0 # Default 0
    min_fee: float = 0.0


@dataclass
class BacktestPosition:
    ticker: str
    entry_price: float
    qty: int
    entry_time: datetime
    stop_loss: float = 0.0
    target: float = 0.0
    high_water: float = 0.0
    break_even_trigger: float = 0.0
    trail_points: float = 0.0
    be_triggered: bool = False

    def __post_init__(self):
        self.high_water = self.entry_price

    def update_high_water(self, current_price: float):
        sign = 1 if self.qty > 0 else -1
        # Update high water mark
        if self.qty > 0: self.high_water = max(self.high_water, current_price)
        else: self.high_water = min(self.high_water, current_price)
        
        # 1. Breakeven logic
        if self.break_even_trigger > 0 and not self.be_triggered:
            pnl = (current_price - self.entry_price) * sign
            if pnl >= self.break_even_trigger:
                self.stop_loss = self.entry_price + (10 * sign)
                self.be_triggered = True
        
        # 2. Continuous Trailing logic
        if self.trail_points > 0:
            new_sl = self.high_water - (self.trail_points * sign)
            if self.qty > 0:
                if self.stop_loss == 0 or new_sl > self.stop_loss:
                    self.stop_loss = new_sl
            else:
                if self.stop_loss == 0 or new_sl < self.stop_loss:
                    self.stop_loss = new_sl

    def check_exit(self, price: float) -> Optional[str]:
        if self.qty > 0:
            if self.stop_loss > 0 and price <= self.stop_loss: return "STOP_LOSS"
            if self.target > 0 and price >= self.target: return "TAKE_PROFIT"
        elif self.qty < 0:
            if self.stop_loss > 0 and price >= self.stop_loss: return "STOP_LOSS"
            if self.target > 0 and price <= self.target: return "TAKE_PROFIT"
        return None


@dataclass
class BacktestResult:
    strategy_name: str
    trades: pd.DataFrame
    equity_curve: pd.Series
    metrics: Dict[str, Any]


class BacktestEngine:
    def __init__(self, profile: AssetProfile, initial_capital: float = 1_000_000, logger: Optional[logging.Logger] = None):
        self.profile = profile
        self.initial_capital = initial_capital
        self.logger = logger or logging.getLogger(__name__)
        self.reset()

    def reset(self):
        self.cash = self.initial_capital
        self.positions: Dict[str, BacktestPosition] = {}
        self.trade_log = []
        self.equity_history = []
        self.time_history = []

    def _calculate_cost(self, price: float, qty: int, is_entry: bool) -> float:
        notional = price * abs(qty) * self.profile.point_value
        fee = notional * self.profile.fee_rate
        tax = notional * self.profile.tax_rate if not is_entry else 0.0
        return fee + tax

    def run(self, df: pd.DataFrame, strategy: StrategyBase, config: Optional[Dict[str, Any]] = None) -> BacktestResult:
        self.reset()
        config = config or {}
        params = config.get("params", {})

        # 1. Optimized Enrichment: Skip if columns exist
        required_indicators = strategy.metadata.get("indicators", [])
        if required_indicators:
            needs_enrich = False
            for ind in required_indicators:
                col = {"kalman": "kalman_close", "squeeze": "fired", "linreg": "lr_slope"}.get(ind, ind)
                if col not in df.columns:
                    needs_enrich = True; break
            
            if needs_enrich:
                from core.data_enricher import enricher
                df = enricher.enrich(df, required_indicators, **params)
        
        df = strategy.prepare_data(df)

        n = len(df)
        if n < 20: return self._empty_result(strategy.name)

        self.equity_history.append(self.initial_capital)
        self.time_history.append(df.index[19])
        strategy.init(StrategyContext(market=MarketData(last_bar={}, df_5m=df.iloc[:20]), position=PositionView(), config=config))

        from core.market_regime import calculate_regimes
        # Pre-calculate daily regimes for the whole dataset
        daily_regimes = calculate_regimes(df)
        
        # Fast lookup: map trading day to regime string
        regime_map = daily_regimes.to_dict()

        for i in range(20, n):
            ts, price = df.index[i], df.iloc[i]["Close"]
            bar = df.iloc[i].to_dict()
            ticker = bar.get("ticker", "TMF")

            # GSD: Detect Market Regime (Wave 19)
            # Use pre-calculated regime for the current trading day
            day_key = ts.normalize()
            current_regime = regime_map.get(day_key, "NEUTRAL")

            if ticker in self.positions:
                pos = self.positions[ticker]
                pos.update_high_water(price)
                reason = pos.check_exit(price)
                if reason: self._execute_signal(ticker, Signal(action="EXIT", reason=reason, stop_loss=0), price, ts)

            if ticker not in self.positions or self.positions[ticker].qty != 0:
                ctx = StrategyContext(market=MarketData(last_bar=bar, df_5m=df.iloc[max(0, i-100):i+1], regime=current_regime), 
                                    position=self._get_position_view(ticker), config=config, bar_counter=i)
                sig = strategy.on_bar(ctx)
                if sig: self._execute_signal(ticker, sig, price, ts)

            unrealized = sum((price - p.entry_price) * p.qty * self.profile.point_value for p in self.positions.values())
            margin = sum(abs(p.qty) * self.profile.margin_per_lot for p in self.positions.values())
            self.equity_history.append(self.cash + margin + unrealized)
            self.time_history.append(ts)

        return self._finalize_result(strategy.name)

    def _get_position_view(self, ticker: str) -> PositionView:
        if ticker not in self.positions: return PositionView()
        pos = self.positions[ticker]
        return PositionView(size=pos.qty, entry_price=pos.entry_price)

    def _execute_signal(self, ticker: str, signal: Signal, price: float, ts: datetime):
        if signal.action in ["BUY", "SELL"] and ticker not in self.positions:
            # GSD: Support dynamic sizing from Signal
            qty = getattr(signal, "quantity", 1)
            if qty <= 0: return
            
            cost = self._calculate_cost(price, qty, is_entry=True)
            margin = qty * self.profile.margin_per_lot if self.profile.asset_type == AssetType.FUTURES else price * qty * self.profile.point_value
            if self.cash >= margin + cost:
                self.cash -= (margin + cost)
                self.positions[ticker] = BacktestPosition(
                    ticker, price, (1 if signal.action == "BUY" else -1) * qty, ts, 
                    stop_loss=signal.stop_loss, target=signal.target,
                    break_even_trigger=signal.break_even_trigger,
                    trail_points=signal.trail_points
                )
                self.trade_log.append({"timestamp": ts, "ticker": ticker, "action": signal.action, "price": price, "qty": qty, "reason": signal.reason, "pnl": 0.0})
        elif signal.action == "EXIT" and ticker in self.positions:
            pos = self.positions.pop(ticker)
            cost, mult = self._calculate_cost(price, abs(pos.qty), is_entry=False), self.profile.point_value
            raw_pnl = (price - pos.entry_price) * pos.qty * mult
            margin = abs(pos.qty) * self.profile.margin_per_lot if self.profile.asset_type == AssetType.FUTURES else pos.entry_price * abs(pos.qty) * mult
            self.cash += (margin + raw_pnl - cost)
            self.trade_log.append({"timestamp": ts, "ticker": ticker, "action": "EXIT", "price": price, "qty": pos.qty, "reason": signal.reason, "pnl": raw_pnl - cost})

    def _finalize_result(self, name: str) -> BacktestResult:
        trades_df, equity_ser = pd.DataFrame(self.trade_log), pd.Series(self.equity_history, index=self.time_history)
        metrics = {"total_pnl": 0.0, "win_rate": 0.0, "trade_count": 0, "sharpe": 0.0, "mdd": 0.0, "cagr": 0.0, "profit_factor": 0.0}
        if not trades_df.empty:
            pnl_exits = trades_df[trades_df["action"] == "EXIT"]["pnl"]
            if not pnl_exits.empty:
                total_return = equity_ser.iloc[-1] / self.initial_capital
                days = (equity_ser.index[-1] - equity_ser.index[0]).days
                years = max(days / 365.25, 1/252)
                metrics = {
                    "total_pnl": pnl_exits.sum(),
                    "win_rate": (pnl_exits > 0).mean(),
                    "trade_count": len(pnl_exits),
                    "sharpe": self._calc_sharpe(equity_ser),
                    "mdd": self._calc_mdd(equity_ser),
                    "cagr": (total_return ** (1/years)) - 1 if total_return > 0 else -1.0,
                    "profit_factor": pnl_exits[pnl_exits > 0].sum() / abs(pnl_exits[pnl_exits < 0].sum()) if any(pnl_exits < 0) else 99.0
                }
        return BacktestResult(name, trades_df, equity_ser, metrics)

    def _empty_result(self, name): return BacktestResult(name, pd.DataFrame(), pd.Series(), {})
    def _calc_sharpe(self, equity: pd.Series) -> float:
        rets = equity.pct_change().dropna()
        return np.sqrt(252 * 54) * (rets.mean() / rets.std()) if rets.std() != 0 else 0.0
    def _calc_mdd(self, equity: pd.Series) -> float:
        if equity.empty: return 0.0
        peak = equity.cummax()
        return ((equity - peak) / peak.replace(0, np.nan)).min()
