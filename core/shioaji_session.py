"""
Shioaji Session Management Singleton
Handles login, logout, and shared API instance.
"""
import shioaji as sj
import threading
import time
import logging
import os
import ast
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SystemReadiness(Enum):
    BOOTING = "BOOTING"
    SYNCING = "SYNCING"
    WARMUP = "WARMUP"
    TRADING = "TRADING"
    DEGRADED = "DEGRADED"
    SHUTDOWN = "SHUTDOWN"

# Global state
_api: sj.Shioaji | None = None
_lock = threading.Lock()
_fetch_lock = threading.Lock()
_is_fetching = False
_system_status = SystemReadiness.BOOTING

_session_generation = 0
_session_records: dict[int, int] = {}
_last_successful_login_generation: int | None = None
_instrument_lock = threading.Lock()


def _session_event_path() -> Path:
    return Path(os.getenv("MTS_SESSION_EVENT_LOG_PATH") or
                os.getenv("MTS_EVENT_LOG_PATH") or
                str(_PROJECT_ROOT / "logs" / "mts_spread_events.jsonl"))


def _session_event(event: str, *, api=None, source: str = "", account=None,
                   **fields) -> dict:
    api_id = id(api) if api is not None else None
    if account is None and api is not None:
        account_obj = getattr(api, "futopt_account", None)
        account = (getattr(account_obj, "account_id", None)
                   or getattr(account_obj, "person_id", None)
                   or getattr(account_obj, "account", None))
    record = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "thread": threading.get_ident(),
        "thread_name": threading.current_thread().name,
        "api_object_id": api_id,
        "account": account,
        "source": source or "core.shioaji_session",
        "session_generation": _session_records.get(api_id) if api_id else None,
        "last_successful_login_generation": _last_successful_login_generation,
        **fields,
    }
    path = _session_event_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _instrument_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.warning("[SESSION_INSTRUMENTATION_WRITE_FAILED] %s", exc)
    return record


def observe_api_created(api, *, source: str = "", account=None) -> dict:
    """Record one Shioaji object creation and assign a process-local generation."""
    global _session_generation
    with _instrument_lock:
        _session_generation += 1
        generation = _session_generation
        _session_records[id(api)] = generation
    return _session_event("SESSION_API_CREATED", api=api, source=source,
                          account=account)


def observe_login(api, *, source: str = "", account=None, success: bool,
                  error: BaseException | None = None) -> dict:
    """Record login result without changing authentication or readiness semantics."""
    global _last_successful_login_generation
    generation = _session_records.get(id(api))
    if success:
        _last_successful_login_generation = generation
    event = _session_event(
        "SESSION_LOGIN", api=api, source=source, account=account,
        success=bool(success), error_type=type(error).__name__ if error else None,
        error_text=str(error)[:240] if error else None)
    if success:
        event["last_successful_login_generation"] = _last_successful_login_generation
    return event


def observe_logout(api, *, source: str = "", account=None) -> dict:
    """Record logout before the object is discarded."""
    return _session_event("SESSION_LOGOUT", api=api, source=source,
                          account=account)


def observe_broker_call(api, operation: str, phase: str, *, source: str = "",
                        error: BaseException | None = None) -> dict:
    """Record read/order API boundaries; this helper never invokes the broker."""
    return _session_event(
        "BROKER_CALL", api=api, source=source, operation=operation,
        phase=phase, error_type=type(error).__name__ if error else None,
        error_text=str(error)[:240] if error else None)


def audit_resident_shioaji_constructors(repo_root: str | Path,
                                        resident_paths: list[str]) -> list[dict]:
    """AST audit: resident code may not construct/login Shioaji directly."""
    root = Path(repo_root).resolve()
    violations: list[dict] = []
    files: list[Path] = []
    for relative in resident_paths:
        target = root / relative
        if target.is_file() and target.suffix == ".py":
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            direct_ctor = (isinstance(node.func, ast.Attribute)
                           and node.func.attr == "Shioaji")
            direct_login = (isinstance(node.func, ast.Attribute)
                            and node.func.attr == "login")
            if direct_ctor or direct_login:
                violations.append({
                    "path": str(path.relative_to(root)),
                    "line": node.lineno,
                    "kind": "constructor" if direct_ctor else "login",
                })
    return violations


