"""RED/GREEN: the read-only preflight must observe a REAL broker position.

Live 2026-09-24 23:52: the deployment evidence capture (core.live_broker_preflight
via scripts/run_live_readonly_preflight.py and the deploy's capture_flat_evidence
stage) reported `positions: []` for an account the running app's own session was
reading as TMFJ6 Sell 1 / TMFK6 Buy 1 seconds earlier.  Two holes:

  1. the read-only path never called `update_status(account=...)`, so a fresh
     session's position view was never populated -> list_positions returned []
     (an empty read that the artifact still recorded as a live_broker success);
  2. the ambiguity guard (POSITIONS_EMPTY_AFTER_PRIOR_HOLDING) only fired when
     the PRIOR canonical artifact carried `scope == "futopt"` -- the running
     app's artifact does not, so the first false-flat overwrote real evidence.

The tests drive the REAL run_once with a Shioaji double whose position view only
populates after update_status (the observed SDK behaviour), then feed the derived
artifact to the REAL deployment gate.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import core.live_broker_preflight as lbp
from core.deployment_safety_gate import is_reconciled_mts_position


class _FakeApi:
    """Minimal Shioaji double: no order method exists, by construction."""

    def __init__(self, legs=None, update_status_ok=True):
        self.futopt_account = SimpleNamespace(person_id="P123", broker_id="B1",
                                              account_id="A1", account_no="1234567")
        self._legs = legs if legs is not None else [
            SimpleNamespace(code="TMFJ6", quantity=1, direction="Sell",
                            price=48176.0, pnl=2050.0),
            SimpleNamespace(code="TMFK6", quantity=1, direction="Buy",
                            price=48320.0, pnl=-1870.0),
        ]
        self._update_status_ok = update_status_ok
        self.status_refreshed = False
        self.calls = []

    # ── the only broker surfaces the read-only adapter may use ──
    def update_status(self, account=None, /, **kw):
        if account is None:
            raise TypeError("account is required")
        self.calls.append("update_status")
        if not self._update_status_ok:
            raise RuntimeError("ACCOUNT_STATUS_UNAVAILABLE")
        self.status_refreshed = True
        return SimpleNamespace(status="OK")

    def list_positions(self, account=None, /, **kw):
        if account is None:
            raise TypeError("account is required")
        self.calls.append("list_positions")
        # Observed 2026-09-24: without update_status the fresh session view is empty
        return list(self._legs) if self.status_refreshed else []

    def list_trades(self, account=None, /, **kw):
        if account is None:
            raise TypeError("account is required")
        self.calls.append("list_trades")
        return []

    def margin(self, account=None, /, **kw):
        if account is None:
            raise TypeError("account is required")
        return SimpleNamespace(available_margin=302520.0, equity_amount=1_000_000.0,
                               risk_indicator=1)

    def trading_limits(self, account=None, **kw):
        raise RuntimeError("trading_limits unavailable for this branch")

    def snapshots(self, contracts):
        return []

    def subscribe(self, contract, **kw):
        self.calls.append("subscribe")

    def unsubscribe(self, contract, **kw):
        pass

    def logout(self):
        pass


def _contract(code, delivery):
    return SimpleNamespace(code=code, delivery_date=delivery, name=code,
                           limit_up=None, limit_down=None, reference=None,
                           update_time=None, margin=None, unit=None)


@pytest.fixture()
def preflight_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_PREFLIGHT_NO_ORDERS", "1")
    monkeypatch.setattr(lbp, "assert_broker_access_allowed", lambda *a, **k: "mini")
    monkeypatch.setattr(
        lbp, "resolve_near_far_contracts",
        lambda api, product: (_contract("TMFJ6", date(2026, 10, 21)),
                              _contract("TMFK6", date(2026, 11, 18))))
    return tmp_path


def _canonical(tmp_path):
    import json
    return json.loads((tmp_path / "exports/trades/live/diagnostics/"
                       "broker_snapshot_canonical.json").read_text())


def test_fresh_session_position_is_observed_not_reported_flat(preflight_env):
    """A held spread must survive the read-only capture."""
    api = _FakeApi()
    resp = lbp.run_once(lambda: api, request_id="TEST-HOLD", product="TMF")

    assert "update_status" in api.calls, "the position view must be refreshed"
    assert api.calls.index("update_status") < api.calls.index("list_positions")
    assert resp["preflight"]["passed"] is True

    art = _canonical(preflight_env)
    legs = [p for p in art["positions"]
            if str(p.get("account", "")) in ("", "futures")]
    assert len(legs) == 2, "a two-leg position must not be reported flat"
    codes = sorted(p["code"] for p in legs)
    assert codes == ["TMFJ6", "TMFK6"]
    for row in legs:
        assert type(row["quantity"]) is int and row["quantity"] == 1
        assert row["direction"] in ("buy", "sell")
        assert row["avg_cost"] > 0
    assert (art.get("fetch_status") or {}).get("capture") == "OK"

    # the REAL deployment gate must accept this artifact for the held spread
    holding = {"canonical_legs": [
        {"code": "TMFJ6", "side": "SHORT", "qty": 1, "avg_cost": 48176.0},
        {"code": "TMFK6", "side": "LONG", "qty": 1, "avg_cost": 48320.0}]}
    assert is_reconciled_mts_position(
        art["positions"], art["open_orders"], {"canonical_holding": holding},
        allow_existing_mts_position=True) is True


def test_unrefreshable_account_status_fails_closed(preflight_env):
    """No position/status proof => UNAVAILABLE, never a live_broker flat."""
    api = _FakeApi(update_status_ok=False)
    resp = lbp.run_once(lambda: api, request_id="TEST-NOSTATUS", product="TMF")

    assert resp["preflight"]["passed"] is False
    art = _canonical(preflight_env)
    assert art["source"] == "unavailable"
    assert art["positions"] is None
    assert art.get("capture_error") is True
    assert (art.get("fetch_status") or {}).get("capture") == "FAIL"


def test_empty_capture_after_a_real_holding_is_flagged(preflight_env):
    """Tonight's hole: the RUNNING app's artifact carries the account hash but no
    `scope`, so the drift guard must not depend on that key."""
    import json
    d = preflight_env / "exports/trades/live/diagnostics"

    # 1. a real holding capture (the account's 2 legs)
    lbp.run_once(lambda: _FakeApi(), request_id="TEST-HOLD", product="TMF")
    prior = json.loads((d / "broker_snapshot_canonical.json").read_text())
    assert prior["source"] == "live_broker" and len(prior["positions"]) == 2
    prior.pop("scope", None)                 # the app's artifact has no scope
    (d / "broker_snapshot_canonical.json").write_text(json.dumps(prior))

    # 2. the same account now reads empty -> ambiguous, never proof of flat
    resp = lbp.run_once(lambda: _FakeApi(legs=[]), request_id="TEST-DRIFT", product="TMF")

    assert resp["preflight"]["passed"] is False
    assert "POSITIONS_EMPTY_AFTER_PRIOR_HOLDING" in resp["preflight"]["failed_checks"]
    art = json.loads((d / "broker_snapshot_canonical.json").read_text())
    assert art["source"] == "unavailable"
    assert art["positions"] is None
