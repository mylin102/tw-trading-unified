#!/usr/bin/env python3
"""RED tests v6: Live Route Certification — MONITOR INTEGRATION phase
(codex round-7 P0-5/P0-6).

This file is EXPLICITLY RED until the separately-reviewed monitor wiring
phase: monitor.py is deliberately untouched in the core phase, so the AST
call-site assertion fails (hits=[522]) — that is the expected, documented
RED. The PAPER-path test asserts existing behavior (green).

P0-6 integration mapping (design §6.5): before any order route —
  1. cert, failures = certify_route(api, ...)        # fresh in-process
  2. failures non-empty → LIVE_QUARANTINED + audit reasons (fail-closed)
  3. ctx = transition_with_certificate(ctx, cert, issuer, ctx_runtime)
  4. not is_live_ready() → LIVE_QUARANTINED (assert_live_order_allowed raises)
PAPER path unchanged: paper_context() never consumes a certificate.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def _monitor_path():
    return Path(__file__).resolve().parents[2] / "strategies" / "futures" / "monitor.py"


def test_ast_call_site_scan_no_legacy_transition_in_monitor():
    """RED until wiring phase: monitor.py must not call
    transition_to_live_ready (known legacy bypass at :522)."""
    monitor = _monitor_path()
    assert monitor.exists(), f"monitor not found: {monitor}"
    tree = ast.parse(monitor.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "transition_to_live_ready":
                hits.append(node.lineno)
            elif isinstance(fn, ast.Name) and fn.id == "transition_to_live_ready":
                hits.append(node.lineno)
    assert not hits, f"monitor.py still calls transition_to_live_ready at: {hits}"


def test_monitor_uses_certificate_flow_before_order_routes():
    """RED until wiring phase: monitor.py must reference certify_route or
    transition_with_certificate (the future wiring) — today it does not."""
    monitor = _monitor_path()
    tree = ast.parse(monitor.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert "certify_route" in names or "transition_with_certificate" in names, \
        "monitor wiring (certify_route / transition_with_certificate) not present"


def test_paper_path_unchanged():
    """GREEN now: PAPER behavior identical to today — no live authorization;
    paper orders stay allowed (assert_live_order_allowed does NOT raise in
    paper mode); no certificate can flip PAPER into LIVE_READY."""
    from core.mode_transition import paper_context
    ctx = paper_context(account_id="A1")
    assert not ctx.is_live_ready()
    assert ctx.to_dict().get("live_order_allowed") is False
    # paper orders are legal by design — the gate only blocks LIVE orders
    ctx.assert_live_order_allowed()          # must NOT raise (paper path)
    # is_live_ready requires requested_mode == LIVE, so PAPER can never
    # become LIVE_READY no matter what (cert or not)
    assert ctx.to_dict().get("requested_mode") != "live"


# ── Phase 2 test matrix (RED until the separately-reviewed wiring phase) ───

def _repo_root():
    return Path(__file__).resolve().parents[2]


def test_release_identity_check_wired_before_certification():
    """(3) GREEN (Step: release identity): the LIVE startup path must
    verify release-dir HEAD == LRC_RELEASE_SHA (core/release_identity,
    wired via verify_release_identity) BEFORE any certification — the
    wiring exists in the monitor startup block."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert "verify_release_identity" in text, \
        "release-identity check not wired into monitor startup"


def test_margin_floor_config_key_defined():
    """(4) RED: the TMF pair margin floor must exist in the effective config
    (owner/version documented) — fail-closed until the config key lands."""
    cfg = _repo_root() / "config" / "futures.yaml"
    assert cfg.exists()
    text = cfg.read_text(encoding="utf-8")
    assert "live_required_margin_per_pair" in text, \
        "mts.live_required_margin_per_pair not defined in futures.yaml"


def test_startup_live_path_recertifies_with_certificate_flow():
    """(1)+(5) RED: the LIVE startup path must reference the certificate
    flow (certify_route / transition_with_certificate) — the legacy
    transition_to_live_ready path is closed in core, so without the wiring
    a LIVE startup can never reach LIVE_READY."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert "certify_route" in text or "transition_with_certificate" in text, \
        "monitor LIVE startup does not reference the certificate flow"


def test_reconnect_path_recertifies_after_safe_login():
    """(5) RED: main._try_shioaji_reconnect must re-certify after the
    reconnected safe_login (the new registry generation kills the old cert)."""
    main_py = _repo_root() / "main.py"
    assert main_py.exists()
    text = main_py.read_text(encoding="utf-8")
    assert "certify_route" in text or "transition_with_certificate" in text, \
        "main reconnect path does not re-certify after safe_login"


def test_logout_invalidates_monitor_certificate_route():
    """(5) RED: after broker logout the monitor must hold no usable
    certificate — shioaji_session.logout unregisters (core, green); the
    monitor must not retain a LIVE_READY context across it."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert "unregister_session" in text or "session_registry" in text, \
        "monitor does not reference session invalidation on logout"


