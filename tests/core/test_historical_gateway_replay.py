import time
from types import SimpleNamespace

import pytest

@pytest.mark.parametrize('variant', ['valid', 'no_receipt', 'wrong_side', 'partial', 'conflict'])
def test_real_flat_refresh_replays_historical_exit_from_broker_deals(tmp_path, monkeypatch, variant):
    from tests.core.test_broker_authority_hydration import _monitor, _snap, _strategy
    from core.mode_transition import ExecutionContext
    from core.order_intent_gateway import OrderIntentGateway
    from core.order_management.order_manager import OrderManager
    from core.order_management.order import Order, OrderSide, OrderType, OrderStatus
    import core.execution_context_state as state
    import core.channel_safety as safety
    monkeypatch.setenv('TRADING_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setenv('MTS_STATE_PATH', str(tmp_path/'position.json'))
    calls = []
    monkeypatch.setattr(safety, 'get_safety_state', lambda: SimpleNamespace(set_reconciled=lambda: calls.append(True)))
    mon = _monitor(tmp_path)
    mon._execution_context = ExecutionContext(requested_mode='live', effective_mode='live_ready',
        live_order_allowed=True, session_id='current-session', account_id_hash='account')
    mon._pending_lifecycle_orders = {}
    mon._order_intent_gateway = OrderIntentGateway(durable_intents={'exit': {
        'state':'SUBMITTED', 'strategy':'MTS_RELEASE', 'reconciliation_id':None,
    }}, record_cb=mon._record_gateway_intent)
    mon.order_mgr = OrderManager(mode='paper')  # no submit; isolated restored order
    order = Order('TMFJ6', OrderSide.SELL, OrderType.MKP, 1, order_id='exit', strategy='MTS_RELEASE')
    order.status = OrderStatus.FILLED
    order.broker_order_id = '20ea7215'
    order.filled_quantity = 1
    order.avg_fill_price = 48313.
    mon.order_mgr.completed.append(order)
    initial = mon._execution_context.to_dict()
    initial['gateway_intents'] = mon._order_intent_gateway.durable_view()
    state.persist_execution_context(initial)
    # Exact observed shape: order price None, deals have no exchange deal id.
    row = dict(id='20ea7215', broker_order_id='20ea7215', code='TMFJ6', direction='sell',
        status='Filled', broker_status='Filled', requested_qty=1, filled_qty=1, quantity=1,
        price=None, seqno='810563', ordno='vE00T', deals=[{'price':48313., 'quantity':1}])
    if variant == 'wrong_side': row['direction'] = 'buy'
    if variant == 'partial': row['filled_qty'] = 0
    trades = [row, dict(row)]  # actual snapshot has duplicate identical observations
    if variant == 'no_receipt': trades = []
    if variant == 'conflict': trades[1] = {**row, 'deals':[{'price':48300., 'quantity':1}]}
    snap = _snap([], broker_trades=trades)
    snap.update(source='live_broker', mode='live', session_id='current-session',
        account_identity_hash='account', canonical_input_hash='evidence-hash', captured_at=int(time.time()*1000))
    mon._capture_post_startup_snapshot = lambda: snap
    mon._refresh_live_broker_authority(_strategy())
    durable = state.read_execution_context()
    if variant == 'valid':
        assert calls == [True]
        rec = durable['gateway_intents']['exit']
        assert rec['state'] == 'FILLED'
        assert rec['broker_order_id'] == '20ea7215'
        assert rec['broker_deal_id'] is None  # cannot invent a broker-issued ID
        assert rec['receipt_identity'].startswith('snapshot-receipt:')
        assert rec['broker_id_source'] == 'DETERMINISTIC_RECEIPT'
        assert durable['canonical_holding']['canonical_legs'] == []
    else:
        assert not calls
        assert durable['gateway_intents']['exit']['state'] == 'SUBMITTED'
