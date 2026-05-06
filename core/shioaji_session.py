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

def get_api() -> sj.Shioaji:
    """Get or create the singleton Shioaji API instance."""
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

def fetch_contracts(api: sj.Shioaji):
    """Safely fetch contracts with a global lock and state check."""
    global _is_fetching
    
    # Check if contracts are already there to avoid concurrent call error
    try:
        # Use dir() to check for content without triggering expensive logic
        if hasattr(api.Contracts, "Futures") and "MXF" in dir(api.Contracts.Futures):
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
            api.fetch_contracts()
            logger.info("✅ Contracts fetched successfully.")
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

def get_system_status() -> SystemReadiness:
    return _system_status