# ── Phase 2 v2 matrix (RED until wiring; codex static-audit findings) ──────

STATE_CHANGING_ATTRS = ("place_order", "cancel_order", "update_order",
                        "modify_order")
# documented downstream-gated call sites (gate lives in OrderManager.submit)
# — function-name based (line numbers drift with edits)
DOWNSTREAM_GATED_FUNCS = {"_submit_order_via_manager"}
# the explicit, separately-authorized emergency marker (wiring phase)
EMERGENCY_MARKERS = ("EMERGENCY_FLATTEN", "emergency_flatten")
# the existing named emergency path (monitor.py:7519 _emergency_flatten_mts)
EMERGENCY_FUNCS = {"_emergency_flatten_mts"}


def _state_changing_sites(path):
    """(site_lineno, enclosing_fn, attr) for every state-changing call."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    sites = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in STATE_CHANGING_ATTRS):
            continue
        enclosing = None
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and fn.lineno <= node.lineno <= getattr(fn, "end_lineno", node.lineno):
                enclosing = fn.name
        sites.append((node.lineno, enclosing, node.func.attr))
    return sites


def _fn_calls_gate(tree, fn_name):
    """Gate dominance: any fail-closed gate pattern inside fn —
    assert_live_order_allowed (manager/order manager), the structured
    is_live_ready gate (Steps 2-4), or _gate_or_raise (Step 8 adapter)."""
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == fn_name:
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    name = (n.func.attr if isinstance(n.func, ast.Attribute)
                            else (n.func.id if isinstance(n.func, ast.Name) else ""))
                    if name in ("assert_live_order_allowed", "_gate_or_raise"):
                        return True
                if isinstance(n, ast.Attribute) and n.attr == "is_live_ready":
                    return True
    return False


def test_exhaustive_state_changing_routes_gated():
    """(1) RED: EVERY live state-changing call (place/cancel/update/modify)
    must be dominated by assert_live_order_allowed, a documented downstream
    gate, or an explicit emergency marker. Static audit found ungated sites:
    2707 safety-stop place, 2721 safety-stop cancel, 5208 _execute_trade."""
    monitor = _monitor_path()
    tree = ast.parse(monitor.read_text(encoding="utf-8"))
    monitor_text = monitor.read_text(encoding="utf-8")
    ungated = []
    for lineno, fn_name, attr in _state_changing_sites(monitor):
        if fn_name in DOWNSTREAM_GATED_FUNCS:
            continue                          # OrderManager.submit gates it
        if _fn_calls_gate(tree, fn_name):
            continue                          # gate dominates this function
        if fn_name in EMERGENCY_FUNCS:
            continue                          # inside the named emergency path
        ungated.append((lineno, fn_name, attr))
    assert not ungated, f"ungated live state-changing calls: {ungated}"


def test_order_manager_gate_exists():
    """(1) RED-support: OrderManager.submit must itself assert the gate
    (the downstream-gated allowlist is only valid while it does)."""
    om = _repo_root() / "core" / "order_management" / "order_manager.py"
    tree = ast.parse(om.read_text(encoding="utf-8"))
    gated_fns = [fn.name for fn in ast.walk(tree)
                 if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and _fn_calls_gate(tree, fn.name)]
    assert gated_fns, "order_manager must contain an assert gate function"
    assert "submit" in gated_fns or "_assert_live_allowed" in gated_fns, \
        f"submit path must reach the gate (gated fns: {gated_fns})"


def test_emergency_path_explicitly_named():
    """(2) RED: an emergency flatten path must be explicitly named,
    durable/idempotent and separately authorized — never an accidental
    bypass of the gate."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert any(m in text for m in EMERGENCY_MARKERS), \
        "no explicit emergency-flatten marker exists yet"


def test_reconnect_quarantines_before_relogin_and_recertifies():
    """(3) RED: reconnect must (a) quarantine the fm execution context
    BEFORE re-login, (b) fresh-certify ONLY after resubscribe — covering
    both the manual _try_shioaji_reconnect and the code-12 auto branch."""
    main_py = _repo_root() / "main.py"
    text = main_py.read_text(encoding="utf-8")
    assert "certify_route" in text and ("transition_with_certificate" in text
                                        or "LIVE_QUARANTINED" in text), \
        "reconnect does not quarantine + recertify atomically"


