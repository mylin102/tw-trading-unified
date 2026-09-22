import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _dashboard_helper():
    source = (ROOT / "ui" / "dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_dashboard_identity_presentation"
    )
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<helper>", "exec"), namespace)
    return namespace["_dashboard_identity_presentation"], source


def test_identity_mismatch_is_stale_and_suppresses_release_panels():
    present, source = _dashboard_helper()
    result = present(False, "actual123", "expected456", "2026-09-22T10:00:00+08:00")

    assert result["level"] == "stale"
    assert result["render_release_panels"] is False
    assert "STALE — 本頁與執行中的 release 不一致（部署進行中或未完成）" in result["message"]
    assert "actual123" in result["message"]
    assert "expected456" in result["message"]
    assert "2026-09-22T10:00:00+08:00" in result["message"]
    assert "check_runtime_identity" in source
    assert "st.stop()" in source


def test_identity_match_renders_normally_without_stale():
    present, _ = _dashboard_helper()
    result = present(True, "same", "same", "2026-09-22T10:00:00+08:00")

    assert result == {"level": "normal", "render_release_panels": True, "message": ""}


def test_trading_identity_enforcer_still_raises_on_sha_mismatch(monkeypatch):
    from core.runtime_paths import enforce_runtime_identity

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("NODE_ENV", "production")
    monkeypatch.setenv("TRADING_CANONICAL_ROOT", str(ROOT))
    monkeypatch.setenv("LRC_RELEASE_SHA", "0" * 40)

    with pytest.raises(RuntimeError, match="RUNTIME_IDENTITY_REJECTED"):
        enforce_runtime_identity(str(ROOT / "main.py"))
