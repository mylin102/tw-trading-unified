#!/usr/bin/env python3
"""Release identity closure — release-dir-scoped HEAD == injected
LRC_RELEASE_SHA before LIVE certification/transition (phase2 §8.5/§9.4).

Contracts:
- verify_release_identity(release_dir, runner) uses `git -C <release_dir>`
  (the release tree), never an arbitrary cwd
- env missing/invalid -> RELEASE_IDENTITY_ENV_MISSING (fail-closed)
- git command failure -> RELEASE_IDENTITY_GIT_FAILED (fail-closed)
- HEAD mismatch -> RELEASE_IDENTITY_MISMATCH (fail-closed)
- the LIVE startup path wires the check BEFORE certification
"""

import os
from pathlib import Path

import pytest

GOOD_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _make_runner(code=0, out=None, err=""):
    calls = []

    def runner(cmd, cwd=None):
        calls.append({"cmd": cmd, "cwd": cwd})
        return code, out if out is not None else GOOD_SHA, err

    return runner, calls


def test_verify_match(tmp_path, monkeypatch):
    import core.release_identity as ri
    monkeypatch.setenv("LRC_RELEASE_SHA", GOOD_SHA)
    runner, calls = _make_runner(out=GOOD_SHA)
    ok, reasons = ri.verify_release_identity(str(tmp_path), runner=runner)
    assert ok is True and reasons == [], (ok, reasons)
    assert calls and "-C" in calls[0]["cmd"], \
        "verifier must use git -C <release_dir>, never an arbitrary cwd"


def test_verify_env_missing_fail_closed(tmp_path, monkeypatch):
    import core.release_identity as ri
    monkeypatch.delenv("LRC_RELEASE_SHA", raising=False)
    runner, calls = _make_runner()
    ok, reasons = ri.verify_release_identity(str(tmp_path), runner=runner)
    assert ok is False
    assert any("RELEASE_IDENTITY_ENV_MISSING" in r for r in reasons), reasons
    assert not calls, "no git call when the env is missing"


def test_verify_env_invalid_fail_closed(tmp_path, monkeypatch):
    import core.release_identity as ri
    monkeypatch.setenv("LRC_RELEASE_SHA", "not-a-sha")
    runner, calls = _make_runner()
    ok, reasons = ri.verify_release_identity(str(tmp_path), runner=runner)
    assert ok is False
    assert any("RELEASE_IDENTITY_ENV_MISSING" in r for r in reasons), reasons
    assert not calls


def test_verify_mismatch_fail_closed(tmp_path, monkeypatch):
    import core.release_identity as ri
    monkeypatch.setenv("LRC_RELEASE_SHA", GOOD_SHA)
    runner, calls = _make_runner(out=OTHER_SHA)
    ok, reasons = ri.verify_release_identity(str(tmp_path), runner=runner)
    assert ok is False
    assert any("RELEASE_IDENTITY_MISMATCH" in r for r in reasons), reasons


def test_verify_git_failure_fail_closed(tmp_path, monkeypatch):
    import core.release_identity as ri
    monkeypatch.setenv("LRC_RELEASE_SHA", GOOD_SHA)
    runner, calls = _make_runner(code=128, out="", err="fatal: not a git repo")
    ok, reasons = ri.verify_release_identity(str(tmp_path), runner=runner)
    assert ok is False
    assert any("RELEASE_IDENTITY_GIT_FAILED" in r for r in reasons), reasons


def test_verify_release_dir_not_cwd(tmp_path, monkeypatch):
    # the release dir is passed explicitly; the runner receives git -C
    import core.release_identity as ri
    monkeypatch.setenv("LRC_RELEASE_SHA", GOOD_SHA)
    runner, calls = _make_runner(out=GOOD_SHA)
    release_dir = str(tmp_path / "release_tree")
    ri.verify_release_identity(release_dir, runner=runner)
    assert calls[0]["cmd"][:3] == ["git", "-C", release_dir], calls


def test_wiring_references_verifier_and_release_tree():
    # monitor startup wires verify_release_identity (module holds the
    # git -C + LRC_RELEASE_SHA contract)
    monitor = Path(__file__).resolve().parents[2] / "strategies" / "futures" / \
        "monitor.py"
    text = monitor.read_text(encoding="utf-8")
    assert "verify_release_identity" in text, \
        "monitor LIVE startup must wire the release identity check"
    verifier = Path(__file__).resolve().parents[2] / "core" / \
        "release_identity.py"
    vtext = verifier.read_text(encoding="utf-8")
    assert "LRC_RELEASE_SHA" in vtext, "verifier must read the injected env"
    assert "-C" in vtext, "verifier must run git -C <release_dir>"