def test_execution_context_persisted_for_dashboard():
    """(4) RED: the execution context (effective_mode + audit_reasons) must
    be PERSISTED to the canonical dashboard-readable file
    ({TRADING_RUNTIME_DIR}/execution_context.json — core/execution_context_state)
    at every transition (to_dict alone is in-memory) with a restart-read
    round-trip."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert "execution_context_state" in text, \
        "execution-context persistence write point not wired into monitor"


def test_release_head_check_in_release_tree():
    """(5) GREEN (Step: release identity): the release identity check runs
    in the ACTUAL release tree — core/release_identity runs
    `git -C <release_dir> rev-parse HEAD` (never an arbitrary cwd) and
    the monitor wires verify_release_identity."""
    monitor = _monitor_path()
    text = monitor.read_text(encoding="utf-8")
    assert "verify_release_identity" in text, \
        "release-tree HEAD check not wired"
    verifier = _repo_root() / "core" / "release_identity.py"
    vtext = verifier.read_text(encoding="utf-8")
    assert "LRC_RELEASE_SHA" in vtext and "-C" in vtext, \
        "release-tree HEAD check must use git -C <release_dir>"


# ── Live wiring Step 1: startup transition path (bounded task) ───────────────

def _minimal_live_cfg(tmp_path):
    cfg = tmp_path / "futures_test.yaml"
    # [sealed live profile] the LIVE certification requires the
    # futures_live profile marker — the test cfg is a live profile
    cfg.write_text("ticker: TMF\nlive_trading: true\n"
                   "config_profile: futures_live\n", encoding="utf-8")
    return cfg


def _set_release_env(monkeypatch):
    # the LIVE startup verifies release-dir HEAD == LRC_RELEASE_SHA
    # BEFORE certification — the tests run at the repo HEAD
    import subprocess
    head = subprocess.check_output(
        ["git", "-C", str(_repo_root()), "rev-parse", "HEAD"],
        text=True).strip()
    monkeypatch.setenv("LRC_RELEASE_SHA", head)


def _stub_post_startup_gate(monkeypatch, ok=True, codes=()):
    """Stub the in-process post-startup gate for tests that focus on the
    cert/transition flow (the gate itself has dedicated tests)."""
    from core.deployment_safety_gate import DeploymentCheck, GuardResult
    from strategies.futures.monitor import FuturesMonitor

    def _fake(self):
        g = GuardResult(guard="post_startup", ok=ok, reasons=codes)
        return DeploymentCheck(ok=ok, results=(g,)), None

    monkeypatch.setattr(FuturesMonitor, "_run_post_startup_gate", _fake,
                        raising=False)


def test_no_hidden_cli_bypass():
    """[P0] the monitor NEVER shells out to check_deployment.py — the
    post-startup gate is in-process (imported), nothing skippable."""
    src = (_repo_root() / "strategies/futures/monitor.py").read_text(
        encoding="utf-8")
    assert "check_deployment.py" not in src, \
        "no CLI subprocess path may exist in the monitor"
    assert "_run_post_startup_gate" in src
    main_src = (_repo_root() / "main.py").read_text(encoding="utf-8")
    assert "check_deployment.py" not in main_src


def test_no_cert_startup_quarantines_no_certificate(tmp_path, monkeypatch):
    """No certificate at startup -> LIVE_QUARANTINED + NO_CERTIFICATE
    (fail-closed; live orders blocked)."""
    monkeypatch.chdir(tmp_path)  # avoid config/futures.yaml fallback
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    from strategies.futures.monitor import FuturesMonitor
    m = FuturesMonitor(api=None, config_path=str(cfg))
    ctx = m._execution_context
    assert ctx is not None
    assert not ctx.is_live_ready(), "no-cert startup must never authorize"
    assert "NO_CERTIFICATE" in ctx.audit_reasons, ctx.audit_reasons


def test_valid_cert_startup_uses_transition_with_certificate(tmp_path, monkeypatch):
    """Valid certificate path invokes transition_with_certificate with the
    issued cert + trusted runtime context (the ONLY path to LIVE_READY)."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    import core.live_route_certificate as lrc
    from core.mode_transition import (ModeTransitionState, live_preflight_context,
                                      with_effective_mode)
    cert_fake = object()
    runtime_fake = object()
    calls = {}

    def fake_certify(api, **kwargs):
        return (cert_fake, [])

    def fake_build(api, config, process_state=None):
        return runtime_fake

    def spy_transition(ctx, cert, issuer, *, runtime):
        calls["cert"] = cert
        calls["runtime"] = runtime
        calls["issuer"] = issuer
        return with_effective_mode(ctx, ModeTransitionState.LIVE_READY.value,
                                   live_order_allowed=True)

    monkeypatch.setattr(lrc, "certify_route", fake_certify)
    monkeypatch.setattr(lrc, "build_runtime_certification_context", fake_build)
    monkeypatch.setattr(lrc, "transition_with_certificate", spy_transition)

    from strategies.futures.monitor import FuturesMonitor
    api_fake = object()
    lrc.register_session(api_fake)   # registry-bound generation (D1 gate)
    _stub_post_startup_gate(monkeypatch)
    m = FuturesMonitor(api=api_fake, config_path=str(cfg))
    assert calls.get("cert") is cert_fake, "monitor must use the issued cert"
    assert calls.get("runtime") is runtime_fake, \
        "monitor must pass the trusted runtime context"
    assert m._execution_context.is_live_ready(), \
        "valid-cert path transitions to LIVE_READY"


