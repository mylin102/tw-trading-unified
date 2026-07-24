# 2026-07-24 Gemini CLI: Level 3 Economic Quality Metrics & Trade Analytics Engine
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TradeEconomicQuality:
    """Pure dataclass capturing Level 3 Economic Quality metrics for a completed trade.
    
    Metrics:
    - MFE (Maximum Favorable Excursion): Peak unrealized profit (points or TWD).
    - MAE (Maximum Adverse Excursion): Peak unrealized loss (points or TWD).
    - Realized PnL: Final closed net PnL after broker/exchange fees & tax.
    - PED (Profit Excursion Decay): MFE - Realized PnL (profit giveback from peak).
    - Capture Ratio: Realized PnL / MFE (efficiency of profit retention, 0.0 to 1.0+).
    - Release Efficiency: MFE at release point / Peak MFE.
    - Exit Efficiency: Realized PnL / MFE at final exit point.
    """
    trade_id: str
    ticker: str
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    mfe: Decimal  # Peak favorable profit
    mae: Decimal  # Peak adverse loss
    ped: Decimal  # MFE - net_pnl (profit giveback)
    capture_ratio: float  # net_pnl / mfe (0.0 to 1.0)
    release_efficiency: float
    exit_efficiency: float
    holding_duration_seconds: float

    @classmethod
    def calculate(
        cls,
        trade_id: str,
        ticker: str,
        entry_price: Decimal,
        exit_price: Decimal,
        net_pnl: Decimal,
        peak_favorable_price: Decimal,
        peak_adverse_price: Decimal,
        release_point_price: Decimal,
        duration_seconds: float,
        is_long: bool = True,
    ) -> "TradeEconomicQuality":
        """Compute pure Economic Quality metrics from trade prices and peak excursions."""
        multiplier = Decimal("1") if is_long else Decimal("-1")

        gross_pnl = (exit_price - entry_price) * multiplier
        
        # Calculate excursions relative to entry price
        if is_long:
            mfe = max(Decimal("0"), peak_favorable_price - entry_price)
            mae = max(Decimal("0"), entry_price - peak_adverse_price)
            release_mfe = max(Decimal("0"), release_point_price - entry_price)
        else:
            mfe = max(Decimal("0"), entry_price - peak_favorable_price)
            mae = max(Decimal("0"), peak_adverse_price - entry_price)
            release_mfe = max(Decimal("0"), entry_price - release_point_price)

        ped = max(Decimal("0"), mfe - net_pnl)

        capture_ratio = float(net_pnl / mfe) if mfe > 0 else (1.0 if net_pnl >= 0 else 0.0)
        release_eff = float(release_mfe / mfe) if mfe > 0 else 1.0
        exit_eff = float(net_pnl / mfe) if mfe > 0 else 1.0

        return cls(
            trade_id=trade_id,
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            mfe=mfe,
            mae=mae,
            ped=ped,
            capture_ratio=capture_ratio,
            release_efficiency=release_eff,
            exit_efficiency=exit_eff,
            holding_duration_seconds=duration_seconds,
        )
