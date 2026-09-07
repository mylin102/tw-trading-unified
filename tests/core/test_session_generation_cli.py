"""G2 regression tests for registry-bound post-startup session wiring."""

import json
import sys
from types import SimpleNamespace

import pytest


TOKEN = "1cab40699f04fa19aba8fe1d9b20a132"


@pytest.mark.parametrize("value", [None, "not-a-registry-generation"])
def test_guard_rejects_missing_or_non_registry_session(value):
    from core.deployment_safety_gate import guard_session_generation

    result = guard_session_generation(value, revoked=False)

    assert not result.ok
    assert result.reasons == ("GUARD_SESSION_MISSING",)


def test_cli_post_startup_falls_back_to_ctx_session_id(tmp_path, monkeypatch):
    """Omitting the CLI option must use the runtime ctx registry token."""
    from core import deployment_safety_gate as gate
    from scripts.deployment import check_deployment as cli

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "execution_context.json").write_text(
        json.dumps({"session_id": TOKEN}), encoding="utf-8")

    captured = {}

    def fake_check_deployment(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(ok=True, refusal_codes=(), results=())

    monkeypatch.setattr(gate, "check_deployment", fake_check_deployment)
    monkeypatch.setattr(gate, "aggregate_gate_groups", lambda *a, **k: {})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_deployment.py",
            "--release-dir", str(tmp_path),
            "--runtime-dir", str(runtime),
            "--pid-file", str(tmp_path / "app.pid"),
            "--position-state", str(tmp_path / "position.json"),
            "--phase", "post_startup",
            "--json",
        ],
    )

    assert cli.main() == 0
    assert captured["phase"] == "post_startup"
    assert captured["session_generation"] == TOKEN