def test_startup_binds_registry_generation_to_ctx(tmp_path, monkeypatch):
    """Post-startup session gate wiring: after login the registry holds the
    registry-bound generation — the monitor binds it into the durable ctx
    (session_id) so the post_startup gate can require it (standalone
    account hash never passes)."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    import core.live_route_certificate as lrc
    from core.mode_transition import (ModeTransitionState, live_preflight_context,
                                      with_effective_mode)
    cert_fake = object()
    runtime_fake = object()
    calls = {}

    def _fake_certify(*a, **k):
        calls["cert"] = a[1] if len(a) > 1 else None
        return (cert_fake, [])

    def _fake_runtime(*a, **k):
        calls["runtime"] = k.get("runtime") or a[-1]
        return runtime_fake

    def _fake_transition(ctx, cert, issuer, runtime=None):
        calls["transition"] = True
        return with_effective_mode(
            ctx, ModeTransitionState.LIVE_READY.value,
            live_order_allowed=True)

    from strategies.futures.monitor import FuturesMonitor
    monkeypatch.setattr(lrc, "certify_route", _fake_certify)
    monkeypatch.setattr(lrc, "build_runtime_certification_context",
                        _fake_runtime)
    monkeypatch.setattr(lrc, "transition_with_certificate", _fake_transition)
    # a registered session -> the registry holds a generation
    api = SimpleNamespace(futopt_account=object())
    lrc.register_session(api)
    gen = lrc.session_registry.generation(api)
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.live_trading = True
    m.api = api
    m.config_path = str(cfg)
    # D1: the binding happens on the QUARANTINED ctx BEFORE certification —
    # no circular LIVE_READY -> session -> gate -> LIVE_READY loop
    m._execution_context = live_preflight_context()
    m._persist_execution_context = lambda: None
    m._apply_reconcile_pending_gate = lambda: None
    m._bind_session_generation()
    assert m._execution_context.session_id == gen, \
        f"ctx must carry the registry-bound generation BEFORE " \
        f"certification: {m._execution_context.session_id} != {gen}"
    assert not m._execution_context.is_live_ready(), \
        "binding must not transition the ctx (still quarantined)"


def test_bind_precedes_certification_in_startup(tmp_path, monkeypatch):
    """D1 order contract: login -> registry generation -> atomic write of
    the quarantined ctx.session_id BEFORE certification/post_startup
    gate -> only then transition LIVE_READY."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    import core.live_route_certificate as lrc
    from core.mode_transition import ModeTransitionState, with_effective_mode
    order = []

    def fake_certify(api, **kwargs):
        return (object(), [])

    def fake_build(api, config, process_state=None):
        return object()

    def spy_transition(ctx, cert, issuer, *, runtime):
        order.append("transition")
        return with_effective_mode(ctx, ModeTransitionState.LIVE_READY.value,
                                   live_order_allowed=True)

    monkeypatch.setattr(lrc, "certify_route", fake_certify)
    monkeypatch.setattr(lrc, "build_runtime_certification_context", fake_build)
    monkeypatch.setattr(lrc, "transition_with_certificate", spy_transition)

    from strategies.futures.monitor import FuturesMonitor

    original_bind = FuturesMonitor._bind_session_generation

    def _spy_bind(self):
        order.append("bind")
        original_bind(self)          # real binding (sets ctx.session_id)

    monkeypatch.setattr(FuturesMonitor, "_bind_session_generation", _spy_bind)
    api_fake = SimpleNamespace(futopt_account=object())
    lrc.register_session(api_fake)
    _stub_post_startup_gate(monkeypatch)
    m = FuturesMonitor(api=api_fake, config_path=str(cfg))
    assert order[:2] == ["bind", "transition"], \
        f"binding must precede certification, got {order}"
    assert m._execution_context.is_live_ready()


