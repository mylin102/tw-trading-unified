# 2026-07-24 Gemini CLI: Wave 0 Contract Economics Model (Decimal Precision)
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ContractEconomics:
    """Immutable contract economics specification for exact TWD PnL and friction calculation.
    
    Eliminates broker object (Shioaji) dependency and floating point inaccuracy in policy evaluations.
    """
    ticker: str
    point_value_twd: Decimal
    quantity: int
    commission_per_side_twd: Decimal
    tax_rate: Decimal
    minimum_tick: Decimal

    def calculate_roundtrip_friction_twd(self, current_price: Decimal) -> Decimal:
        """Calculate estimated total roundtrip friction (commissions + estimated transaction tax)."""
        commissions = self.commission_per_side_twd * Decimal(2) * Decimal(self.quantity)
        # Taiwan Futures Tax: Price * Point Value * Tax Rate * 2 sides
        tax = (current_price * self.point_value_twd * self.tax_rate * Decimal(2) * Decimal(self.quantity)).quantize(Decimal("1"))
        return commissions + tax

    @classmethod
    def from_ticker(cls, ticker: str, quantity: int = 1) -> "ContractEconomics":
        """Factory method deriving economics from contract ticker without hardcoding."""
        ticker_upper = ticker.upper()
        if "TMF" in ticker_upper:
            # TMF: 10 TWD / pt, ~20 TWD commission
            return cls(
                ticker=ticker_upper,
                point_value_twd=Decimal("10"),
                quantity=quantity,
                commission_per_side_twd=Decimal("20"),
                tax_rate=Decimal("0.00002"),
                minimum_tick=Decimal("1"),
            )
        elif "MTX" in ticker_upper or "MXF" in ticker_upper:
            # MTX: 50 TWD / pt, ~30 TWD commission
            return cls(
                ticker=ticker_upper,
                point_value_twd=Decimal("50"),
                quantity=quantity,
                commission_per_side_twd=Decimal("30"),
                tax_rate=Decimal("0.00002"),
                minimum_tick=Decimal("1"),
            )
        elif "TX" in ticker_upper:
            # TX: 200 TWD / pt, ~50 TWD commission
            return cls(
                ticker=ticker_upper,
                point_value_twd=Decimal("200"),
                quantity=quantity,
                commission_per_side_twd=Decimal("50"),
                tax_rate=Decimal("0.00002"),
                minimum_tick=Decimal("1"),
            )
        else:
            raise ValueError(f"Unsupported ticker for ContractEconomics: {ticker}")
