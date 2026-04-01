"""
Singleton Shioaji session — shared across all strategies.
Only one sj.Shioaji() instance per process.
"""
import os
import time
import threading
import shioaji as sj
from dotenv import load_dotenv

_api: sj.Shioaji | None = None
_lock = threading.Lock()


def get_api() -> sj.Shioaji:
    """Return the shared API instance, logging in if needed."""
    global _api
    with _lock:
        if _api is not None:
            return _api
        _api = _login()
        return _api


def logout():
    global _api
    with _lock:
        if _api is not None:
            try:
                _api.logout()
            except Exception:
                pass
            _api = None


def _login() -> sj.Shioaji:
    load_dotenv(override=True)
    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_PERSON_ID")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_PASSWD")
    ca_path = os.getenv("SHIOAJI_CA_PATH", "")
    ca_name = os.getenv("SHIOAJI_CA_NAME", "")
    ca_passwd = os.getenv("SHIOAJI_CA_PASSWD", "")
    person_id = os.getenv("SHIOAJI_PERSON_ID", api_key)

    if not api_key or not secret_key:
        raise EnvironmentError("SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY not set")

    api = sj.Shioaji()
    for attempt in range(1, 4):
        try:
            api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
            print(f"[session] Logged in (attempt {attempt})")
            break
        except Exception as e:
            if "Too Many Connections" in str(e) and attempt < 3:
                wait = attempt * 30
                print(f"[session] Too Many Connections — retrying in {wait}s ({attempt}/3)")
                time.sleep(wait)
            else:
                raise

    ca_full = os.path.join(ca_path, ca_name) if ca_path and ca_name else ""
    if ca_full and os.path.exists(ca_full):
        try:
            ok = api.activate_ca(ca_path=ca_full, ca_passwd=ca_passwd, person_id=person_id)
            print(f"[session] CA {'activated' if ok else 'activation failed'}: {ca_full}")
        except Exception as e:
            print(f"[session] CA error: {e}")

    return api
