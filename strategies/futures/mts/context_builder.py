# 2026-07-24 Gemini CLI: Wave 0 Anti-Corruption Layer (ACL) - SpreadContext & ContextBuilder
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from .contracts import Leg, Side
from .economics import ContractEconomics


@dataclass(frozen=True)
class LegSnapshot:
    """Immutable snapshot of a single spread leg's current state."""
    leg: Leg
    contract: str
    side: Side
    qty: int
    entry_price: Decimal
    current_price: Decimal
    high_price: Decimal
    low_price: Decimal
    unrealized_pnl_twd: int
    mfe_twd: int
    mae_twd: int


@dataclass(frozen=True)
class SpreadContext:
    """Pure immutable context data passed into ExitPolicy.evaluate().
    
    Contains ZERO broker objects, ZERO wall clock calls, and ZERO filesystem references.
    """
    event_time_ns: int
    session: str  # "DAY" | "NIGHT"
    economics: ContractEconomics
    near_leg: LegSnapshot
    far_leg: LegSnapshot
    spread_z: float
    spread_atr: float
    combined_unrealized_pnl_twd: int
    realized_pnl_twd: int
    estimated_exit_friction_twd: int
    quote_valid: bool
    broker_health_valid: bool

    @property
    def combined_net_pnl_twd(self) -> int:
        """Combined net PnL considering unrealized, realized, and estimated friction."""
        return self.combined_unrealized_pnl_twd + self.realized_pnl_twd - self.estimated_exit_friction_twd


class SpreadContextBuilder:
    """Anti-Corruption Layer (ACL) Builder.
    
    Converts raw runtime data (dictionaries, tickers, prices) into an immutable SpreadContext.
    """

    @staticmethod
    def build_context(
        *,
        event_time_ns: int,
        session: str,
        ticker: str,
        quantity: int,
        near_contract: str,
        near_side: Side,
        near_entry_price: Decimal,
        near_current_price: Decimal,
        near_high_price: Decimal,
        near_low_price: Decimal,
        far_contract: str,
        far_side: Side,
        far_entry_price: Decimal,
        far_current_price: Decimal,
        far_high_price: Decimal,
        far_low_price: Decimal,
        spread_z: float,
        spread_atr: float,
        realized_pnl_twd: int = 0,
        quote_valid: bool = True,
        broker_health_valid: bool = True,
    ) -> SpreadContext:
        """Build immutable SpreadContext with precise Decimal economics calculation."""
        economics = ContractEconomics.from_ticker(ticker, quantity)
        
        # Calculate leg unrealized PnLs using Decimal contract point value
        near_diff = (near_current_price - near_entry_price) if near_side == Side.LONG else (near_entry_price - near_current_price)
        near_upl = int(near_diff * economics.point_value_twd * Decimal(quantity))

        far_diff = (far_current_price - far_entry_price) if far_side == Side.LONG else (far_entry_price - far_current_price)
        far_upl = int(far_diff * economics.point_value_twd * Decimal(quantity))

        near_mfe_diff = (near_high_price - near_entry_price) if near_side == Side.LONG else (near_entry_price - near_low_price)
        near_mfe = int(near_mfe_diff * economics.point_value_twd * Decimal(quantity))
        
        near_mae_diff = (near_entry_price - near_low_price) if near_side == Side.LONG else (near_high_price - near_entry_price)
        near_mae = int(near_mae_diff * economics.point_value_twd * Decimal(quantity))

        far_mfe_diff = (far_high_price - far_entry_price) if far_side == Side.LONG else (far_entry_price - far_low_price)
        far_mfe = int(far_mfe_diff * economics.point_value_twd * Decimal(quantity))
        
        far_mae_diff = (far_entry_price - far_low_price) if far_side == Side.LONG else (far_high_price - far_entry_price)
        far_mae = int(far_mae_diff * economics.point_value_twd * Decimal(quantity))

        near_snap = LegSnapshot(
            leg=Leg.NEAR,
            contract=near_contract,
            side=near_side,
            qty=quantity,
            entry_price=near_entry_price,
            current_price=near_current_price,
            high_price=near_high_price,
            low_price=near_low_price,
            unrealized_pnl_twd=near_upl,
            mfe_twd=near_mfe,
            mae_twd=near_mae,
        )

        far_snap = LegSnapshot(
            leg=Leg.FAR,
            contract=far_contract,
            side=far_side,
            qty=quantity,
            entry_price=far_entry_price,
            current_price=far_current_price,
            high_price=far_high_price,
            low_price=far_low_price,
            unrealized_pnl_twd=far_upl,
            mfe_twd=far_mfe,
            mae_twd=far_mae,
        )

        estimated_friction = int(
            economics.calculate_roundtrip_friction_twd(far_current_price)
        )

        return SpreadContext(
            event_time_ns=event_time_ns,
            session=session.upper(),
            economics=economics,
            near_leg=near_snap,
            far_leg=far_snap,
            spread_z=spread_z,
            spread_atr=spread_atr,
            combined_unrealized_pnl_twd=near_upl + far_upl,
            realized_pnl_twd=realized_pnl_twd,
            estimated_exit_friction_twd=estimated_friction,
            quote_valid=quote_valid,
            broker_health_valid=broker_health_valid,
        )
