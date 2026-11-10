"""2026-11-10 Hermes Agent: cross-process single-owner enforcement for the
Shioaji session login.

Root cause chain under investigation (read-only probe on Mini, same day):
trading-system and stock-runner are two independent processes sharing one
SHIOAJI_API_KEY/SECRET_KEY. core.shioaji_session.get_api()'s
threading.Lock only prevents two callers in the SAME process from racing;
it does nothing when a second PROCESS logs in with the same credential.
Evidence (pm2-trading-out__2026-11-10_11-57-00.log) shows
stock-runner restarting (new login) ~35 minutes before trading-system's
log fills with SessionNotEstablished (124 occurrences) followed by
"Found 0 TMF contracts" (5 occurrences) -- consistent with, but not an
independently-proven case of, a single-effective-session collision (see
shioaji-session-death-diagnosis skill's wording discipline).

This adds a cross-process OS-held exclusive lock (fcntl.flock) around
login, scoped to TRADING_RUNTIME_DIR (the one env var both apps already
share), so a second process attempting to log in while the first still
holds the slot fails CLOSED immediately with a named reason instead of
silently racing the broker for the one session slot.
"""
import os
import time
import multiprocessing

import pytest

import core.shioaji_session as session_mod


def _hold_lock_in_subprocess(path_str, ready_evt, release_evt):
    import fcntl
    fd = open(path_str, "a+")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    ready_evt.set()
    release_evt.wait(timeout=10)
    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    fd.close()


def _acquire_and_exit_without_release(path_str, ready_evt):
    import fcntl
    fd = open(path_str, "a+")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    ready_evt.set()
    os._exit(9)  # simulate a crash: no logout(), no flock unlock


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture(autouse=True)
def _reset_lock_state():
    session_mod._session_lock_fd = None
    yield
    session_mod._release_session_lock()


def test_lock_path_is_under_shared_runtime_dir(runtime_dir):
    path = session_mod._session_lock_path()
    assert path == runtime_dir / "locks" / "shioaji_session.lock"


def test_acquire_then_release_allows_reacquire_in_same_process(runtime_dir):
    session_mod._acquire_session_lock(timeout_s=1)
    assert session_mod._session_lock_fd is not None
    session_mod._release_session_lock()
    assert session_mod._session_lock_fd is None
    # must not hang or raise -- the lock was actually released
    session_mod._acquire_session_lock(timeout_s=1)
    session_mod._release_session_lock()


def test_second_process_is_refused_while_first_holds_the_lock(runtime_dir):
    """The real-world scenario: two independent OS processes, same
    credential, overlapping login attempts."""
    lock_path = session_mod._session_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_hold_lock_in_subprocess, args=(str(lock_path), ready, release))
    proc.start()
    try:
        assert ready.wait(timeout=5), "subprocess never acquired the lock"
        with pytest.raises(RuntimeError) as exc:
            session_mod._acquire_session_lock(timeout_s=1)
        assert "SHIOAJI_SESSION_LOCK_CONTENDED" in str(exc.value)
    finally:
        release.set()
        proc.join(timeout=5)

    # After the other process releases, this process can acquire it.
    session_mod._acquire_session_lock(timeout_s=5)
    assert session_mod._session_lock_fd is not None


def test_crash_without_logout_releases_lock_via_kernel(runtime_dir):
    """A process that dies without calling logout() must not leave a stale
    lock behind -- the kernel releases flock automatically on process exit."""
    lock_path = session_mod._session_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    ready = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_acquire_and_exit_without_release, args=(str(lock_path), ready))
    proc.start()
    assert ready.wait(timeout=5)
    proc.join(timeout=5)
    assert proc.exitcode == 9

    # The crashed process is gone; the kernel must have released its flock.
    session_mod._acquire_session_lock(timeout_s=5)
    assert session_mod._session_lock_fd is not None


def test_get_api_releases_lock_on_login_failure(runtime_dir, monkeypatch):
    """A failed login must not leave the lock held forever."""
    monkeypatch.setattr(session_mod, "_login",
                         lambda api: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(session_mod, "assert_broker_access_allowed",
                         lambda: None, raising=False)
    import core.deployment_role_gate as gate_mod
    monkeypatch.setattr(gate_mod, "assert_broker_access_allowed", lambda: None)

    session_mod._api = None
    with pytest.raises(RuntimeError, match="boom"):
        session_mod.get_api()

    assert session_mod._session_lock_fd is None, (
        "lock must be released after a failed login, not leaked")
    # And a fresh acquisition must succeed (proves it is truly released).
    session_mod._acquire_session_lock(timeout_s=2)
