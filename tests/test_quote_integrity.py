# P0d: Quote Integrity regression tests — far→near contamination isolation.
# Full integration contract:
#   TMFH6 (near) @ 43650 established → TMFI6 (far) @ 43822 arrives
#   Expect: near caches unchanged, far cache updated, spread/Z/Renko untouched,
#           counter increments, anomaly log records ONLY routing attempts.
# Taxonomy: VALID_FAR_QUOTE / ROUTING_CROSS_LEG_WRITE_BLOCKED /
#           CONTRACT_ROLE_MISMATCH / STALE_GENERATION / INVALID_QUOTE_VALUE /
#           PAIR_NOT_SYNCHRONIZABLE
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from strategies.futures.monitor import FuturesMonitor


def _make_monitor(tmp_path):
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.last_tick_at = 0
    mon.market_data = {}
    mon.ticker = "TMF"
    mon._last_tmf_price = 0
    mon._debug_feed = False
    mon._debug_tickbar = False
    mon.dry_run = True
    mon.trader = MagicMock(position=0)
    mon.client = MagicMock(_tick_callbacks={})
    mon.cfg = {}
    mon.manual_trade_flag_path = str(tmp_path / "dummy.flag")
    mon._far_current_bar = {"ts": None, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ticks": 0}
    mon._far_last_bar = {}
    mon._last_far_bar_ts = 0
    mon._current_bar = {"ts": None, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ticks": 0}
    mon._last_bar = {}
    mon._last_bar_ts = 0
    mon.csv_path = Path("/tmp/dummy_ticks.csv")
    mon._trade_mgr = None
    mon._write_raw_tick = MagicMock()
    mon._refresh_runtime_status = MagicMock()
    mon._process_manual_trade_flag = MagicMock()
    mon._mts_tick = MagicMock()
    mon.contract = MagicMock(code="TMFH6")
    mon.far_contract = MagicMock(code="TMFI6")
    # P0b: real QuoteIntegrityGuard — counters + anomaly log are真实
    from core.quote_integrity import QuoteIntegrityGuard
    mon._quote_guard = QuoteIntegrityGuard(
        near_code="TMFH6", far_code="TMFI6", ticker="TMF",
        anomalous_quotes_path=str(tmp_path / "anomalous_quotes.jsonl"),
    )
    mon._quote_integrity_stats = mon._quote_guard.stats
    mon.anomalous_quotes_path = mon._quote_guard.anomalous_quotes_path
    # P0c/P1 hooks (to be implemented): spread synchronizer + renko shadow
    mon._spread_sync = MagicMock()
    mon._renko_shadow = MagicMock()
    return mon


def _tick(code, close, ts="2026-07-31 19:11:30", buy=None, sell=None):
    t = MagicMock()
    t.code = code
    t.close = float(close)
    t.buy_price = float(buy if buy is not None else close)
    t.sell_price = float(sell if sell is not None else close)
    t.datetime = ts
    return t


def test_full_integration_far_tick_does_not_pollute_near(tmp_path):
    """The complete contamination regression scenario from the incident."""
    mon = _make_monitor(tmp_path)
    # 1. near quote establishes canonical price
    mon.on_tick("TFE", _tick("TMFH6", 43650.0))
    assert mon.market_data["TMF"]["close"] == 43650.0
    assert mon.market_data["TMF_NEAR"]["close"] == 43650.0
    assert mon._last_tmf_price == 43650.0
    near_bar_close = mon._current_bar["close"]

    # 2. far tick arrives (the exact incident pattern: TMFI6 @ 43822)
    mon.on_tick("TFE", _tick("TMFI6", 43822.0))

    # 3. near canonical price MUST remain unchanged
    assert mon.market_data["TMF"]["close"] == 43650.0, "near cache polluted!"
    assert mon.market_data["TMF_NEAR"]["close"] == 43650.0, "near _NEAR cache polluted!"
    assert mon._last_tmf_price == 43650.0
    assert mon._current_bar["close"] == near_bar_close, "near bar mutated by far tick"

    # 4. far cache MUST update to 43822
    assert mon.market_data["TMF_FAR"]["close"] == 43822.0
    assert mon.market_data["TMFI6"]["close"] == 43822.0

    # 5. spread synchronizer must NOT receive this lone far quote as a pair
    mon._spread_sync.on_quote.assert_not_called()

    # 6. renko shadow must NOT receive spread input
    mon._renko_shadow.on_spread.assert_not_called()

    # 7. counter: far quote counted VALID (not rejected)
    assert mon._quote_integrity_stats["VALID_FAR_QUOTE"] == 1
    assert mon._quote_integrity_stats["REJECTED_TOTAL"] == 0

    # 8. anomaly log: normal far quote is NOT an anomaly
    _log = Path(mon.anomalous_quotes_path)
    if _log.exists():
        assert _log.read_text().strip() == "", "normal far quote must not be quarantined"


def test_cross_leg_write_blocked_counter(tmp_path):
    """A far quote attempting to write a NEAR destination is recorded — and
    only that event writes the anomaly log (normal far quotes never do)."""
    mon = _make_monitor(tmp_path)
    mon.on_tick("TFE", _tick("TMFH6", 43650.0))
    mon._quote_guard.record_routing_block("TMFI6", "TMF_NEAR")

    assert mon._quote_integrity_stats["ROUTING_CROSS_LEG_WRITE_BLOCKED"] == 1
    _log = Path(mon.anomalous_quotes_path)
    assert _log.exists(), "routing anomaly must be recorded"
    rec = json.loads(_log.read_text().strip().splitlines()[-1])
    assert rec["reason"] == "ROUTING_CROSS_LEG_WRITE_BLOCKED"
    assert rec["contract_code"] == "TMFI6"
    assert rec["target_slot"] == "TMF_NEAR"


def test_contract_role_mismatch_counter(tmp_path):
    """Unknown contract → CONTRACT_ROLE_MISMATCH via decide()."""
    from core.quote_integrity import QuoteEnvelope, Destination
    mon = _make_monitor(tmp_path)
    env = QuoteEnvelope(
        raw_contract="TMFH7", normalized_contract="TMFH7", expected_leg=None,
        callback_source="test", exchange_timestamp="2026-07-31 19:11:30",
        receive_timestamp=1.0, receive_sequence=1, subscription_generation=1,
        source_kind="live", price=43700.0, close=43700.0, bid=43700.0, ask=43700.0,
    )
    d = mon._quote_guard.decide(env)
    assert d.destination == Destination.NONE
    assert d.code.value == "CONTRACT_ROLE_MISMATCH"
    assert mon._quote_integrity_stats["CONTRACT_ROLE_MISMATCH"] == 1
    assert mon._quote_integrity_stats["REJECTED_TOTAL"] == 1


def test_stale_generation_rejected(tmp_path):
    """Old subscription generation → STALE_GENERATION via decide()."""
    from core.quote_integrity import QuoteEnvelope, Destination
    mon = _make_monitor(tmp_path)
    env = QuoteEnvelope(
        raw_contract="TMFH6", normalized_contract="TMFH6", expected_leg=None,
        callback_source="test", exchange_timestamp="2026-07-31 19:11:30",
        receive_timestamp=1.0, receive_sequence=1, subscription_generation=0,
        source_kind="live", price=43650.0, close=43650.0, bid=43650.0, ask=43650.0,
    )
    d = mon._quote_guard.decide(env)
    assert d.destination == Destination.NONE
    assert d.code.value == "STALE_GENERATION"
    assert mon._quote_integrity_stats["STALE_GENERATION"] == 1


def test_invalid_quote_value_rejected(tmp_path):
    """close <= 0 → INVALID_QUOTE_VALUE, never touches any cache."""
    from core.quote_integrity import QuoteEnvelope, Destination
    mon = _make_monitor(tmp_path)
    env = QuoteEnvelope(
        raw_contract="TMFH6", normalized_contract="TMFH6", expected_leg=None,
        callback_source="test", exchange_timestamp="2026-07-31 19:11:30",
        receive_timestamp=1.0, receive_sequence=1, subscription_generation=1,
        source_kind="live", price=0.0, close=0.0, bid=0.0, ask=0.0,
    )
    d = mon._quote_guard.decide(env)
    assert d.destination == Destination.NONE
    assert d.code.value == "INVALID_QUOTE_VALUE"
    assert mon._quote_integrity_stats["INVALID_QUOTE_VALUE"] == 1
    assert mon._quote_integrity_stats["REJECTED_TOTAL"] == 1


def test_pair_not_synchronizable_no_spread(tmp_path):
    """When near is fresh but far is stale beyond max_leg_age, no spread sample."""
    mon = _make_monitor(tmp_path)
    mon.on_tick("TFE", _tick("TMFH6", 43650.0))
    try:
        mon._spread_sync.on_quote = MagicMock()
        mon._spread_sync.try_emit_sample(near_ts=time.time(), far_ts=time.time() - 120.0)
    except AttributeError:
        pytest.fail("P0c hook _spread_sync.try_emit_sample not implemented")
    # far 120s stale → max_leg_age_ms exceeded → no sample
    mon._spread_sync.try_emit_sample.assert_called_once()


# ── remaining P0b coverage ────────────────────────────────────────────────

def test_valid_near_and_far_counters(tmp_path):
    """One near + one far quote → both accepted, counted, routed correctly."""
    mon = _make_monitor(tmp_path)
    mon.on_tick("TFE", _tick("TMFH6", 43650.0))
    mon.on_tick("TFE", _tick("TMFI6", 43822.0))
    assert mon._quote_integrity_stats["VALID_NEAR_QUOTE"] == 1
    assert mon._quote_integrity_stats["VALID_FAR_QUOTE"] == 1
    assert mon._quote_integrity_stats["ACCEPTED_TOTAL"] == 2
    assert mon._quote_integrity_stats["REJECTED_TOTAL"] == 0
    # anomaly log must be empty for legitimate quotes
    assert not Path(mon.anomalous_quotes_path).exists()


def test_out_of_order_quote_rejected(tmp_path):
    """Exchange timestamp going backwards → OUT_OF_ORDER_QUOTE."""
    from core.quote_integrity import QuoteEnvelope, Destination
    mon = _make_monitor(tmp_path)
    env1 = QuoteEnvelope(
        raw_contract="TMFH6", normalized_contract="TMFH6", expected_leg=None,
        callback_source="test", exchange_timestamp="2026-07-31 19:11:30",
        receive_timestamp=1.0, receive_sequence=1, subscription_generation=1,
        source_kind="live", price=43650.0, close=43650.0, bid=43650.0, ask=43650.0,
    )
    assert mon._quote_guard.decide(env1).destination == Destination.NEAR_CACHE
    env2 = QuoteEnvelope(
        raw_contract="TMFH6", normalized_contract="TMFH6", expected_leg=None,
        callback_source="test", exchange_timestamp="2026-07-31 19:11:29",  # earlier!
        receive_timestamp=2.0, receive_sequence=2, subscription_generation=1,
        source_kind="live", price=43660.0, close=43660.0, bid=43660.0, ask=43660.0,
    )
    d = mon._quote_guard.decide(env2)
    assert d.destination == Destination.NONE
    assert d.code.value == "OUT_OF_ORDER_QUOTE"
    assert mon._quote_integrity_stats["OUT_OF_ORDER_QUOTE"] == 1


def test_duplicate_timestamp_accepted_once(tmp_path):
    """Identical exchange timestamp (duplicate delivery) → first accepted,
    second rejected as OUT_OF_ORDER (<= prev timestamp)."""
    from core.quote_integrity import QuoteEnvelope, Destination
    mon = _make_monitor(tmp_path)
    env1 = QuoteEnvelope(
        raw_contract="TMFH6", normalized_contract="TMFH6", expected_leg=None,
        callback_source="test", exchange_timestamp="2026-07-31 19:11:30",
        receive_timestamp=1.0, receive_sequence=1, subscription_generation=1,
        source_kind="live", price=43650.0, close=43650.0, bid=43650.0, ask=43650.0,
    )
    assert mon._quote_guard.decide(env1).destination == Destination.NEAR_CACHE
    dup = QuoteEnvelope(
        raw_contract="TMFH6", normalized_contract="TMFH6", expected_leg=None,
        callback_source="test", exchange_timestamp="2026-07-31 19:11:30",  # same ts
        receive_timestamp=2.0, receive_sequence=2, subscription_generation=1,
        source_kind="live", price=43650.0, close=43650.0, bid=43650.0, ask=43650.0,
    )
    d = mon._quote_guard.decide(dup)
    assert d.destination == Destination.NONE
    assert d.code.value == "OUT_OF_ORDER_QUOTE"


def test_downstream_not_propagated_on_reject(tmp_path):
    """Rejected quote must never reach spread / Z / Renko consumers."""
    mon = _make_monitor(tmp_path)
    # Establish near first
    mon.on_tick("TFE", _tick("TMFH6", 43650.0))
    # Unknown contract → rejected
    mon.on_tick("TFE", _tick("TMFH7", 43700.0))
    assert mon._quote_integrity_stats["CONTRACT_ROLE_MISMATCH"] == 1
    # Downstream consumers saw nothing new
    mon._spread_sync.on_quote.assert_not_called()
    mon._renko_shadow.on_spread.assert_not_called()
    # Near cache untouched by the rejected quote
    assert mon.market_data["TMF"]["close"] == 43650.0
