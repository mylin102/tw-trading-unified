"""2026-10-08 Codex: deployment orchestration must not report partial success."""
from types import SimpleNamespace

import pytest


class Operations:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __getattr__(self, stage):
        def call(*args):
            self.calls.append(stage)
            if stage == self.fail:
                raise RuntimeError(stage)
        return call


def run(ops, operation="release", apply=True):
    from scripts.deployment.deploy_mini import execute
    return execute(SimpleNamespace(operation=operation, apply=apply,
                                   maintenance_approved=True, mode="paper"), ops)


def test_release_real_orchestrator_orders_all_stages():
    ops = Operations()
    run(ops)
    assert ops.calls == ["preflight", "lock", "backup", "stop", "install",
                         "start", "observe", "save", "unlock", "complete"]


@pytest.mark.parametrize("stage", ["preflight", "backup", "stop", "install", "start", "observe", "save"])
def test_failed_release_never_unlocks_or_succeeds(stage):
    ops = Operations(fail=stage)
    with pytest.raises(RuntimeError, match=stage):
        run(ops)
    assert "unlock" not in ops.calls and "complete" not in ops.calls
    if stage == "observe":
        assert "save" not in ops.calls


def test_parameter_update_never_calls_pm2_or_moves_head():
    ops = Operations()
    run(ops, "params")
    assert ops.calls == ["preflight", "lock", "backup", "publish_params",
                         "observe", "unlock", "complete"]


def test_plan_is_read_only():
    ops = Operations()
    run(ops, apply=False)
    assert ops.calls == ["preflight", "plan"]


def test_live_cannot_enter_paper_workflow():
    from scripts.deployment.deploy_mini import execute, DeployError
    ops = Operations()
    with pytest.raises(DeployError, match="LIVE"):
        execute(SimpleNamespace(mode="live", operation="release", apply=True,
                                maintenance_approved=True), ops)
    assert ops.calls == []


def test_release_requires_explicit_maintenance_approval():
    from scripts.deployment.deploy_mini import execute, DeployError
    ops = Operations()
    with pytest.raises(DeployError, match="MAINTENANCE"):
        execute(SimpleNamespace(mode="paper", operation="release", apply=True,
                                maintenance_approved=False), ops)
    assert ops.calls == []


def test_pin_verifier_rejects_the_observed_stale_case(tmp_path):
    from scripts.deployment.deploy_mini import verify_app, DeployError
    row = {"name": "dashboard", "pid": 12, "pm2_env": {
        "pm_cwd": str(tmp_path), "TRADING_CANONICAL_ROOT": str(tmp_path),
        "status": "online", "LRC_RELEASE_SHA": "c" * 40,
        "TRADING_RUNTIME_DIR": str(tmp_path / "runtime"),
    }}
    with pytest.raises(DeployError, match="SHA"):
        verify_app(row, tmp_path, "9" * 40, tmp_path / "runtime")


def test_params_allow_only_width_change():
    from scripts.deployment.deploy_mini import updated_width, DeployError
    old = b"ticker: TMF\nlive_trading: false\nmts:\n  params:\n    max_spread_width: 3\n"
    new = updated_width(old, 4)
    import yaml
    assert yaml.safe_load(new)["mts"]["params"]["max_spread_width"] == 4
    assert yaml.safe_load(old)["mts"]["params"]["max_spread_width"] == 3
    for bad in (0, float("nan"), float("inf"), True):
        with pytest.raises((DeployError, ValueError)):
            updated_width(old, bad)


