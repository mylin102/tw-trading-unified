"""RED: sealed live config profile (Codex P0 decision 2).

Design:
- config/futures_live.yaml = a FULL, version-controlled copy of
  config/futures.yaml with `live_trading: true` and
  `config_profile: futures_live` — the ONLY config that may enter live
  certification. PM2 live deployment runs `--config futures_live`.
- The paper default (config/futures.yaml) stays UNCHANGED and can never
  enter certification (no config_profile marker).
- Unknown/missing config => fail-closed (the loader refuses).
- The certification context records config_path + config_hash
  (sha256 of the profile); a profile/hash mismatch => quarantine
  (GUARD_CONFIG_PROFILE_MISMATCH).
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PAPER = _REPO / "config" / "futures.yaml"
_LIVE = _REPO / "config" / "futures_live.yaml"

_LIVE_ONLY_KEYS = {"live_trading", "config_profile"}


def test_paper_default_unchanged():
    # the paper default stays exactly as-is: live_trading false and NO
    # config_profile marker (it must never enter live certification)
    import yaml
    cfg = yaml.safe_load(_PAPER.read_text(encoding="utf-8")) or {}
    assert cfg.get("live_trading", False) is False
    assert "config_profile" not in cfg


def test_futures_live_profile_exists_and_sealed():
    # the sealed live profile exists with the mandatory markers
    import yaml
    assert _LIVE.is_file(), "config/futures_live.yaml missing"
    cfg = yaml.safe_load(_LIVE.read_text(encoding="utf-8")) or {}
    assert cfg.get("live_trading") is True
    assert cfg.get("config_profile") == "futures_live"


def test_live_profile_parity_with_paper():
    # the live profile is a FULL snapshot: every non-live key matches the
    # paper config (drift protection — the sealed profile cannot silently
    # diverge from the paper baseline)
    import yaml
    paper = yaml.safe_load(_PAPER.read_text(encoding="utf-8")) or {}
    live = yaml.safe_load(_LIVE.read_text(encoding="utf-8")) or {}
    paper_keys = set(paper) - _LIVE_ONLY_KEYS
    live_keys = set(live) - _LIVE_ONLY_KEYS
    assert paper_keys == live_keys, \
        f"key drift: paper-only={paper_keys - live_keys} " \
        f"live-only={live_keys - paper_keys}"
    for k in paper_keys:
        assert paper[k] == live[k], f"value drift at key {k}"


def test_unknown_config_fail_closed(tmp_path, monkeypatch):
    # an unknown/missing config name => the loader refuses (fail-closed)
    monkeypatch.chdir(_REPO)
    r = subprocess.run(
        [sys.executable, "-B", "main.py", "--config",
         "futures_does_not_exist"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "TRADING_RUNTIME_DIR": str(tmp_path),
             "LRC_RELEASE_SHA": "ab" * 20})
    assert r.returncode != 0, "unknown config must fail closed"


def test_guard_config_profile_live_profile_passes(tmp_path):
    from core.deployment_safety_gate import guard_config_profile
    ts = int(__import__("time").time() * 1000)
    r = guard_config_profile(str(_LIVE), hashlib.sha256(
        _LIVE.read_bytes()).hexdigest(), captured_at=ts)
    assert r.ok, r.reasons


def test_guard_config_profile_paper_refused(tmp_path):
    # the paper config (no config_profile) can NEVER pass the live gate
    from core.deployment_safety_gate import guard_config_profile
    ts = int(__import__("time").time() * 1000)
    r = guard_config_profile(str(_PAPER), hashlib.sha256(
        _PAPER.read_bytes()).hexdigest(), captured_at=ts)
    assert not r.ok and "GUARD_CONFIG_PROFILE" in "".join(r.reasons)


def test_guard_config_profile_missing_fail_closed(tmp_path):
    from core.deployment_safety_gate import guard_config_profile
    r = guard_config_profile(
        str(tmp_path / "no-such.yaml"), "ab" * 64,
        captured_at=int(__import__("time").time() * 1000))
    assert not r.ok and "GUARD_CONFIG_PROFILE" in "".join(r.reasons)


def test_guard_config_profile_hash_mismatch_quarantine(tmp_path):
    # the ctx's recorded config_hash != the profile's current hash =>
    # quarantine (the sealed profile was tampered/drifted)
    from core.deployment_safety_gate import guard_config_profile
    ts = int(__import__("time").time() * 1000)
    r = guard_config_profile(str(_LIVE), "cd" * 32, captured_at=ts)
    assert not r.ok and "GUARD_CONFIG_PROFILE_MISMATCH" in "".join(r.reasons)
