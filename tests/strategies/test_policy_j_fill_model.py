# 2026-07-26 Gemini CLI: Unit tests for PolicyJFillModel
import pytest

from strategies.futures.mts.counterfactual_evidence_schema import FillModel
from strategies.futures.mts.policy_j_fill_model import LegQuote, PolicyJFillModel


def test_fill_model_buy_near_sell_far_exit():
    # Trade direction: BUY_NEAR_SELL_FAR (LONG near, SHORT far)
    # Exit direction: SELL near (at bid), BUY far (at ask)
    near_quote = LegQuote(bid=22000.0, ask=22001.0, tick_size=1.0)
    far_quote = LegQuote(bid=22050.0, ask=22051.0, tick_size=1.0)

    # Executable (1 tick slippage)
    res_exec = PolicyJFillModel.compute_fill_prices(
        direction="BUY_NEAR_SELL_FAR",
        near_quote=near_quote,
        far_quote=far_quote,
        fill_model=FillModel.EXECUTABLE.value,
    )
    # SELL near: bid(22000) - 1 = 21999
    # BUY far: ask(22051) + 1 = 22052
    assert res_exec.near_fill_price == 21999.0
    assert res_exec.far_fill_price == 22052.0

    # Conservative (2 ticks slippage)
    res_cons = PolicyJFillModel.compute_fill_prices(
        direction="BUY_NEAR_SELL_FAR",
        near_quote=near_quote,
        far_quote=far_quote,
        fill_model=FillModel.CONSERVATIVE.value,
    )
    # SELL near: bid(22000) - 2 = 21998
    # BUY far: ask(22051) + 2 = 22053
    assert res_cons.near_fill_price == 21998.0
    assert res_cons.far_fill_price == 22053.0


def test_fill_model_sell_near_buy_far_exit():
    # Trade direction: SELL_NEAR_BUY_FAR (SHORT near, LONG far)
    # Exit direction: BUY near (at ask), SELL far (at bid)
    near_quote = LegQuote(bid=22000.0, ask=22001.0, tick_size=1.0)
    far_quote = LegQuote(bid=22050.0, ask=22051.0, tick_size=1.0)

    res_exec = PolicyJFillModel.compute_fill_prices(
        direction="SELL_NEAR_BUY_FAR",
        near_quote=near_quote,
        far_quote=far_quote,
        fill_model=FillModel.EXECUTABLE.value,
    )
    # BUY near: ask(22001) + 1 = 22002
    # SELL far: bid(22050) - 1 = 22049
    assert res_exec.near_fill_price == 22002.0
    assert res_exec.far_fill_price == 22049.0