# 2026-10-12 Codex: run real filesystem/Git/CLI/backend stages, mocking PM2 only.
@pytest.fixture
def mini(tmp_path, monkeypatch):
    import ast
    import copy
    import hashlib
    import json
    import plistlib
    import subprocess
    import sys
    from pathlib import Path
    from scripts.deployment import deploy_mini as deploy

    code = Path(__file__).resolve().parents[2]
    baseline = b"ticker: TMF\nlive_trading: false\nmts:\n  params:\n    max_spread_width: 3\n"
    roots = {n: tmp_path / n for n in ("trading-system", "dashboard")}
    runtime = tmp_path / "runtime"
    (runtime / "paper/logs").mkdir(parents=True)
    (runtime / "paper/data/state").mkdir(parents=True)
    (runtime / "data/state").mkdir(parents=True)
    (runtime / "paper/logs/mts_trade_fills.jsonl").write_text("")
    log = runtime / "out.log"
    log.write_text("")
    def git(root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    classifier = [n for n in ast.parse((code / "ui/dashboard.py").read_text()).body
                  if isinstance(n, ast.FunctionDef) and n.name in
                  {"load_active_runtime_truth", "summarize_execution_context"}]
    for name, root in roots.items():
        root.mkdir()
        (root / "config").mkdir()
        (root / "core").mkdir()
        (root / "ui").mkdir()
        (root / ".gitignore").write_text(".deployment-target\ndata/\n")
        (root / "config/futures.yaml").write_bytes(baseline)
        (root / "core/futures_profiles.py").write_text((code / "core/futures_profiles.py").read_text())
        (root / "ui/dashboard.py").write_text("\n\n".join(ast.unparse(n) for n in classifier))
        (root / "ecosystem.config.js").write_text("module.exports={apps:[{name:'trading-system',cwd:__dirname}]};")
        git(root, "init", "-q")
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "config", "user.name", "offline test")
        git(root, "add", ".")
        git(root, "commit", "-qm", "base")
        identity = {"deployment_id": "mini", "host_role": "runtime-primary", "instance_id": name,
                    "repo_path": str(root), "execution_modes": ["paper"], "allowed_operations": ["live_runtime"]}
        (root / ".deployment-target").write_text(json.dumps(identity))
    heads = {n: git(r, "rev-parse", "HEAD") for n, r in roots.items()}
    pm2_home = tmp_path / "pm2"
    pm2_home.mkdir()
    plist = tmp_path / "launchd.plist"
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"PM2_HOME": str(pm2_home)}}))
    site = {"mts_root": str(roots["trading-system"]), "dashboard_root": str(roots["dashboard"]),
            "runtime_dir": str(runtime), "python_bin": sys.executable, "pm2_bin": "/pm2fake",
            "pm2_home": str(pm2_home), "launchd_plist": str(plist)}
    site_path = tmp_path / "site.json"
    site_path.write_text(json.dumps(site))
    rows = []
    for i, (name, root) in enumerate(roots.items()):
        env = {"pm_cwd": str(root), "status": "online", "LRC_RELEASE_SHA": heads[name],
               "TRADING_CANONICAL_ROOT": str(root), "TRADING_RUNTIME_DIR": str(runtime),
               "TRADING_FUTURES_MODE": "paper", "restart_time": 0, "pm_out_log_path": str(log),
               "args": (["-c", "background", sys.executable, "main.py", "--config", "futures"]
                        if name == "trading-system" else ["--server.port", "8500"])}
        env.update({key: "offline-only" for key in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY", "SHIOAJI_CA_PATH",
                                                    "SHIOAJI_CA_PASSWD", "TRADING_RUNTIME_ID")})
        rows.append({"name": name, "pid": 1100 + i, "pm2_env": env})
    rows.append({"name": "stock-runner", "pid": 99, "pm2_env": {
        "pm_cwd": str(tmp_path / "stock"), "status": "online", "restart_time": 0}})
    context = {"requested_mode": "paper", "effective_mode": "paper_active", "live_order_allowed": False,
               "state_namespace": "paper", "audit_reasons": [], "revision": 1,
               "config_hash": hashlib.sha256(baseline).hexdigest()}

    def engine_tick():
        pid = deploy.one(rows, "trading-system")["pid"]
        shared = runtime / "config/futures.paper.yaml"
        if shared.exists():
            context["config_hash"] = hashlib.sha256(shared.read_bytes()).hexdigest()
        (runtime / "execution_context.json").write_text(json.dumps(context))
        (runtime / "paper/data/state/mts_position_state.json").write_text(json.dumps({
            "has_position": False, "writer_identity": "mts_spread:" + str(pid)}))
        (runtime / "data/state/trading_safety_state.json").write_text(json.dumps({
            "pid": pid, "entry_allowed": True, "account_health": "HEALTHY"}))
    engine_tick()
    calls = []
    original = deploy.MiniDeployment.command
    def command(self, argv, *, root=None, env=None, body=None):
        if argv[0] == "git":
            return original(self, argv, root=root, env=env, body=body)
        calls.append(argv[:])
        if argv[0] == "node":
            return b""
        if argv[0] == "/bin/ps":
            return (str(deploy.one(rows, "trading-system")["pid"]) + " python3 main.py --config futures\n").encode()
        assert argv[0] == "/pm2fake"
        action = argv[1]
        if action == "jlist":
            engine_tick()
            return json.dumps(rows).encode()
        if action == "save":
            (pm2_home / "dump.pm2").write_text(json.dumps([dict(r["pm2_env"], name=r["name"]) for r in rows]))
            return b""
        name = "trading-system" if action == "start" else argv[2]
        row = deploy.one(rows, name)
        if action in ("stop", "delete"):
            row["pid"] = 0
            row["pm2_env"]["status"] = "stopped"
        else:
            row["pid"] = 2200 if name == "trading-system" else 2201
            row["pm2_env"].update({k: env[k] for k in
                                    ("LRC_RELEASE_SHA", "TRADING_CANONICAL_ROOT", "TRADING_RUNTIME_DIR")})
            row["pm2_env"]["status"] = "online"
            log.write_text("PAPER_STARTUP_READY: local simulation\n" + "\n".join(
                "Subscribed " + far + "TMF " + kind + ": CODE"
                for far in ("", "far-month ") for kind in ("tick", "bidask")))
            engine_tick()
        return b""
    monkeypatch.setattr(deploy.MiniDeployment, "command", command)
    def killed(pid, signal):
        raise ProcessLookupError
    monkeypatch.setattr(deploy.os, "kill", killed)
    clock = iter(range(0, 10000, 5))
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(deploy.urllib.request, "urlopen", lambda *a, **kw: Response())
    from core import date_utils
    monkeypatch.setattr(date_utils, "is_taifex_futures_market_open", lambda: False)
    return SimpleNamespace(deploy=deploy, roots=roots, runtime=runtime, heads=heads, site=site_path,
                           git=git, calls=calls, rows=rows, baseline=baseline, tick=engine_tick,
                           protected=copy.deepcopy(rows[-1]))


