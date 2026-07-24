"""
Shioaji Session Management Singleton
Handles login, logout, and shared API instance.
"""
import shioaji as sj
import threading
import time
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Optional

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

# IPC: Shared state file to communicate between trading-system and dashboard processes
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATUS_FILE = _PROJECT_ROOT / "logs" / "system_status.tmp"

def _system_status_path() -> Path:
    """Helper for tests to mock the status file path."""
    return _STATUS_FILE

def get_api() -> sj.Shioaji:
    """Get or create the singleton Shioaji API instance."""
    from core.deployment_role_gate import assert_broker_access_allowed
    assert_broker_access_allowed()

    global _api
    with _lock:
        if _api is None:
            _api = sj.Shioaji()
            _login(_api)
        return _api

def _login(api: sj.Shioaji):
    """Internal login logic."""
    from dotenv import load_dotenv
    load_dotenv(override=True)
    
    api_key = os.getenv("SHIOAJI_API_KEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY")
    ca_path = os.getenv("SHIOAJI_CA_PATH")
    ca_passwd = os.getenv("SHIOAJI_CA_PASSWD")

    if not api_key or not secret_key:
        raise ValueError("Missing Shioaji credentials in .env")

    # Login
    from core.broker.shioaji_compat import safe_login
    res = safe_login(api, api_key, secret_key, contracts_timeout=10000)
    logger.info(f"[session] Logged in (attempt 1)")
    
    if ca_path and os.path.exists(ca_path):
        api.activate_ca(ca_path, ca_passwd, os.path.dirname(ca_path))
        logger.info(f"[session] CA activated: {ca_path}")

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
            api.activate_ca(ca_path, ca_passwd, os.path.dirname(ca_path))
        api.fetch_contracts()
        q.put(True)
    except Exception as e:
        q.put(str(e))


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
