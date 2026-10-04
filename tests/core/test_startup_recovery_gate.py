"""Fail-closed startup reconciliation tests."""

from types import SimpleNamespace

from core.channel_safety import get_safety_state, reset_safety_state
from main import verify_startup_reconciliation

# 2026-10-04 Codex: execute the real run_system startup prefix, offline only.
import ast
import copy
from pathlib import Path
import pytest
import main as startup


def run_startup_callsite(monkeypatch, tmp_path, broker, flags, *, options=False, mode="paper"):
    """Keep production statements through reconciliation, omit quote/thread startup."""
    config = tmp_path / "config"
    config.mkdir()
    for name, flag in flags.items():
        import yaml
        (config / f"{name}.yaml").write_text(yaml.safe_dump({"live_trading": flag}))
    import yaml
    (config / "options_strategy.yaml").write_text(yaml.safe_dump({"live_trading": options}))
    monkeypatch.setenv("TRADING_FUTURES_MODE", mode)
    monkeypatch.setattr(startup, "BASE", str(tmp_path))
    monkeypatch.setattr(startup, "get_api", lambda: broker)
    monkeypatch.setattr(startup, "update_startup_progress", lambda *a, **k: None)
    import core.lifecycle_provenance as lifecycle
    monkeypatch.setattr(lifecycle, "LifecycleRecorder", lambda: SimpleNamespace(record_event=lambda *a, **k: None))
    source = ast.parse(Path(startup.__file__).read_text())
    fn = copy.deepcopy(next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "run_system"))
    entry = next(n for n in fn.body if isinstance(n, ast.Try) and any(
        isinstance(x, ast.Name) and x.id == "verify_startup_reconciliation" for x in ast.walk(n)))
    call_index = next(i for i, n in enumerate(entry.body[0].body) if any(
        isinstance(x, ast.Name) and x.id == "verify_startup_reconciliation" for x in ast.walk(n)))
    entry.body[0].body = entry.body[0].body[:call_index + 1]
    fn.body = fn.body[:fn.body.index(entry)] + entry.body[:1]
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    namespace = dict(vars(startup))
    exec(compile(module, startup.__file__, "exec"), namespace)
    namespace["run_system"](config_name=",".join(flags))
    return get_safety_state().entry_allowed()


@pytest.mark.parametrize("failure,positions", [
    (RuntimeError("SystemMaintenance 503"), []),
    (None, [SimpleNamespace(code="REAL_LEG", quantity=1)]),
])
def test_real_paper_startup_never_queries_broker_positions(monkeypatch, tmp_path, failure, positions):
    broker = FakeBroker(positions=positions, failure=failure)
    assert run_startup_callsite(monkeypatch, tmp_path, broker, {"futures": False}) is True
    assert broker.update_calls == 0


@pytest.mark.parametrize("flags,options,mode", [
    ({"futures_live": True}, False, "live"),
    ({"futures": False, "other": True}, False, "paper"),
    ({"futures": False}, True, "paper"),
    ({"futures": "false"}, False, "paper"),
    ({"futures": None}, False, "paper"),
    ({"futures": False}, False, "live"),
    ({"futures": False}, False, "UNKNOWN"),
])
def test_real_startup_keeps_live_or_uncertain_reconciliation(monkeypatch, tmp_path, flags, options, mode):
    broker = FakeBroker(failure=RuntimeError("SystemMaintenance 503"))
    assert run_startup_callsite(monkeypatch, tmp_path, broker, flags, options=options, mode=mode) is False
    assert broker.update_calls == 1


def test_real_live_startup_still_requires_successful_broker_read(monkeypatch, tmp_path):
    broker = FakeBroker()
    assert run_startup_callsite(monkeypatch, tmp_path, broker, {"futures_live": True}, mode="live") is True
    assert broker.update_calls == 1


class FakeBroker:
    def __init__(self, positions=(), trades=(), failure=None):
        self.futopt_account = object()
        self.positions = list(positions)
        self.trades = list(trades)
        self.failure = failure
        self.update_calls = 0

    def update_status(self, account):
        self.update_calls += 1
        if self.failure:
            raise self.failure

    def list_positions(self, account):
        if self.failure:
            raise self.failure
        return self.positions

    def list_trades(self):
        if self.failure:
            raise self.failure
        return self.trades


def trade(status):
    return SimpleNamespace(status=SimpleNamespace(status=status))


def setup_function():
    reset_safety_state()


def test_flat_broker_state_unlocks_after_read_only_reconciliation():
    broker = FakeBroker(trades=[trade("Filled"), trade("Cancelled")])

    assert verify_startup_reconciliation(broker) is True
    safety = get_safety_state()
    assert broker.update_calls == 1
    assert safety.reconciled is True
    assert safety.entry_allowed() is True


def test_open_broker_position_keeps_entry_blocked():
    broker = FakeBroker(positions=[SimpleNamespace(code="TMF", quantity=1)])

    assert verify_startup_reconciliation(broker) is False
    safety = get_safety_state()
    assert safety.reconciled is False
    assert safety.entry_allowed() is False
    assert "RECONCILIATION_PENDING" in (safety.entry_blocked_reason or "")


def test_open_order_keeps_entry_blocked():
    broker = FakeBroker(trades=[trade("Submitted")])

    assert verify_startup_reconciliation(broker) is False
    assert get_safety_state().entry_allowed() is False


def test_broker_query_failure_keeps_entry_blocked():
    broker = FakeBroker(failure=RuntimeError("broker unavailable"))

    assert verify_startup_reconciliation(broker) is False
    safety = get_safety_state()
    assert safety.reconciled is False
    assert safety.entry_allowed() is False
