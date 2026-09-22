import json
import threading
import os
import time
import shioaji as sj
from dotenv import load_dotenv

_api_instance = None
_api_lock = threading.Lock()

def logout():
    global _api_instance
    with _api_lock:
        if _api_instance is not None:
            try:
                from core.shioaji_session import logout as shared_logout
                shared_logout()
            except Exception:
                pass
            _api_instance = None

def login():
    global _api_instance
    with _api_lock:
        if _api_instance is not None:
            return _api_instance
        _api_instance = _do_login()
        return _api_instance

def _do_login():
    """
    Login with account info from environment variables or 'account_info.json'.
    :return: shioaji api.
    """
    # Live options is a resident consumer and must not create a second
    # Shioaji owner.  The shared session owns credentials, CA, generations,
    # and lifecycle instrumentation.
    from core.shioaji_session import get_api
    return get_api()
