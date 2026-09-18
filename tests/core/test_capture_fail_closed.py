"""RED contract: a failed/ambiguous broker capture is never flat evidence."""
import json
from types import SimpleNamespace

import pytest

from core import live_broker_preflight as preflight


class _Api:
    def __init__(self, *, positions=None, margin=None, raise_positions=False):
        self.futopt_account = SimpleNamespace(
            person_id="p", broker_id="b", account_id="a"
        )
        self._positions = positions
        self._margin = margin
        self._raise_positions = raise_positions

    def list_positions(self, *, account):
        if self._raise_positions:
            raise RuntimeError("position endpoint unavailable")
        return self._positions

    def list_trades(self):
        return []

    def margin(self, *, account):
        return self._margin

    def trading_limits(self, *, account):
        return []

    def snapshots(self, contracts):
        return []


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv(preflight.REQUEST_ENV, "1")
    monkeypatch.setattr(preflight, "diagnostics_dir", lambda: tmp_path)
    monkeypatch.setattr(preflight, "assert_broker_access_allowed", lambda: "mini")
    monkeypatch.setattr(
        "core.broker.shioaji_compat.get_contracts_list",
        lambda *_: [
            SimpleNamespace(code="TMFI6", delivery_date="2099/08/19"),
            SimpleNamespace(code="TMFJ6", delivery_date="2099/09/16"),
        ],
    )
    monkeypatch.setattr("core.broker.shioaji_compat.safe_subscribe", lambda *a, **k: None)
    monkeypatch.setattr(preflight, "_unsubscribe_bidask", lambda *a, **k: None)


def test_position_query_error_writes_unavailable_not_flat(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    response = preflight.run_once(
        lambda: _Api(raise_positions=True, margin=SimpleNamespace(available_margin=300000)),
        request_id="positions-error",
    )
    artifact = json.loads((tmp_path / "broker_snapshot_canonical.json").read_text())
    assert response["preflight"]["passed"] is False
    assert artifact["source"] == "unavailable"
    assert artifact["capture_error"] is True
    assert artifact["positions"] is None
    assert "POSITIONS_QUERY_FAILED" in artifact["error"]


def test_empty_positions_after_prior_holding_is_unavailable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    api = _Api(positions=[], margin=SimpleNamespace(available_margin=300000))
    prior = {
        "source": "live_broker", "mode": "live",
        "positions": [{"code": "TMFJ6", "qty": 1, "direction": "buy", "avg_cost": 46451}],
        "open_orders": [], "available_margin": 300000,
        "account_identity_hash": preflight._account_hash(api.futopt_account),
        "scope": "futopt", "captured_at": 1,
        "canonical_input_hash": "b" * 64, "session_id": "old",
    }
    (tmp_path / "broker_snapshot_canonical.json").write_text(json.dumps(prior))
    response = preflight.run_once(
        lambda: api,
        request_id="empty-after-position",
    )
    artifact = json.loads((tmp_path / "broker_snapshot_canonical.json").read_text())
    assert response["preflight"]["passed"] is False
    assert artifact["source"] == "unavailable"
    assert artifact["capture_error"] is True
    assert "POSITIONS_EMPTY_AFTER_PRIOR_HOLDING" in artifact["error"]


def test_empty_positions_different_account_scope_does_not_reuse_prior_holding(
        monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    prior = {
        "source": "live_broker", "mode": "live", "scope": "futopt",
        "positions": [{"code": "TMFJ6", "qty": 1, "direction": "buy"}],
        "open_orders": [], "available_margin": 300000,
        "account_identity_hash": "different-account", "captured_at": 1,
        "canonical_input_hash": "b" * 64, "session_id": "old",
    }
    (tmp_path / "broker_snapshot_canonical.json").write_text(json.dumps(prior))

    response = preflight.run_once(
        lambda: _Api(positions=[], margin=SimpleNamespace(available_margin=300000)),
        request_id="empty-different-account",
    )
    artifact = json.loads((tmp_path / "broker_snapshot_canonical.json").read_text())

    assert response["preflight"]["passed"] is True
    assert artifact["source"] == "live_broker"
    assert artifact["positions"] == []


def test_margin_unavailable_cannot_create_flat_artifact(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    response = preflight.run_once(
        lambda: _Api(positions=[], margin=None), request_id="margin-unavailable"
    )
    artifact = json.loads((tmp_path / "broker_snapshot_canonical.json").read_text())
    assert response["preflight"]["passed"] is False
    assert artifact["source"] == "unavailable"
    assert artifact["capture_error"] is True
    assert "MARGIN_UNAVAILABLE" in artifact["error"]


def test_gate_rejects_capture_error_even_if_positions_empty(tmp_path):
    from core.deployment_safety_gate import guard_flat_no_pending

    path = tmp_path / "capture.json"
    path.write_text(json.dumps({
        "source": "live_broker", "mode": "live", "capture_error": True,
        "error": "MARGIN_UNAVAILABLE", "positions": [], "open_orders": [],
        "captured_at": 9999999999999, "canonical_input_hash": "a" * 64,
    }))
    result = guard_flat_no_pending(str(path), {"effective_mode": "live"})
    assert not result.ok
    assert "GUARD_CAPTURE_UNAVAILABLE" in result.reasons