def test_session_generation_race_blocks_transition(tmp_path, monkeypatch):
    """D1 race guard: a logout/relogin between the binding and the
    certification invalidates the old generation — the startup must NOT
    promote (transition_with_certificate skipped, ctx stays QUARANTINED
    with SESSION_GENERATION_MISMATCH)."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    import core.live_route_certificate as lrc
    from core.mode_transition import ModeTransitionState, with_effective_mode
    calls = {}

    def fake_certify(api, **kwargs):
        return (object(), [])

    def fake_build(api, config, process_state=None):
        return object()

    def spy_transition(ctx, cert, issuer, *, runtime):
        calls["transition"] = True
        return with_effective_mode(ctx, ModeTransitionState.LIVE_READY.value,
                                   live_order_allowed=True)

    monkeypatch.setattr(lrc, "certify_route", fake_certify)
    monkeypatch.setattr(lrc, "build_runtime_certification_context", fake_build)
    monkeypatch.setattr(lrc, "transition_with_certificate", spy_transition)

    from strategies.futures.monitor import FuturesMonitor
    api_fake = SimpleNamespace(futopt_account=object())
    lrc.register_session(api_fake)          # generation A

    original_bind = FuturesMonitor._bind_session_generation

    def _racy_bind(self):
        original_bind(self)                  # ctx.session_id = A
        lrc.unregister_session(api_fake)     # logout
        lrc.register_session(api_fake)       # relogin -> generation B

    monkeypatch.setattr(FuturesMonitor, "_bind_session_generation",
                        _racy_bind)
    m = FuturesMonitor(api=api_fake, config_path=str(cfg))
    assert "transition" not in calls, "race must skip the transition"
    assert not m._execution_context.is_live_ready()
    assert "SESSION_GENERATION_MISMATCH" in \
        m._execution_context.audit_reasons


def test_recertify_after_relogin_recovers_live_ready(tmp_path, monkeypatch):
    """D1 retry path: generation mismatch => QUARANTINED => relogin (new
    generation) => recertify => LIVE_READY. The old-generation path made
    ZERO order calls (transition skipped, orders blocked)."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    import core.live_route_certificate as lrc
    from core.mode_transition import ModeTransitionState, with_effective_mode
    calls = {"transitions": 0}

    def fake_certify(api, **kwargs):
        return (object(), [])

    def fake_build(api, config, process_state=None):
        return object()

    def spy_transition(ctx, cert, issuer, *, runtime):
        calls["transitions"] += 1
        return with_effective_mode(ctx, ModeTransitionState.LIVE_READY.value,
                                   live_order_allowed=True)

    monkeypatch.setattr(lrc, "certify_route", fake_certify)
    monkeypatch.setattr(lrc, "build_runtime_certification_context", fake_build)
    monkeypatch.setattr(lrc, "transition_with_certificate", spy_transition)

    from strategies.futures.monitor import FuturesMonitor
    from main import _recertify_after_reconnect
    api_fake = SimpleNamespace(futopt_account=object())
    lrc.register_session(api_fake)          # generation A

    original_bind = FuturesMonitor._bind_session_generation

    def _racy_bind(self):
        original_bind(self)                  # ctx.session_id = A
        lrc.unregister_session(api_fake)     # logout
        lrc.register_session(api_fake)       # relogin -> generation B

    monkeypatch.setattr(FuturesMonitor, "_bind_session_generation",
                        _racy_bind)
    m = FuturesMonitor(api=api_fake, config_path=str(cfg))
    # race path: quarantined, zero transitions, zero order calls
    assert calls["transitions"] == 0
    assert not m._execution_context.is_live_ready()
    assert "SESSION_GENERATION_MISMATCH" in \
        m._execution_context.audit_reasons
    # retry: restore the REAL bind (the racy monkeypatch must not race
    # again during recertify), then relogin already produced a NEW
    # generation — recertify binds the new generation + confirms +
    # transitions to LIVE_READY
    monkeypatch.setattr(FuturesMonitor, "_bind_session_generation",
                        original_bind)
    _stub_post_startup_gate(monkeypatch)
    ok = _recertify_after_reconnect(m, api_fake)
    assert ok, "recertify must succeed with the new generation"
    assert m._execution_context.is_live_ready()
    assert calls["transitions"] == 1


