import pytest
from core.order_intent_gateway import OrderIntentGateway

def gateway(callback=None):
    return OrderIntentGateway(durable_intents={'exit': {
        'state': 'SUBMITTED', 'strategy': 'MTS_EXIT',
        'broker_order_id': 'broker-exit', 'symbol': 'TMFK6',
        'side': 'buy', 'quantity': 1,
    }}, record_cb=callback)

def proof(**over):
    return dict(broker_order_id='broker-exit', deal_id='receipt-1',
                symbol='TMFK6', side='buy', quantity=1, **over)

@pytest.mark.parametrize('field,value', [('broker_order_id', 'other'),
    ('deal_id', ''), ('symbol', 'TMFJ6'), ('side', 'sell'), ('quantity', True),
    ('quantity', 0), ('quantity', 2)])
def test_mismatched_receipt_cannot_terminalize_gateway(field, value):
    gw = gateway()
    data = proof()
    data[field] = value
    assert not gw.reconcile_confirmed_fill('exit', **data)
    assert gw.intent_state('exit') == 'SUBMITTED'

def test_persist_failure_preserves_pending_and_never_authorizes_retry():
    def fail(view):
        raise OSError('isolated persistence error')
    gw = gateway(fail)
    assert not gw.reconcile_confirmed_fill('exit', **proof())
    assert gw.intent_state('exit') == 'SUBMITTED'
    assert gw.durable_view()['exit'].get('reconciliation_id') is None
