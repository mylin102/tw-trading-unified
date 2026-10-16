"""Disk-floor check used by deploy_release.sh stage 1 (audit 2026-09-20).

The threshold lives in the deploy script as a literal; the computation is a
pure function here so it can be unit-tested by injecting the filesystem stats
instead of exposing a tunable knob to the operator.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

VLC = Path(__file__).resolve().parents[2] / "scripts" / "deployment" / "verify_live_context.py"


def _load():
    spec = importlib.util.spec_from_file_location("vlc_mod", VLC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stat:
    def __init__(self, bavail: int, frsize: int) -> None:
        self.f_bavail = bavail
        self.f_frsize = frsize


def test_disk_free_gb_is_a_pure_function_of_statvfs():
    mod = _load()
    free = mod.disk_free_gb("/ignored", _statvfs=lambda _p: _Stat(10, 1024 ** 3))
    assert free == pytest.approx(10.0)
    free = mod.disk_free_gb("/ignored", _statvfs=lambda _p: _Stat(3, 512))
    assert free == pytest.approx(3 * 512 / float(1024 ** 3))


def test_check_disk_cli_passes_with_a_reachable_threshold(tmp_path):
    r = subprocess.run([sys.executable, "-B", str(VLC), "--check-disk",
                        "--min-free-gb", "0", "--runtime-dir", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert float(r.stdout.strip()) >= 0


def test_check_disk_cli_fails_closed_below_the_threshold(tmp_path):
    r = subprocess.run([sys.executable, "-B", str(VLC), "--check-disk",
                        "--min-free-gb", "999999", "--runtime-dir", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "FAIL" in r.stdout


def test_check_disk_cli_reports_unstattable_path(tmp_path):
    r = subprocess.run([sys.executable, "-B", str(VLC), "--check-disk",
                        "--min-free-gb", "1", "--runtime-dir", str(tmp_path / "nope")],
                       capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
