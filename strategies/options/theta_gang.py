"""
ThetaGang strategies for TXO options — sell premium, collect theta.
Integrates with existing options monitor via auto-regime switching.

Strategies:
  - Credit Spread (Bull Put / Bear Call)
  - Iron Condor (Bull Put + Bear Call)
  - Short Strangle (naked, higher risk)

Entry: squeeze_on (low vol compression) → sell premium
Exit: target profit %, max loss %, DTE floor, or squeeze release
"""
import datetime
from dataclasses import dataclass, field
from typing import Optional, List
from rich.console import Console

console = Console()


@dataclass
class SpreadLeg:
    side: str          # "C" or "P"
    strike: float
    action: str        # "SELL" or "BUY"
    premium: float = 0.0
    contract: object = None


@dataclass
class SpreadPosition:
    strategy: str      # "bull_put_spread", "bear_call_spread", "iron_condor", "short_strangle"
    legs: List[SpreadLeg] = field(default_factory=list)
    entry_time: datetime.datetime = None
    net_credit: float = 0.0
    max_loss: float = 0.0
    quantity: int = 1

    @property
    def is_open(self):
        return self.entry_time is not None and self.net_credit > 0


def select_strikes(spot, strike_rounding, strategy, wing_width=200, otm_offset=200):
    """
    Select strikes for spread strategies.
    spot: current underlying price
    strike_rounding: TXO strike interval (100 for TXO)
    wing_width: distance between spread legs
    otm_offset: how far OTM to place short strike
    """
    def round_strike(p):
        return round(p / strike_rounding) * strike_rounding

    round_strike(spot)

    if strategy == "bull_put_spread":
        short_put = round_strike(spot - otm_offset)
        long_put = short_put - wing_width
        return [
            SpreadLeg("P", short_put, "SELL"),
            SpreadLeg("P", long_put, "BUY"),
        ]

    elif strategy == "bear_call_spread":
        short_call = round_strike(spot + otm_offset)
        long_call = short_call + wing_width
        return [
            SpreadLeg("C", short_call, "SELL"),
            SpreadLeg("C", long_call, "BUY"),
        ]

    elif strategy == "iron_condor":
        short_put = round_strike(spot - otm_offset)
        long_put = short_put - wing_width
        short_call = round_strike(spot + otm_offset)
        long_call = short_call + wing_width
        return [
            SpreadLeg("P", short_put, "SELL"),
            SpreadLeg("P", long_put, "BUY"),
            SpreadLeg("C", short_call, "SELL"),
            SpreadLeg("C", long_call, "BUY"),
        ]

    elif strategy == "short_strangle":
        short_put = round_strike(spot - otm_offset)
        short_call = round_strike(spot + otm_offset)
        return [
            SpreadLeg("P", short_put, "SELL"),
            SpreadLeg("C", short_call, "SELL"),
        ]

    return []


def price_spread(legs, bs_fn, spot, r, sigma, dte_years):
    """
    Price a spread using a BS pricing function.
    Returns (net_credit, max_loss, leg_details).
    """
    total_credit = 0.0
    total_debit = 0.0
    details = []

    for leg in legs:
        opt_type = leg.side
        res = bs_fn(spot, leg.strike, dte_years, r, sigma, opt_type)
        premium = res["price"]
        leg.premium = premium

        if leg.action == "SELL":
            total_credit += premium
        else:
            total_debit += premium

        details.append({
            "side": leg.side, "strike": leg.strike, "action": leg.action,
            "premium": round(premium, 1), "delta": round(res.get("delta", 0), 4),
        })

    net_credit = total_credit - total_debit

    # Max loss for spreads: take the wider side only (not sum)
    strikes_by_side = {}
    for leg in legs:
        strikes_by_side.setdefault(leg.side, []).append(leg.strike)

    max_loss = 0
    for side, strikes in strikes_by_side.items():
        if len(strikes) >= 2:
            width = abs(max(strikes) - min(strikes))
            max_loss = max(max_loss, width)  # take wider side
    max_loss = max_loss - net_credit if max_loss > 0 else float('inf')

    return net_credit, max_loss, details


def should_enter_theta(squeeze_on, iv, iv_rank_pct=None, min_iv=0.18, min_dte=5):
    """
    ThetaGang entry conditions:
    - Squeeze ON (vol compression → premium overpriced relative to realized vol)
    - IV above minimum (enough premium to collect)
    - Optional: IV rank/percentile high
    """
    if not squeeze_on:
        return False
    if iv < min_iv:
        return False
    if iv_rank_pct is not None and iv_rank_pct < 30:
        return False
    return True


