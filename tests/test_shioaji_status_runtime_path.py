from pathlib import Path

from core import shioaji_session


# 2026-10-28 Hermes Agent: Pin shared status IPC to the common runtime path,
# while preserving the project-local fallback when the runtime is unspecified.
def test_system_status_io_uses_runtime_directory_then_project_fallback(
    tmp_path, monkeypatch
):
    project_root = tmp_path / "project"
    fallback_path = project_root / "logs" / "system_status.tmp"
    runtime_root = tmp_path / "runtime"
    runtime_path = runtime_root / "logs" / "system_status.tmp"

    monkeypatch.setattr(shioaji_session, "_PROJECT_ROOT", project_root)
    monkeypatch.setattr(shioaji_session, "_STATUS_FILE", fallback_path)
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(runtime_root))

    shioaji_session.set_system_status(shioaji_session.SystemReadiness.SYNCING)

    assert shioaji_session._system_status_path() == runtime_path
    assert runtime_path.read_text() == "SYNCING"
    assert shioaji_session.get_shared_system_status() is shioaji_session.SystemReadiness.SYNCING
    assert not fallback_path.exists()

    monkeypatch.delenv("TRADING_RUNTIME_DIR")
    shioaji_session.set_system_status(shioaji_session.SystemReadiness.TRADING)

    assert shioaji_session._system_status_path() == fallback_path
    assert fallback_path.read_text() == "TRADING"
    assert shioaji_session.get_shared_system_status() is shioaji_session.SystemReadiness.TRADING