def test_post_startup_gate_runs_before_transition_exactly_once(tmp_path, monkeypatch):
    """[P0] the in-process post-startup gate runs BEFORE the transition
    and exactly once; a passing gate yields exactly one transition."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    import core.live_route_certificate as lrc
    from core.mode_transition import ModeTransitionState, with_effective_mode
    from core.deployment_safety_gate import DeploymentCheck, GuardResult
    from strategies.futures.monitor import FuturesMonitor
    calls = {"gate": 0, "transition": 0}

    def fake_certify(api, **kwargs):
        return (object(), [])

    def fake_build(api, config, process_state=None):
        return object()

    def spy_transition(ctx, cert, issuer, *, runtime):
        calls["transition"] += 1
        return with_effective_mode(ctx, ModeTransitionState.LIVE_READY.value,
                                   live_order_allowed=True)

    def _spy_gate(self):
        calls["gate"] += 1
        assert calls["transition"] == 0, \
            "post-startup gate must run BEFORE the transition"
        g = GuardResult(guard="post_startup", ok=True)
        return DeploymentCheck(ok=True, results=(g,)), None

    monkeypatch.setattr(FuturesMonitor, "_run_post_startup_gate", _spy_gate)
    monkeypatch.setattr(lrc, "certify_route", fake_certify)
    monkeypatch.setattr(lrc, "build_runtime_certification_context", fake_build)
    monkeypatch.setattr(lrc, "transition_with_certificate", spy_transition)
    api_fake = SimpleNamespace(futopt_account=object())
    lrc.register_session(api_fake)
    m = FuturesMonitor(api=api_fake, config_path=str(cfg))
    assert calls["gate"] == 1, "gate must run exactly once"
    assert calls["transition"] == 1, "exactly one transition"
    assert m._execution_context.is_live_ready()


def test_gate_fail_zero_transition_persisted(tmp_path, monkeypatch):
    """[P0] a failing gate => LIVE_QUARANTINED + POST_STARTUP_GATE_FAILED +
    the refusal codes persisted; ZERO transitions."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    import core.live_route_certificate as lrc
    from core.mode_transition import ModeTransitionState, with_effective_mode
    from core.deployment_safety_gate import DeploymentCheck, GuardResult
    from strategies.futures.monitor import FuturesMonitor
    calls = {"transition": 0}

    def fake_certify(api, **kwargs):
        return (object(), [])

    def spy_transition(ctx, cert, issuer, *, runtime):
        calls["transition"] += 1
        return with_effective_mode(ctx, ModeTransitionState.LIVE_READY.value,
                                   live_order_allowed=True)

    def _fail_gate(self):
        g = GuardResult(guard="post_startup", ok=False,
                        reasons=("GUARD_MARGIN_UNAVAILABLE",))
        return DeploymentCheck(ok=False, results=(g,)), None

    monkeypatch.setattr(FuturesMonitor, "_run_post_startup_gate", _fail_gate)
    monkeypatch.setattr(lrc, "certify_route", fake_certify)
    monkeypatch.setattr(lrc, "transition_with_certificate", spy_transition)
    api_fake = SimpleNamespace(futopt_account=object())
    lrc.register_session(api_fake)
    m = FuturesMonitor(api=api_fake, config_path=str(cfg))
    assert calls["transition"] == 0, "a failing gate must never transition"
    assert not m._execution_context.is_live_ready()
    assert "POST_STARTUP_GATE_FAILED" in m._execution_context.audit_reasons
    assert "GUARD_MARGIN_UNAVAILABLE" in m._execution_context.audit_reasons
    assert m._execution_context.live_order_allowed is False


def test_session_mismatch_gate_fails(tmp_path):
    """[P0] the core gate's flat guard refuses when the snapshot session
    != the bound generation (GUARD_SESSION_INTERVENED) — the post-startup
    gate cannot be fooled by a stale session."""
    import json
    import time
    from core.deployment_safety_gate import guard_flat_no_pending
    _ts = int(time.time() * 1000)
    snap = {"source": "live_broker", "mode": "live", "positions": [],
            "open_orders": [], "captured_at": _ts,
            "canonical_input_hash": "b" * 64,
            "session_id": "deadbeefdeadbeef",
            "account_identity_hash": "a" * 64}
    pf = tmp_path / "preflight.json"
    pf.write_text(json.dumps(snap), encoding="utf-8")
    ctx = {"session_id": "ab" * 8}   # the bound registry generation
    r = guard_flat_no_pending(str(pf), ctx)
    assert not r.ok and "GUARD_SESSION_INTERVENED" in r.reasons


def _real_schema_api():
    """A mock api shaped like the REAL shioaji 1.x instance: the read
    methods are list_positions/list_trades/margin (NOT positions/orders —
    the old attribute does not exist on the SDK)."""
    from types import SimpleNamespace
    acct = SimpleNamespace(account_id="A1", available_margin=None)
    margin_obj = SimpleNamespace(available_margin=343082.0)
    return SimpleNamespace(
        futopt_account=acct,
        stock_account=None,
        list_positions=lambda a: [],          # live flat
        list_trades=lambda a: [],
        margin=lambda a: margin_obj,
    )


