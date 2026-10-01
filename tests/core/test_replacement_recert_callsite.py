"""Execute the exact nested production reconnect closure, never a replica."""
import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


def _actual_callback(monkeypatch, outcomes, *, restore_ok=True, swap_ok=True,
                     paper=False):
    import main
    from core.mode_transition import live_preflight_context
    from core.shioaji_session import SessionReconnectController

    calls, events = [], []
    old_api = object()
    new_api = SimpleNamespace(futopt_account=object())
    new_api.list_positions = lambda **kw: []
    mons = []
    for _ in outcomes:
        ctx = replace(live_preflight_context(), requested_mode="paper" if paper else "live",
                      effective_mode="paper_active" if paper else "live_ready",
                      live_order_allowed=not paper)
        mons.append(SimpleNamespace(_execution_context=ctx, api=old_api,
                                    client=SimpleNamespace(api=old_api)))
    om = SimpleNamespace(api=old_api, monitor=SimpleNamespace(api=old_api))
    monkeypatch.setattr(main, "_persist_ctx", lambda mon: None)

    def restore(api):
        calls.append("restore")
        assert all(not m._execution_context.live_order_allowed for m in mons)
        if not restore_ok:
            raise RuntimeError("restore failed")

    def recert(mon, api, *, defer_promotion=False):
        calls.append("recert")
        assert defer_promotion, "no live permission until every monitor is certified"
        assert all(not m._execution_context.live_order_allowed for m in mons)
        assert api is new_api and mon.api is new_api and mon.client.api is new_api
        value = outcomes[mons.index(mon)]
        if value == "raise":
            raise RuntimeError("cert failed")
        if value:
            return replace(mon._execution_context, effective_mode="live_ready",
                           live_order_allowed=True)
        return value

    env = dict(vars(main))
    env.update(futures_mons=mons, om=om, sj=SimpleNamespace(Shioaji=lambda: new_api),
               _session_reconnect=SessionReconnectController(max_attempts=1, sleep=lambda _: None),
               _session_login=lambda api: calls.append("login"),
               _restore_session_channels=restore,
               _session_event=lambda event, **kw: events.append((event, kw)),
               install_replacement_api=lambda old, new: swap_ok,
               _recertify_after_reconnect=recert)
    tree = ast.parse(Path("main.py").read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "_request_session_reconnect")
    wrapper = ast.FunctionDef(
        name="build", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="api")],
                                          kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[node, ast.Return(value=ast.Name(id=node.name, ctx=ast.Load()))],
        decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(code, "main.py", "exec"), env)
    return env["build"](old_api), mons, calls, events


@pytest.mark.parametrize("outcomes", [[True], [False], ["raise"], [True, False]])
def test_actual_replacement_callback_requires_every_live_recert(monkeypatch, outcomes):
    callback, mons, calls, events = _actual_callback(monkeypatch, outcomes)
    ok = callback(RuntimeError("SessionNotEstablished"))
    expected = all(v is True for v in outcomes)
    assert ok is expected
    assert calls.count("recert") >= 1
    assert all(m._execution_context.live_order_allowed is expected for m in mons)
    assert any(e == "SESSION_RECONNECT_READY" for e, _ in events) is expected
    if expected:
        assert calls.index("restore") < calls.index("recert")


@pytest.mark.parametrize("restore_ok,swap_ok", [(False, True), (True, False)])
def test_actual_replacement_failure_never_recertifies(monkeypatch, restore_ok, swap_ok):
    callback, mons, calls, events = _actual_callback(
        monkeypatch, [True], restore_ok=restore_ok, swap_ok=swap_ok)
    assert callback(RuntimeError("SessionNotEstablished")) is False
    assert "recert" not in calls
    assert all(not m._execution_context.live_order_allowed for m in mons)
    assert not any(e == "SESSION_RECONNECT_READY" for e, _ in events)


def test_actual_paper_replacement_never_promotes_live(monkeypatch):
    callback, mons, calls, events = _actual_callback(monkeypatch, [True], paper=True)
    assert callback(RuntimeError("SessionNotEstablished")) is True
    assert "recert" not in calls
    assert mons[0]._execution_context.effective_mode == "paper_active"
    assert mons[0]._execution_context.live_order_allowed is False
