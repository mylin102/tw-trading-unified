"""2026-10-13 Codex: confirmed PAPER peak survives a rejected exit + restart."""
import json
from datetime import datetime, timedelta

import pytest

from core.strategy_context import StrategyContext, MarketData, PositionView
from strategies.plugins.futures.active import tmf_spread as mod
from tests.strategies.test_mts_crossday_policy_j import held_paper
from tests.strategies.test_mts_paper_exit_bbo import paper_exit
from tests.strategies.test_mts_z_state_roundtrip import state_case, enter


def restart_case(held_paper, monkeypatch, tmp_path, records=None):
    # 2026-10-13 Codex: mirror the production PAPER namespace injection;
    # offline runtime_paths deliberately returns None without this boundary.
    from core import runtime_paths
    monkeypatch.setattr(runtime_paths, 'mts_data_mode', lambda: 'paper')
    s, bar, cfg, events = held_paper
    s._entry_ts = datetime.now() - timedelta(days=6)
    s._entry_guard_start_ms = s._entry_ts.timestamp() * 1000
    s._entry_ts_ms = s._entry_guard_start_ms
    s._params.update(enable_combined_upl_trail=True,
                     combined_upl_activation_net_pnl_twd=200.,
                     combined_upl_giveback_twd=50.)
    # Actual incident shape: state still OPEN but its displayed peak has been
    # lowered by a post-restart baseline. Candidate-only spikes aren't proof.
    s._peak_net_exit_pnl_twd = -32.
    confirmed = dict(event='POLICY_J_PEAK_CONFIRMED', trade_id=s._trade_id,
                     ts=(datetime.now()-timedelta(hours=1)).isoformat(),
                     durable_peak=1138., candidate_peak=1138., candidate_count=1)
    path = tmp_path / 'events.jsonl'
    path.write_text(''.join(json.dumps(r)+'\n' for r in (records if records is not None else [confirmed])))
    monkeypatch.setattr(mod, '_MTS_EVENT_LOG', str(path))
    return s, bar, cfg, events, confirmed, path


def tick(s, bar, cfg):
    return s.on_bar(StrategyContext(market=MarketData(last_bar=bar, ticker='TMF'),
                                   position=PositionView(size=2), config=cfg))


def test_confirmed_peak_recovered_and_real_on_bar_retries_combined_exit(held_paper, monkeypatch, tmp_path):
    s, bar, cfg, events, _, path = restart_case(held_paper, monkeypatch, tmp_path)
    before = path.read_bytes()
    tick(s, bar, cfg)  # first fresh mark retains the normal one-tick guard
    assert s._pj_durable_peak == 1138.
    signal = tick(s, bar, cfg)
    assert signal is not None and signal.action == 'EXIT'
    assert 'COMBINED_EXIT' in signal.reason
    assert s._has_position is True  # signal is not a fill
    assert path.read_bytes() == before
    assert any(n == 'POLICY_J_PEAK_RESTORED' for n, _ in events)


def test_recovered_exit_bad_bbo_rearms_then_reemits(held_paper, paper_exit, monkeypatch, tmp_path):
    s, bar, cfg, _, _, _ = restart_case(held_paper, monkeypatch, tmp_path)
    tick(s, bar, cfg)
    signal = tick(s, bar, cfg)
    assert signal is not None
    mon, _, dispatch_bar = paper_exit
    dispatch_bar['bbo_near_bid'] = None
    mon._submit_mts_order_signal(signal, s, dispatch_bar, ts=None)
    assert mon.order_mgr.submit.call_count == 0
    assert s._has_position and s._lifecycle == 'OPEN'
    retry = tick(s, bar, cfg)
    assert retry is not None and retry.action == 'EXIT'
    assert s._pj_durable_peak == 1138.


@pytest.mark.parametrize('case', ['candidate', 'foreign', 'nan', 'bool', 'future', 'before_entry',
                                 'single_leg', 'live', 'unknown', 'live_allowed', 'quarantine'])
