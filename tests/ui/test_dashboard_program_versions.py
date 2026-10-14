"""2026-10-15 Hermes Agent: dashboard reports running code, never disk/context revision."""

import ast
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def helpers(monkeypatch, tmp_path):
    source = (ROOT / "ui/dashboard.py").read_text()
    names = {"_running_program_version", "get_running_program_versions"}
    functions = [node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in functions:
        node.decorator_list = []
    namespace = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<version-helpers>", "exec"), namespace)
    root, runtime = tmp_path / "source", tmp_path / "runtime"
    root.mkdir()
    runtime.mkdir()
    sha = "a" * 40
    process = SimpleNamespace(
        cwd=lambda: str(root),
        environ=lambda: {"LRC_RELEASE_SHA": sha, "TRADING_RUNTIME_DIR": str(runtime)},
        cmdline=lambda: ["python3", "main.py"],
        create_time=lambda: 1791973171.0,
        is_running=lambda: True,
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv == ["git", "-C", str(root), "show", "-s", "--format=%cI", sha]
        return SimpleNamespace(stdout="2026-10-14T10:18:00+00:00\n")

    monkeypatch.setattr(subprocess, "run", run)
    return namespace, process, root, runtime, calls


def test_running_sha_and_taipei_dates_not_context_or_disk_head(helpers):
    ns, _, root, runtime, calls = helpers
    result = ns["_running_program_version"](123, root, runtime, "main.py")
    assert result == {"sha": "aaaaaaaa", "commit_date": "2026-10-14 18:18:00",
                      "started_at": "2026-10-14 18:19:31"}
    assert len(calls) == 1
    assert "HEAD" not in calls[0]


@pytest.mark.parametrize("invalid", ["pid", "cwd", "runtime", "sha", "entry", "dead", "denied"])
def test_unverified_or_stopped_process_never_looks_current(helpers, invalid):
    ns, process, root, runtime, _ = helpers
    pid = 123
    if invalid == "pid":
        pid = 0
    elif invalid == "cwd":
        process.cwd = lambda: "/different-release"
    elif invalid == "runtime":
        process.environ = lambda: {"LRC_RELEASE_SHA": "a" * 40, "TRADING_RUNTIME_DIR": "/different-runtime"}
    elif invalid == "sha":
        process.environ = lambda: {"LRC_RELEASE_SHA": "226", "TRADING_RUNTIME_DIR": str(runtime)}
    elif invalid == "entry":
        process.cmdline = lambda: ["python3", "unrelated.py"]
    elif invalid == "dead":
        process.is_running = lambda: False
    elif invalid == "denied":
        def denied():
            raise psutil.AccessDenied(pid)
        process.environ = denied
    result = ns["_running_program_version"](pid, root, runtime, "main.py")
    assert result["sha"] == "無法確認"
    assert result["started_at"] == "—"


def test_missing_commit_metadata_preserves_verified_running_sha(helpers, monkeypatch):
    ns, _, root, runtime, _ = helpers

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 2)

    monkeypatch.setattr(subprocess, "run", fail)
    result = ns["_running_program_version"](123, root, runtime, "main.py")
    assert result["sha"] == "aaaaaaaa"
    assert result["commit_date"] == "無法確認"
    assert result["started_at"] == "2026-10-14 18:19:31"


@pytest.mark.parametrize("saved_sha", ["b" * 40, "c" * 40])
def test_saved_pm2_sha_is_ignored_and_dashboard_uses_own_pid(helpers, tmp_path, monkeypatch, saved_sha):
    ns, _, root, runtime, _ = helpers
    home = tmp_path / "pm2"
    home.mkdir()
    pidfile = home / "engine.pid"
    pidfile.write_text("123")
    (home / "dump.pm2").write_text(json.dumps([{
        "name": "trading-system", "pm_pid_path": str(pidfile),
        "pm_cwd": str(root), "LRC_RELEASE_SHA": saved_sha,
        "pm_uptime": 1,
    }]))
    monkeypatch.setenv("PM2_HOME", str(home))
    import os
    calls = []

    def read(pid, expected_root, runtime_root, entry):
        calls.append((pid, expected_root, runtime_root, entry))
        return {"sha": "live-sha", "commit_date": "date", "started_at": "start"}

    ns["_running_program_version"] = read
    result = ns["get_running_program_versions"](str(root), str(runtime))
    assert calls == [(123, str(root), str(runtime), "main.py"),
                     (os.getpid(), str(root), str(runtime), "dashboard.py")]
    assert result["MTS"]["sha"] == "live-sha"


@pytest.mark.parametrize("rows", [None, [], [{"name": "trading-system"}] * 2,
                                  {"invalid": "format"}, ["invalid"]])
def test_missing_or_ambiguous_pm2_record_leaves_mts_unknown(helpers, tmp_path, monkeypatch, rows):
    ns, process, root, runtime, _ = helpers
    home = tmp_path / "pm2"
    home.mkdir()
    if rows is not None:
        (home / "dump.pm2").write_text(json.dumps(rows))
    monkeypatch.setenv("PM2_HOME", str(home))
    process.cmdline = lambda: ["streamlit", "run", "ui/dashboard.py"]
    result = ns["get_running_program_versions"](str(root), str(runtime))
    assert result["MTS"]["sha"] == "無法確認"
    assert result["Dashboard"]["sha"] == "aaaaaaaa"


def test_version_caption_is_beside_runtime_context_and_cannot_call_pm2():
    source = (ROOT / "ui/dashboard.py").read_text()
    header = source[source.index('"期貨執行內容："'):source.index('if active_runtime_truth["audit_reasons"]:')]
    assert "get_running_program_versions(" in header
    assert "版本日期=" in header and "啟動時間=" in header and "台北時間" in header
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_running_program_version", "get_running_program_versions"}]
    for function in functions:
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run":
                assert isinstance(node.args[0], ast.List)
                assert node.args[0].elts[0].value == "git"