def test_actual_params_cli_no_pm2_mutations_no_source_edits(mini):
    shared = mini.runtime / "config/futures.paper.yaml"
    shared.parent.mkdir()
    shared.write_bytes(mini.baseline)
    mini.tick()
    assert mini.deploy.main(["--site", str(mini.site), "--apply", "params", "--max-spread-width", "4"]) == 0
    assert all(c[1] == "jlist" for c in mini.calls if c[0] == "/pm2fake")
    assert all(mini.git(root, "rev-parse", "HEAD") == mini.heads[name] for name, root in mini.roots.items())
    assert all((root / "config/futures.yaml").read_bytes() == mini.baseline for root in mini.roots.values())
    assert mini.rows[-1] == mini.protected
    assert not (mini.roots["trading-system"] / "data/maintenance_entry_lock.flag").exists()
    assert list((mini.runtime / "logs/deployments").glob("*/complete.json"))


def test_actual_release_cli_pins_both_roots_before_save(mini):
    import hashlib
    candidates = {}
    for name, root in mini.roots.items():
        (root / "version.txt").write_text("new release")
        mini.git(root, "add", "version.txt")
        mini.git(root, "commit", "-qm", "candidate")
        tree = mini.git(root, "ls-tree", "-r", "HEAD")
        (root / "PHASE1_FINAL_FREEZE.md").write_text("frozen_tree_hash: " + hashlib.sha256(tree.encode()).hexdigest() + "\n")
        mini.git(root, "add", "PHASE1_FINAL_FREEZE.md")
        mini.git(root, "commit", "-qm", "freeze")
        candidates[name] = mini.git(root, "rev-parse", "HEAD")
        # A stopped-source fixture: return current worktree to the baseline.
        mini.git(root, "switch", "--detach", mini.heads[name])
    assert mini.deploy.main(["--site", str(mini.site), "--apply", "release", "--maintenance-approved",
                             "--mts-sha", candidates["trading-system"], "--dashboard-sha", candidates["dashboard"]]) == 0
    for name, root in mini.roots.items():
        assert mini.git(root, "rev-parse", "HEAD") == candidates[name]
        assert mini.deploy.one(mini.rows, name)["pm2_env"]["LRC_RELEASE_SHA"] == candidates[name]
    assert mini.rows[-1] == mini.protected
    assert any(c[1] == "save" for c in mini.calls if c[0] == "/pm2fake")
    assert list((mini.runtime / "logs/deployments").glob("*/complete.json"))


