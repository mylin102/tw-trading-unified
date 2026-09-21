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


def _tick_without_bbo(code: str, close: float):
    tick = create_mock_tick(code, close)
    tick.buy_price = None
    tick.sell_price = None
    return tick


def test_near_tick_without_bbo_preserves_cached_bidask(mock_monitor):
    cached = {"bid": 44299.0, "ask": 44301.0, "bidask_at": 123.0}
    mock_monitor.market_data["TMF"] = dict(cached)
    mock_monitor.market_data["TMF_NEAR"] = dict(cached)

    mock_monitor.on_tick("TFE", _tick_without_bbo("TMFH6", 44300.0))

    for key in ("TMF", "TMF_NEAR"):
        assert mock_monitor.market_data[key]["bid"] == 44299.0
        assert mock_monitor.market_data[key]["ask"] == 44301.0
        assert mock_monitor.market_data[key]["bidask_at"] == 123.0
        assert "tick_bbo_at" not in mock_monitor.market_data[key]
        assert mock_monitor.market_data[key]["close"] == 44300.0


def test_far_tick_without_bbo_preserves_cached_bidask(mock_monitor):
    mock_monitor.market_data["TMF_FAR"] = {
        "bid": 44049.0, "ask": 44051.0, "bidask_at": 456.0,
    }

    mock_monitor.on_tick("TFE", _tick_without_bbo("TMFI6", 44050.0))

    slot = mock_monitor.market_data["TMF_FAR"]
    assert slot["bid"] == 44049.0
    assert slot["ask"] == 44051.0
    assert slot["bidask_at"] == 456.0
    assert "tick_bbo_at" not in slot
    assert slot["close"] == 44050.0


def test_far_tick_bar_keeps_cached_near_and_far_bidask(mock_monitor):
    mock_monitor.dry_run = False
    mock_monitor.cfg["mts"]["enabled"] = True
    mock_monitor._mts_tick = MagicMock()
    mock_monitor.market_data["TMF"] = {
        "bid": 44299.0, "ask": 44301.0, "bidask_at": 123.0,
        "local_arrival_at": time.time(),
    }
    mock_monitor.market_data["TMF_FAR"] = {
        "bid": 44049.0, "ask": 44051.0, "bidask_at": 456.0,
        "local_arrival_at": time.time(),
    }

    mock_monitor.on_tick("TFE", _tick_without_bbo("TMFI6", 44050.0))

    bar = mock_monitor._mts_tick.call_args.kwargs["enriched_bar"]
    assert (bar["near_bid"], bar["near_ask"]) == (44299.0, 44301.0)
    assert (bar["far_bid"], bar["far_ask"]) == (44049.0, 44051.0)


def test_bar_without_bidask_never_uses_close_and_gate_reports_missing(
        mock_monitor, monkeypatch):
    mock_monitor.dry_run = False
    mock_monitor.cfg["mts"]["enabled"] = True
    mock_monitor._mts_tick = MagicMock()
    mock_monitor.market_data.clear()

    mock_monitor.on_tick("TFE", _tick_without_bbo("TMFI6", 44050.0))

    bar = mock_monitor._mts_tick.call_args.kwargs["enriched_bar"]
    assert bar["far_close_rt"] == 44050.0
    assert all(bar.get(key) is None for key in (
        "near_bid", "near_ask", "far_bid", "far_ask"))
    assert 44050.0 not in tuple(bar.get(key) for key in (
        "near_bid", "near_ask", "far_bid", "far_ask"))

    captured = []
    import core.entry_research_store as store
    monkeypatch.setattr(store, "record_entry_observation",
                        lambda audit, **kw: captured.append(audit) or True)
    bar.update({"spread_z": 2.0, "entry_z": 1.0})
    strategy = type("Strategy", (), {"_entry_z": 1.0, "_trade_id": None})()
    mock_monitor._record_mts_entry_research_candidate(
        strategy, bar, pd.Timestamp("2026-09-21 10:00:00"))
    assert captured[-1]["decision"] == "REJECT"
    assert captured[-1]["rejection_reason"] == "BBO_MISSING"
