import os
from types import SimpleNamespace

import pytest

from core import live_broker_preflight as preflight


class Api:
    def __init__(self):
        self.futopt_account = SimpleNamespace(person_id="p", broker_id="b", account_id="a")
        self.Contracts = SimpleNamespace(Futures={"TMF": []})
        self.calls = []
    def list_positions(self, account): self.calls.append("positions"); return []
    def list_trades(self): self.calls.append("trades"); return []
    def margin(self, account): self.calls.append("margin"); return SimpleNamespace(available_margin=1, equity_amount=2, risk_indicator=3)
    def trading_limits(self, account): self.calls.append("limits"); return []
    def snapshots(self, contracts): self.calls.append("snapshots"); return [SimpleNamespace(code=c.code) for c in contracts]
    def unsubscribe(self, contract, quote_type): self.calls.append(f"unsubscribe:{contract.code}")
    def logout(self): self.calls.append("logout")


def test_preflight_requires_explicit_no_order_guard(monkeypatch):
    monkeypatch.delenv(preflight.REQUEST_ENV, raising=False)
    with pytest.raises(preflight.PreflightBlocked):
        preflight.run_once(lambda: Api())


def test_preflight_writes_read_only_snapshot(monkeypatch, tmp_path):
    api = Api()
    near, far = SimpleNamespace(code="TMFH6", delivery_date="2026/08/19"), SimpleNamespace(code="TMFI6", delivery_date="2026/09/16")
    monkeypatch.setenv(preflight.REQUEST_ENV, "1")
    monkeypatch.setattr(preflight, "diagnostics_dir", lambda: tmp_path)
    monkeypatch.setattr(preflight, "assert_broker_access_allowed", lambda: "mini")
    monkeypatch.setattr("core.broker.shioaji_compat.get_contracts_list", lambda *_: [near, far])
    monkeypatch.setattr("core.broker.shioaji_compat.safe_subscribe", lambda api, contract, quote_type: api.calls.append(f"subscribe:{contract.code}"))
    monkeypatch.setattr(preflight, "_unsubscribe_bidask", lambda api, contract: api.calls.append(f"unsubscribe:{contract.code}"))
    response = preflight.run_once(lambda: api, request_id="r1")
    assert response["read_only"] is True
    assert response["live_order_allowed"] is False
    assert response["preflight"]["passed"] is True
    assert (tmp_path / "broker_snapshot_latest.json").exists()
    assert "logout" in api.calls
    assert not any("order" in c for c in api.calls)


def test_contract_resolution_uses_compat_adapter_not_native_bracket(monkeypatch):
    api = Api()
    near = SimpleNamespace(code="TMFH6", delivery_date="2099/08/19")
    far = SimpleNamespace(code="TMFI6", delivery_date="2099/09/16")
    monkeypatch.setattr("core.broker.shioaji_compat.get_contracts_list", lambda *_: [near, far])
    assert preflight.resolve_near_far_contracts(api, "TMF") == (near, far)


def test_preflight_preserves_evidence_when_trading_limits_fails(monkeypatch, tmp_path):
    api = Api()
    api.trading_limits = lambda account: (_ for _ in ()).throw(RuntimeError("broker mapping unavailable"))
    near = SimpleNamespace(code="TMFH6", delivery_date="2099/08/19")
    far = SimpleNamespace(code="TMFI6", delivery_date="2099/09/16")
    monkeypatch.setenv(preflight.REQUEST_ENV, "1")
    monkeypatch.setattr(preflight, "diagnostics_dir", lambda: tmp_path)
    monkeypatch.setattr(preflight, "assert_broker_access_allowed", lambda: "mini")
    monkeypatch.setattr("core.broker.shioaji_compat.get_contracts_list", lambda *_: [near, far])
    monkeypatch.setattr("core.broker.shioaji_compat.safe_subscribe", lambda api, contract, quote_type: None)
    monkeypatch.setattr(preflight, "_unsubscribe_bidask", lambda api, contract: None)
    response = preflight.run_once(lambda: api, request_id="limits-fail")
    assert response["snapshot"] is not None
    assert response["snapshot"]["positions"] == []
    assert response["snapshot"]["margin"]["available_margin"] == 1
    assert response["preflight"]["passed"] is False
    assert response["preflight"]["failed_checks"] == [
        "TRADING_LIMITS_QUERY_FAILED: RuntimeError: broker mapping unavailable"
    ]


def test_preflight_lock_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv(preflight.REQUEST_ENV, "1")
    monkeypatch.setattr(preflight, "diagnostics_dir", lambda: tmp_path)
    monkeypatch.setattr(preflight, "assert_broker_access_allowed", lambda: "mini")
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / preflight.LOCK_NAME).write_text("busy")
    with pytest.raises(preflight.PreflightBlocked, match="ALREADY_RUNNING"):
        preflight.run_once(lambda: Api())
