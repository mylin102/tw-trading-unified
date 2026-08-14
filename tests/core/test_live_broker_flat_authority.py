from types import SimpleNamespace


def test_live_broker_flat_snapshot_is_explicit_flat_authority(monkeypatch, tmp_path):
    from strategies.futures.monitor import FuturesMonitor
    from strategies.futures.mts_ledger_authority import MtsAuthority

    class Api:
        futopt_account = SimpleNamespace(account_id="futopt")
        stock_account = None

        def list_positions(self, account):
            return []

        def list_trades(self, account):
            return []

        def margin(self, account):
            return SimpleNamespace(available_margin=500000.0)

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = Api()
    monitor.live_trading = True
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._execution_context = SimpleNamespace(
        requested_mode="live", effective_mode="live_ready", session_id="sess")
    monitor._live_broker_authority_at = 0.0
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))

    strategy = SimpleNamespace(_has_position=True, _trade_id="ghost")
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is not None
    assert auth.status is MtsAuthority.FLAT
    assert auth.trade_id is None
    assert auth.near_qty == 0 and auth.far_qty == 0
