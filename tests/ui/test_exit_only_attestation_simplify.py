"""EXIT_ONLY attestation UI simplification: no manual operator /
trade-id / evidence inputs; fixed dashboard-confirmed operator marker;
generated non-secret evidence; backend attestation contract unchanged.
"""
import inspect

import ui.dashboard as dash
from ui.dashboard import (
    _DASHBOARD_CONFIRMED_OPERATOR,
    _generated_exit_only_evidence,
    build_exit_only_attestation_request,
)

_LEGS = [
    {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
    {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
]


def test_exit_only_attestation_ui_has_no_manual_input_fields():
    """The EXIT_ONLY attestation UI must NOT collect manual operator /
    trade-id / evidence input fields."""
    src = inspect.getsource(dash)
    for key in ("exit_only_operator", "exit_only_trade_id",
                "exit_only_evidence", "exit_only_update_evidence"):
        assert f'key="{key}"' not in src, f"manual input {key} still present"


def test_dashboard_confirmed_attestation_payload_schema():
    """The dashboard-confirmed attestation builds the unchanged monitor
    schema: fixed operator marker + generated non-secret evidence."""
    evidence = _generated_exit_only_evidence(
        _LEGS, "ORD-20260813-000001", now_ms=1786600000000)
    payload = build_exit_only_attestation_request(
        None, operator=_DASHBOARD_CONFIRMED_OPERATOR,
        trade_id="ORD-20260813-000001", evidence=evidence,
        now_ms=1786600000000, expected_legs=_LEGS)
    assert payload["action"] == "ATTEST_EXIT_ONLY"
    assert payload["operator"] == "dashboard-confirmed"
    assert payload["trade_id"] == "ORD-20260813-000001"
    assert payload["expected_legs"] == _LEGS
    assert _DASHBOARD_CONFIRMED_OPERATOR in payload["evidence"]
    assert "password" not in payload["evidence"].lower()
    assert "secret" not in payload["evidence"].lower()


def test_attestation_payload_live_and_paper_capability():
    """The build contract accepts both live and paper capability shapes
    with the same schema; the capability's trade_id is always locked."""
    for mode in ("live", "paper"):
        cap = {"trade_id": f"T-{mode}", "legs": _LEGS, "mode": mode}
        payload = build_exit_only_attestation_request(
            cap, operator="dashboard-confirmed", trade_id="form-value",
            evidence="dashboard-confirmed evidence")
        assert payload["trade_id"] == f"T-{mode}"  # never form-provided
        assert payload["expected_legs"] == _LEGS
        assert payload["action"] == "ATTEST_EXIT_ONLY"
        assert payload["operator"] == "dashboard-confirmed"
