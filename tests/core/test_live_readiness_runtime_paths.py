from pathlib import Path

import core.live_readiness as readiness


def test_transition_state_reads_shared_runtime_log(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    log_dir = runtime / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "pm2-trading-out.log").write_text("[MTS_EXEC_CTX] LIVE_READY\n")
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(runtime))

    passed, message = readiness.check_transition_state()

    assert passed is True
    assert message == "LIVE_READY (authorized)"


def test_broker_login_reads_shared_runtime_log(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    log_dir = runtime / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "pm2-trading-error.log").write_text("System status changed to: TRADING\n")
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(runtime))

    passed, message = readiness.check_broker_login()

    assert passed is True
    assert message == "TRADING (logged in)"