def test_untrusted_or_out_of_scope_peak_never_activates_exit(held_paper, monkeypatch, tmp_path, case):
    s, bar, cfg, events, row, path = restart_case(held_paper, monkeypatch, tmp_path)
    if case == 'candidate': row['event'] = 'POLICY_J_PEAK_CANDIDATE'
    elif case == 'foreign': row['trade_id'] = 'OTHER'
    elif case == 'nan': row['durable_peak'] = float('nan')
    elif case == 'bool': row['durable_peak'] = True
    elif case == 'future': row['ts'] = (datetime.now()+timedelta(days=1)).isoformat()
    elif case == 'before_entry': row['ts'] = (s._entry_ts-timedelta(days=1)).isoformat()
    elif case == 'single_leg': row['phase'] = 'SINGLE_LEG'
    elif case == 'live': s._hvwap_execution_mode = 'live_active'
    elif case == 'unknown': s._hvwap_execution_mode = None
    elif case == 'live_allowed': s._hvwap_live_order_allowed = True
    else:
        s._entry_ts = datetime.now()
        s._entry_guard_start_ms = s._entry_ts.timestamp()*1000
        s._entry_ts_ms = s._entry_guard_start_ms
    path.write_text(json.dumps(row)+'\n')
    tick(s, bar, cfg)
    tick(s, bar, cfg)
    assert s._pj_durable_peak != 1138.
    assert not any(n == 'POLICY_J_PEAK_RESTORED' for n, _ in events)


def test_unreadable_evidence_keeps_normal_baseline_and_never_retries_io(held_paper, monkeypatch, tmp_path):
    s, bar, cfg, events, _, path = restart_case(held_paper, monkeypatch, tmp_path)
    path.unlink()
    path.mkdir()  # genuine OSError on open
    tick(s, bar, cfg)
    tick(s, bar, cfg)
    assert s._pj_durable_peak != 1138.
    assert not any(n == 'POLICY_J_PEAK_RESTORED' for n, _ in events)


def test_actual_json_and_fills_restore_then_on_bar_emits_unfilled_exit(state_case, monkeypatch, tmp_path):
    # 2026-10-13 Codex: exercise the actual restart reader, not just an OPEN
    # in-memory fixture. Preserve both ledger rows until execution confirms.
    from core import runtime_paths
    monkeypatch.setattr(runtime_paths, 'mts_data_mode', lambda: 'paper')
    monkeypatch.setattr(mod, '_abandoned_trade_ids', lambda: set())
    create, path, cfg = state_case
    tid = 'mts-auto-151001-499'
    enter(create, -5.89, tid)
    state = json.loads(path.read_text())
    entry_ts = (datetime.now()-timedelta(days=6)).isoformat()
    state.update(entry_ts=entry_ts, peak_net_exit_pnl_twd=-32.)
    path.write_text(json.dumps(state))
    fills = tmp_path / 'fills.jsonl'
    fills.write_text(''.join(json.dumps(dict(timestamp=entry_ts, trade_id=tid,
        leg=leg, side=side, price=price, qty=1, fill_type='ENTRY', mode='paper'))+'\n'
        for leg, side, price in [('NEAR','LONG',49854.),('FAR','SHORT',50035.)]))
    before = fills.read_bytes()
    events = tmp_path / 'events.jsonl'
    events.write_text(json.dumps(dict(event='POLICY_J_PEAK_CONFIRMED',trade_id=tid,
        ts=(datetime.now()-timedelta(hours=1)).isoformat(),durable_peak=1138.,
        candidate_peak=1138.,candidate_count=1))+'\n')
    cfg['params'] = dict(enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=200., combined_upl_giveback_twd=50.,
        release_stop_points=1000, trail_distance_points=1000,
        release_filter={'bb_enabled':False}, mfe_tighten={'enabled':False})
    s = create()
    assert s._restore_position_state() is True
    s._hvwap_execution_mode = 'paper_active'
    bar = dict(timestamp=datetime.now(),near_close=49854.,far_close=50035.,
        near_bid=49853.,near_ask=49855.,far_bid=50034.,far_ask=50036.,
        near_tick_age_ms=0.,far_tick_age_ms=0.,quote_age_ms=0.,pair_skew_ms=0.,
        spread_z=1.,atr=75.65,volume=10,far_volume=10)
    tick(s,bar,cfg)
    assert s._pj_durable_peak == 1138.
    signal = tick(s,bar,cfg)
    assert signal is not None and signal.action == 'EXIT'
    assert s._has_position and s._trade_id == tid
    assert fills.read_bytes() == before
