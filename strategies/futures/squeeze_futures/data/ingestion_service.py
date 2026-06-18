"""
IngestionService — standalone futures data ingestion layer.

Purpose:
  Move ALL Shioaji API access (api.kbars, client.get_kline) out of
  _strategy_tick() and into a dedicated ingestion/backfill service.

  Strategy code reads from canonical bars only. This service is the
  only layer that calls Shioaji for kbar data.

Design:
  - Every API response is saved to CSV BEFORE any processing (GSD).
  - Rate limiting is enforced at the service level.
  - TXFR1 data is pre-fetched as a side effect of MXF backfill.
  - Legacy get_kline() fallback is only available as a last resort
    when all primary sources are empty.
"""

import time
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
from rich.console import Console

console = Console()


class IngestionService:
    """Ingestion layer for futures kbar data.

    Wraps all Shioaji API calls (api.kbars, client.get_kline) behind
    rate-limited, CSV-persisting methods. Strategy code never needs to
    touch API objects directly.

    The FuturesMonitor passes its api, client, contract, ticker, and a
    _save_raw_kbars callback at construction time.
    """

    def __init__(self, api, client, contract, ticker: str,
                 save_raw_kbars_cb=None):
        """
        Args:
            api: Shioaji API instance (for api.kbars)
            client: ShioajiClient instance (for client.get_kline)
            contract: Shioaji contract object
            ticker: Contract ticker string (e.g. "MXFR1")
            save_raw_kbars_cb: Callback to save raw bars to CSV
                               Signature: (bars) -> None
        """
        self._api = api
        self._client = client
        self._contract = contract
        self._ticker = ticker
        self._save_raw_kbars = save_raw_kbars_cb or (lambda _: None)

        # Rate-limit timestamps
        self._last_kbars_fetch_at: float = 0.0        # api.kbars (120s)
        self._last_legacy_kline_at: float = 0.0        # get_kline (300s)
        self._last_tx_prefetch_at: float = 0.0          # TXFR1 pre-fetch (120s)

        # TX cache: populated during backfill/startup
        self._tx_cached_kbars: Optional[list] = None

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def set_contract(self, contract) -> None:
        """Update contract reference after deferred resolution.
        
        IngestionService may be constructed before contract is resolved
        from Shioaji API. Call this once contract becomes available.
        """
        self._contract = contract

    def fetch_backfill(self) -> Optional[pd.DataFrame]:
        """Primary backfill: api.kbars() for 1m kbar data.

        Rate-limited to 120s between calls. Saves to CSV before return.
        As a side effect, also pre-fetches TXFR1 bars for cross-regime engine.

        Returns DataFrame with OHLCV columns, or None if rate-limited/empty.
        """
        if not self._api or not self._contract:
            return None

        now_ts = time.time()
        if now_ts - self._last_kbars_fetch_at < 120:
            return None  # Rate-limited

        try:
            today = datetime.now()
            start_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
            if today.hour < 5:
                today = today - timedelta(days=1)
            date_str = today.strftime("%Y-%m-%d")

            console.print(
                f"[cyan][Ingestion] Fetching kbars for {self._contract.code}, "
                f"from {start_date} to {date_str}[/cyan]"
            )
            bars = self._api.kbars(self._contract, start=start_date, end=date_str)
            self._last_kbars_fetch_at = now_ts

            if bars is None:
                return None
                
            # [rshioaji 1.5.10 Fix] Robust emptiness check for both lowercase and Uppercase attribute names
            # Handles objects that don't support len(bars)
            has_data = False
            for attr in ["close", "Close", "ts", "Timestamp"]:
                val = getattr(bars, attr, None)
                if val is not None and hasattr(val, "__len__") and len(val) > 0:
                    has_data = True
                    break
            
            if not has_data:
                return None

            # [GSD Data Safety] Save raw API response to CSV FIRST
            self._save_raw_kbars(bars)

            from core.broker.shioaji_compat import kbars_to_dataframe
            frame = kbars_to_dataframe(bars)
            
            if frame.empty:
                return None

            # Standardized column names are already handled by get_kbars_df
            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            available_cols = [col for col in required_cols if col in frame.columns]
            if len(available_cols) < 4:
                return None

            result = frame[available_cols].sort_index()

            # 2026-06-18 Gemini CLI: [Pure TMF Refactoring] Disabled TXFR1 pre-fetch
            # [Side effect] Pre-fetch TXFR1 bars for cross-regime engine
            # self._prefetch_tx_bars()

            console.print(
                f"[green][Ingestion] Backfill complete: {len(result)} rows[/green]"
            )
            return result

        except Exception as e:
            console.print(f"[yellow][Ingestion] api.kbars failed: {e}[/yellow]")
            return None

    def fetch_legacy_fallback(self) -> Optional[pd.DataFrame]:
        """Tertiary / last-resort fallback: client.get_kline().

        Only called when BOTH tick-based bars AND api.kbars backfill
        return empty data. Rate-limited to 300s.

        Returns DataFrame with OHLCV columns, or None.
        """
        if not self._client:
            return None

        now_ts = time.time()
        if now_ts - self._last_legacy_kline_at < 300:
            return None  # Rate-limited

        try:
            console.print("[dim][Ingestion][FALLBACK] Trying legacy get_kline...[/dim]")
            df = self._client.get_kline(self._ticker, interval="5m")
            self._last_legacy_kline_at = now_ts

            if df is not None and not df.empty:
                # [GSD Data Safety] Save raw kline data to CSV
                self._save_raw_kbars(df)
                console.print(
                    f"[green][Ingestion] Legacy fallback: {len(df)} rows[/green]"
                )
                return df
            return None

        except Exception as e:
            console.print(f"[yellow][Ingestion] get_kline fallback failed: {e}[/yellow]")
            return None

    def get_tx_cache(self) -> Optional[list]:
        """Return cached TXFR1 bars for cross-regime engine.

        Returns list of dicts with keys {close, high, low}, or None.
        This is populated as a side effect of fetch_backfill() and
        fetch_legacy_fallback(), so strategy_tick() never needs to
        call get_kline() for TX data on-demand.
        """
        return self._tx_cached_kbars

    def fetch_recovery_kline(self) -> Optional[pd.DataFrame]:
        """Recovery kline fetch for contract staleness checks.

        Separate rate limit (120s) from the main backfill path.
        Used exclusively by _check_futures_contract_staleness().

        Returns DataFrame or None.
        """
        if not self._client:
            return None

        now_ts = time.time()
        if now_ts - self._last_legacy_kline_at < 120:
            return None  # 120s for recovery (shorter than fallback)

        try:
            df = self._client.get_kline(self._ticker, interval="5m")
            self._last_legacy_kline_at = now_ts

            if df is not None and not df.empty:
                self._save_raw_kbars(df)
                return df
            return None

        except Exception as e:
            console.print(
                f"[yellow][Ingestion] Recovery kline failed: {e}[/yellow]"
            )
            return None

    def fetch_setup_kline(self) -> Optional[pd.DataFrame]:
        """Startup warm-up kline fetch.

        Called once during setup(). Less aggressive rate limit than
        backfill to avoid hammering the API at startup.
        Returns DataFrame or None.
        """
        if not self._client:
            return None

        try:
            df = self._client.get_kline(self._ticker, interval="5m")
            if df is not None and not df.empty:
                self._save_raw_kbars(df)
                # Also pre-fetch TX bars on startup
                self._prefetch_tx_bars()
                return df
            return None

        except Exception as e:
            console.print(
                f"[yellow][Ingestion] Setup kline failed: {e}[/yellow]"
            )
            return None

    # ──────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────

    def _prefetch_tx_bars(self):
        """Pre-fetch TXFR1 bars for cross-regime engine.

        Called as a side effect of MXF backfill/startup fetches.
        Populates _tx_cached_kbars so strategy_tick() never needs
        on-demand get_kline() calls for TX data.

        Rate-limited to 120s. Best-effort; failures are silent.
        """
        if not self._client:
            return

        now_ts = time.time()
        if now_ts - self._last_tx_prefetch_at < 120:
            return

        try:
            tx_df = self._client.get_kline("TXFR1", interval="5m")
            self._last_tx_prefetch_at = now_ts

            if tx_df is not None and not tx_df.empty:
                tx_bars_list = [
                    {
                        "close": float(r.get("Close", 0)),
                        "high": float(r.get("High", 0)),
                        "low": float(r.get("Low", 0)),
                    }
                    for _, r in tx_df.tail(100).iterrows()
                ]
                self._tx_cached_kbars = tx_bars_list
                console.print(
                    f"[dim][Ingestion] Cached {len(tx_bars_list)} TX bars for "
                    f"cross-regime[/dim]"
                )
        except Exception:
            pass  # Best-effort
