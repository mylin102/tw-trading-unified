# 2026-10-12 Codex: true strategy emission + dispatcher quote rejection retry.
import pytest
from datetime import datetime
from types import SimpleNamespace
from tests.strategies.test_mts_crossday_policy_j import held_paper
from tests.strategies.test_mts_paper_exit_width import run_release
from tests.strategies.test_mts_paper_exit_bbo import paper_exit


def test_unsent_paper_release_retries_when_quote_recovers(held_paper, paper_exit, monkeypatch):
    strategy, signal = run_release(held_paper, monkeypatch)
    assert signal is not None
    mon, _, bar = paper_exit
    bar["bbo_near_bid"] = None
    mon._submit_mts_order_signal(signal, strategy, bar, ts=None)
    assert mon.order_mgr.submit.call_count == 0
    assert strategy._has_position is True
    assert strategy._lifecycle == "OPEN"
    strategy, retry = run_release(held_paper, monkeypatch)
    assert retry is not None
    assert retry.action == "PARTIAL_EXIT"


def test_pending_paper_order_never_rearmed_by_bad_quote(held_paper, paper_exit, monkeypatch):
    strategy, signal = run_release(held_paper, monkeypatch)
    mon, _, bar = paper_exit
    mon._pending_lifecycle_orders["existing"] = {"trade_id": strategy._trade_id}
    bar["bbo_near_bid"] = None
    mon._submit_mts_order_signal(signal, strategy, bar, ts=None)
    assert mon.order_mgr.submit.call_count == 0
    assert strategy._lifecycle == "RELEASE_NEAR"


def test_durably_attempted_paper_order_never_rearmed(held_paper, paper_exit, monkeypatch, tmp_path):
    # 2026-10-12 Codex: durable submit evidence protects restart/in-flight cases.
    from core.exit_intent import IntentLog
    strategy, signal = run_release(held_paper, monkeypatch)
    mon, _, bar = paper_exit
    log = IntentLog(str(tmp_path))
    iid = log.create(strategy._trade_id, "COMBINED_EXIT", leg="NEAR")
    def crash_after_attempt(*args):
        raise RuntimeError("simulated crash after durable attempt, before local I/O")
    with pytest.raises(RuntimeError):
        log.submit_leg(iid, "NEAR", mon.order_mgr, submit_hook=crash_after_attempt)
    bar["bbo_near_bid"] = None
    mon._submit_mts_order_signal(signal, strategy, bar, ts=None)
    assert strategy._lifecycle == "RELEASE_NEAR"
    assert log.get(iid)["legs"]["NEAR"]["status"] == "SUBMIT_ATTEMPTED"
    assert mon.order_mgr.submit.call_count == 0


def test_hvwap_unsent_quote_rejection_retries_same_trade(held_paper, paper_exit):
    from strategies.plugins.futures.active.mts_hvwap_candidate import (
        HvwapStatus, Regime60m, Signal15m,
    )

    strategy, bar, _, _ = held_paper
    strategy._hvwap_armed = True
    strategy._hvwap_baseline_clear_tick = True
    strategy._hvwap_last_verdict = SimpleNamespace(
        status=HvwapStatus.ALIGNED_PASS, regime_60m=Regime60m.RANGING,
        signal_15m=Signal15m.CONFIRMED_CONTINUATION,
        hypothetical_release_leg="NEAR", decision_ts="incident-fixture",
        session_label="2026-10-13", retained_direction="BULLISH",
        consecutive_confirmed_bars=2,
    )
    strategy._max_quote_age_ms = 30000
    mon, _, dispatch_bar = paper_exit
    quote_bar = dict(bar, near_bid=49998.0, near_ask=50000.0,
                     near_tick_age_ms=100.0)
    signal = strategy._hvwap_maybe_paper_release(
        quote_bar, datetime.now(), quote_bar["near_close"], quote_bar["far_close"])
    assert signal is not None
    dispatch_bar["bbo_near_bid"] = None
    mon._submit_mts_order_signal(signal, strategy, dispatch_bar, ts=None)
    assert mon.order_mgr.submit.call_count == 0

    retry = strategy._hvwap_maybe_paper_release(
        quote_bar, datetime.now(), quote_bar["near_close"], quote_bar["far_close"])
    assert retry is not None
    assert retry.action == "PARTIAL_EXIT"
    assert strategy._trade_id == "mts-auto-151001-499"

    mon._pending_lifecycle_orders["inflight"] = {"trade_id": strategy._trade_id}
    mon._submit_mts_order_signal(retry, strategy, dispatch_bar, ts=None)
    assert strategy._hvwap_release_sent_trade_id == strategy._trade_id
    assert strategy._release_pending_mono > 0
    assert strategy._hvwap_maybe_paper_release(
        quote_bar, datetime.now(), quote_bar["near_close"], quote_bar["far_close"]) is None
