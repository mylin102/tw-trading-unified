import json
from types import SimpleNamespace


def test_held_status_sync_preserves_lifecycle_without_flat_heartbeat(tmp_path, monkeypatch):
    import strategies.futures.monitor as module
    path = tmp_path / 'position.json'
    original = json.dumps({'has_position': True, 'lifecycle': 'RELEASED',
                           'trade_id': 'broker-confirmed-held'})
    path.write_text(original)
    monkeypatch.setattr(module, '_mts_position_state_path', lambda: path)
    messages = []
    monkeypatch.setattr(module.console, 'print', lambda *a, **kw: messages.append(str(a)))
    mon = module.FuturesMonitor.__new__(module.FuturesMonitor)
    mon.cfg = {'mts': {'strategy': 'tmf_spread'}}
    mon._registry = {'tmf_spread': SimpleNamespace(_has_position=True)}
    mon.market_data = {}
    mon.ticker = 'TMF'
    mon._far_current_bar = {}
    mon._pending_lifecycle_orders = {}
    mon._manual_trade_status = 'FILLED'
    mon.trader = SimpleNamespace(initial_balance=0, balance=0)
    mon.live_trading = True
    def forbidden_margin(*a, **kw):
        raise AssertionError('held heartbeat must not query broker margin')
    mon.api = SimpleNamespace(margin=forbidden_margin, futopt_account=None)
    mon._sync_mts_status()
    assert not messages, messages
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]