def should_exit_theta(position, current_value, dte_days, cfg):
    """
    ThetaGang exit conditions:
    - Target profit reached (e.g., 50% of max credit)
    - Max loss reached
    - DTE too low (gamma risk)
    - Squeeze released (vol expanding, get out)
    """
    if not position.is_open:
        return False, ""

    profit_pct = (position.net_credit - current_value) / position.net_credit if position.net_credit > 0 else 0
    target = cfg.get("take_profit_pct", 0.50)
    max_loss_pct = cfg.get("max_loss_pct", 1.0)
    min_dte = cfg.get("min_dte_exit", 3)

    if profit_pct >= target:
        return True, f"TP {profit_pct:.0%} >= {target:.0%}"

    if position.max_loss > 0:
        loss_pct = (current_value - position.net_credit) / position.max_loss
        if loss_pct >= max_loss_pct:
            return True, f"SL {loss_pct:.0%} >= {max_loss_pct:.0%}"

    if dte_days <= min_dte:
        return True, f"DTE {dte_days:.1f} <= {min_dte}"

    return False, ""


class ThetaGangManager:
    """
    Manages ThetaGang positions within the existing options monitor.
    Call from run_strategy_logic() when regime is ranging/squeeze.
    """

    def __init__(self, cfg, bs_fn, strike_rounding=100):
        self.cfg = cfg.get("theta_gang", {})
        self.bs_fn = bs_fn
        self.strike_rounding = strike_rounding
        self.position: Optional[SpreadPosition] = None
        self.strategy = self.cfg.get("strategy", "iron_condor")
        self.wing_width = self.cfg.get("wing_width", 200)
        self.otm_offset = self.cfg.get("otm_offset", 200)
        self.quantity = self.cfg.get("quantity", 1)
        self.r = self.cfg.get("risk_free_rate", 0.02)

    def evaluate_entry(self, spot, iv, dte_years, squeeze_on):
        """Check if we should open a ThetaGang position."""
        if self.position and self.position.is_open:
            return None

        if not should_enter_theta(squeeze_on, iv, min_iv=self.cfg.get("min_iv", 0.18)):
            return None

        if dte_years * 365 < self.cfg.get("min_dte_entry", 7):
            return None

        legs = select_strikes(spot, self.strike_rounding, self.strategy,
                              self.wing_width, self.otm_offset)
        if not legs:
            return None

        net_credit, max_loss, details = price_spread(
            legs, self.bs_fn, spot, self.r, iv, dte_years)

        # Minimum credit filter
        min_credit = self.cfg.get("min_credit", 30)
        if net_credit < min_credit:
            return None
        
        # GSD fix: 確保net_credit有效
        if net_credit <= 0:
            console.print(f"[yellow]⚠️ ThetaGang: net_credit={net_credit} <= 0, rejecting entry[/yellow]")
            return None

        return {
            "strategy": self.strategy,
            "legs": legs,
            "net_credit": net_credit,
            "max_loss": max_loss,
            "details": details,
        }

    def open_position(self, entry_info):
        """Record a new ThetaGang position."""
        self.position = SpreadPosition(
            strategy=entry_info["strategy"],
            legs=entry_info["legs"],
            entry_time=datetime.datetime.now(),
            net_credit=entry_info["net_credit"],
            max_loss=entry_info["max_loss"],
            quantity=self.quantity,
        )
        return self.position

    def evaluate_exit(self, spot, iv, dte_years, squeeze_on):
        """Check if we should close the ThetaGang position."""
        if not self.position or not self.position.is_open:
            return None

        # Reprice current spread value
        current_value = 0
        for leg in self.position.legs:
            res = self.bs_fn(spot, leg.strike, dte_years, self.r, iv, leg.side)
            val = res["price"]
            if leg.action == "SELL":
                current_value += val
            else:
                current_value -= val

        exit_cfg = self.cfg
        should_exit, reason = should_exit_theta(
            self.position, current_value, dte_years * 365, exit_cfg)

        # Also exit if squeeze releases (vol expanding)
        if not squeeze_on and self.cfg.get("exit_on_squeeze_release", True):
            should_exit = True
            reason = "SQUEEZE_RELEASE (vol expanding)"

        if should_exit:
            # 計算淨PnL（包含交易成本）
            gross_pnl = self.position.net_credit - current_value
            # 交易成本：進出各一次
            # 假設每邊手續費20元，交易所費用5元，稅0.1%
            broker_fee = 20 * 2 * self.position.quantity  # 進出各一次
            exchange_fee = 5 * 2 * self.position.quantity
            tax_rate = 0.001
            tax = (self.position.net_credit + current_value) * 50 * tax_rate * self.position.quantity
            total_cost = broker_fee + exchange_fee + tax
            net_pnl = gross_pnl * 50 - total_cost  # 轉換為台幣，減去成本
            
            # 轉換回點數（四捨五入）
            pnl_points = round(net_pnl / 50)
            
            return {"reason": reason, "current_value": current_value,
                    "pnl": pnl_points, "gross_pnl": gross_pnl, "cost": total_cost}
        return None

    def close_position(self):
        """Clear the position."""
        pos = self.position
        self.position = None
        return pos
