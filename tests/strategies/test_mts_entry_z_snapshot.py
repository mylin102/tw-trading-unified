# 2026-10-06 Codex: actual paper gateway -> simulator callback -> persisted Z.
import json
from datetime import datetime

import pytest

from tests.test_paper_combo_fill import real_paper_monitor


@pytest.mark.parametrize('decision_z,expected', [
    (-4.656461385288359, -4.66), (4.656461385288359, 4.66),
    (0.0, 0.0), (None, None), ('', None), (float('nan'), None),
])
def test_real_combo_fill_preserves_decision_z(real_paper_monitor, tmp_path, monkeypatch,
                                              decision_z, expected):
    import strategies.plugins.futures.active.tmf_spread as mod
    from core.order_management.order import OrderSide
    from core.strategy_context import MarketData, PositionView, StrategyContext
    mon = real_paper_monitor
    monkeypatch.setattr(mod, '_MTS_FILL_LOG', str(tmp_path/'fills.jsonl'))
    monkeypatch.setattr(mod, '_MTS_EVENT_LOG', str(tmp_path/'events.jsonl'))
    monkeypatch.setattr(mod, '_get_state_file_path', lambda: tmp_path/'state.json')
    monkeypatch.setattr(mod, '_PENDING_STATE_REWRITE', None)
    monkeypatch.delenv('MTS_BACKTEST', raising=False)
    strategy = mod.TMFSpread()
    strategy.init(StrategyContext(market=MarketData(last_bar={}, ticker='TMF'),
                                 position=PositionView(size=0), config={'ticker':'TMF'}))
    mon.ticker = 'TMF'
    mon.cfg = {'ticker':'TMF'}
    mon._registry = {'tmf_spread': strategy}
    strategy._entry_z = 3.0  # threshold is deliberately NOT the observed Z
    strategy._entry_spread_z = 99.0  # RAM is not the submitted decision snapshot
    mon.order_mgr.register_callback('on_fill', lambda ev:
        mon._check_mts_multi_leg_fill(ev.order_id, ev.fill_price))
    mon._submit_mts_combo_entry(
        trade_id='Z-AUDIT', action='BUY_NEAR_SELL_FAR', reason='snapshot regression',
        near_code='TMFJ6', far_code='TMFK6', near_side=OrderSide.BUY, far_side=OrderSide.SELL,
        near_price=49854.0, far_price=50035.0,
        ts=datetime(2026,10,6,10,18,28), snapshot={},
        bar_dict={'spread':-181.0, 'spread_z':decision_z,
                  'spread_ma':-165.9, 'spread_std':3.242805802643825,
                  'bbo_near_bid':49853., 'bbo_near_ask':49855.,
                  'bbo_far_bid':50034., 'bbo_far_ask':50036.,
                  'bbo_near_age_ms':10, 'bbo_far_age_ms':10})
    fills=[json.loads(x) for x in (tmp_path/'fills.jsonl').read_text().splitlines()]
    entries=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()
             if json.loads(x).get('event')=='ENTRY']
    assert len(fills)==2 and len(entries)==1
    assert [r['spread_z'] for r in fills] == [expected, expected]
    # 2026-10-06 Codex: events keep full precision; fills/state round to 2dp.
    assert entries[0]['spread_z'] == (None if expected is None else pytest.approx(float(decision_z)))
    state=json.loads((tmp_path/'state.json').read_text())
    assert state['spread_z'] == expected
    assert strategy._entry_spread_z == (None if expected is None else pytest.approx(float(decision_z)))
    assert mon._mts_pending_fills == {}  # completion consumed the tracker once


