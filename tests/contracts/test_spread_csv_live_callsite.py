"""S4b: real strategy poll -> real CSV writer, existing empty columns."""
import pathlib
import time
from types import SimpleNamespace
import pandas as pd
import pytest


@pytest.mark.parametrize('blank', ['', '  ', float('nan'), None])
@pytest.mark.parametrize('real_producer', [False, True])
def test_strategy_tick_persists_same_bar_decision_after_mts(tmp_path, monkeypatch, blank, real_producer):
    import strategies.futures.monitor as mod
    from tests.contracts.test_dashboard_5m_contract import _monitor
    monkeypatch.setattr(mod, 'runtime_logs', lambda _: str(tmp_path))
    monkeypatch.setattr('core.date_utils.is_day_session', lambda _: True)
    monkeypatch.setattr('core.date_utils.is_night_session', lambda _: False)
    mon = _monitor()
    mon.cfg = {'mts': {'enabled': True}}
    mon.STRATEGY = {}
    mon.MONITOR = {}
    mon.is_trading_ready = True
    mon._tmf_feed_age_secs=lambda:0
    mon.PB_ARGS = {}
    mon.manual_trade_flag_path = str(tmp_path/'no-manual-flag')
    mon._manual_trade_status = 'READY'
    mon.last_tick_at = time.time()
    mon.trader = SimpleNamespace(position=0)
    mon._bar_counter = 1
    mon._bars_since_session_open = 0
    mon._bars_since_trade = 0
    mon.previous_session_type = None
    mon.last_processed_bar = None
    mon._current_bar = {'ts': None, 'open': 0}
    mon._last_bar_context = {}
    mon._far_current_bar = {'close': 48435.0}
    row = {'Open':48274., 'High':48275., 'Low':48273., 'Close':48274.,
           'Volume':1., 'atr':10., 'atr_raw':10., 'vwap':48274.,
           'spread':blank, 'spread_z':blank, 'near_close':48274., 'far_close':48435.,
           'spread_ma':-170., 'spread_std':4.}
    frame = pd.DataFrame([row,row], index=pd.date_range('2026-09-30 20:00',periods=2,freq='5min'))
    for name in ('_process_live_upl_refresh_command','_reconcile_exit_only_authorization',
                 '_run_mts_watchdog','_sync_mts_status','_reload_config_if_changed',
                 '_check_futures_contract_staleness','_refresh_runtime_status',
                 '_check_tick_api_consistency','_hourly_no_trade_audit','_mts_tag_bar_session',
                 '_update_mtf_snapshot'):
        setattr(mon,name,lambda *a,**kw:None)
    mon._check_canonical_freshness=lambda _:[]
    mon._get_tick_bars_df=lambda:frame
    mon._periodic_backfill_bars=lambda:None
    monkeypatch.setattr(mod,'build_preferred_canonical_bar_frames',lambda *a,**kw:({'5m':frame},{'source':'tick-5m'}))
    monkeypatch.setattr(mod,'calculate_futures_squeeze',lambda f,**kw:f)
    monkeypatch.setattr(mod,'attach_bar_metadata',lambda f:f)
    calls=[]
    def tick(enriched_bar):
        calls.append(enriched_bar['ts'])
        # This seam isolates strategy execution/order sends, not the save callsite.
        enriched_bar['spread_z']=2.25
    if real_producer:
        class StopBeforeOrderExecution(Exception): pass
        mon.api = None
        mon._last_far_snap_ts = time.time()
        mon._registry = {'tmf_spread':SimpleNamespace(_has_position=False)}
        mon._execution_context = SimpleNamespace(requested_mode='paper')
        mon._oco_reconciled = True
        mon._reconcile_pending_mts_combos = lambda: None
        mon._check_broker_snapshot_request = lambda: False
        mon._mts_has_open_position_from_fills = lambda: False
        monkeypatch.setattr(mod, '_mts_position_state_path', lambda: tmp_path/'no-state.json')
        monkeypatch.setattr(mod, 'is_taifex_futures_market_open', lambda: True)
        def observe_actual_calculation(bar):
            assert bar['spread'] == -161.
            assert bar['spread_z'] == 2.25
            raise StopBeforeOrderExecution
        mon._apply_spread_dynamics = observe_actual_calculation
        def tick_actual(enriched_bar):
            calls.append(enriched_bar['ts'])
            try:
                mod.FuturesMonitor._mts_tick(mon, enriched_bar=enriched_bar)
            except StopBeforeOrderExecution:
                return  # instrumentation stops before any trading authority/order path
            raise AssertionError('actual MTS calculation was not reached')
        mon._mts_tick = tick_actual
    else:
        mon._mts_tick=tick
    mon._strategy_tick()
    saved=pd.read_csv(next(pathlib.Path(tmp_path).glob('TMF_*_indicators.csv')))
    assert calls == [frame.index[-1]]
    assert saved.iloc[-1]['spread'] == -161.0
    assert saved.iloc[-1]['spread_z'] == 2.25
    assert mon.last_processed_bar == frame.index[-1] and mon._bar_counter == 2
    mon._strategy_tick()
    assert len(pd.read_csv(next(pathlib.Path(tmp_path).glob('TMF_*_indicators.csv')))) == 1
    assert mon._bar_counter == 2
