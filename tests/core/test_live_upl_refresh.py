"""RED: dashboard live-UPL refresh goes through the RUNNING
trading-system process (same Shioaji session) — a small atomic runtime
command consumed once by the monitor's existing loop; never a new
preflight subprocess/session.  The capture reuses the same-session
read-only list_positions/list_trades/margin path; zero
place/cancel/update; success re-persists the canonical artifact,
failure leaves live UPL N/A; paper is a no-op.
"""
import json
import os
from types import SimpleNamespace

from ui.dashboard import write_live_upl_refresh_command


class _FakeApi:
    futopt_account = SimpleNamespace(account_id="acc-1")
    stock_account = None

    def __init__(self, positions=(), trades=(), failing=False):
        self._positions = positions
        self._trades = trades
        self._failing = failing
        self.calls = []

    def list_positions(self, acct=None):
        self.calls.append("list_positions")
        if self._failing:
            raise RuntimeError("broker session down")
        return self._positions

    def list_trades(self, acct=None):
        self.calls.append("list_trades")
        return self._trades

    def margin(self, acct=None):
        self.calls.append("margin")
        return SimpleNamespace(available_margin=100000.0)

    def place_order(self, *a, **k):
        self.calls.append("place_order")

    def cancel_order(self, *a, **k):
        self.calls.append("cancel_order")

    def update_order(self, *a, **k):
        self.calls.append("update_order")


_POS = [
    SimpleNamespace(code="TMFH6", direction="sell", quantity=1,
                    price=46077.0, pnl=-3240.0),
    SimpleNamespace(code="TMFI6", direction="buy", quantity=1,
                    price=45231.0, pnl=3090.0),
]


def _cmd_path(tmp_path):
    return (tmp_path / "commands" / "live_upl_refresh.json")


def _monitor(monkeypatch, tmp_path, mode="live", failing=False):
    from strategies.futures.monitor import FuturesMonitor
    monkeypatch.setenv("LRC_RELEASE_SHA", "")
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.api = _FakeApi(_POS, failing=failing)
    m._execution_context = SimpleNamespace(
        effective_mode=mode, session_id="sess-1")
    return m


def test_command_write_atomic_idempotent(tmp_path):
    """The refresh command writes atomically (O_EXCL, no replacement)
    with exactly command_id/action/created_at and no credentials or
    order fields."""
    from ui.dashboard import write_live_upl_refresh_command

    assert write_live_upl_refresh_command(
        _cmd_path(tmp_path)) is True
    # a pending command is never overwritten (monitor consumes once)
    assert write_live_upl_refresh_command(
        _cmd_path(tmp_path)) is False
    _data = json.loads(_cmd_path(tmp_path).read_text(encoding="utf-8"))
    assert set(_data) == {"command_id", "action", "created_at"}
    assert _data["action"] == "LIVE_UPL_REFRESH"
    assert _data["command_id"].startswith("UPL_REFRESH-")
    assert isinstance(_data["created_at"], int)


def test_monitor_consumes_once_and_same_query_methods(
        monkeypatch, tmp_path):
    """The monitor consumes the command once via its existing loop hook
    and reuses the same-session read-only list_positions/list_trades/
    margin path — zero order/cancel/update calls."""
    m = _monitor(monkeypatch, tmp_path)
    write_live_upl_refresh_command(_cmd_path(tmp_path))

    assert m._process_live_upl_refresh_command() is True
    # the command is gone — a second consume is a no-op
    assert m._process_live_upl_refresh_command() is False

    assert "list_positions" in m.api.calls
    assert "list_trades" in m.api.calls
    assert "margin" in m.api.calls
    assert "place_order" not in m.api.calls
    assert "cancel_order" not in m.api.calls
    assert "update_order" not in m.api.calls


def test_monitor_success_updates_canonical(monkeypatch, tmp_path):
    """On success the canonical broker snapshot artifact is
    re-persisted with the current-session evidence."""
    m = _monitor(monkeypatch, tmp_path)
    write_live_upl_refresh_command(_cmd_path(tmp_path))
    m._process_live_upl_refresh_command()

    _canon = (tmp_path / "exports" / "trades" / "live" / "diagnostics"
              / "broker_snapshot_canonical.json")
    assert _canon.exists()
    _data = json.loads(_canon.read_text(encoding="utf-8"))
    assert _data["session_id"] == "sess-1"
    assert _data["source"] == "live_broker"
    assert _data["mode"] == "live"
    assert _data["fetch_status"]["capture"] == "OK"


def test_monitor_failure_leaves_na(monkeypatch, tmp_path):
    """A query failure records the typed status and does NOT overwrite
    the canonical artifact — live UPL stays N/A."""
    m = _monitor(monkeypatch, tmp_path, failing=True)
    write_live_upl_refresh_command(_cmd_path(tmp_path))
    assert m._process_live_upl_refresh_command() is True

    _canon = (tmp_path / "exports" / "trades" / "live" / "diagnostics"
              / "broker_snapshot_canonical.json")
    assert not _canon.exists()
    assert "list_positions" in m.api.calls
    assert "place_order" not in m.api.calls


def test_monitor_paper_noop(monkeypatch, tmp_path):
    """Paper mode is a no-op: the command is consumed but no broker
    capture or canonical write happens (the paper ledger is its own
    truth)."""
    m = _monitor(monkeypatch, tmp_path, mode="paper_ready")
    write_live_upl_refresh_command(_cmd_path(tmp_path))
    assert m._process_live_upl_refresh_command() is True

    assert m.api.calls == []
    _canon = (tmp_path / "exports" / "trades" / "live" / "diagnostics"
              / "broker_snapshot_canonical.json")
    assert not _canon.exists()
