# 2026-07-24 Gemini CLI: Minimal Acceptance Test Suite for Near/Far Market Data Routing Parity
import time
from unittest.mock import MagicMock, Mock
import pandas as pd
import pytest

from strategies.futures.monitor import FuturesMonitor


def create_mock_tick(code: str, close: float, dt_str: str = "2026-07-24 10:00:00"):
    tick = Mock()
    tick.code = code
    tick.close = close
    tick.datetime = dt_str
    tick.volume = 10
    tick.buy_price = close - 5.0
    tick.sell_price = close + 5.0
    return tick


@pytest.fixture
def dummy_near_contract():
    c = Mock()
    c.code = "TMFH6"
    c.delivery_date = "2026/08/20"
    return c


@pytest.fixture
def dummy_far_contract():
    c = Mock()
    c.code = "TMFI6"
    c.delivery_date = "2026/09/17"
    return c


@pytest.fixture
def mock_monitor(dummy_near_contract, dummy_far_contract):
    mon = FuturesMonitor(api=None, config_path="config/futures.yaml", dry_run=True)
    mon.contract = dummy_near_contract
    mon.far_contract = dummy_far_contract
    mon.market_data = {}
    mon._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
    mon._write_raw_tick = MagicMock()
    mon._accumulate_far_tick = MagicMock(wraps=mon._accumulate_far_tick)
    return mon


def test_case_1_near_exact_routing(mock_monitor):
    """Case 1: Near exact routing updates TMF_NEAR only."""
    tick = create_mock_tick("TMFH6", 44300.0)

    mock_monitor.on_tick("TFE", tick)

    # TMF_NEAR must be updated
    assert "TMF_NEAR" in mock_monitor.market_data
    assert mock_monitor.market_data["TMF_NEAR"]["close"] == 44300.0

    # TMF_FAR must be untouched
    assert "TMF_FAR" not in mock_monitor.market_data

    # Raw persistence called for near primary tick
    mock_monitor._write_raw_tick.assert_called_once_with(tick)


def test_case_2_far_exact_routing(mock_monitor):
    """Case 2: Far exact routing updates TMF_FAR only."""
    tick = create_mock_tick("TMFI6", 44050.0)

    mock_monitor.on_tick("TFE", tick)

    # TMF_FAR must be updated
    assert "TMF_FAR" in mock_monitor.market_data
    assert mock_monitor.market_data["TMF_FAR"]["close"] == 44050.0

    # TMF_NEAR must be untouched
    assert "TMF_NEAR" not in mock_monitor.market_data


def test_case_3_far_contract_none_fail_closed(mock_monitor):
    """Case 3: When far_contract=None, far tick does NOT write to near or far (fail-closed drop)."""
    mock_monitor.far_contract = None
    mock_monitor.contract = None  # Ensure no fallback primary code match

    tick = create_mock_tick("TMFI6", 44050.0)

    mock_monitor.on_tick("TFE", tick)

    # Neither near nor far cache should be updated
    assert "TMF_NEAR" not in mock_monitor.market_data
    assert "TMF_FAR" not in mock_monitor.market_data


def test_case_4_unknown_contract_isolation(mock_monitor):
    """Case 4: Unknown contract code (e.g. TXFH6 on TMF monitor) changes neither near nor far."""
    tick = create_mock_tick("TXFH6", 22000.0)

    mock_monitor.on_tick("TFE", tick)

    assert "TMF_NEAR" not in mock_monitor.market_data
    assert "TMF_FAR" not in mock_monitor.market_data


def test_case_5_downstream_parity(mock_monitor):
    """Case 5: Equal near/far ticks produce symmetric call counts."""
    near_tick = create_mock_tick("TMFH6", 44300.0)
    far_tick = create_mock_tick("TMFI6", 44050.0)

    mock_monitor.on_tick("TFE", near_tick)
    mock_monitor.on_tick("TFE", far_tick)

    # Both near and far market data slots must exist
    assert "TMF_NEAR" in mock_monitor.market_data
    assert "TMF_FAR" in mock_monitor.market_data
    assert mock_monitor.market_data["TMF_NEAR"]["close"] == 44300.0
    assert mock_monitor.market_data["TMF_FAR"]["close"] == 44050.0


def test_case_6_no_early_return_loss(mock_monitor):
    """Case 6: Far tick executes in-memory update and accumulate without silent drop."""
    far_tick = create_mock_tick("TMFI6", 44050.0)
    mock_monitor.on_tick("TFE", far_tick)

    # Accumulate far tick must be invoked
    mock_monitor._accumulate_far_tick.assert_called_once_with(far_tick)
    assert mock_monitor.market_data["TMF_FAR"]["close"] == 44050.0
