"""Real recertify callsite must bind authenticated facts, never a flag."""
from dataclasses import replace
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("initial_account,runtime_account,generation,late_confirm,expected", [
    (None, "proved-account", "new-generation", True, True),
    ("proved-account", "proved-account", "new-generation", True, True),
    ("different-account", "proved-account", "new-generation", True, False),
    (None, "", "new-generation", True, False),
    (None, "proved-account", "", True, False),
    (None, "proved-account", "new-generation", False, False),
])
def test_real_recertify_identity_and_final_generation_guard(
        monkeypatch, initial_account, runtime_account, generation, late_confirm, expected):
    import main
    import core.live_route_certificate as certificate
    from core.mode_transition import live_preflight_context

    ctx = replace(live_preflight_context(), effective_mode="live_quarantined",
                  account_id_hash=initial_account, session_id="old-generation")
    runtime = SimpleNamespace(account_hash=runtime_account, session_generation=generation)
    mon = SimpleNamespace(_execution_context=ctx, config_path="fixture-live.yaml")
    calls = []

    def bind():
        mon._execution_context = replace(mon._execution_context, session_id=generation)

    def confirm():
        calls.append("confirm")
        return True if calls.count("confirm") == 1 else late_confirm

    def gate():
        calls.append("gate")
        assert mon._execution_context.account_id_hash == runtime_account
        assert mon._execution_context.session_id == generation
        assert mon._execution_context.live_order_allowed is False
        return SimpleNamespace(ok=True), None

    def transition(ctx, *args, **kwargs):
        calls.append("transition")
        return replace(ctx, effective_mode="live_ready", live_order_allowed=True)

    mon._bind_session_generation = bind
    mon._confirm_session_generation = confirm
    mon._run_post_startup_gate = gate
    monkeypatch.setattr(main, "_persist_ctx", lambda mon: None)
    monkeypatch.setattr(certificate, "certify_route", lambda *a, **kw: (object(), []))
    monkeypatch.setattr(certificate, "build_runtime_certification_context", lambda *a: runtime)
    monkeypatch.setattr(certificate, "transition_with_certificate", transition)
    assert main._recertify_after_reconnect(mon, object()) is expected
    assert ("transition" in calls) is expected
    assert mon._execution_context.live_order_allowed is expected
    if not runtime_account or not generation or initial_account not in (None, runtime_account):
        assert "gate" not in calls
    if expected:
        assert calls[-2:] == ["confirm", "transition"]
