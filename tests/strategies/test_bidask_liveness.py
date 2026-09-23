from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import strategies.futures.monitor as module
from strategies.futures.monitor import FuturesMonitor


@pytest.fixture
def setup(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.api = object()
    mon.dry_run = False
    mon.ticker = 'TMF'
    mon.contract = SimpleNamespace(code='TMFJ6')
    mon.far_contract = SimpleNamespace(code='TMFK6')
    mon._canonical_near_codes = {'TMFJ6', 'MXFJ6'}
    mon._canonical_far_codes = {'TMFK6', 'MXFK6'}
    mon.market_data = {}
    mon.MONITOR = {}
    mon.is_trading_ready = True
    mon._tmf_feed_age_secs = lambda: 1.0
    mon._set_runtime_status = Mock()
    mon._append_mts_event = Mock()
    import core.shioaji_session as session
    import core.broker.shioaji_compat as compat
    ready = Mock()
    subscribe = Mock()
    monkeypatch.setattr(session, '_verify_request_readiness', ready)
    monkeypatch.setattr(compat, 'safe_subscribe', subscribe)
    return mon, clock, ready, subscribe


def quote(code, bid=100, ask=101):
    return SimpleNamespace(code=code, bid_price=[bid], ask_price=[ask], datetime=datetime.now())


def events(mon, name):
    return [c for c in mon._append_mts_event.call_args_list if c.args[0] == name]


def test_live_tick_dead_bidask_is_visible_from_production_status_path(setup):
    mon, clock, ready, subscribe = setup
    mon._refresh_runtime_status()
    clock[0] += 61
    mon._refresh_runtime_status()
    assert mon._bidask_health == 'RECOVERING'
    assert mon._bidask_stale_count == 1
    assert len(events(mon, 'BIDASK_STREAM_STALE')) == 1
    assert subscribe.call_count == 2
    assert ready.call_count == 1
    assert mon._set_runtime_status.call_args.args[0].value == 'DEGRADED'


def test_resubscribe_backoff_and_hard_cap(setup):
    mon, clock, ready, subscribe = setup
    mon._refresh_runtime_status()
    clock[0] = 161
    mon._refresh_runtime_status()
    for _ in range(10):
        mon._refresh_runtime_status()
    assert subscribe.call_count == 2
    for now in (221, 341, 581, 1000, 2000):
        clock[0] = now
        mon._refresh_runtime_status()
    assert subscribe.call_count == 6
    assert mon._bidask_recovery_attempts == 3
    assert mon._bidask_health == 'RESTART_REQUIRED'


def test_readiness_failure_never_subscribes_or_claims_recovery(setup):
    mon, clock, ready, subscribe = setup
    ready.side_effect = RuntimeError('NotReady')
    mon._refresh_runtime_status()
    for now in (161, 221, 341, 581):
        clock[0] = now
        mon._refresh_runtime_status()
    subscribe.assert_not_called()
    assert mon._bidask_health == 'RESTART_REQUIRED'


def test_real_both_leg_callbacks_required_for_recovery(setup):
    mon, clock, ready, subscribe = setup
    mon._refresh_runtime_status()
    clock[0] = 161
    mon._refresh_runtime_status()
    clock[0] = 162
    mon.on_bidask(None, quote('TMFJ6'))
    mon._refresh_runtime_status()
    assert mon._bidask_health != 'HEALTHY'
    mon.on_bidask(None, quote('TMFK6'))
    mon._refresh_runtime_status()
    assert mon._bidask_health == 'HEALTHY'
    assert ready.call_count == 2
    assert set(mon._exit_only_bbo_slots()) == {'near', 'far'}
    assert mon._exit_only_bbo_slots()['far']['ask'] == 101


def test_post_callback_readiness_failure_is_also_bounded(setup):
    mon, clock, ready, subscribe = setup
    mon._refresh_runtime_status()
    clock[0] = 161
    mon._refresh_runtime_status()
    clock[0] = 162
    mon.on_bidask(None, quote('TMFJ6'))
    mon.on_bidask(None, quote('TMFK6'))
    ready.side_effect = RuntimeError('NotReady')
    for _ in range(20):
        mon._refresh_runtime_status()
    assert ready.call_count == 2
    assert mon._bidask_health == 'READINESS_FAILED'


@pytest.mark.parametrize('code,bid,ask,reason', [
    ('OTHER', 100, 101, 'CONTRACT_MISMATCH'),
    ('TMFJ6', 102, 101, 'INVALID_BBO'),
    ('TMFJ6', float('inf'), float('inf'), 'INVALID_BBO'),
    ('TMFJ6', float('nan'), 101, 'INVALID_BBO'),
    ('TMFJ6', None, None, 'INVALID_BBO'),
])
def test_dropped_bidask_is_counted_rate_limited_and_never_cached(setup, code, bid, ask, reason):
    mon, clock, _, _ = setup
    for _ in range(3):
        mon.on_bidask(None, quote(code, bid, ask))
    assert mon.market_data == {}
    assert mon._bidask_drop_counts[reason] == 3
    assert len(events(mon, 'BIDASK_DROPPED')) == 1
    clock[0] += 61
    mon.on_bidask(None, quote(code, bid, ask))
    assert len(events(mon, 'BIDASK_DROPPED')) == 2
