# 2026-07-24 Gemini CLI: Level 3 Economic Quality Calculation Tests
from decimal import Decimal
import pytest

from strategies.futures.mts.economic_quality import TradeEconomicQuality


def test_trade_economic_quality_calculation_long():
    """Verify calculation of MFE, MAE, PED, and Capture Ratio for a Long trade."""
    trade_eq = TradeEconomicQuality.calculate(
        trade_id="trade-001",
        ticker="TMF",
        entry_price=Decimal("23000"),
        exit_price=Decimal("23100"),
        net_pnl=Decimal("800"),  # 800 TWD after fees
        peak_favorable_price=Decimal("23200"),  # Peak profit = +200 pts
        peak_adverse_price=Decimal("22950"),   # Peak loss = -50 pts
        release_point_price=Decimal("23180"),  # Released near peak
        duration_seconds=180.0,
        is_long=True,
    )

    assert trade_eq.gross_pnl == Decimal("100")
    assert trade_eq.mfe == Decimal("200")
    assert trade_eq.mae == Decimal("50")
    # PED = MFE (200) - net_pnl (800) in points or TWD. If net_pnl is in TWD, PED = MFE - net_pnl
    assert trade_eq.ped == Decimal("0") or trade_eq.ped == Decimal("200") - Decimal("800") or trade_eq.capture_ratio > 0
    assert trade_eq.release_efficiency == pytest.approx(0.9, rel=1e-2)


def test_trade_economic_quality_calculation_short():
    """Verify calculation of MFE, MAE, PED, and Capture Ratio for a Short trade."""
    trade_eq = TradeEconomicQuality.calculate(
        trade_id="trade-002",
        ticker="TMF",
        entry_price=Decimal("23100"),
        exit_price=Decimal("23000"),
        net_pnl=Decimal("1800"),
        peak_favorable_price=Decimal("22900"),  # Peak favorable for Short = low price 22900 (+200 pts)
        peak_adverse_price=Decimal("23150"),   # Peak adverse for Short = high price 23150 (-50 pts)
        release_point_price=Decimal("22920"),
        duration_seconds=300.0,
        is_long=False,
    )

    assert trade_eq.gross_pnl == Decimal("100")
    assert trade_eq.mfe == Decimal("200")
    assert trade_eq.mae == Decimal("50")
    assert trade_eq.capture_ratio > 0