def test_actual_params_failure_keeps_lock_and_never_saves(mini, monkeypatch):
    shared = mini.runtime / "config/futures.paper.yaml"
    shared.parent.mkdir()
    shared.write_bytes(mini.baseline)
    mini.tick()
    def unavailable(*args, **kwargs):
        raise OSError("offline health failure")
    monkeypatch.setattr(mini.deploy.urllib.request, "urlopen", unavailable)
    assert mini.deploy.main(["--site", str(mini.site), "--apply", "params", "--max-spread-width", "4"]) == 1
    assert (mini.roots["trading-system"] / "data/maintenance_entry_lock.flag").exists()
    assert not list((mini.runtime / "logs/deployments").glob("*/complete.json"))
    assert list((mini.runtime / "logs/deployments").glob("*/failed.json"))
    assert all(c[1] == "jlist" for c in mini.calls if c[0] == "/pm2fake")


def test_actual_plan_creates_no_profile_or_entry_lock(mini):
    shared = mini.runtime / "config/futures.paper.yaml"
    shared.parent.mkdir()
    shared.write_bytes(mini.baseline)
    mini.tick()
    assert mini.deploy.main(["--site", str(mini.site), "params", "--max-spread-width", "4"]) == 0
    assert shared.read_bytes() == mini.baseline
    assert not (mini.runtime / "logs/deployments").exists()
    assert not (mini.roots["trading-system"] / "data/maintenance_entry_lock.flag").exists()


def test_wrong_host_refused_before_any_pm2_call(mini):
    import json
    target = mini.roots["trading-system"] / ".deployment-target"
    value = json.loads(target.read_text())
    value["deployment_id"] = "air4"
    target.write_text(json.dumps(value))
    assert mini.deploy.main(["--site", str(mini.site), "params", "--max-spread-width", "4"]) == 1
    assert mini.calls == []


# 2026-10-12 Codex: engine-only must preserve a running dirty-data dashboard.
def engine_only_candidate(mini):
    import hashlib
    root = mini.roots['trading-system']
    (root / 'version.txt').write_text('engine candidate')
    mini.git(root, 'add', 'version.txt')
    mini.git(root, 'commit', '-qm', 'candidate')
    tree = mini.git(root, 'ls-tree', '-r', 'HEAD')
    (root / 'PHASE1_FINAL_FREEZE.md').write_text('frozen_tree_hash: ' + hashlib.sha256(tree.encode()).hexdigest() + '\n')
    mini.git(root, 'add', 'PHASE1_FINAL_FREEZE.md')
    mini.git(root, 'commit', '-qm', 'freeze')
    sha = mini.git(root, 'rev-parse', 'HEAD')
    mini.git(root, 'switch', '--detach', mini.heads['trading-system'])
    dash = mini.roots['dashboard']
    csv = dash / 'data/taifex_raw/STOCK_2330_5m.csv'
    csv.parent.mkdir(parents=True)
    csv.write_text('old data\n')
    mini.git(dash, 'add', '-f', str(csv))
    mini.git(dash, 'commit', '-qm', 'existing generated data')
    mini.heads['dashboard'] = mini.git(dash, 'rev-parse', 'HEAD')
    mini.deploy.one(mini.rows, 'dashboard')['pm2_env']['LRC_RELEASE_SHA'] = mini.heads['dashboard']
    csv.write_text('updated data\n')
    return ['--site', str(mini.site), '--apply', 'release', '--maintenance-approved',
            '--engine-only', '--mts-sha', sha, '--dashboard-sha', mini.heads['dashboard']], csv