def is_session_death_error(exc: BaseException) -> bool:
    """Return True only for errors that prove the broker session is dead.

    Generic timeouts and provider 5xx responses remain degraded reads; they
    must not create a login storm.  Shioaji exposes the terminal condition
    either through its exception type or the SolClient sub-code text.
    """
    _name = type(exc).__name__.lower()
    _text = str(exc).lower()
    return (
        "sessionnotestablished" in _text
        or "session not established" in _text
        or "shioajiconnectionerror" in _name
        or "shioajiconnectionerror" in _text
    )


class SessionReconnectController:
    """Single-flight, bounded replacement-session recovery.

    The controller never submits, cancels, or mutates an order.  Callers
    provide only login, callback/subscription restoration and a read-only
    verification probe.  Exhaustion is sticky until process restart or an
    explicit successful recovery, preventing repeated call sites from
    turning one outage into an unbounded login loop.
    """

    def __init__(self, *, max_attempts: int = 3,
                 backoff_seconds=(1.0, 2.0), sleep=time.sleep):
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_seconds = tuple(float(v) for v in backoff_seconds)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._attempts = 0
        self._exhausted = False

    def reconnect(self, exc: BaseException, *, api_factory: Callable,
                  login: Callable, restore: Callable, verify: Callable,
                  emit: Callable):
        if not is_session_death_error(exc) or self._exhausted:
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            while self._attempts < self.max_attempts:
                self._attempts += 1
                _attempt = self._attempts
                emit("SESSION_RECONNECT_ATTEMPT", attempt=_attempt,
                     max_attempts=self.max_attempts)
                try:
                    candidate = api_factory()
                    observe_api_created(candidate,
                                        source="SessionReconnectController.reconnect")
                    login(candidate)
                    restore(candidate)
                    if not verify(candidate):
                        raise RuntimeError("replacement session verification failed")
                    emit("SESSION_RECONNECT_OK", attempt=_attempt)
                    self._attempts = 0
                    self._exhausted = False
                    return candidate
                except Exception as reconnect_exc:
                    emit("SESSION_RECONNECT_FAILED", attempt=_attempt,
                         max_attempts=self.max_attempts,
                         error_type=type(reconnect_exc).__name__)
                    if self._attempts < self.max_attempts:
                        _idx = min(self._attempts - 1,
                                   len(self.backoff_seconds) - 1)
                        if self.backoff_seconds:
                            self._sleep(self.backoff_seconds[_idx])
            self._exhausted = True
            return None
        finally:
            self._lock.release()

# IPC: Shared state file to communicate between trading-system and dashboard processes
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATUS_FILE = _PROJECT_ROOT / "logs" / "system_status.tmp"

def _system_status_path() -> Path:
    """Helper for tests to mock the status file path."""
    return _STATUS_FILE


def _require_ca_material(ca_path: str | None,
                         ca_passwd: str | None) -> tuple[str, str]:
    """Return usable signing material or refuse a live-session startup.

    A successful login alone is not sufficient for a production order path.
    Never silently continue without a readable certificate: that produces a
    misleading LIVE_READY state followed by local order rejection.
    """
    if (not isinstance(ca_path, str) or not ca_path
            or not isinstance(ca_passwd, str) or not ca_passwd):
        raise RuntimeError("CA configuration unavailable")
    if not os.path.isfile(ca_path):
        raise RuntimeError("CA certificate file unavailable")
    return ca_path, ca_passwd


def _activate_futopt_ca(api: sj.Shioaji, ca_path: str,
                        ca_passwd: str) -> None:
    """Activate order signing for the futures/options account."""
    account = getattr(api, "futopt_account", None)
    person_id = getattr(account, "person_id", None)
    if not isinstance(person_id, str) or not person_id:
        raise RuntimeError("Cannot activate CA: futures account identity unavailable")
    api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd,
                    person_id=person_id)


def get_api() -> sj.Shioaji:
    """Get or create the singleton Shioaji API instance."""
    from core.deployment_role_gate import assert_broker_access_allowed
    assert_broker_access_allowed()

    global _api
    with _lock:
        if _api is None:
            candidate = sj.Shioaji()
            observe_api_created(candidate, source="core.shioaji_session.get_api")
            try:
                _login(candidate)
            except Exception:
                # Do not let a failed login leave an unauthenticated
                # singleton that startup retries would silently reuse.
                _api = None
                raise
            _api = candidate
        return _api


def install_replacement_api(expected, replacement) -> bool:
    """Atomically replace the dead singleton after verified reconstruction."""
    global _api
    with _lock:
        if _api is not expected:
            return False
        observe_api_created(replacement,
                            source="core.shioaji_session.install_replacement_api")
        _api = replacement
        return True

