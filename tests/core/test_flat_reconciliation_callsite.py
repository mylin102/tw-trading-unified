import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

@pytest.mark.parametrize('block', [None, 'pending', 'stale', 'session', 'account',
    'unknown_positions', 'unknown_orders', 'held_local', 'persist_failure'])
def test_actual_broker_flat_refresh_requires_all_reconciliation_proofs(tmp_path, monkeypatch, block):
    from tests.core.test_broker_authority_hydration import _monitor, _snap, _strategy
    from core.mode_transition import ExecutionContext
    from core.order_intent_gateway import OrderIntentGateway
    import core.channel_safety as safety
    import core.execution_context_state as state
    monkeypatch.setenv('TRADING_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setenv('MTS_STATE_PATH', str(tmp_path/'position.json'))
    calls = []
    monkeypatch.setattr(safety, 'get_safety_state', lambda: SimpleNamespace(set_reconciled=lambda: calls.append('reconciled')))
    mon = _monitor(tmp_path)
    mon._execution_context = ExecutionContext(requested_mode='live', effective_mode='live_ready',
        live_order_allowed=True, session_id='current-session', account_id_hash='account',
        canonical_holding={'canonical_legs':[{'code':'old-contract'}]})
    mon._pending_lifecycle_orders = {}
    rec = {'state':'FILLED', 'strategy':'MTS_EXIT', 'broker_order_id':'confirmed-order',
           'broker_deal_id':'confirmed-receipt', 'reconciliation_id':'broker-fill:confirmed-receipt'}
    if block == 'pending': rec['state'] = 'SUBMITTED'
    mon._order_intent_gateway = OrderIntentGateway(durable_intents={'exit':rec})
    snap = _snap([])
    snap.update(source='live_broker', mode='live', session_id='current-session',
        account_identity_hash='account', canonical_input_hash='evidence-hash', captured_at=int(time.time()*1000))
    if block == 'stale': snap['captured_at'] -= 60001
    if block == 'session': snap['session_id'] = 'other-session'
    if block == 'account': snap['account_identity_hash'] = 'other-account'
    if block == 'unknown_positions': snap['positions'] = None
    if block == 'unknown_orders': snap['open_orders'] = None
    strategy = _strategy(_has_position=(block=='held_local'))
    if block == 'persist_failure':
        def fail(*a, **kw): raise OSError('isolated write failed')
        monkeypatch.setattr(state, 'persist_execution_context', fail)
    mon._capture_post_startup_snapshot = lambda: snap
    mon._refresh_live_broker_authority(strategy)
    if block:
        assert not calls
        assert mon._execution_context.canonical_holding['canonical_legs']
    else:
        assert calls == ['reconciled']
        persisted = state.read_execution_context(runtime_dir=str(tmp_path))
        assert persisted['canonical_holding']['canonical_legs'] == []
        assert persisted['canonical_holding']['evidence_hash'] == 'evidence-hash'
        assert persisted['gateway_intents']['exit']['state'] == 'FILLED'
        assert mon._execution_context.effective_mode == 'live_ready'  # no promotion