def test_actual_engine_only_preserves_dashboard_pid_head_and_dirty_csv(mini):
    import copy
    args, csv = engine_only_candidate(mini)
    before = copy.deepcopy(mini.deploy.one(mini.rows, 'dashboard'))
    assert mini.deploy.main(args) == 0
    assert mini.deploy.one(mini.rows, 'dashboard') == before
    assert mini.git(mini.roots['dashboard'], 'rev-parse', 'HEAD') == mini.heads['dashboard']
    assert csv.read_text() == 'updated data\n'
    mutations = [c for c in mini.calls if c[0] == '/pm2fake' and c[1] != 'jlist']
    assert not any('dashboard' in c for c in mutations)
    assert any(c[1] == 'save' for c in mutations)
    assert not (mini.roots['trading-system'] / 'data/maintenance_entry_lock.flag').exists()


@pytest.mark.parametrize('case', ['both', 'dashboard_sha', 'dashboard_code', 'engine_dirty', 'same_root'])
def test_actual_engine_only_refuses_unsafe_scope_before_mutation(mini, case):
    import json
    args, _ = engine_only_candidate(mini)
    if case == 'both':
        args.remove('--engine-only')
    elif case == 'dashboard_sha':
        args[-1] = mini.heads['trading-system']
    elif case == 'dashboard_code':
        with (mini.roots['dashboard'] / 'ui/dashboard.py').open('a') as h:
            h.write('\n# foreign source edit\n')
    elif case == 'engine_dirty':
        with (mini.roots['trading-system'] / 'config/futures.yaml').open('a') as h:
            h.write('\n# foreign config edit\n')
    else:
        site = json.loads(mini.site.read_text())
        site['dashboard_root'] = site['mts_root']
        mini.site.write_text(json.dumps(site))
    assert mini.deploy.main(args) == 1
    assert all(c[1] == 'jlist' for c in mini.calls if c[0] == '/pm2fake')
    assert not (mini.runtime / 'logs/deployments').exists()


@pytest.mark.parametrize('change', ['csv', 'pid'])
def test_actual_engine_only_detects_preserved_dashboard_drift(mini, monkeypatch, change):
    args, csv = engine_only_candidate(mini)
    original = mini.deploy.MiniDeployment.start
    def start(self):
        original(self)
        if change == 'csv':
            csv.write_text('concurrent writer\n')
        else:
            mini.deploy.one(mini.rows, 'dashboard')['pid'] += 1
    monkeypatch.setattr(mini.deploy.MiniDeployment, 'start', start)
    assert mini.deploy.main(args) == 1
    assert (mini.roots['trading-system'] / 'data/maintenance_entry_lock.flag').exists()
    assert not any(c[1] == 'save' for c in mini.calls if c[0] == '/pm2fake')


def _write_failed_observe_receipt(mini, transaction_id):
    import json

    targets = {
        name: json.loads((root / ".deployment-target").read_text())
        for name, root in mini.roots.items()
    }
    receipt = mini.runtime / "logs/deployments" / transaction_id
    receipt.mkdir(parents=True)
    (receipt / "before.json").write_text(json.dumps({
        "heads": mini.heads,
        # Historical before.json records every PM2 app, not only the apps
        # managed by this engine-only deployment.
        "pm2": {
            name: {"pid": 100 + i, "status": "online", "cwd": str(root),
                  "sha": mini.heads[name]}
            for i, (name, root) in enumerate(mini.roots.items())
        } | {"pm2-logrotate": {"pid": 1, "status": "online"}},
        "operation": "release",
        "mode": "paper",
        "managed_apps": ["trading-system"],
        "deployment_targets": targets,
        "ticker": "TMF",
    }))
    (receipt / "failed.json").write_text(json.dumps({
        "stage": "observe",
        "entry_lock_retained": True,
        "recovery": "Review before.json; no automatic state rewrite or restart.",
    }))
    lock = mini.roots["trading-system"] / "data/maintenance_entry_lock.flag"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(transaction_id)
    return receipt, lock


