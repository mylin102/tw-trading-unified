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