def test_capture_uses_real_shioaji_schema(tmp_path, monkeypatch):
    """[P0 fix] the in-process capture must use the shioaji 1.x read
    methods (list_positions/list_trades/margin) — the old 'positions'
    attribute does not exist on the real SDK and made the capture fail
    (GUARD_MARGIN_UNAVAILABLE at the real startup)."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    from strategies.futures.monitor import FuturesMonitor
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.api = _real_schema_api()
    m.config_path = str(cfg)
    m._execution_context = SimpleNamespace(session_id="ab" * 8)
    snap = m._capture_post_startup_snapshot()
    assert not snap["errors"], snap["errors"]
    assert snap["available_margin"] == 343082.0
    assert snap["positions"] == []
    assert snap["open_orders"] == []
    assert snap["session_id"] == "ab" * 8
    assert isinstance(snap["captured_at"], int)
    assert snap["canonical_input_hash"]


def test_capture_normalize_with_account_tag(tmp_path):
    """positions carry the account tag (futures/stock) + normalized
    code/quantity/direction; open orders drop terminal states and keep
    order_id/code/status."""
    from types import SimpleNamespace
    from strategies.futures.monitor import FuturesMonitor
    pos = [SimpleNamespace(code="TMFH6", quantity=1,
                           direction=SimpleNamespace(name="Buy"))]
    out = FuturesMonitor._normalize_snapshot_positions(pos, "futures")
    assert out == [{"account": "futures", "code": "TMFH6", "quantity": 1,
                    "direction": "Buy"}]
    t = SimpleNamespace(code="TMFH6", status=SimpleNamespace(name="Pending"),
                        order=SimpleNamespace(id="o1"))
    orders = FuturesMonitor._normalize_snapshot_orders([t])
    assert orders == [{"order_id": "o1", "code": "TMFH6",
                       "status": "Pending"}]


def test_run_gate_passes_release_dir_manifests(tmp_path, monkeypatch):
    """[P0 fix] the in-process gate call must pass the DEPLOYED
    release_dir's canonical manifest paths — NEVER the CWD (a worktree/
    dir change must not silently lose the manifest guard)."""
    import os
    monkeypatch.chdir(tmp_path)     # a random CWD must not matter
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    from strategies.futures.monitor import FuturesMonitor
    from core.deployment_safety_gate import DeploymentCheck, GuardResult
    calls = {}

    import core.deployment_safety_gate as dg

    def _spy_check_deployment(**kw):
        calls["manifest_paths"] = kw.get("manifest_paths")
        g = GuardResult(guard="post_startup", ok=True)
        return DeploymentCheck(ok=True, results=(g,))

    monkeypatch.setattr(dg, "check_deployment", _spy_check_deployment)
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.api = _real_schema_api()
    m.config_path = str(cfg)
    m._execution_context = SimpleNamespace(session_id="ab" * 8)
    gate, _ = m._run_post_startup_gate()
    assert gate.ok
    rel = _repo_root()
    expect = [str(rel / "PHASE1_RC_CANDIDATE.md"),
              str(rel / "PHASE2_DEPLOYMENT_MANIFEST.md"),
              str(rel / "PHASE1_FINAL_FREEZE.md")]
    assert calls["manifest_paths"] == expect, calls["manifest_paths"]
    for p in calls["manifest_paths"]:
        assert os.path.exists(p), f"canonical manifest missing: {p}"


def test_gate_fail_keeps_process_alive(tmp_path, monkeypatch):
    """[P0 fix] a REAL (expected) gate refusal keeps the process ALIVE in
    LIVE_QUARANTINED: the constructor returns normally (the supervisor
    can keep monitoring), zero transitions, zero orders."""
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    import core.live_route_certificate as lrc
    from core.mode_transition import ModeTransitionState, with_effective_mode
    from core.deployment_safety_gate import DeploymentCheck, GuardResult
    from strategies.futures.monitor import FuturesMonitor
    calls = {"transition": 0}

    def fake_certify(api, **kwargs):
        return (object(), [])

    def spy_transition(ctx, cert, issuer, *, runtime):
        calls["transition"] += 1
        return with_effective_mode(ctx, ModeTransitionState.LIVE_READY.value,
                                   live_order_allowed=True)

    def _fail_gate(self):
        g = GuardResult(guard="post_startup", ok=False,
                        reasons=("GUARD_CAPTURE_MISMATCH",))
        return DeploymentCheck(ok=False, results=(g,)), None

    monkeypatch.setattr(FuturesMonitor, "_run_post_startup_gate", _fail_gate)
    monkeypatch.setattr(lrc, "certify_route", fake_certify)
    monkeypatch.setattr(lrc, "transition_with_certificate", spy_transition)
    api = _real_schema_api()
    lrc.register_session(api)
    m = FuturesMonitor(api=api, config_path=str(cfg))  # must NOT raise/exit
    assert calls["transition"] == 0
    assert not m._execution_context.is_live_ready()
    assert "POST_STARTUP_GATE_FAILED" in m._execution_context.audit_reasons
    assert m._execution_context.live_order_allowed is False
    assert getattr(m, "api", None) is api     # the process stays alive


def test_run_gate_passes_margin_available_same_source(tmp_path, monkeypatch):
    """[P0 wiring] the in-process gate must pass the fresh snapshot's
    available_margin VALUE + the SAME margin_evidence — the evidence has
    343082.0 but the guard received None (GUARD_MARGIN_UNAVAILABLE at the
    real release9 startup)."""
    monkeypatch.chdir(tmp_path)     # a random CWD must not matter
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    from strategies.futures.monitor import FuturesMonitor
    from core.deployment_safety_gate import DeploymentCheck, GuardResult
    calls = {}

    import core.deployment_safety_gate as dg

    def _spy_check_deployment(**kw):
        calls["margin_available"] = kw.get("margin_available")
        calls["margin_evidence"] = kw.get("margin_evidence")
        g = GuardResult(guard="post_startup", ok=True)
        return DeploymentCheck(ok=True, results=(g,))

    monkeypatch.setattr(dg, "check_deployment", _spy_check_deployment)
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.api = _real_schema_api()      # margin -> 343082.0
    m.config_path = str(cfg)
    m._execution_context = SimpleNamespace(session_id="ab" * 8)
    gate, _ = m._run_post_startup_gate()
    assert gate.ok
    assert calls["margin_available"] == 343082.0, calls["margin_available"]
    ev = calls["margin_evidence"]
    assert ev["available_margin"] == 343082.0
    assert ev["canonical_input_hash"] and ev["captured_at"]


def test_run_gate_manifest_exclude_parity_with_cli(tmp_path, monkeypatch):
    """[P0 wiring] the in-process gate passes the SAME manifest paths AND
    exclude semantics as the production CLI (the ACTIVE canonical freeze
    manifest rule) — the missing exclude made the exclude-self tree
    include the manifest docs (GUARD_MANIFEST_STALE at release9)."""
    import os
    monkeypatch.chdir(tmp_path)
    _set_release_env(monkeypatch)
    cfg = _minimal_live_cfg(tmp_path)
    from strategies.futures.monitor import FuturesMonitor
    from core.deployment_safety_gate import DeploymentCheck, GuardResult
    calls = {}

    import core.deployment_safety_gate as dg

    def _spy_check_deployment(**kw):
        calls["manifest_paths"] = kw.get("manifest_paths")
        calls["exclude_paths"] = kw.get("exclude_paths")
        g = GuardResult(guard="post_startup", ok=True)
        return DeploymentCheck(ok=True, results=(g,))

    monkeypatch.setattr(dg, "check_deployment", _spy_check_deployment)
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.api = _real_schema_api()
    m.config_path = str(cfg)
    m._execution_context = SimpleNamespace(session_id="ab" * 8)
    gate, _ = m._run_post_startup_gate()
    assert gate.ok
    rel = _repo_root()
    assert calls["manifest_paths"] == [
        str(rel / "PHASE1_RC_CANDIDATE.md"),
        str(rel / "PHASE2_DEPLOYMENT_MANIFEST.md"),
        str(rel / "PHASE1_FINAL_FREEZE.md")]
    assert calls["exclude_paths"] == [
        "PHASE1_RC_CANDIDATE.md", "PHASE2_DEPLOYMENT_MANIFEST.md",
        "PHASE1_FINAL_FREEZE.md"], calls["exclude_paths"]
    assert os.path.exists(calls["manifest_paths"][-1])


def test_rollback_parity_active_canonical_manifest(tmp_path):
    """[P0 wiring] guard-level parity: with the CLI's manifest paths +
    exclude semantics, a history-stale intermediate doc (no hash) is
    SKIPPED and the ACTIVE canonical freeze manifest (PHASE1_FINAL_FREEZE)
    governs — the verdict is CWD-independent and identical for the
    in-process wiring."""
    import subprocess
    from core.deployment_safety_gate import guard_rollback_manifest
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                    "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name",
                    "t"], check=True)
    # history-stale docs: RC_CANDIDATE + PHASE2 carry NO hash
    (repo / "PHASE1_RC_CANDIDATE.md").write_text(
        "# RC candidate (history doc — no frozen hash)\n", encoding="utf-8")
    (repo / "PHASE2_DEPLOYMENT_MANIFEST.md").write_text(
        "# Phase2 manifest (no frozen hash)\n", encoding="utf-8")
    (repo / "PHASE1_FINAL_FREEZE.md").write_text(
        "placeholder\n", encoding="utf-8")
    (repo / "main.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],
                   check=True)
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    manifests = [str(repo / "PHASE1_RC_CANDIDATE.md"),
                 str(repo / "PHASE2_DEPLOYMENT_MANIFEST.md"),
                 str(repo / "PHASE1_FINAL_FREEZE.md")]
    exclude = ["PHASE1_RC_CANDIDATE.md", "PHASE2_DEPLOYMENT_MANIFEST.md",
               "PHASE1_FINAL_FREEZE.md"]
    # the active freeze record (the exclude-self tree identity)
    from core.deployment_safety_gate import _exclude_self_tree_hash
    frozen = _exclude_self_tree_hash(str(repo), "HEAD", exclude)
    (repo / "PHASE1_FINAL_FREEZE.md").write_text(
        f"frozen_tree_hash: {frozen}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                    "freeze record"], check=True)
    # random CWD
    import os
    old = os.getcwd()
    os.chdir(tmp_path / "elsewhere")
    try:
        r = guard_rollback_manifest(str(repo), manifests, head,
                                    exclude_paths=exclude)
        assert r.ok, r.reasons
        # the SAME inputs WITHOUT the exclude semantics drift (the bug)
        r2 = guard_rollback_manifest(str(repo), manifests, head)
        assert not r2.ok and "GUARD_MANIFEST_STALE" in r2.reasons
    finally:
        os.chdir(old)