def test_resume_adopts_only_matching_failed_observe_lock_and_keeps_it_until_complete(mini, monkeypatch):
    import json

    transaction_id = "codex-" + "a" * 32
    old_receipt, lock = _write_failed_observe_receipt(mini, transaction_id)
    args, _ = engine_only_candidate(mini)
    args.extend(["--resume-failed-transaction", transaction_id])
    observed_lock_tokens = []
    original = mini.deploy.MiniDeployment.command

    def command_with_lock_assertion(self, argv, **kwargs):
        if argv[0] == "/pm2fake" and argv[1] in {"stop", "delete", "start", "save"}:
            observed_lock_tokens.append(lock.read_text() if lock.exists() else None)
        return original(self, argv, **kwargs)

    monkeypatch.setattr(mini.deploy.MiniDeployment, "command", command_with_lock_assertion)
    assert mini.deploy.main(args) == 0
    assert observed_lock_tokens and set(observed_lock_tokens) == {transaction_id}
    assert not lock.exists()
    assert (old_receipt / "failed.json").exists()
    completed = next((mini.runtime / "logs/deployments").glob("*/complete.json"))
    assert json.loads(completed.read_text())["resumed_from"] == transaction_id


def test_resume_rejects_lock_without_matching_failed_receipt_before_pm2(mini):
    transaction_id = "codex-" + "b" * 32
    lock = mini.roots["trading-system"] / "data/maintenance_entry_lock.flag"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(transaction_id)
    args, _ = engine_only_candidate(mini)
    args.extend(["--resume-failed-transaction", transaction_id])
    assert mini.deploy.main(args) == 1
    assert mini.calls == []
    assert lock.read_text() == transaction_id
    assert not list((mini.runtime / "logs/deployments").glob("*/complete.json"))


def test_resume_started_engine_after_start_failure_only_verifies_saves_and_unlocks(mini, monkeypatch):
    import json

    args, _ = engine_only_candidate(mini)
    original_id = "codex-" + "c" * 32
    _write_failed_observe_receipt(mini, original_id)
    start_id = "codex-" + "d" * 32
    old_heads = dict(mini.heads)
    old_apps = {row["name"]: mini.deploy.brief(row) for row in mini.rows}
    targets = {name: json.loads((root / ".deployment-target").read_text())
               for name, root in mini.roots.items()}
    failed_start = mini.runtime / "logs/deployments" / start_id
    failed_start.mkdir(parents=True)
    (failed_start / "before.json").write_text(json.dumps({
        "heads": old_heads, "pm2": old_apps, "operation": "release", "mode": "paper",
        "managed_apps": ["trading-system"], "deployment_targets": targets,
    }))
    (failed_start / "failed.json").write_text(json.dumps({
        "stage": "start", "entry_lock_retained": True, "resumed_from": original_id,
    }))

    candidate_sha = args[args.index("--mts-sha") + 1]
    engine = mini.roots["trading-system"]
    mini.git(engine, "switch", "--detach", candidate_sha)
    mini.heads["trading-system"] = candidate_sha
    process = mini.deploy.one(mini.rows, "trading-system")
    process["pid"] = 3300
    process["pm2_env"].update({"LRC_RELEASE_SHA": candidate_sha, "status": "online",
                               "pm_uptime": __import__("time").time() * 1000})
    mini.deploy.write_paper_profile(
        mini.runtime / "config/futures.paper.yaml", mini.baseline,
        ticker="TMF", runtime_dir=mini.runtime,
    )
    mini.tick()
    log = mini.runtime / "out.log"
    log.write_text("PAPER_STARTUP_READY: local simulation\n" + "\n".join(
        "Subscribed " + far + "TMF " + kind + ": CODE"
        for far in ("", "far-month ") for kind in ("tick", "bidask")))

    lock = mini.roots["trading-system"] / "data/maintenance_entry_lock.flag"
    args.extend(["--resume-start-failed-transaction", start_id])
    observed = []
    original_command = mini.deploy.MiniDeployment.command

    def track_pm2(self, argv, **kwargs):
        if argv[0] == "/pm2fake" and argv[1] in {"stop", "delete", "start", "save"}:
            observed.append((argv[1], lock.read_text() if lock.exists() else None))
        return original_command(self, argv, **kwargs)

    monkeypatch.setattr(mini.deploy.MiniDeployment, "command", track_pm2)
    assert mini.deploy.main(args) == 0
    assert [action for action, _ in observed] == ["save"]
    assert observed == [("save", original_id)]
    assert not lock.exists()
    complete = next((mini.runtime / "logs/deployments").glob("*/complete.json"))
    value = json.loads(complete.read_text())
    assert value["resumed_from"] == original_id
    assert value["continued_from"] == start_id
