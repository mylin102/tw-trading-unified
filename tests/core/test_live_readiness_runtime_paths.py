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


def test_transition_state_reads_only_log_tail(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    log_dir = runtime / "logs"
    log_dir.mkdir(parents=True)
    log = log_dir / "pm2-trading-out.log"
    log.write_bytes(b"old line\n" * 50_000 + b"[MTS_EXEC_CTX] LIVE_READY\n")
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(runtime))

    def fail_whole_file_read(*args, **kwargs):
        raise AssertionError("read_text would load the complete PM2 log")

    monkeypatch.setattr(Path, "read_text", fail_whole_file_read)

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
