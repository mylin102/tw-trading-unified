"""
Futures monitor — wraps daily_simulation logic.
Accepts an injected Shioaji API instance (no internal login).
"""
import sys
import os
import time
import yaml
import threading
from datetime import datetime
import pandas as pd
from rich.console import Console

# local squeeze_futures src
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from squeeze_futures.engine.constants import get_point_value
from squeeze_futures.engine.simulator import PaperTrader
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment, calculate_atr
from squeeze_futures.data.shioaji_client import ShioajiClient
from squeeze_futures.data.data_storage import save_trade, get_storage

console = Console()


class FuturesMonitor:
    def __init__(self, api, config_path: str, dry_run: bool = False):
        self.api = api
        self.dry_run = dry_run
        self.cfg = self._load_config(config_path)
        self.ticker = "TMF"
        self.contract = None
        self._running = False

        # Wrap the injected api into ShioajiClient without re-login
        self.client = ShioajiClient.__new__(ShioajiClient)
        self.client.api = api
        self.client.is_logged_in = not dry_run
        self.client._tick_callbacks = {}
        self.client._kbar_callbacks = {}
        from collections import deque
        self.client._latest_kbars = {}

    def _load_config(self, path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def setup(self):
        if self.dry_run:
            console.print("[yellow][FuturesMonitor] dry-run: skipping contract fetch[/yellow]")
            return True
        self.contract = self.client.get_futures_contract(self.ticker)
        if self.contract is None:
            console.print("[red][FuturesMonitor] contract not found[/red]")
            return False
        console.print(f"[green][FuturesMonitor] contract: {self.contract.code}[/green]")
        return True

    def on_tick(self, exchange, tick):
        """Shared tick dispatcher calls this — filter by contract code."""
        if self.contract and tick.code != self.contract.code:
            return
        # delegate to client's internal handler if registered
        cb = self.client._tick_callbacks.get(tick.code)
        if cb:
            cb(exchange, tick)

    def run(self):
        self._running = True
        console.print(f"[green][FuturesMonitor] started ({'dry-run' if self.dry_run else 'live'})[/green]")
        # Main loop delegated to existing daily_simulation logic
        # For now: poll kbars every 60s (same as original)
        while self._running:
            try:
                self._strategy_tick()
            except Exception as e:
                console.print(f"[red][FuturesMonitor] error: {e}[/red]")
            time.sleep(60)

    def stop(self):
        self._running = False

    def _strategy_tick(self):
        if self.dry_run:
            return
        df = self.client.get_kline(self.ticker, "5m")
        if df.empty:
            return
        # minimal: just log latest bar
        last = df.iloc[-1]
        console.print(f"[dim][FuturesMonitor] {datetime.now().strftime('%H:%M')} TMF close={last['Close']:.0f}[/dim]")