def _load_env_credentials():
    """載入 Shioaji 憑證與環境變數，支援多重路徑 Fallback (包含隔離 worktree 與 runtime 目錄)
    2026-08-18 Gemini CLI: 解決 PM2 / isolated worktree 環境下遺漏 .env 的問題
    """
    from dotenv import load_dotenv
    env_candidates = [
        os.getenv("TRADING_ENV_FILE"),
        str(Path.cwd() / ".env"),
        str(_PROJECT_ROOT / ".env"),
        str(Path.home() / "Documents/mylin102/tw-trading-unified-runtime/credentials/.env"),
    ]
    for env_path in env_candidates:
        if env_path and Path(env_path).is_file():
            load_dotenv(dotenv_path=env_path, override=False)
    load_dotenv(override=False)

def _login(api: sj.Shioaji):
    """Internal login logic."""
    _load_env_credentials()
    
    api_key = os.getenv("SHIOAJI_API_KEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY")
    ca_path = os.getenv("SHIOAJI_CA_PATH")
    ca_passwd = os.getenv("SHIOAJI_CA_PASSWD")

    if not api_key or not secret_key:
        raise ValueError("Missing Shioaji credentials in .env")

    # Production order signing is fail-closed: login success is not live
    # readiness unless the CA material and futures-account identity are valid.
    ca_path, ca_passwd = _require_ca_material(ca_path, ca_passwd)

    # Login
    from core.broker.shioaji_compat import safe_login
    try:
        safe_login(api, api_key, secret_key, contracts_timeout=10000)
        _activate_futopt_ca(api, ca_path, ca_passwd)
    except Exception as exc:
        observe_login(api, source="core.shioaji_session._login", success=False,
                      error=exc)
        raise
    observe_login(api, source="core.shioaji_session._login", success=True)
    logger.info(f"[session] Logged in (attempt 1)")
    logger.info("[session] CA activated for futures account")

def _sync_worker(api_key: str, secret_key: str, ca_path: str, ca_passwd: str, q):
    """Child process worker: logs in and fetches contracts to populate the local disk cache.
    
    2026-07-01 Gemini CLI
    """
    try:
        import shioaji as sj
        import os
        api = sj.Shioaji()
        api.login(api_key, secret_key)
        if ca_path and os.path.exists(ca_path):
            _activate_futopt_ca(api, ca_path, ca_passwd)
        api.fetch_contracts()
        q.put(True)
    except Exception:
        # Do not send certificate paths, account identity, or provider error
        # text over the IPC queue.
        q.put(False)


def _fetch_contracts_subprocess(api_key: str, secret_key: str, ca_path: str, ca_passwd: str, timeout: int = 120) -> bool:
    """Run api.fetch_contracts() in a spawned subprocess to isolate C extension crashes.
    
    V-Model Rationale:
    - Shioaji's C extension (api.pyx:851 SolaceAPI._fetch_contracts_cb) has a race
      condition causing IndexError: list assignment index out of range
    - Python try/except does not reliably catch C-level callback crashes
    - Spawning isolates the crash: child dies, parent continues
    - Uses spawn mode with credentials serialization to avoid Solace DLL FD/connection conflicts on macOS/Unix.
    
    Returns:
        True if fetch succeeded (child exited 0), False otherwise
    """
    import multiprocessing as _mp
    # 2026-07-01 Gemini CLI: Use spawn mode context as it is macOS default and safer for C extensions
    ctx = _mp.get_context("spawn")
    
    _result_queue = ctx.Queue()
    
    child = ctx.Process(
        target=_sync_worker, 
        args=(api_key, secret_key, ca_path, ca_passwd, _result_queue), 
        name="shioaji-fetch-contracts"
    )
    child.start()
    child.join(timeout)
    
    if child.is_alive():
        child.terminate()
        child.join(5)
        logger.error(
            f"[V-MODEL][CONTRACT_FETCH] Subprocess timed out after {timeout}s — killed"
        )
        # Drain the queue (may be empty)
        while not _result_queue.empty():
            try:
                _result_queue.get_nowait()
            except Exception:
                pass
        return False
    
    # Read result from Queue (IPC-safe, works across spawn/fork)
    try:
        _val = _result_queue.get(timeout=2)
        if _val is True:
            return True
        else:
            logger.error(
                f"[V-MODEL][CONTRACT_FETCH] Subprocess failed: {_val}"
            )
            return False
    except Exception:
        logger.error(
            "[V-MODEL][CONTRACT_FETCH] Subprocess crashed or Queue empty "
            "(IndexError in api.pyx:851) — isolated, parent alive"
        )
        return False


