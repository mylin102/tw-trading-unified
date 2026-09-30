"""Actual confirmed fill callsite; broker receipts, never quote-inferred fills."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('live', [True, False])
def test_confirmed_release_then_exit_preserves_actual_provenance(tmp_path, monkeypatch, live):
    from tests.strategies.test_tmf_spread_atr import _setup_armed
    from strategies.futures.monitor import FuturesMonitor
    import strategies.plugins.futures.active.tmf_spread as plugin
    from core.order_management.order_manager import OrderEvent
    from core.order_management.order import OrderSide, OrderStatus
    monkeypatch.setattr(plugin, '_MTS_FILL_LOG', str(tmp_path / 'fills.jsonl'))
    monkeypatch.setattr(plugin, '_write_mts_state', lambda **kw: None)
    monkeypatch.setattr(plugin, '_append_event', lambda *a, **kw: None)
    monkeypatch.setattr(plugin, '_authorized_fill_mode', lambda: 'live' if live else 'paper')
    strategy, _ = _setup_armed(tmp_path)
    strategy._trade_id = 'broker-entry-anchor'
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.live_trading = live
    mon.ticker = 'TMF'
    mon.contract = SimpleNamespace(code='TMFH6')
    mon.far_contract = SimpleNamespace(code='TMFI6')
    mon._registry = {'tmf_spread': strategy}
    mon.market_data = {'TMF_FAR': {'close': 46020}}
    mon.trader = SimpleNamespace(position=0, execute_signal=lambda *a, **kw: None)
    mon._applied_lifecycle_deals = set()
    mon._pending_lifecycle_orders = {
        'release': {'signal': 'RELEASE_NEAR', 'strategy': 'MTS_RELEASE'},
        'exit': {'signal': 'EXIT', 'strategy': 'MTS_EXIT'},
    }
    for name in ('_check_mts_multi_leg_fill', '_maybe_complete_emergency_command',
                 '_append_mts_event', '_save_orders_file_wrapper'):
        setattr(mon, name, lambda *a, **kw: None)
    mon._get_lifecycle_order = lambda oid: None
    events = [OrderEvent(order_id='release', status=OrderStatus.FILLED,
              symbol='TMFH6', side=OrderSide.BUY, fill_qty=1, fill_price=45690,
              broker_order_id='brk-release' if live else None,
              deal_id='receipt-release', ordno='ord-release', seqno='seq-release',
              timestamp=datetime(2026, 9, 30, 15, 0, 5)),
              OrderEvent(order_id='exit', status=OrderStatus.FILLED,
              symbol='TMFI6', side=OrderSide.SELL, fill_qty=1, fill_price=46030,
              broker_order_id='brk-exit' if live else None,
              deal_id='receipt-exit', timestamp=datetime(2026, 9, 30, 17, 9, 56))]
    for event in events:
        mon._apply_confirmed_futures_deal(event)
        mon._apply_confirmed_futures_deal(event)  # real idempotent callsite
    rows = [json.loads(line) for line in (tmp_path/'fills.jsonl').read_text().splitlines()]
    assert len(rows) == 2
    assert [r['price'] for r in rows] == [45690, 46030]
    assert all(r['trade_id'] == 'broker-entry-anchor' for r in rows)
    if live:
        assert all(r['execution_source'] == 'BROKER_FILL' for r in rows)
        assert all(r['price_source'] == 'BROKER_FILL' for r in rows)
        assert [r['broker_order_id'] for r in rows] == ['brk-release', 'brk-exit']
        assert [r['broker_deal_id'] for r in rows] == ['receipt-release', 'receipt-exit']
        assert all(not r.get('paper_fill_id') for r in rows)
        assert all(not str(r.get('parent_combo_order_id') or '').startswith('PAPER-') for r in rows)
    else:
        assert all(r['execution_source'] == 'PAPER_EXIT_FILL' for r in rows)
        assert all(r['paper_fill_id'].startswith('PF-') for r in rows)


def test_broker_refresh_keeps_entry_identity_through_partial_close(tmp_path, monkeypatch):
    from tests.core.test_broker_authority_hydration import _monitor, _two_leg_snap, _strategy
    monkeypatch.setenv('TRADING_RUNTIME_DIR', str(tmp_path))
    mon = _monitor(tmp_path)
    snap = _two_leg_snap()
    mon._capture_post_startup_snapshot = lambda: snap
    strategy = _strategy()
    first = mon._refresh_live_broker_authority(strategy)
    anchor = first.trade_id
    snap['positions'] = snap['positions'][1:]
    mon._live_broker_authority_at = 0
    second = mon._refresh_live_broker_authority(strategy)
    assert second.trade_id == anchor
    assert strategy._trade_id == anchor
    # A fresh monitor must recover the same broker-bound lifecycle anchor.
    restarted = _monitor(tmp_path)
    restarted._capture_post_startup_snapshot = lambda: snap
    assert restarted._refresh_live_broker_authority(_strategy()).trade_id == anchor
    # Proven broker flat terminates continuity; changed inventory gets fresh identity.
    snap['positions'] = []
    restarted._live_broker_authority_at = 0
    restarted._refresh_live_broker_authority(strategy)
    snap['positions'] = _two_leg_snap()['positions'][1:]
    restarted._live_broker_authority_at = 0
    assert restarted._refresh_live_broker_authority(_strategy()).trade_id != anchor


def test_streamed_combo_uses_real_entry_sync_writer(tmp_path, monkeypatch):
    from tests.core.test_fills_callback_bridge import _monitor_with, _LiveFDealPayload
    from tests.strategies.test_tmf_spread_atr import _setup_armed
    from core.order_management.order_manager import OrderManager
    from core.order_management.order import Order, OrderSide, OrderType
    import strategies.plugins.futures.active.tmf_spread as plugin
    monkeypatch.setattr(plugin,'_MTS_FILL_LOG',str(tmp_path/'fills.jsonl'))
    monkeypatch.setattr(plugin,'_write_mts_state',lambda **kw:None)
    monkeypatch.setattr(plugin,'_append_event',lambda *a,**kw:None)
    monkeypatch.setattr(plugin,'_authorized_fill_mode',lambda:'live')
    manager=OrderManager(mode='paper')  # in-memory broker receipts, never submit
    order=Order('TMFJ6-TMFK6',OrderSide.BUY,OrderType.MKP,1,
                order_id='combo',strategy='MTS_ENTRY',truth_source='broker_combo',
                combo_legs=[{'symbol':'TMFJ6','side':'buy','quantity':1},
                            {'symbol':'TMFK6','side':'sell','quantity':1}])
    manager.active_orders['combo']=order
    order.submit('2a97043a',broker_order_id='2a97043a',ordno='2a97043a')
    mon=_monitor_with(manager)
    strategy,_=_setup_armed(tmp_path)
    mon._registry={'tmf_spread':strategy}
    mon.live_trading=True
    mon.cfg={}
    mon.ticker='TMF'
    mon._execution_context=SimpleNamespace(effective_mode='live_ready')
    mon._pending_lifecycle_orders={}
    mon._applied_lifecycle_deals=set()
    mon._manual_trade_status='READY'
    mon._maybe_complete_emergency_command=lambda *a,**kw:None
    mon._check_oco_release_fill=lambda *a,**kw:None
    mon._save_orders_file_wrapper=lambda:None
    mon._submit_release_oco_bracket=lambda *a,**kw:None
    mon._mts_pending_fills={'entry-anchor':{'combo_order_id':'combo','combo':True,
        'combo_legs':order.combo_legs,'near_filled':False,'far_filled':False,
        'side':'LONG','spread_side':'BUY_NEAR_SELL_FAR','near_label':'LONG','far_label':'SHORT'}}
    mon._wire_order_callbacks()
    callbacks=[_LiveFDealPayload(trade_id='2a97043a',seqno='787254',ordno=None,
                exchange_seq=None,action=side,code=code,price=price,quantity=1,combo=True,
                ts=1790733370.414+i*.036)
               for i,(code,side,price) in enumerate([('TMFJ6','Buy',48605.),('TMFK6','Sell',48754.)])]
    for callback in callbacks*2:
        mon.on_order_event(SimpleNamespace(name='FuturesDeal',value='FDEAL'),callback)
    rows=[json.loads(line) for line in (tmp_path/'fills.jsonl').read_text().splitlines()]
    assert len(rows)==2
    assert [r['price'] for r in rows]==[48605.,48754.]
    assert all(r['execution_source']=='BROKER_FILL' for r in rows)
    assert all(r['broker_order_id']=='2a97043a' for r in rows)
    assert all(not r.get('paper_fill_id') for r in rows)
