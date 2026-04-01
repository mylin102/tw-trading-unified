"""
Options monitor wrapper — injects shared Shioaji API, no internal login.
"""
import sys
import os

# Ensure options strategy's own options_engine is on path BEFORE any import
_options_dir = os.path.dirname(os.path.abspath(__file__))
if _options_dir not in sys.path:
    sys.path.insert(0, _options_dir)

from options_engine.engine.broker_adapter import ShioajiBrokerAdapter
from live_options_squeeze_monitor import ShioajiOptionsSmartMonitor, MockBrokerAdapter


class OptionsMonitor:
    """Thin wrapper that injects an external api into ShioajiOptionsSmartMonitor."""

    def __init__(self, api, dry_run: bool = False, **kwargs):
        # Force dry_run=True during construction to skip internal login
        self.monitor = ShioajiOptionsSmartMonitor(dry_run=True, **kwargs)

        # Now override with the real api if not dry-run
        if not dry_run and api is not None:
            self.monitor.dry_run = False
            self.monitor.api = api
            self.monitor.live_trading = self.monitor.full_cfg.get("live_trading", False)
            self.monitor.broker = ShioajiBrokerAdapter(api, self.monitor.execution_cfg)

    def on_tick(self, exchange, tick):
        self.monitor.on_tick(exchange, tick)

    def run(self):
        self.monitor.run()

    def stop(self):
        self.monitor._running = False
