# 2026-07-26 Gemini CLI: Wave J2-B Pure Policy J Fill Pricing Model
from dataclasses import dataclass
from typing import Tuple

from strategies.futures.mts.counterfactual_evidence_schema import FillModel


@dataclass(frozen=True)
class LegQuote:
    bid: float
    ask: float
    tick_size: float = 1.0


@dataclass(frozen=True)
class FillPriceResult:
    near_fill_price: float
    far_fill_price: float
    near_slippage_pts: float
    far_slippage_pts: float
    total_friction_twd: float


class PolicyJFillModel:
    """
    Pure Fill Pricing Engine for Counterfactual Policy J Exit Valuation.
    Enforces strict bid/ask exit direction:
    - Exit LONG leg:  SELL at (bid - slippage_ticks * tick_size)
    - Exit SHORT leg: BUY at  (ask + slippage_ticks * tick_size)
    """

    @staticmethod
    def compute_fill_prices(
        direction: str,  # "BUY_NEAR_SELL_FAR" (LONG near, SHORT far) or "SELL_NEAR_BUY_FAR" (SHORT near, LONG far)
        near_quote: LegQuote,
        far_quote: LegQuote,
        fill_model: str = FillModel.EXECUTABLE.value,
        commission_twd: float = 68.0,
        exchange_fee_twd: float = 24.0,
        tax_twd: float = 0.0,
    ) -> FillPriceResult:
        """
        Compute hypothetical exit fill prices and total friction (net of fee/tax/slippage).
        """
        slippage_ticks = 1.0 if fill_model == FillModel.EXECUTABLE.value else (2.0 if fill_model == FillModel.CONSERVATIVE.value else 0.0)

        if fill_model == FillModel.IDEAL.value:
            near_fill = (near_quote.bid + near_quote.ask) / 2.0
            far_fill = (far_quote.bid + far_quote.ask) / 2.0
            near_slip_pts = 0.0
            far_slip_pts = 0.0
        else:
            if direction == "BUY_NEAR_SELL_FAR":
                # Currently: LONG near, SHORT far
                # Exit: SELL near (at bid), BUY far (at ask)
                near_fill = near_quote.bid - (slippage_ticks * near_quote.tick_size)
                far_fill = far_quote.ask + (slippage_ticks * far_quote.tick_size)
            elif direction == "SELL_NEAR_BUY_FAR":
                # Currently: SHORT near, LONG far
                # Exit: BUY near (at ask), SELL far (at bid)
                near_fill = near_quote.ask + (slippage_ticks * near_quote.tick_size)
                far_fill = far_quote.bid - (slippage_ticks * far_quote.tick_size)
            else:
                raise ValueError(f"Unknown direction: '{direction}'")

            near_slip_pts = slippage_ticks * near_quote.tick_size
            far_slip_pts = slippage_ticks * far_quote.tick_size

        total_friction = commission_twd + exchange_fee_twd + tax_twd

        return FillPriceResult(
            near_fill_price=near_fill,
            far_fill_price=far_fill,
            near_slippage_pts=near_slip_pts,
            far_slippage_pts=far_slip_pts,
            total_friction_twd=total_friction,
        )
