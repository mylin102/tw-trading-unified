"""
session_watchdog.py

Auto-reconnect + session health manager for Shioaji.

Usage:
    from session_watchdog import SessionWatchdog

    watchdog = SessionWatchdog(api)

    # call periodically in your main loop
    watchdog.check()

Requirements:
    - api: logged-in Shioaji instance
"""

import time
from datetime import datetime, timedelta


class SessionWatchdog:
    def __init__(self, api, reconnect_interval_sec=10):
        self.api = api
        self.last_ok = datetime.now()
        self.last_reconnect = None
        self.reconnect_interval = timedelta(seconds=reconnect_interval_sec)

    def _kbars_ok(self):
        try:
            # minimal test (adjust contract if needed)
            contract = self.api.Contracts.Futures.MXF["MXF202605"]
            kbars = self.api.kbars(contract=contract, start=None, end=None)

            # check iterable + content
            return hasattr(kbars, "__len__") and len(kbars) > 0
        except Exception:
            return False

    def _reconnect(self):
        now = datetime.now()

        if self.last_reconnect and (now - self.last_reconnect) < self.reconnect_interval:
            return

        print(f"[WATCHDOG] Reconnecting Shioaji session at {now}")

        try:
            self.api.logout()
        except Exception:
            pass

        time.sleep(1)

        try:
            self.api.login(
                api_key=self.api._api_key,
                secret_key=self.api._secret_key
            )
            print("[WATCHDOG] Re-login success")
        except Exception as e:
            print(f"[WATCHDOG] Re-login failed: {e}")

        self.last_reconnect = now

    def check(self):
        now = datetime.now()

        if self._kbars_ok():
            self.last_ok = now
            return "OK"

        # stale session
        if (now - self.last_ok) > timedelta(seconds=5):
            print("[WATCHDOG] DATA_INVALID detected, attempting reconnect...")
            self._reconnect()
            return "RECONNECTING"

        return "WAIT"