@pytest.mark.parametrize('decision_z,expected', [
    (-4.656461385288359, -4.66), (4.656461385288359, 4.66),
    (0.0, 0.0), (None, None), ('', None), (float('nan'), None),
])
def test_delayed_combo_callback_keeps_submitted_z(
        real_paper_monitor, tmp_path, monkeypatch, decision_z, expected):
    import strategies.plugins.futures.active.tmf_spread as mod
    from core.order_management.order import OrderSide, OrderStatus
    from core.strategy_context import MarketData, PositionView, StrategyContext
    from types import SimpleNamespace

    mon = real_paper_monitor
    fills_path, events_path, state_path = [
        tmp_path / name for name in ('fills.jsonl', 'events.jsonl', 'state.json')]
    monkeypatch.setattr(mod, '_MTS_FILL_LOG', str(fills_path))
    monkeypatch.setattr(mod, '_MTS_EVENT_LOG', str(events_path))
    monkeypatch.setattr(mod, '_get_state_file_path', lambda: state_path)
    monkeypatch.setattr(mod, '_PENDING_STATE_REWRITE', None)
    monkeypatch.delenv('MTS_BACKTEST', raising=False)
    strategy = mod.TMFSpread()
    strategy.init(StrategyContext(market=MarketData(last_bar={}, ticker='TMF'),
                                 position=PositionView(size=0), config={'ticker': 'TMF'}))
    mon.ticker, mon.cfg = 'TMF', {'ticker': 'TMF'}
    mon._registry = {'tmf_spread': strategy}
    mon.order_mgr.register_callback('on_fill', lambda ev:
        mon._check_mts_multi_leg_fill(ev.order_id, ev.fill_price))
    simulator = mon.paper_fill_sim
    deliver = simulator.process_combo_ticks
    held_ticks = []
    monkeypatch.setattr(simulator, 'process_combo_ticks',
                        lambda near, far: held_ticks.append((near, far)))
    bar = {'spread': -181.0, 'spread_z': decision_z,
           'bbo_near_bid': 49853., 'bbo_near_ask': 49855.,
           'bbo_far_bid': 50034., 'bbo_far_ask': 50036.,
           'bbo_near_age_ms': 10, 'bbo_far_age_ms': 10}
    mon._submit_mts_combo_entry(
        trade_id='DELAYED-Z', action='BUY_NEAR_SELL_FAR', reason='delayed snapshot',
        near_code='TMFJ6', far_code='TMFK6',
        near_side=OrderSide.BUY, far_side=OrderSide.SELL,
        near_price=49854., far_price=50035.,
        ts=datetime(2026, 10, 6, 10, 18, 28), snapshot={}, bar_dict=bar)
    assert len(held_ticks) == 1 and len(mon._mts_pending_fills) == 1
    assert not fills_path.exists() and not state_path.exists()
    assert mon.callbacks == []
    order = next(iter(mon.order_mgr.active_orders.values()))
    assert order.status is OrderStatus.SUBMITTED

    # Change both later bar data and strategy RAM after the actual submit.
    bar['spread_z'] = 12.25
    mon._current_bar = {'spread_z': 12.25}
    strategy._entry_spread_z = 99.0
    strategy._entry_z = 3.0
    monkeypatch.setattr(simulator, 'process_combo_ticks', deliver)
    near_tick, far_tick = held_ticks[0]
    # One valid leg tick cannot manufacture a parent fill or consume pending.
    deliver(near_tick, SimpleNamespace(symbol='TMFK6', close=float('nan')))
    assert mon.callbacks == [] and len(mon._mts_pending_fills) == 1
    assert not fills_path.exists() and not state_path.exists()
    deliver(near_tick, far_tick)

    fills = [json.loads(line) for line in fills_path.read_text().splitlines()]
    entries = [json.loads(line) for line in events_path.read_text().splitlines()
               if json.loads(line).get('event') == 'ENTRY']
    assert [row['spread_z'] for row in fills] == [expected, expected]
    observed = None if expected is None else pytest.approx(float(decision_z))
    assert len(entries) == 1 and entries[0]['spread_z'] == observed
    assert json.loads(state_path.read_text())['spread_z'] == expected
    assert strategy._entry_spread_z == observed
    assert mon._mts_pending_fills == {} and len(mon.callbacks) == 1

    # Replay the actual OrderEvent at the real callback dispatcher, preserving
    # the original parent order/deal identity; do not invoke the helper directly.
    snapshot = [path.read_bytes() for path in (fills_path, events_path, state_path)]
    mon.order_mgr._emit('on_fill', mon.callbacks[0])
    simulator.register_combo(order, near_symbol='TMFJ6', far_symbol='TMFK6')
    deliver(near_tick, far_tick)
    assert [path.read_bytes() for path in (fills_path, events_path, state_path)] == snapshot
    assert len(order.fills) == 1 and mon._mts_pending_fills == {}