def fetch_contracts(api: sj.Shioaji):
    """Safely fetch contracts with a global lock and state check.
    
    Uses subprocess isolation to survive C extension crashes in the
    Shioaji Solace callback path.
    """
    global _is_fetching
    
    # Check if contracts are already there to avoid concurrent call error
    try:
        # Use dir() to check for actual contract symbols (fully uppercase) to avoid early return on empty cache
        if hasattr(api.Contracts, "Futures"):
            # 2026-07-01 Gemini CLI: Avoid counting Swig helper properties (like 'this') by checking for uppercase tickers
            has_contracts = any(attr.isupper() for attr in dir(api.Contracts.Futures))
            if has_contracts:
                return 
    except Exception:
        pass

    with _fetch_lock:
        if _is_fetching:
            logger.info("📡 Contracts already being fetched, waiting...")
            return

        try:
            _is_fetching = True
            logger.info("📡 Fetching contracts...")
            
            # 2026-07-01 Gemini CLI: Load credentials to pass to the spawned sync worker
            from dotenv import load_dotenv
            load_dotenv(override=True)
            api_key = os.getenv("SHIOAJI_API_KEY")
            secret_key = os.getenv("SHIOAJI_SECRET_KEY")
            ca_path = os.getenv("SHIOAJI_CA_PATH")
            ca_passwd = os.getenv("SHIOAJI_CA_PASSWD")

            if not api_key or not secret_key:
                raise ValueError("Missing Shioaji credentials in .env")
            
            # [V-Model] Guard C extension against IndexError crash in _fetch_contracts_cb
            # Use subprocess isolation: if the C callback crashes, only the child dies.
            _ok = _fetch_contracts_subprocess(api_key, secret_key, ca_path or "", ca_passwd or "", timeout=120)
            if _ok:
                logger.info("✅ Subprocess contract download complete. Loading contracts in parent process...")
                api.fetch_contracts()
                logger.info("✅ Contracts fetched successfully.")
            else:
                logger.error(
                    "[V-MODEL][CONTRACT_FETCH] C extension fetch_contracts crashed "
                    "(IndexError in api.pyx:851) — isolated in subprocess, parent alive"
                )
        except Exception as e:
            # Silence expected concurrent call warnings
            if "concurrent API call" in str(e) or "exclusive access lost" in str(e):
                logger.warning(f"📡 Concurrent fetch detected, skipping.")
            else:
                logger.error(f"❌ fetch_contracts error: {e}")
        finally:
            _is_fetching = False

def logout():
    """Cleanup and logout."""
    global _api
    with _lock:
        if _api:
            # Live Route Certification (round-9 #1): invalidate the session
            # registration so no certificate remains generation-valid after
            # broker logout. Function-level import avoids import cycles.
            # round-10 P0-2: invalidation is MANDATORY and must NOT be
            # swallowed — if it fails we force global revocation and
            # re-raise (broker logout must not proceed with a valid
            # generation).
            from core.live_route_certificate import session_registry
            try:
                session_registry.unregister(_api)
            except Exception:
                session_registry.invalidate_all()   # force global revocation
                raise
            observe_logout(_api, source="core.shioaji_session.logout")
            try:
                _api.logout()
                logger.info("[session] Logged out cleanly")
            except Exception as e:
                logger.error(f"[session] Logout error: {e}")
            _api = None

def set_system_status(status: SystemReadiness):
    global _system_status
    _system_status = status
    logger.info(f"🚀 System status changed to: {status.value}")
    
    # Persist to file for cross-process communication (Dashboard)
    try:
        _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_STATUS_FILE, "w") as f:
            f.write(status.name)
    except Exception as e:
        logger.error(f"❌ Failed to write system status file: {e}")

def get_system_status() -> SystemReadiness:
    """Get status within the current process."""
    return _system_status

def get_shared_system_status() -> SystemReadiness:
    """Get status from shared file (for cross-process Dashboard)."""
    if not _STATUS_FILE.exists():
        logger.debug(f"[session] Status file not found at {_STATUS_FILE}")
        return _system_status
    
    try:
        with open(_STATUS_FILE, "r") as f:
            val = f.read().strip()
            status = SystemReadiness[val]
            logger.debug(f"[session] Read shared status: {status.name}")
            return status
    except Exception as e:
        logger.error(f"[session] Failed to read shared status: {e}")
        return _system_status