@pytest.mark.parametrize('decision_z,expected', [(4.656461385288359, 4.66), (0., 0.), ('', None)])
def test_separate_leg_callbacks_wait_for_far_and_keep_snapshot(
        real_paper_monitor, tmp_path, monkeypatch, decision_z, expected):
    import strategies.futures.monitor as monitor_mod
    import strategies.plugins.futures.active.tmf_spread as mod
    from strategies.futures.mts_ledger_authority import MtsLedgerProjection
    from core.strategy_context import MarketData, PositionView, StrategyContext
    from core.signal import Signal

    mon = real_paper_monitor
    fills_path, state_path = tmp_path / 'fills.jsonl', tmp_path / 'state.json'
    mon._ledger_projection = MtsLedgerProjection(path=str(fills_path), source='PAPER')
    monkeypatch.setattr(mod, '_MTS_FILL_LOG', str(fills_path))
    monkeypatch.setattr(mod, '_MTS_EVENT_LOG', str(tmp_path / 'events.jsonl'))
    monkeypatch.setattr(mod, '_get_state_file_path', lambda: state_path)
    monkeypatch.setattr(monitor_mod, '_mts_position_state_path', lambda: state_path)
    monkeypatch.setattr(mod, '_PENDING_STATE_REWRITE', None)
    monkeypatch.delenv('MTS_BACKTEST', raising=False)
    # Only the external session clock is a fake; authorization, creation,
    # gateway submission, callback dispatch and persistence remain real.
    monkeypatch.setattr(monitor_mod, 'is_taifex_futures_market_open', lambda: True)
    strategy = mod.TMFSpread()
    strategy.init(StrategyContext(market=MarketData(last_bar={}, ticker='TMF'),
                                 position=PositionView(size=0), config={'ticker': 'TMF'}))
    mon.ticker, mon.cfg = 'TMF', {'ticker': 'TMF'}
    mon._registry = {'tmf_spread': strategy}
    # Exercise the existing separate-leg fallback, with no synchronous ticks.
    mon.paper_fill_sim = None
    mon.order_mgr.register_callback('on_fill', lambda ev:
        mon._check_mts_multi_leg_fill(ev.order_id, ev.fill_price))
    bar = {'near_close': 49854., 'far_close': 50035., 'spread_z': decision_z}
    mon._submit_mts_order_signal(
        Signal('BUY_NEAR_SELL_FAR', 'separate leg delayed snapshot', quantity=1),
        strategy, bar, datetime(2026, 10, 6, 10, 18, 28))
    assert len(mon._mts_pending_fills) == 1
    trade_id, pending = next(iter(mon._mts_pending_fills.items()))
    assert pending['near_order_id'] and pending['far_order_id']
    bar['spread_z'], strategy._entry_spread_z = 12.25, 99.
    mon.order_mgr.on_fill(pending['near_order_id'], 49854., 1, False)
    assert pending['near_filled'] is True and pending['far_filled'] is False
    assert not fills_path.exists() and not state_path.exists()
    mon.order_mgr._emit('on_fill', mon.callbacks[0])
    assert trade_id in mon._mts_pending_fills and not state_path.exists()
    mon.order_mgr.on_fill(pending['far_order_id'], 50035., 1, False)
    fills = [json.loads(line) for line in fills_path.read_text().splitlines()]
    assert [row['spread_z'] for row in fills] == [expected, expected]
    assert json.loads(state_path.read_text())['spread_z'] == expected
    assert mon._mts_pending_fills == {}
    before = fills_path.read_bytes(), state_path.read_bytes()
    mon.order_mgr._emit('on_fill', mon.callbacks[-1])
    assert (fills_path.read_bytes(), state_path.read_bytes()) == before
