"""
Futures monitor — full strategy from daily_simulation.
Accepts an injected Shioaji API instance (no internal login).
"""
import sys
import os
import glob
import hashlib
import time
import json
import math
import yaml
import traceback
import uuid
import contextlib
from pathlib import Path
from collections import deque
from strategies.futures.mts_ledger_authority import (
    MtsAuthority,
    MtsGateAction,
    MtsLedgerProjection,
    gate_decision_post_signal,
    gate_decision_pre_signal,
)
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import logging
logger = logging.getLogger("FuturesMonitor")
import pandas as pd
from rich.console import Console

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from squeeze_futures.engine.constants import get_point_value
from squeeze_futures.engine.simulator import PaperTrader
# 指標計算
# 指標計算
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from squeeze_futures.data.data_storage import save_trade

# GSD: Pluggable Strategy Integration
from core.strategy_registry import StrategyRegistry
from core.runtime_paths import runtime_logs, runtime_path
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal
from core.futures_bar_regime import classify_futures_bar_regime
from core.futures_strategy_router import FuturesRouterDecision, route_futures_signal

# Data ingestion layer — all Shioaji API access is isolated here
from squeeze_futures.data.ingestion_service import IngestionService
# GSD: 策略外掛系統
from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal
from core.bar_utils import attach_bar_metadata, build_canonical_bar_frames, build_preferred_canonical_bar_frames, resample_ohlcv
from core.date_utils import get_taifex_futures_hhmm, is_taifex_futures_market_open, get_taifex_futures_session_type, get_session_date_str
from core.spread_loader import get_spread_loader
from squeeze_futures.data.shioaji_client import AdapterOrderError, ShioajiClient
from squeeze_futures.data.data_storage import save_trade
from squeeze_futures.data.tick_writer import RawTickWriter
from squeeze_futures.data.kbar_writer import RawKbarWriter

try:
    from squeeze_futures.report.notifier import send_email_notification as _legacy_notify
except ImportError:
    _legacy_notify = None

# Structured notification system (core/notification/)

# 2026-07-07 Hermes Agent: Module-level constant so tests can
# monkeypatch the path without touching the live state file.
MTS_POSITION_STATE_PATH = Path("/tmp/mts_position_state.json")

# [S1] EXIT_ONLY snapshot staleness TTL (ms): canonical with
# core.reconciled_exit.SNAPSHOT_TTL_MS (60s) — a snapshot older than this,
# or stamped in the future, is never trusted for exit evaluation.
from core.reconciled_exit import \
    SNAPSHOT_TTL_MS as EXIT_ONLY_SNAPSHOT_TTL_MS

# 2026-07-14 Gemini CLI: Dataclass snapshot for MTF score tracking under ADR-009 Phase 1
from dataclasses import dataclass, field

@dataclass(frozen=True)
class MtfSnapshot:
    score: Optional[float] = None
    timestamp: Optional[datetime] = None
    valid: bool = False
    components: dict[str, float] = field(default_factory=dict)
    reason: str = "NOT_INITIALIZED"




import threading
_thread_local = threading.local()


def _extract_bbo(quote_obj):
    """Pure BBO extractor with explicit quote-quality classification.

    Returns (bid, ask, quality) where quality is one of:
      "BBO_VALID"            — real bid/ask (BidAskFOPv1 or scalar bid/ask);
      "TICK_ONLY"            — last/close present, no BBO (diagnostics only);
      "DATA_QUALITY_BLOCKED" — no usable price at all.

    2026-08-04 review: the buy_price/sell_price fallback (f3743daa) was
    removed — runtime capture proved futures ticks carry last/close only, so
    the fallback silently fabricated nothing and masked the real contract gap.
    NEVER falls back to close/last to fake a BBO; callers must gate Model C
    decisions on quality == "BBO_VALID" only.
    """
    def _first(v):
        if isinstance(v, list):
            return v[0] if v else None
        return v
    _bp = _first(getattr(quote_obj, "bid_price", None))
    _ap = _first(getattr(quote_obj, "ask_price", None))
    # BidAskFOPv1 exposes bid_price/ask_price lists; scalar bid/ask also valid.
    if _bp is None:
        _bp = getattr(quote_obj, "bid", None)
    if _ap is None:
        _ap = getattr(quote_obj, "ask", None)
    if _bp is not None and _ap is not None:
        try:
            _b = float(_bp)
            _a = float(_ap)
        except (TypeError, ValueError):
            _b = _a = None
        if _b is not None and _a is not None and _b > 0 and _a > 0 and _a >= _b:
            return (_b, _a, "BBO_VALID")
    # No real BBO: classify what IS available (last/close) for diagnostics.
    _last = _first(getattr(quote_obj, "last", None))
    if _last is None:
        _last = _first(getattr(quote_obj, "close", None))
    if _last is not None:
        try:
            float(_last)
            return (None, None, "TICK_ONLY")
        except (TypeError, ValueError):
            pass
    return (None, None, "DATA_QUALITY_BLOCKED")


def _repo_root() -> str:
    """Repo root from __file__ (strategies/futures/monitor.py -> parents[2]).
    Never cwd-dependent."""
    import pathlib
    return str(pathlib.Path(__file__).resolve().parents[2])


def _mts_position_state_path() -> Path:
    """Return the MTS position state file path.

    P1-B: also hosts the durable exit-intent log under <runtime>/logs.

    Respects MTS_STATE_PATH env var override (used in production for
    alternate deployments), falling back to the module-level constant.
    Tests can monkeypatch MTS_POSITION_STATE_PATH to redirect.
    """
    # 2026-07-21 Gemini CLI: Support thread-local path override for multi-instance concurrent processes
    if getattr(_thread_local, "state_path", None) is not None:
        return Path(_thread_local.state_path)
    # 2026-07-08 Gemini CLI: Allow test injection but fall back to test path to avoid leaking to production /tmp/mts_position_state.json
    import sys
    import os
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        _env = os.getenv("MTS_STATE_PATH")
        if _env:
            return Path(_env)
        # If the constant was monkeypatched in the test, honor the patch!
        if MTS_POSITION_STATE_PATH != Path("/tmp/mts_position_state.json"):
            return MTS_POSITION_STATE_PATH
        # Allow contract test to verify default constant fallback
        if "uses_constant" in os.getenv("PYTEST_CURRENT_TEST", ""):
            return MTS_POSITION_STATE_PATH
        return Path("/tmp/test_mts_position_state.json")
    return Path(os.getenv("MTS_STATE_PATH", str(MTS_POSITION_STATE_PATH)))


def _mts_intent_log_dir() -> str:
    """P1-B: durable COMBINED_EXIT intent log at the shared runtime dir.

    Same isolation convention as _mts_position_state_path: under pytest
    without TRADING_RUNTIME_DIR, use a PER-TEST temp dir (never the repo)."""
    import sys as _sys
    if "pytest" in _sys.modules:
        _rt = os.environ.get("TRADING_RUNTIME_DIR")
        if _rt:
            return os.path.join(_rt, "logs")
        import tempfile as _tf
        import hashlib as _hl
        _pt = os.environ.get("PYTEST_CURRENT_TEST", "") or ""
        _pt_id = _hl.md5(_pt.encode()).hexdigest()[:10] if _pt else "default"
        # per-test AND per-run (pid): a previous run's SUBMITTED intent must
        # never bleed into this run's assertions
        return os.path.join(_tf.gettempdir(),
                            f"test_mts_exit_intent_{_pt_id}_{os.getpid()}")
    from core.runtime_paths import runtime_root
    return os.path.join(runtime_root(), "logs")

try:
    from core.notification.notifier import notify_trade_event as _notify_trade_event
    from core.notification.formatters.futures_formatter import (
        FuturesPositionState,
        compute_futures_pnl,
    )
    _has_notification_system = True
except ImportError:
    _has_notification_system = False

console = Console()


def _check_trend_breakout_signal(df_5m, df_15m):
    try:
        from squeeze_futures.engine.trend_breakout import check_trend_breakout
    except ImportError:
        return {"trend_long": False, "trend_short": False, "reasons": []}
    result = {"trend_long": False, "trend_short": False, "reasons": []}
    if len(df_5m) >= 20:
        b = check_trend_breakout(df_5m, lookback=20, ma_length=20, compare_bars=5, slope_threshold=0.1)
        if b["long_signal"]:
            result["trend_long"] = True
            result["reasons"].extend([f"5m: {r}" for r in b["long_reasons"]])
        if b["short_signal"]:
            result["trend_short"] = True
            result["reasons"].extend([f"5m: {r}" for r in b["short_reasons"]])
    return result


# 2026-07-07 Gemini CLI: Generation tracking dictionary wrapper for pending lifecycle orders
class GenerationDict(dict):
    def __init__(self, monitor):
        self.monitor = monitor
        super().__init__()
    def __setitem__(self, key, value):
        if isinstance(value, dict) and "generation" not in value:
            value["generation"] = getattr(self.monitor, "_lifecycle_generation", 0)
        super().__setitem__(key, value)


class ExecutionContextSyncFatal(RuntimeError):
    """[P1] a committed canonical execution-context write could not be
    rolled back.  In-memory state is already forced to fail-closed
    quarantine; an order-capable process must not continue."""


class FuturesMonitor:
    def __init__(self, api, config_path: str, dry_run: bool = False):
        self.api = api
        self.config_path = config_path
        self._config_mtime = 0
        self.dry_run = dry_run
        self.cfg = self._load_config(config_path)
        # 2026-05-27 Gemini CLI: Generalize ticker initialization (no hardcoded default)
        self.ticker = self.cfg.get("ticker", "UNKNOWN")
        # 2026-07-21 Gemini CLI: Determine state path based on ticker name for multi-instance isolation
        _base_name = self.ticker.lower()
        if _base_name == "tmf":
            self._state_path = Path("/tmp/mts_position_state.json")
        else:
            self._state_path = Path(f"/tmp/mts_position_state_{_base_name}.json")
        self.contract = None
        self.far_contract = None  # Far-month contract for dual chart
        self._running = False

        # Far-month tick-based bar accumulation (independent from near-month)
        self._far_tick_bars_deque = deque(maxlen=300)
        self._far_current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
        self._last_far_bar_ts = 0

        # 2026-07-08 Hermes Agent: incremental VWAP accumulators (updated per tick)
        self._near_cum_vol = 0.0
        self._near_cum_pv = 0.0   # cumulative price × volume
        self._far_cum_vol = 0.0
        self._far_cum_pv = 0.0

        # Compatibility placeholders for external integrations
        self.feed_health = None
        self.tx_bar_builder = None
        # [TX Cache] Pre-computed TX bars for cross-regime engine (populated
        # during backfill/startup, NOT fetched on-demand in strategy tick).
        self._tx_cached_kbars = None

        # Wrap injected api into ShioajiClient without re-login
        self.client = ShioajiClient.__new__(ShioajiClient)
        self.client.api = api
        self.client.is_logged_in = not dry_run

        # [Skew Integration] Option surface engine — populated by bidask
        # dispatcher via OptionQuoteEvent, consumed in _build_strategy_context.
        self._skew_engine = None
        self.client._tick_callbacks = {}
        self.client._kbar_callbacks = {}
        self.client._latest_kbars = {}
        # ``__new__`` bypasses ShioajiClient.__init__.  Initialize the
        # fail-closed adapter gate explicitly, then persist/transition code
        # can propagate LIVE_READY before any order is attempted.
        self.client._execution_context = None

        # [ThetaGate] Latest router decision — consumed by options monitor
        # to check theta_allowed flag before entering theta positions.
        self.latest_router_decision: FuturesRouterDecision | None = None

        # 2026-06-23 Gemini CLI: Initialize strategy registry early to prevent AttributeError in tests/methods called before setup
        self._registry = StrategyRegistry()

        # [Phase 2] IngestionService — all Shioaji API access is isolated here.
        # strategy_tick() and signal generation read from canonical bars only.
        self._ingestion = IngestionService(
            api=api,
            client=self.client,
            contract=self.contract,
            ticker=self.ticker,
            save_raw_kbars_cb=self._save_raw_kbars,
        )

        # GSD: Initialize stateful attributes before applying config
        self.cooldown_until = 0
        self.consecutive_losses = 0
        self.session_losses = []  # [(timestamp, pnl_pts, exit_reason, session)]
        self.session_type = None  # "day" or "night", set per bar
        self.previous_session_type = None  # Track previous session for transition detection
        self._last_bar_context = {}  # Phase 0c: snapshot for entry diagnostic
        self._circuit_breaker = None
        self._session_pnl = 0.0  # Session PnL for circuit breaker
        
        # Failure detection state: tracks pending squeeze fire
        self._fire_pending_dir = 0   # +1=bullish fire, -1=bearish fire
        self._fire_bar_idx = 0
        self._fire_high = 0.0
        self._fire_low = 0.0
        self._bar_counter = 0        # monotonic bar counter for fire tracking
        self.is_monitoring_ready = True # [GSD 4.13] Phase A Ready
        self.is_trading_ready = False   # [GSD 4.13] Phase B Ready
        self._vwap_violation_bars = 0  # VWAP exit debounce counter
        self._atr_trail_peak = 0.0    # ATR trailing stop: peak price tracker

        # GSD Phase 0d: Hourly no-trade audit tracking
        self._last_trade_ts = None       # timestamp of last trade
        self._bars_since_trade = 0       # bars since last trade
        self._bars_since_session_open = 0 # [V-Model Upgrade] Track session bar count
        # ── Squeeze Fire Scout time stop tracking ──
        self._scout_entry_bar: int = -1
        self._scout_time_stop_bars: int = 0
        self._signals_generated = 0      # valid signals this hour
        self._signals_rejected = 0       # rejected signals this hour (reason, count)
        self._last_audit_hour = -1       # last hour we ran the audit
        self._data_stale_bars = 0        # consecutive bars with no new data
        self.options_monitor = None      # shared options monitor for hourly audit / repair
        
        # 💡 GSD: Market data cache for virtual ticks
        # 2026-05-27 Gemini CLI: Use dynamic ticker instead of hardcoded MTX
        self.market_data = {self.ticker: {"close": None}}
        self.last_tick_at = time.time()  # [gstack] 數據新鮮度追蹤 — must init before _strategy_tick()
        self._last_real_tmf_tick_at = self.last_tick_at
        self._runtime_status = None
        self._manual_trade_status = "READY"  # [GSD] Track manual trade state (READY, PROCESSING, FILLED, FAILED)
        # Tracks the terminal audit status for a manual emergency close.  It is
        # deliberately in-memory: a restart while orders are outstanding must
        # remain reconcilable rather than being reported as completed.
        self._emergency_cmd = None
        # 2026-06-26 Gemini CLI: Initialize dynamic flag path from environment variable
        # 2026-07-21 Gemini CLI: Use ticker-specific manual trade flag path for multi-instance separation
        _env_flag = os.environ.get("FUTURES_MANUAL_TRADE_FLAG_PATH")
        if _env_flag:
            self.manual_trade_flag_path = _env_flag
        else:
            _base_name = self.ticker.lower()
            if _base_name == "tmf":
                self.manual_trade_flag_path = "/tmp/futures_manual_trade.flag"
            else:
                self.manual_trade_flag_path = f"/tmp/futures_manual_trade_{_base_name}.flag"
        # 2026-06-05 JVS Claw: NO_LIVE_TICK fix — atomic flag lifecycle + idempotency
        self._processed_flag_ids: set = set()   # C2: idempotency set (in-memory, reset on restart)
        # 2026-08-06 Hermes Agent P1: incremental fills-ledger authority
        # (three-state FLAT/OPEN/UNKNOWN). Bootstrap reads the ledger once;
        # afterwards only NEW bytes are tail-read (never a per-tick full scan).
        self._ledger_projection = MtsLedgerProjection(
            path=os.environ.get("MTS_FILL_LOG_PATH")
            or runtime_logs("mts_trade_fills.jsonl"),
            source="PAPER",
        )
        self._ledger_projection_sync_ts = 0.0
        # 2026-08-06 codex audit: evaluator-lag SLO (split clocks). While a
        # position is open, both the tick loop and the strategy evaluator
        # must heartbeat; alert when either goes silent past its SLO.
        # Baselines are seeded at startup so a restored OPEN position with no
        # first evaluation is detected (not silently skipped).
        self._startup_mono = time.monotonic()
        self._last_mts_tick_mono = self._startup_mono
        self._prev_mts_tick_mono = self._startup_mono
        self._last_mts_tick_wall = datetime.now().isoformat()
        self._last_strategy_evaluation_mono = self._startup_mono
        self._last_strategy_evaluation_wall = datetime.now().isoformat()
        self._strategy_evaluated_once = False
        self._last_slo_alert_mono: dict = {}

        self._flag_retry_count: int = 0         # C7: retry counter (in-memory)
        self._current_flag_id: str | None = None  # C2: tracks flag being processed
        # 2026-06-05 JVS Claw: R1 — startup cleanup of orphaned .processing files
        for _orph in glob.glob(self.manual_trade_flag_path + ".processing"):
            try:
                os.rename(_orph, _orph.replace(".processing", ""))
                console.print(f"[yellow]🔄 [STARTUP] Recovered orphaned flag: {_orph}[/yellow]")
            except Exception:
                pass

        # Apply config (Initial create for Trader and OrderMgr happens here)
        self.order_mgr = None
        self.paper_fill_sim = None
        self._apply_config_params()
        self._config_mtime = os.path.getmtime(self.config_path) if os.path.exists(self.config_path) else 0

        self.has_tp1_hit = False
        self.last_processed_bar = None
        self._last_exit_bar = None  # 防止同根 K bar exit 後再進場
        self._last_entry_reason = None
        self.active_strategy_name = None
        self._initialized_strategy_names = set()
        self._safety_stop_trade = None  # Exchange-side safety stop order
        # 2026-07-07 Gemini CLI: Generation token for order lifecycle and stale callback guard
        self._lifecycle_generation = 0
        self._emergency_reset_at = None
        self._pending_lifecycle_orders = GenerationDict(self)
        self._applied_lifecycle_deals = set()
        self._mts_pending_fills: Dict[str, Dict[str, Any]] = {}  # [GSD] Track multi-leg spread fills before sync
        # 2026-05-27 Gemini CLI: Track orders currently undergoing timeout cancellation to prevent re-entry
        self._mts_stale_order_cancels = set()
        # 💡 GSD: Initialize with current time bucket to prevent immediate flip
        self._last_bar_ts = int(time.time() / 300) * 300

        # ── [V-Model] SpreadLoader for calendar spread data (near-far spread_z) ──
        self._spread_loader = get_spread_loader()
        # 2026-06-26 Gemini CLI: Pass active ticker to prevent loading default MXF CSV files
        self._spread_loaded = self._spread_loader.load_latest_csv(self.ticker)
        if self._spread_loaded:
            print(f"[V-Model] SpreadLoader initiated: {self._spread_loader.status()}")
        else:
            print("[V-Model] SpreadLoader: no calendar spread data found")
            active_strat = self.cfg.get("active_strategy") or self.cfg.get("strategy", {}).get("active_strategy")
            # 2026-06-26 Gemini CLI: If active strategy is a spread strategy, block startup if CSV is missing
            if active_strat in ("tmf_spread", "calendar_condor_v2"):
                raise ValueError(
                    f"[V-Model] Critical error: active strategy is '{active_strat}' but calendar spread CSV "
                    f"data failed to load for ticker '{self.ticker}'. Silent start with missing data is blocked to prevent data pollution."
                )

        # Research dynamics: dz / spread_slope / velocity_ema features.
        # Best-effort — absence only leaves research columns NULL.
        try:
            from strategies.futures.mts.spread_dynamics import \
                SpreadDynamicsCalculator
            self._spread_dynamics = SpreadDynamicsCalculator()
        except Exception:
            self._spread_dynamics = None

        # 2026-07-14 Gemini CLI: Initialize MTF snapshot cache for ADR-009 Phase 1
        self._current_mtf_snapshot = MtfSnapshot()

    @property
    def initial_balance(self) -> int:
        """Get initial balance dynamically. Day and night sessions share the same capital.

        Check order:
        1. State file (/tmp/mts_position_state.json) if exists and valid.
        2. Master config (config/futures.yaml) to keep day/night in sync.
        3. Local session config execution.initial_balance fallback.
        """
        # 1. State file check
        try:
            _state_path = _mts_position_state_path()
            if _state_path.exists():
                with open(_state_path, "r", encoding="utf-8") as _f:
                    _state = json.load(_f)
                _val = _state.get("initial_balance")
                if _val is not None:
                    return int(_val)
        except Exception:
            pass

        # 2. Master config check
        try:
            import yaml
            from pathlib import Path
            _master_path = Path("/Users/mylin/Documents/mylin102/tw-trading-unified/config/futures.yaml")
            if not _master_path.exists():
                _master_path = Path(__file__).parent.parent.parent / "config" / "futures.yaml"
            if _master_path.exists():
                with open(_master_path, "r", encoding="utf-8") as _f:
                    _m_cfg = yaml.safe_load(_f)
                return int(_m_cfg.get("execution", {}).get("initial_balance", 100000))
        except Exception:
            pass

        # 3. Local fallback
        return int(self.EXEC.get("initial_balance", 100000))

    def _apply_config_params(self):
        """[GSD] Extract parameters from self.cfg into instance attributes."""
        # Strategy config
        self.STRATEGY = self.cfg.get("strategy", {})
        self.RISK = self.cfg.get("risk_mgmt", {})
        self.MGMT = self.cfg.get("trade_mgmt", {})
        self.EXEC = self.cfg.get("execution", {})
        self.MONITOR = self.cfg.get("monitoring", {})
        self.PB = self.STRATEGY.get("pullback", {})
        self.TP = self.STRATEGY.get("partial_exit", {})
        self.FILTER_MODE = self.STRATEGY.get("regime_filter", "mid")
        self.ATR_MULT = self.RISK.get("atr_multiplier", 0.0)
        self.ATR_LENGTH = self.RISK.get("atr_length", 14)
        self.POLL_INTERVAL = self.MONITOR.get("poll_interval_secs", 30)

        # Debug flags from config
        _debug_cfg = self.cfg.get("debug", {})
        self._debug_tickbar = bool(_debug_cfg.get("tickbar", False))
        self._debug_feed = bool(_debug_cfg.get("feed", False))

        # Data freshness thresholds (seconds)
        self.STALE_WARN_SECS = self.MONITOR.get("stale_tick_warn_secs", 120)
        self.STALE_CRITICAL_SECS = self.MONITOR.get("stale_tick_critical_secs", 600)
        self.PB_CONFIRM_BARS = self.MONITOR.get("pb_confirmation_bars", 12)
        
        self.PB_ARGS = {
            "ema_fast": self.PB.get("ema_fast", 20),
            "ema_slow": self.PB.get("ema_slow", 60),
            "pb_buffer": self.PB.get("buffer", 1.002),
        }
        
        self.live_trading = self.cfg.get("live_trading", False)
        self.cooldown_bars = self.cfg.get("cooldown_bars", self.STRATEGY.get("cooldown_bars", 8))

        # ── Execution Context (P0-A Mode Model) ──
        # Initializes on first run; preserves across config hot-reload.
        # LIVE starts at LIVE_PREFLIGHT — no orders until transition completes.
        if not hasattr(self, "_execution_context"):
            try:
                from core.mode_transition import live_preflight_context, paper_context, LiveOrderBlocked, EntryBlocked
                if self.live_trading:
                    self._execution_context = live_preflight_context()
                    # [release identity] release-dir HEAD == LRC_RELEASE_SHA
                    # verified in the ACTUAL release tree BEFORE any
                    # certification/transition (fail-closed; missing/
                    # invalid env, git failure, mismatch -> QUARANTINED)
                    _rel_ok = False
                    _rel_reasons = ["RELEASE_IDENTITY_GIT_FAILED"]
                    try:
                        from core.release_identity import verify_release_identity
                        _rel_dir = str(Path(__file__).resolve().parents[2])
                        _rel_ok, _rel_reasons = verify_release_identity(
                            _rel_dir)
                    except Exception:
                        _rel_ok = False
                        _rel_reasons = ["RELEASE_IDENTITY_GIT_FAILED"]
                    if not _rel_ok:
                        from core.mode_transition import (ModeTransitionState,
                                                          with_effective_mode)
                        self._execution_context = with_effective_mode(
                            self._execution_context,
                            ModeTransitionState.LIVE_QUARANTINED.value,
                            live_order_allowed=False,
                            audit_reasons=tuple(_rel_reasons))
                        self._persist_execution_context()
                        print(f"[MTS_EXEC_CTX] LIVE_QUARANTINED "
                              f"{_rel_reasons} — release identity not "
                              f"verified; live orders blocked")
                    else:
                        # [sealed live profile] the LIVE certification
                        # requires the tracked config/futures_live.yaml
                        # profile — the paper default (no config_profile)
                        # can NEVER enter certification => QUARANTINED
                        _profile = self.cfg.get("config_profile")
                        if str(_profile) != "futures_live":
                            self._execution_context = with_effective_mode(
                                self._execution_context,
                                ModeTransitionState.LIVE_QUARANTINED.value,
                                live_order_allowed=False,
                                audit_reasons=("GUARD_CONFIG_PROFILE",))
                            self._persist_execution_context()
                            print(
                                "[MTS_EXEC_CTX] LIVE_QUARANTINED "
                                "GUARD_CONFIG_PROFILE — sealed "
                                "futures_live profile required; live "
                                "orders blocked")
                        else:
                            # [Live Route Certification wiring] certificate-required
                            # startup: certify_route -> transition_with_certificate is
                            # the ONLY path to LIVE_READY; no certificate -> 
                            # LIVE_QUARANTINED (NO_CERTIFICATE). Single startup-path
                            # replacement — no reconnect/order-route changes here.
                            try:
                                from core.live_route_certificate import (
                                    CertificateIssuer, build_runtime_certification_context,
                                    certify_route, transition_with_certificate)
                                from core.mode_transition import (ModeTransitionState,
                                                                  with_effective_mode)
                                _api = getattr(self, "api", None)
                                _issuer = CertificateIssuer()
                                _cert, _cert_failures = (
                                    certify_route(
                                        _api,
                                        process_start_id=f"monitor-{os.getpid()}",
                                        issuer=_issuer,
                                        config_path=self.config_path)
                                    if _api is not None else (None, []))
                                if _cert is None:
                                    self._execution_context = with_effective_mode(
                                        self._execution_context,
                                        ModeTransitionState.LIVE_QUARANTINED.value,
                                        live_order_allowed=False,
                                        audit_reasons=tuple(_cert_failures) or ("NO_CERTIFICATE",))
                                    self._persist_execution_context()
                                    print(f"[MTS_EXEC_CTX] LIVE_QUARANTINED cert=None "
                                          f"reasons={_cert_failures or ['NO_CERTIFICATE']} "
                                          f"— live orders blocked")
                                else:
                                    # [post_startup session gate; D1] bind the
                                    # registry-bound generation into the
                                    # QUARANTINED ctx BEFORE certification —
                                    # the post_startup gate validates
                                    # generation/session/snapshot consistency
                                    # before ANY LIVE_READY transition
                                    self._bind_session_generation()
                                    if not self._confirm_session_generation():
                                        # [D1 race guard] the registry generation
                                        # changed since binding (logout/relogin):
                                        # do NOT promote — stay QUARANTINED with
                                        # SESSION_GENERATION_MISMATCH (zero
                                        # live-order calls; the cert flow is
                                        # skipped entirely)
                                        self._execution_context = (
                                            with_effective_mode(
                                                self._execution_context,
                                                ModeTransitionState.
                                                LIVE_QUARANTINED.value,
                                                live_order_allowed=False,
                                                audit_reasons=(
                                                    "SESSION_GENERATION_MISMATCH",)))
                                        self._persist_execution_context()
                                        print("[MTS_EXEC_CTX] LIVE_QUARANTINED "
                                              "SESSION_GENERATION_MISMATCH — "
                                              "generation changed before "
                                              "certification; live orders blocked")
                                    else:
                                        # [P0 post-startup gate] in-process,
                                        # UNAVOIDABLE — the fresh read-only
                                        # snapshot (same authenticated
                                        # api/session) + the core gate run
                                        # BEFORE any transition_with_certificate
                                        # / LIVE_READY. NO operator CLI
                                        # subprocess — nothing skippable. Any
                                        # failure keeps LIVE_QUARANTINED +
                                        # POST_STARTUP_GATE_FAILED + refusal
                                        # codes; zero transition / zero orders.
                                        _gate, _gate_ev = self._run_post_startup_gate()
                                        if not _gate.ok:
                                            self._execution_context = with_effective_mode(
                                                self._execution_context,
                                                ModeTransitionState.LIVE_QUARANTINED.value,
                                                live_order_allowed=False,
                                                audit_reasons=(
                                                    "POST_STARTUP_GATE_FAILED",) +
                                                    tuple(getattr(
                                                        _gate, "refusal_codes", ()) or ()))
                                            self._persist_execution_context()
                                            print(
                                                "[MTS_EXEC_CTX] LIVE_QUARANTINED "
                                                "POST_STARTUP_GATE_FAILED "
                                                f"refusal_codes={getattr(_gate, 'refusal_codes', ())} "
                                                "— live orders blocked")
                                        elif not self._confirm_session_generation():
                                            # [D1 race] the generation changed
                                            # between the gate and the
                                            # transition — do NOT promote
                                            self._execution_context = with_effective_mode(
                                                self._execution_context,
                                                ModeTransitionState.LIVE_QUARANTINED.value,
                                                live_order_allowed=False,
                                                audit_reasons=(
                                                    "SESSION_GENERATION_MISMATCH",))
                                            self._persist_execution_context()
                                            print(
                                                "[MTS_EXEC_CTX] LIVE_QUARANTINED "
                                                "SESSION_GENERATION_MISMATCH — "
                                                "race after post-startup gate; "
                                                "live orders blocked")
                                        else:
                                            _runtime = build_runtime_certification_context(
                                                _api, self.config_path,
                                                {"process_state": {"process_start_id":
                                                                   f"monitor-{os.getpid()}"}})
                                            self._execution_context = transition_with_certificate(
                                                self._execution_context, _cert, _issuer,
                                                runtime=_runtime)
                                            self._persist_execution_context()
                                            # [orphan reconciliation] a pending
                                            # SAFETY_STOP_RECONCILE intent keeps
                                            # QUARANTINED until the broker state
                                            # reconciles
                                            self._apply_reconcile_pending_gate()
                                    if self._execution_context.is_live_ready():
                                        print("[MTS_EXEC_CTX] LIVE_READY — "
                                              "certificate-required transition complete; "
                                              "live orders authorized")
                                    else:
                                        print(f"[MTS_EXEC_CTX] LIVE_QUARANTINED "
                                              f"reasons={self._execution_context.audit_reasons} "
                                              f"— live orders blocked")
                            except Exception as _exc:
                                print(f"[MTS_EXEC_CTX] transition error: {_exc} — stays LIVE_PREFLIGHT (blocked)")
                else:
                    self._execution_context = paper_context()
            except ImportError:
                self._execution_context = None
                print("[MTS_EXEC_CTX] core.mode_transition not available — hard gate disabled")

        # [sealed live profile] record the config identity (path + sha256)
        # in the execution context for the certification/deployment gate
        try:
            if self._execution_context is not None and \
                    not getattr(self._execution_context, "config_hash", None):
                import hashlib as _hl
                import dataclasses as _dc
                _ch = _hl.sha256(
                    Path(self.config_path).read_bytes()).hexdigest() \
                    if os.path.exists(self.config_path) else ""
                if _ch:
                    self._execution_context = _dc.replace(
                        self._execution_context, config_hash=_ch)
                    self._persist_execution_context()
        except Exception:
            pass

        # 2026-07-07 Gemini CLI: Day and night session capital should share the same parameter.
        # Dynamically adjust trader state if balance changed
        if hasattr(self, 'trader') and self.trader:
            _old_init = getattr(self.trader, "initial_balance", 100000)
            _new_init = self.initial_balance
            if _new_init != _old_init:
                self.trader.initial_balance = _new_init
                self.trader.balance += (_new_init - _old_init)
            self.trader.fee_per_side = self.EXEC.get("broker_fee_per_side", 20)
            self.trader.exchange_fee_per_side = self.EXEC.get("exchange_fee_per_side", 0)
            self.trader.tax_rate = self.EXEC.get("tax_rate", 0)
            self.trader.margin_per_lot = self.EXEC.get("margin_per_lot", 18000)

        # 2026-05-27 Gemini CLI: Dynamic Ticker Support (No hardcoded defaults)
        _old_ticker = self.ticker
        self.ticker = self.cfg.get("ticker", "UNKNOWN")
        if self.ticker != _old_ticker:
            console.print(f"[cyan]🔄 Ticker updated: {_old_ticker} -> {self.ticker}[/cyan]")
            if hasattr(self, '_ingestion'):
                self._ingestion.ticker = self.ticker

        # Squeeze Failure Counter mode
        self.COUNTER = self.STRATEGY.get("counter_mode", {})
        self.counter_enabled = self.COUNTER.get("enabled", False)
        self.counter_auto_regime = self.COUNTER.get("auto_regime", True)
        self.counter_confirm_bars = self.COUNTER.get("confirm_bars", 5)
        self.counter_atr_sl_mult = self.COUNTER.get("atr_sl_mult", 1.0)
        self.counter_exit_vwap = self.COUNTER.get("exit_on_vwap", True)
        self.trend_hold_enabled = self.RISK.get("trend_hold_enabled", True)
        self.trend_hold_atr_mult = self.RISK.get("trend_hold_atr_mult", 2.5)
        self.trend_hold_min_score = self.RISK.get("trend_hold_min_score", 40)
        self.trend_hold_min_trend_strength = self.RISK.get("trend_hold_min_trend_strength", 0.001)
        self.trend_hold_min_price_vs_vwap = self.RISK.get("trend_hold_min_price_vs_vwap", 0.0003)
        self.trend_hold_min_time_to_close_mins = self.RISK.get("trend_hold_min_time_to_close_mins", 20)

        # Update Order Lifecycle settings if needed
        self._use_order_manager = self.MONITOR.get("use_order_manager", False)

        # ── [L3] Order Lifecycle Manager initialization logic (only if not already set) ──
        if self._use_order_manager and not getattr(self, 'order_mgr', None):
            from core.order_management.order_manager import OrderManager
            from core.order_management.paper_fill import PaperFillSimulator
            _om_mode = "live" if self.live_trading else "paper"
            broker = self.client if self.live_trading else None
            self.order_mgr = OrderManager(mode=_om_mode, broker_adapter=broker,
                                          execution_context=getattr(self, '_execution_context', None))
            # [S0] registry injected BEFORE any order path (not lazily)
            self._gateway()
            if _om_mode == "paper":
                self.paper_fill_sim = PaperFillSimulator(self.order_mgr)
                self.order_mgr.set_simulator(self.paper_fill_sim)
            
            # [GSD Fix] Recover orders from trades CSV BEFORE wiring callbacks
            self._recover_orders_from_trades_csv()
            
            self._wire_order_callbacks()
            console.print(f"[green]📋 Order Lifecycle Manager enabled ({_om_mode} mode)[/green]")

        # Create or update Trader
        if not hasattr(self, 'trader'):
            self.trader = PaperTrader(
                ticker=self.ticker,
                initial_balance=self.initial_balance,
                point_value=get_point_value(self.ticker),
                fee_per_side=self.EXEC.get("broker_fee_per_side", 20),
                exchange_fee_per_side=self.EXEC.get("exchange_fee_per_side", 0),
                tax_rate=self.EXEC.get("tax_rate", 0),
                margin_per_lot=self.EXEC.get("margin_per_lot", 18000),
            )
        else:
            # 2026-07-07 Gemini CLI: Day and night session capital should share the same parameter.
            # Dynamically adjust trader state if balance changed
            _old_init = getattr(self.trader, "initial_balance", 100000)
            _new_init = self.initial_balance
            if _new_init != _old_init:
                self.trader.initial_balance = _new_init
                self.trader.balance += (_new_init - _old_init)
            self.trader.fee_per_side = self.EXEC.get("broker_fee_per_side", 20)
            self.trader.exchange_fee_per_side = self.EXEC.get("exchange_fee_per_side", 0)
            self.trader.tax_rate = self.EXEC.get("tax_rate", 0)
            self.trader.margin_per_lot = self.EXEC.get("margin_per_lot", 18000)

    def _reload_config_if_changed(self):
        """[Rule 9] Hot-reload config if YAML file has been updated."""
        if not os.path.exists(self.config_path):
            return
            
        mtime = os.path.getmtime(self.config_path)
        if mtime > self._config_mtime:
            try:
                self.cfg = self._load_config(self.config_path)
                self._apply_config_params()
                self._config_mtime = mtime
                console.print(f"[cyan]🔄 Config hot-reloaded from {self.config_path}[/cyan]")
            except Exception as e:
                console.print(f"[red]❌ Failed to reload config: {e}[/red]")

    def _is_trend_follow_entry(self, reason: Optional[str] = None) -> bool:
        reason = reason or self._last_entry_reason or ""
        return (
            reason.startswith("ADAPTIVE_TREND_V3")
            or reason.startswith("AI_ORB_V3_")
            or reason.startswith("ORB_UP_BREAKOUT")
            or reason.startswith("ORB_DOWN_BREAKOUT")
            or reason.startswith("LR_ACCEL_")
        )

    def _trend_hold_active(self, last_5m, last_price: float, score: float, vwap: float, time_to_close: float) -> bool:
        if not self.trend_hold_enabled or self.trader.position == 0:
            return False
        if not self._is_trend_follow_entry():
            return False
        if time_to_close <= self.trend_hold_min_time_to_close_mins:
            return False
        if abs(score) < self.trend_hold_min_score:
            return False

        trend_strength = float(last_5m.get("trend_strength_raw", 0.0))
        price_vs_vwap = float(last_5m.get("price_vs_vwap", 0.0))
        if price_vs_vwap == 0.0 and vwap:
            price_vs_vwap = (last_price - vwap) / vwap

        if self.trader.position > 0:
            bullish_align = bool(last_5m.get("bullish_align", last_5m.get("bull_align", False)))
            momentum_ok = float(last_5m.get("momentum", 0.0)) >= 0
            return (
                bullish_align
                and momentum_ok
                and trend_strength >= self.trend_hold_min_trend_strength
                and price_vs_vwap >= self.trend_hold_min_price_vs_vwap
            )

        bearish_align = bool(last_5m.get("bearish_align", last_5m.get("bear_align", False)))
        momentum_ok = float(last_5m.get("momentum", 0.0)) <= 0
        return (
            bearish_align
            and momentum_ok
            and trend_strength <= -self.trend_hold_min_trend_strength
            and price_vs_vwap <= -self.trend_hold_min_price_vs_vwap
        )

    def _apply_trend_hold_trail(self, last_price: float, last_5m, timestamp):
        atr_val = float(last_5m.get("atr", 50) or 50)
        if self.trader.position > 0:
            self._atr_trail_peak = max(self._atr_trail_peak, last_price)
            trail_floor = self._atr_trail_peak - atr_val * self.trend_hold_atr_mult
            if last_price <= trail_floor:
                return self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="TREND_HOLD_TRAIL")

        elif self.trader.position < 0:
            if self._atr_trail_peak == 0:
                self._atr_trail_peak = last_price
            self._atr_trail_peak = min(self._atr_trail_peak, last_price)
            trail_ceil = self._atr_trail_peak + atr_val * self.trend_hold_atr_mult
            if last_price >= trail_ceil:
                return self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="TREND_HOLD_TRAIL")

        return None

    @staticmethod
    def _deep_merge_dict(base: dict, override: dict) -> dict:
        res = dict(base)
        for key, val in override.items():
            if key in res and isinstance(res[key], dict) and isinstance(val, dict):
                res[key] = FuturesMonitor._deep_merge_dict(res[key], val)
            else:
                res[key] = val
        return res

    def _load_config(self, path):
        actual_path = str(path)
        if not os.path.exists(actual_path) and os.path.exists("config/futures.yaml"):
            actual_path = "config/futures.yaml"
            console.print(f"[cyan]ℹ️ Config file {path} redirected to primary config/futures.yaml[/cyan]")
        elif os.path.exists("config/futures.yaml") and ("futures_night" in actual_path or "futures_day" in actual_path or "futures_mtx" in actual_path):
            actual_path = "config/futures.yaml"

        with open(actual_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        session_key = "night" if "night" in str(path).lower() else "day"
        overrides = cfg.get("session_overrides", {})
        if session_key in overrides and isinstance(overrides[session_key], dict):
            cfg = self._deep_merge_dict(cfg, overrides[session_key])

        local_path = actual_path.replace(".yaml", ".local.yaml") if actual_path.endswith(".yaml") else actual_path + ".local"
        if os.path.exists(local_path):
            try:
                with open(local_path, encoding="utf-8") as lf:
                    local_cfg = yaml.safe_load(lf) or {}
                cfg = self._deep_merge_dict(cfg, local_cfg)
                console.print(f"[cyan]ℹ️ Applied local config override from {local_path}[/cyan]")
            except Exception as e:
                console.print(f"[yellow]⚠️ Failed to load local config override {local_path}: {e}[/yellow]")
        return cfg

    def _get_tick_bars_df(self):
        """[Wave 2] Rebuild deque cache on every call so _strategy_tick sees latest bars."""
        _perf_started = time.perf_counter()
        if len(self._tick_bars_deque) > 0:
            records = list(self._tick_bars_deque)
            self._tick_bars_cache = pd.DataFrame({
                "Open": [r["open"] for r in records],
                "High": [r["high"] for r in records],
                "Low": [r["low"] for r in records],
                "Close": [r["close"] for r in records],
                "Volume": [r["volume"] for r in records],
            }, index=[r["ts"] for r in records])
        _result = self._tick_bars_cache if self._tick_bars_cache is not None else pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        _elapsed_ms = (time.perf_counter() - _perf_started) * 1000
        if _elapsed_ms >= 100:
            logger.info("[PERF] tick_bars_dataframe duration_ms=%.1f rows=%d", _elapsed_ms, len(_result))
        return _result

    def get_far_tick_bars_df(self):
        """Return far-month tick bars as DataFrame for dashboard consumption."""
        records = list(self._far_tick_bars_deque)
        if not records:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        return pd.DataFrame({
            "Open": [r["open"] for r in records],
            "High": [r["high"] for r in records],
            "Low": [r["low"] for r in records],
            "Close": [r["close"] for r in records],
            "Volume": [r["volume"] for r in records],
        }, index=[r["ts"] for r in records])

    def _bars_time_aligned(self, tx_bars, df_5m):
        """Check that the latest TX bar and MXF 5m bar share the same timestamp bucket.

        Args:
            tx_bars (list[dict]): list of tx bars from TxBarBuilder.bars()
            df_5m (pd.DataFrame): processed 5m dataframe for MXF

        Returns:
            bool: True if aligned, False otherwise
        """
        try:
            if not tx_bars or df_5m is None or len(df_5m) == 0:
                return False
            tx_last = tx_bars[-1].get("ts")
            if tx_last is None:
                return False
            # df_5m index's last timestamp
            tmf_last = df_5m.index[-1]
            # Compare normalized timestamps (both are pandas.Timestamp)
            return pd.Timestamp(tx_last) == pd.Timestamp(tmf_last)
        except Exception:
            return False

    def setup(self):
        # ── GSD: Initialize Strategy Registry ────────────────────────
        # 2026-06-23 Gemini CLI: Reuse existing registry instance if already created in __init__
        if not hasattr(self, "_registry") or self._registry is None:
            self._registry = StrategyRegistry()
        self._registry.discover()
        # [V-Model] Log discovered strategies for startup diagnostics
        _all = self._registry.list_all()
        _available = [s["name"] for s in _all if s.get("available")]
        _names = [s["name"] for s in _all]
        console.print(f"[dim][StrategyRegistry] discovered={len(_all)} available={len(_available)} "
                      f"names={_names}[/dim]")
        self._active_strategy_name = None  # Track initialized strategy

        # ── GSD Phase 3: Circuit Breaker initialization ──────────────
        try:
            from core.circuit_breaker import CircuitBreaker
            # Create two independent breakers (day/night)
            self._circuit_breaker = CircuitBreaker(
                session="day",  # Will be used based on session_type at runtime
                daily_loss_cap=5000,  # 5% of 100k capital
                max_consecutive=3,
            )
            console.print("[green]🛡️ Circuit Breaker initialized[/green]")
        except Exception as e:
            console.print(f" [yellow]⚠️ Circuit Breaker init failed: {e}[/yellow] ")
            self._circuit_breaker = None

        # ── Pre-init the active strategy ─────────────────────────────
        active_name = self.STRATEGY.get("active_strategy", "counter_vwap")
        strategy = self._registry.get(active_name)

        # SAFETY CHECK 2026-04-14: Crash prevention — validate strategy is registered
        if strategy is None:
            available = [s["name"] for s in self._registry.list_all() if s.get("available")]
            console.print(f"[bold red]🚨 Strategy '{active_name}' NOT in registry! Available: {available}[/bold red]")
            console.print(f"[bold red]   System will run in MONITOR-ONLY mode (no entries) until config is fixed.[/bold red]")
            # Set a safe fallback so setup() doesn't crash
            self._active_strategy_name = None
        else:
            # Create a minimal context for init
            dummy_ctx = StrategyContext(
                market=MarketData(
                    last_bar={},
                    # 2026-05-27 Gemini CLI: Pass current ticker to dummy context
                    ticker=self.ticker
                ),
                position=PositionView(),
                config=self.cfg,
                bar_counter=0,
            )
            self._ensure_strategy_initialized(active_name, strategy, dummy_ctx)
            self._active_strategy_name = active_name
            self.active_strategy_name = active_name
            console.print(f"[green]🔧 Pre-initialized strategy: {active_name}[/green]")

        # Tick-based bar builder (Initialize always to avoid AttributeError in dry_run)
        # [Wave 2 optimization] Use deque for O(1) append/trim instead of DataFrame.loc + slicing
        self._tick_bars_deque = deque(maxlen=300)
        self._tick_bars_cache = None  # Cached DF for indicator calculations
        self._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}

        # [GSD Data Safety] RawTickWriter: every tick lands on CSV before memory
        self._tick_writer = None  # Initialised lazily on first real tick
        # One append-only writer per subscribed contract, never shared across legs.
        self._raw_tick_writers = {}

        # [GSD Data Safety] RawKbarWriter: every api.kbars() response lands on CSV before computation
        self._kbar_writer = None  # Initialised lazily on first kbar fetch

        if self.dry_run:
            console.print(" [yellow][FuturesMonitor] dry-run: skipping contract fetch[/yellow] ")
            return True

        # [GSD Fix] Warm-up from Parquet SSOT (Wave 5 Integration)
        try:
            # 2026-06-18 Gemini CLI: [Pure TMF Refactoring] Disabled TXFR1 fallback
            from core.data_manager import data_manager
            ticker_warm = self.ticker  # e.g. "TMF"
            df_hist = data_manager.load_historical(ticker_warm)
            # if df_hist.empty or len(df_hist) < 20:
            #     # Fallback: try TXFR1 which has broader coverage
            #     df_hist = data_manager.load_historical("TXFR1")
            
            if not df_hist.empty and len(df_hist) >= 20:
                df_warm = df_hist.tail(100)
                for ts, row in df_warm.iterrows():
                    self._tick_bars_deque.append({
                        "open": row["Open"], "high": row["High"], "low": row["Low"], 
                        "close": row["Close"], "volume": row["Volume"], "ts": ts
                    })
                # Initialize cache to None — _get_tick_bars_df() will rebuild from deque
                self._tick_bars_cache = None
                console.print(f"[green][FuturesMonitor] ✓ Warmed up with {len(df_warm)} bars from {ticker_warm} Parquet DB[/green]")
            else:
                # [Night Session Fix] Fallback: read from today's indicators CSV for warm-up
                console.print(f" [yellow][FuturesMonitor] Parquet warm-up empty, trying CSV fallback...[/yellow] ")
                from core.date_utils import get_session_date_str
                import os as _os
                log_dir = runtime_logs("market_data")
                date_str = get_session_date_str(datetime.now())
                tag = "_PAPER" if not self.live_trading else "_LIVE"
                csv_path = _os.path.join(log_dir, f"{self.ticker}_{date_str}{tag}_indicators.csv")
                if _os.path.exists(csv_path):
                    df_csv = pd.read_csv(csv_path)
                    if "timestamp" in df_csv.columns:
                        df_csv["timestamp"] = pd.to_datetime(df_csv["timestamp"], errors="coerce")
                        df_csv = df_csv.set_index("timestamp")
                        if len(df_csv) >= 20:
                            df_warm = df_csv.tail(100)
                            for ts, row in df_warm.iterrows():
                                self._tick_bars_deque.append({
                                    "open": row.get("open", row.get("Open", 0)),
                                    "high": row.get("high", row.get("High", 0)),
                                    "low": row.get("low", row.get("Low", 0)),
                                    "close": row.get("close", row.get("Close", 0)),
                                    "volume": row.get("volume", row.get("Volume", 0)),
                                    "ts": ts,
                                })
                            self._tick_bars_cache = None
                            console.print(f"[green][FuturesMonitor] ✓ Warmed up with {len(df_warm)} bars from CSV fallback[/green]")
        except Exception as e:
            console.print(f"[dim][FuturesMonitor] Warm-up failed: {e}[/dim]")

        # Resolve TMF/MTX contracts so they can be subscribed
        self._resolve_contracts()

    def _resolve_contracts(self):
        """[Safe Mode] Get front/far month contracts to prevent deadlock during login."""
        # 💡 Gemini CLI: Import datetime and date at top of function to prevent UnboundLocalError in warm-up block
        from datetime import datetime, date
        try:
            self._warmup_from_local_storage()
        except Exception as e:
            console.print(f"[dim][FuturesMonitor] Warm-up failed: {e}[/dim]")

        # 獲取TMF/MTX合約
        # 💡 Gemini CLI: Added symbol mapping (MTX -> MXF) and type-safe delivery_date handling (datetime.date vs str)
        try:
            # 2026-07-24 Gemini CLI: Keep TMF target_symbol as TMF to query TMFH6 (near) and TMFI6 (far) Micro-TX contracts
            raw_symbol = str(self.ticker).upper()
            target_symbol = "MXF" if raw_symbol == "MTX" else raw_symbol
            print(f"[FuturesMonitor] Getting {target_symbol} contracts (Safe Mode)...")
            
            # [rshioaji 1.5.10 Workaround] Use robust list helper to avoid C++ binding crash
            from core.broker.shioaji_compat import get_contracts_list
            tmf_list = get_contracts_list(self.api, "Futures", target_symbol)
            
            print(f"[FuturesMonitor] Found {len(tmf_list)} {target_symbol} contracts")
            if tmf_list:
                # 2026-07-24 Gemini CLI: Type-safe delivery_date conversion helper to avoid date vs str TypeError
                now_dt = datetime.now()
                now_date = now_dt.date()
                settlement_time = now_dt.replace(hour=13, minute=30, second=0, microsecond=0)

                def _to_deliv_date(c):
                    if c is None:
                        return None
                    d = getattr(c, "delivery_date", None)
                    if d is None:
                        return None
                    if isinstance(d, str):
                        try:
                            return datetime.strptime(d.replace("-", "/"), "%Y/%m/%d").date()
                        except Exception:
                            return None
                    if hasattr(d, "year") and hasattr(d, "month") and hasattr(d, "day"):
                        try:
                            return date(d.year, d.month, d.day)
                        except Exception:
                            return None
                    return None

                valid_contracts = []
                for c in tmf_list:
                    if c is None or not hasattr(c, "code"):
                        continue
                    c_date = _to_deliv_date(c)
                    if c_date is None:
                        continue
                    if c_date > now_date:
                        valid_contracts.append(c)
                    elif c_date == now_date:
                        if now_dt < settlement_time:
                            valid_contracts.append(c)

                def _get_deliv_sort_key(c):
                    cd = _to_deliv_date(c)
                    return cd if cd else date(2099, 12, 31)

                tmf_sorted = sorted(valid_contracts, key=_get_deliv_sort_key)
                
                if tmf_sorted:
                    # Pick the first one (nearest delivery)
                    self.contract = tmf_sorted[0]
                    console.print(f"[green][FuturesMonitor] ✓ {self.ticker} front-month: {getattr(self.contract, 'code', '?')}[/green]")
                    # Sync contract to ingestion service (resolved after __init__)
                    try:
                        self._ingestion.set_contract(self.contract)
                    except Exception:
                        pass
                else:
                    # Fallback to absolute nearest if no valid ones found (shouldn't happen in live)
                    self.contract = sorted(tmf_list, key=_get_deliv_sort_key)[0]
                    console.print(f" [yellow][FuturesMonitor] No future delivery found, using absolute nearest: {getattr(self.contract, 'code', '?')}[/yellow] ")
                
                # Log all available codes for verification
                try:
                    all_codes = [f"{getattr(c, 'code', '?')}({getattr(c, 'delivery_date', '?')})" for c in tmf_sorted]
                    print(f"[FuturesMonitor] Valid {self.ticker} queue: {', '.join(all_codes)}")
                except Exception:
                    pass

                # [Far Month] Select first contract with DIFFERENT delivery date for dual chart
                front_delivery_date = _to_deliv_date(self.contract) if self.contract else None
                self.far_contract = None
                for c in tmf_sorted[1:]:
                    if _to_deliv_date(c) != front_delivery_date:
                        self.far_contract = c
                        break
                # 2026-07-24 Gemini CLI: Fallback search across full tmf_list if tmf_sorted only had 1 valid contract
                if self.far_contract is None and self.contract is not None:
                    for c in tmf_list:
                        if c is not None and hasattr(c, "code") and front_delivery_date:
                            cd = _to_deliv_date(c)
                            if cd and cd > front_delivery_date:
                                self.far_contract = c
                                break
                if self.far_contract is not None:
                    console.print(f"[green][FuturesMonitor] ✓ {self.ticker} far-month: {getattr(self.far_contract, 'code', '?')}[/green]")
                else:
                    self.far_contract = None
                    console.print(f" [yellow][FuturesMonitor] No far-month contract available[/yellow] ")
            else:
                console.print(f"[red][FuturesMonitor] No {self.ticker} contracts found![/red]")
        except Exception as e:
            console.print(f"[red][FuturesMonitor] Error selecting {self.ticker} contract: {e}[/red]")

        # [Bug Fix] Add contract rollover check
        self._last_contract_code = self.contract.code if self.contract else None

        # 2026-08-04 review item 2: canonical contract identity.
        # Shioaji streams the SAME near contract under two code families:
        #   query code (TMFH6) and real stream code (MXFH6); far: TMFI6/MXFI6.
        # FShadow/Model C compare against these alias sets — a tick whose code
        # matches none of them is a CONTRACT_MISMATCH (blocked, never "other").
        self._canonical_near_codes = set()
        self._canonical_far_codes = set()
        _c0 = getattr(self.contract, "code", None)
        _f0 = getattr(self.far_contract, "code", None) if self.far_contract is not None else None
        if _c0:
            self._canonical_near_codes.add(_c0)
            # stream-code alias: MXF<month> for TMF<month> (and vice versa)
            if _c0.startswith("TMF"):
                self._canonical_near_codes.add("MXF" + _c0[3:])
            elif _c0.startswith("MXF"):
                self._canonical_near_codes.add("TMF" + _c0[3:])
        if _f0:
            self._canonical_far_codes.add(_f0)
            if _f0.startswith("TMF"):
                self._canonical_far_codes.add("MXF" + _f0[3:])
            elif _f0.startswith("MXF"):
                self._canonical_far_codes.add("TMF" + _f0[3:])

        
        # 2026-06-24 Gemini CLI: Pre-fill near/far contract prices from snapshots at startup to prevent identical execution prices on first manual trade.
        if self.api and not self.dry_run:
            try:
                _contracts_to_query = []
                if self.contract:
                    _contracts_to_query.append(self.contract)
                if self.far_contract:
                    _contracts_to_query.append(self.far_contract)
                
                if _contracts_to_query:
                    _snaps = self.api.snapshots(_contracts_to_query)
                    for _snap in _snaps:
                        if _snap.close and _snap.close > 0:
                            if self.contract and _snap.code == self.contract.code:
                                self.market_data[self.ticker] = {
                                    "close": float(_snap.close),
                                    "local_arrival_at": time.time(),
                                    "datetime": datetime.now()
                                }
                                self.market_data[f"{self.ticker}_NEAR"] = {
                                    "close": float(_snap.close),
                                    "local_arrival_at": time.time(),
                                    "datetime": datetime.now()
                                }
                            elif self.far_contract and _snap.code == self.far_contract.code:
                                self._far_current_bar["close"] = float(_snap.close)
                                self._far_current_bar["open"] = float(_snap.close)
                                self._far_current_bar["high"] = float(_snap.close)
                                self._far_current_bar["low"] = float(_snap.close)
                                self.market_data[f"{self.ticker}_FAR"] = {
                                    "close": float(_snap.close),
                                    "local_arrival_at": time.time(),
                                    "datetime": datetime.now()
                                }
                                print(f"[WRITER_AUDIT] slot=far code={_snap.code} old_price={_old_far} new_price={_snap.close} source=FuturesMonitor.snapshot_prefill event_time={datetime.now().isoformat()} caller=FuturesMonitor._init_market_data", flush=True)
                                console.print(f"[green][FuturesMonitor] Pre-filled far-month price from snapshot: {_snap.close}[/green]")
            except Exception as _snap_err:
                console.print(f"[yellow][FuturesMonitor] Failed to pre-fill prices from snapshot: {_snap_err}[/yellow]")

        # Pre-fill from kbars if available (使用新的方法)
        try:
            # 首先嘗試使用新的方法獲取當天1分鐘K棒
            df_1min = self._fetch_today_kbars()
            if df_1min is not None and len(df_1min) >= 1:
                # 重採樣為5分鐘K棒
                df = resample_ohlcv(df_1min, "5min")
                
                if not df.empty:
                    # Convert pre-filled bars to deque format
                    for _, row in df[["Open", "High", "Low", "Close", "Volume"]].iterrows():
                        bar_dict = {
                            "open": row["Open"],
                            "high": row["High"],
                            "low": row["Low"],
                            "close": row["Close"],
                            "volume": row["Volume"],
                            "ts": row.name,  # DataFrame index is timestamp
                        }
                        self._tick_bars_deque.append(bar_dict)
                    self._tick_bars_cache = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                    console.print(f"[green][FuturesMonitor] pre-filled {len(self._tick_bars_deque)} bars from today's 1min kbars[/green]")
                    
                    # [GSD Fix] Backfill night session gaps on startup
                    self._backfill_night_gaps(df)
            else:
                # [Phase 2] Legacy fallback delegated to IngestionService (startup-only)
                # If _fetch_today_kbars() is rate-limited or unavailable, the
                # strategy loop will naturally fill from tick accumulation.
                console.print("[dim][FuturesMonitor] No kbar data at startup — will fill from live ticks[/dim]")
        except Exception:
            pass
        return True

    def _backfill_night_gaps(self, api_df):
        """[GSD Fix] On startup, check if today's CSV has night session data.
        If missing or incomplete, merge API bars with existing CSV.
        
        [BUG FIX 2026-04-20] Use get_session_date_str() (trading session date) instead of
        today.strftime('%Y%m%d') (wall-clock date) so the backfill writes to the SAME file
        as _save_bar.  The old code wrote night-session bars to e.g. TMF_20260420 while
        _save_bar correctly wrote them to TMF_20260421, and the dashboard's
        drop_duplicates(keep='first') would then prefer the indicator-less rows from 20260420
        over the correctly-computed rows from 20260421, making the dashboard show NaN until
        ~17:10 when the wrong file's last timestamp was exceeded.
        """
        if self.dry_run or not self.api:
            return
        
        from pathlib import Path
        from core.date_utils import get_session_date_str
        today = datetime.now()
        date_str = get_session_date_str(today)
        tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
        csv_path = Path(runtime_logs("market_data")) / f"{self.ticker}_{date_str}{tag}_indicators.csv"
        
        # [ARCHITECTURE FIX 2026-05-13] NEVER write indicator CSV if it doesn't exist yet.
        # The indicator CSV is an enriched output — only _save_bar should create it.
        # _backfill_night_gaps must not be the first writer, or the CSV header
        # will have raw API column ordering (timestamp-as-index) instead of canonical order.
        # Strategy tick will trigger _save_bar on the next bar boundary anyway.
        if not csv_path.exists():
            console.print(f"[dim][FuturesMonitor] Skipping backfill write — CSV doesn't exist yet; _save_bar will create it on next bar[/dim]")
            return
        
        def _load_existing_indicator_csv(path: Path):
            if not path.exists():
                return pd.DataFrame(), None
            try:
                existing_df = pd.read_csv(path, parse_dates=['timestamp'])
            except Exception:
                existing_df = pd.read_csv(path)
                renamed = False
                for col in existing_df.columns:
                    if not str(col).strip() or str(col).startswith("Unnamed"):
                        if "timestamp" not in existing_df.columns:
                            existing_df = existing_df.rename(columns={col: "timestamp"})
                            renamed = True
                            break
                if "timestamp" in existing_df.columns:
                    existing_df["timestamp"] = pd.to_datetime(existing_df["timestamp"], errors="coerce")
                    existing_df = existing_df.dropna(subset=["timestamp"])
                if renamed:
                    console.print(" [yellow][FuturesMonitor] Repaired corrupt startup CSV timestamp header[/yellow] ")
            if "timestamp" not in existing_df.columns:
                return pd.DataFrame(), None
            existing_df.set_index('timestamp', inplace=True)
            existing_df.index = pd.to_datetime(existing_df.index, errors="coerce")
            existing_df = existing_df[~existing_df.index.isna()]
            existing_df = existing_df[~existing_df.index.duplicated(keep='first')].sort_index()
            last_existing_ts = existing_df.index.max() if not existing_df.empty else None
            return existing_df, last_existing_ts

        # Read existing CSV
        if csv_path.exists():
            try:
                existing, last_ts = _load_existing_indicator_csv(csv_path)
                console.print(f"[dim][FuturesMonitor] Existing CSV: {len(existing)} bars, latest={last_ts}[/dim]")
            except Exception:
                existing = pd.DataFrame()
                last_ts = None
        else:
            existing = pd.DataFrame()
            last_ts = None
        
        api_df = api_df.copy()
        if not api_df.empty:
            api_df.index = pd.to_datetime(api_df.index, errors="coerce")
            api_df = api_df[~api_df.index.isna()]
            api_df = api_df[~api_df.index.duplicated(keep='last')].sort_index()
        
        # Find bars from API that are newer than CSV, or rebuild from API if CSV timestamp is corrupt/missing
        if not api_df.empty:
            if last_ts is None:
                new_bars = api_df
            else:
                new_bars = api_df[api_df.index > last_ts]
            if last_ts is None or not new_bars.empty:
                # [BUG FIX 2026-04-20] Do NOT write raw indicator-less bars to the session file
                # if _save_bar has already written bars with computed indicators.  The raw OHLCV
                # bars (no indicators) from the API would contaminate the file: later reads by the
                # dashboard would see NaN for indicator columns on those rows.  When _save_bar
                # processes the same bar it only APPENDS (not updates), so the NaN rows persist.
                #
                # Heuristic: if the existing file already has indicator data (e.g. has a 'momentum'
                # column with at least one non-NaN value), skip the raw backfill entirely.
                # _save_bar will write fully-computed rows going forward.
                #
                # [BUG FIX 2026-05-13] Check MULTIPLE indicator columns, not just momentum.
                # The raw API backfill produces NaN for ALL indicator columns. A single column
                # check (momentum.notna().any()) can return False if a previous backfill already
                # overwrote the enriched rows with raw data. Checking multiple columns ensures
                # we only skip when genuine enrichment has been committed to the CSV.
                _indicator_cols_in_csv = ["momentum", "atr", "vwap"]
                _present = [c for c in _indicator_cols_in_csv if c in existing.columns]
                has_indicator_data = (
                    not existing.empty
                    and len(_present) >= 2
                    and all(existing[c].notna().any() for c in _present)
                )
                # heuristic: also NEVER overwrite if _save_bar has been called at least once
                # in this session (tracked via _backfill_has_seen_enriched_row).
                if has_indicator_data or getattr(self, '_backfill_has_seen_enriched_row', False):
                    if not has_indicator_data and getattr(self, '_backfill_has_seen_enriched_row', False):
                        console.print(f"[dim][FuturesMonitor] Skipping raw backfill — _save_bar has written enriched rows this session[/dim]")
                    return

                if last_ts is None:
                    console.print(f"[bold cyan]🔧 Rebuilding startup CSV from API ({api_df.index[0]} → {api_df.index[-1]})[/bold cyan]")
                else:
                    console.print(f"[bold cyan]🔧 Backfilling {len(new_bars)} missing bars from API ({new_bars.index[0]} → {new_bars.index[-1]})[/bold cyan]")
                
                combined = existing.copy() if not existing.empty else pd.DataFrame()
                if combined.empty:
                    combined = new_bars.copy()
                else:
                    combined = pd.concat([combined, new_bars], sort=False)
                    combined = combined[~combined.index.duplicated(keep='last')].sort_index()
                
                # Add missing columns if needed
                for col in ['score', 'regime', 'session', 'bull_align', 'bear_align', 'in_pb_zone']:
                    if col not in combined.columns:
                        combined[col] = 0 if col in ['score'] else ('NORMAL' if col == 'regime' else (2 if col == 'session' else False))
                
                combined.index.name = "timestamp"
                combined.to_csv(csv_path, index_label="timestamp")
                console.print(f"[green][FuturesMonitor] ✅ Backfill complete: {len(combined)} total bars in CSV[/green]")

    def _tmf_feed_age_secs(self):
        """Prefer real MXF tick age over feed_health (which may be updated by non-tick sources).
        # 2026-05-22 Hermes Agent: use _last_real_tmf_tick_at as ground truth — never polluted by TMF_VIRTUAL"""
        try:
            # [FeedHealth] Use _last_real_tmf_tick_at as ground truth — only updated by
            # real MXF/TMF ticks in on_tick(), never by TMF_VIRTUAL synthetic ticks.
            if self._last_real_tmf_tick_at > 0:
                return max(0.0, time.time() - self._last_real_tmf_tick_at)
        except Exception:
            pass
        try:
            if hasattr(self, "feed_health") and self.feed_health is not None:
                # 2026-05-27 Gemini CLI: Use dynamic ticker for health check
                age = self.feed_health.age(self.ticker)
                if age is not None and math.isfinite(float(age)):
                    return max(0.0, float(age))
        except Exception:
            pass
        # Fall back to self.last_tick_at which is updated in on_tick even if bucket classification fails
        return max(0.0, time.time() - self.last_tick_at)

    def _set_runtime_status(self, status):
        if getattr(self, "_runtime_status", None) == status:
            return
        from core.shioaji_session import set_system_status
        set_system_status(status)
        self._runtime_status = status

    def _refresh_runtime_status(self):
        from core.shioaji_session import SystemReadiness

        warn = getattr(self, "STALE_WARN_SECS", self.MONITOR.get("stale_tick_warn_secs", 120))
        tmf_age = self._tmf_feed_age_secs()
        if tmf_age > warn:
            self._set_runtime_status(SystemReadiness.DEGRADED)
        elif self.is_trading_ready:
            self._set_runtime_status(SystemReadiness.TRADING)
        else:
            self._set_runtime_status(SystemReadiness.TRADING)

    def _check_futures_contract_staleness(self):
        """[Wave 1 Fix] Check if MXF ticks are stale and attempt recovery.

        Behavior:
        - If no new tick for < warn_secs: no-op.
        - If >= warn_secs but < critical_secs: attempt light recovery (rollover/resubscribe) and try fetching kline.
        - If >= critical_secs: mark monitor not running and raise to trigger supervisor restart.

        All watchdog actions log in unified structured format for grep-ability:
            [IngestionWatchdog] reason=<reason> symbol=<sym> tick_age_secs=<N>
            last_bar_ts=<ts> canonical_age_secs=<N> action=<action> result=<result>
        """
        if self.dry_run or not self.api:
            return

        secs_since_tick = self._tmf_feed_age_secs()
        warn = getattr(self, 'STALE_WARN_SECS', self.MONITOR.get('stale_tick_warn_secs', 120))
        critical = getattr(self, 'STALE_CRITICAL_SECS', self.MONITOR.get('stale_tick_critical_secs', 600))
        if secs_since_tick < warn:
            return

        from core.shioaji_session import SystemReadiness
        self._set_runtime_status(SystemReadiness.DEGRADED)

        # Gather structured context for watchdog log
        symbol = getattr(self.contract, 'code', self.ticker) if hasattr(self, 'contract') else self.ticker
        now_dt = datetime.now()
        last_bar_ts = "N/A"
        canonical_age_secs = -1
        try:
            df_5m = self._get_tick_bars_df()
            if df_5m is not None and not df_5m.empty:
                last_idx = df_5m.index[-1]
                if isinstance(last_idx, pd.Timestamp):
                    last_bar_ts = last_idx.strftime('%H:%M:%S')
                    canonical_age_secs = int((now_dt - last_idx.to_pydatetime()).total_seconds())
        except Exception:
            pass

        # ── Feed stale (warn threshold exceeded) ──
        _real_tick_age = max(0.0, time.time() - self._last_real_tmf_tick_at)
        console.print(
            f" [yellow][IngestionWatchdog] "
            f"reason=feed_stale symbol={symbol} "
            f"tick_age_secs={secs_since_tick:.0f} "
            f"real_tick_age_secs={_real_tick_age:.0f} "
            f"last_bar_ts={last_bar_ts} "
            f"canonical_age_secs={canonical_age_secs} "
            f"action=check_contract "
            f"result=degraded[/yellow] "
        )

        if not is_taifex_futures_market_open():
            console.print(
                f"[dim][IngestionWatchdog] "
                f"reason=market_closed symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=none "
                f"result=market_closed_keep_alive[/dim]"
            )
            return

        # ── Session transition buffer: 15:00-15:15 is a scheduled break ──
        # No ticks are emitted during this window. Don't treat it as a failure.
        hhmm_now = int(now_dt.strftime("%H%M"))
        in_transition_break = (1500 <= hhmm_now <= 1515)

        # If we exceed critical threshold, stop the monitor so external supervisor restarts the process
        if secs_since_tick >= critical and not in_transition_break:
            console.print(
                f"[red][IngestionWatchdog] "
                f"reason=feed_stale_critical symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=shutdown "
                f"result=trigger_supervisor_restart[/red]"
            )
            try:
                if self.contract:
                    self.api.quote.unsubscribe(self.contract, quote_type='tick')
            except Exception:
                pass
            # Mark monitor as not running and raise to break out of run loop
            self._running = False
            raise RuntimeError(f"{self.ticker} tick stale for {secs_since_tick} seconds (>{critical}), exiting monitor.")

        # Between warn and critical: attempt light recovery
        console.print(
            f"[dim][IngestionWatchdog] "
            f"reason=feed_stale symbol={symbol} "
            f"tick_age_secs={secs_since_tick:.0f} "
            f"last_bar_ts={last_bar_ts} "
            f"canonical_age_secs={canonical_age_secs} "
            f"action=light_recovery "
            f"result=attempting[/dim]"
        )

        # Check for expiry/rollover
        today_str = datetime.now().strftime("%Y/%m/%d")
        if self.contract and self.contract.delivery_date < today_str:
            console.print(
                f" [yellow][IngestionWatchdog] "
                f"reason=contract_expired symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=rollover "
                f"result=triggered[/yellow] "
            )
            self._check_contract_rollover()
            self.last_tick_at = time.time()
            return

        # If contract valid but no ticks, could be session transition or connection drop
        # Try contract rollover/resubscribe first
        try:
            self._check_contract_rollover()
        except Exception as e:
            console.print(
                f" [yellow][IngestionWatchdog] "
                f"reason=rollover_failed symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=rollover "
                f"result=exception:{e}[/yellow] "
            )

        # ═══ STARTUP / RECOVERY-ONLY: Light kline fetch via IngestionService ═══
        # This recovery path is only triggered when tick data has gone stale.
        # Delegates to IngestionService which handles rate limiting, CSV persistence,
        # and TXFR1 pre-fetch. The resulting data goes through the canonical bar pipeline.
        try:
            df_backfill = self._ingestion.fetch_recovery_kline()
            if df_backfill is not None and not df_backfill.empty:
                console.print(
                    f"[green][IngestionWatchdog] "
                    f"reason=feed_stale symbol={symbol} "
                    f"tick_age_secs={secs_since_tick:.0f} "
                    f"last_bar_ts={last_bar_ts} "
                    f"canonical_age_secs={canonical_age_secs} "
                    f"action=fetch_recovery_kline "
                    f"result=success:rows={len(df_backfill)}[/green]"
                )
                self.last_tick_at = time.time()
                return
            else:
                console.print(
                    f" [yellow][IngestionWatchdog] "
                    f"reason=feed_stale symbol={symbol} "
                    f"tick_age_secs={secs_since_tick:.0f} "
                    f"last_bar_ts={last_bar_ts} "
                    f"canonical_age_secs={canonical_age_secs} "
                    f"action=fetch_recovery_kline "
                    f"result=empty_response[/yellow] "
                )
        except Exception as e:
            console.print(
                f" [yellow][IngestionWatchdog] "
                f"reason=feed_stale symbol={symbol} "
                f"tick_age_secs={secs_since_tick:.0f} "
                f"last_bar_ts={last_bar_ts} "
                f"canonical_age_secs={canonical_age_secs} "
                f"action=fetch_recovery_kline "
                f"result=exception:{e}[/yellow] "
            )

        # Reset timer to avoid spamming retries; next loop will re-evaluate
        self.last_tick_at = time.time()

    def _is_contract_expired(self, contract_delivery_date):
        """[GSD Settlement Fix] Check if contract is expired considering settlement time (13:30).
        
        Args:
            contract_delivery_date: Delivery date in "YYYY/MM/DD" format
            
        Returns:
            bool: True if contract is expired (past settlement time on delivery date)
        """
        try:
            # Parse contract delivery date
            contract_date = datetime.strptime(contract_delivery_date, "%Y/%m/%d").date()
            now = datetime.now()
            today = now.date()
            
            # If contract date is in the future, it's not expired
            if contract_date > today:
                return False
            
            # If contract date is before today, it's expired
            if contract_date < today:
                return True
            
            # Same day: check if past settlement time (13:30)
            settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
            return now >= settlement_time
            
        except Exception as e:
            console.print(f" [yellow]⚠️ Error checking contract expiration: {e}[/yellow] ")
            return False
    
    def _is_settlement_day(self, contract_delivery_date):
        """[GSD Settlement Fix] Check if today is settlement day for the given contract.
        
        Args:
            contract_delivery_date: Delivery date in "YYYY/MM/DD" format
            
        Returns:
            bool: True if today is the delivery date (settlement day)
        """
        try:
            contract_date = datetime.strptime(contract_delivery_date, "%Y/%m/%d").date()
            today = datetime.now().date()
            return contract_date == today
        except Exception as e:
            console.print(f" [yellow]⚠️ Error checking settlement day: {e}[/yellow] ")
            return False
    
    def _get_settlement_time_remaining(self):
        """[GSD Settlement Fix] Calculate time remaining until settlement (13:30).
        
        Returns:
            tuple: (hours_remaining, minutes_remaining) or None if not settlement day
        """
        try:
            now = datetime.now()
            today = now.date()
            
            # Check if current contract expires today
            if not self.contract or not self._is_settlement_day(self.contract.delivery_date):
                return None
            
            # Calculate time until 13:30
            settlement_time = now.replace(hour=13, minute=30, second=0, microsecond=0)
            if now >= settlement_time:
                return (0, 0)  # Already past settlement time
            
            time_diff = settlement_time - now
            total_minutes = int(time_diff.total_seconds() / 60)
            hours = total_minutes // 60
            minutes = total_minutes % 60
            
            return (hours, minutes)
            
        except Exception as e:
            console.print(f" [yellow]⚠️ Error calculating settlement time: {e}[/yellow] ")
            return None
    
    def _resubscribe_after_session_transition(self):
        """Re-subscribe near/far tick + bidask after a session transition.

        2026-08-05 INCIDENT: night->day handoff (05:00) drops quote
        subscriptions with no resubscribe; feed goes silent and the broker
        session later dies (list_positions 500) -> PM2 restart storm.
        This re-establishes the 4 subscriptions idempotently.
        """
        if not self.api or self.dry_run:
            return
        from core.broker.shioaji_compat import safe_subscribe
        _subs = [
            (self.contract, "tick"),
            (self.contract, "bidask"),
        ]
        if self.far_contract is not None:
            _subs += [
                (self.far_contract, "tick"),
                (self.far_contract, "bidask"),
            ]
        for _c, _qt in _subs:
            try:
                safe_subscribe(self.api, _c, quote_type=_qt)
                console.print(
                    f"[dim]📡 [TRANSITION_RESUB] {_c.code} {_qt}[/dim]")
            except Exception as _e:
                console.print(
                    f"[yellow]⚠️ [TRANSITION_RESUB] {_c.code} {_qt} failed: {_e}[/yellow]")

    def _check_contract_rollover(self):
        """[GSD Fix] Check if MXF contract has rolled over and re-subscribe if needed."""
        if not self.api or self.dry_run or not self.contract:
            return
        
        try:
            current_code = self.contract.code
            
            # [GSD Settlement Fix] Check if today is settlement day
            if self._is_settlement_day(self.contract.delivery_date):
                time_remaining = self._get_settlement_time_remaining()
                if time_remaining:
                    hours, minutes = time_remaining
                    if hours == 0 and minutes == 0:
                        console.print(f"[bold red]⚠️ SETTLEMENT DAY: Contract {current_code} has expired at 13:30[/bold red]")
                    elif hours > 0 or minutes > 0:
                        console.print(f"[bold yellow]⚠️ SETTLEMENT DAY: Contract {current_code} expires at 13:30 ({hours}h {minutes}m remaining)[/bold yellow]")
            
            # 2026-05-27 Gemini CLI: Get all available contracts using dynamic ticker attribute access
            target_contracts = getattr(self.api.Contracts.Futures, self.ticker, None)
            if target_contracts is None:
                console.print(f" [yellow]⚠️ Ticker {self.ticker} not found in Contracts.Futures[/yellow] ")
                return
            
            tmf_list = list(target_contracts)
            if not tmf_list:
                console.print(f" [yellow]⚠️ No {self.ticker} contracts available[/yellow] ")
                return
            
            # [GSD Settlement Fix] Filter out expired contracts considering settlement time
            now = datetime.now()
            valid_contracts = []
            for contract in tmf_list:
                if not self._is_contract_expired(contract.delivery_date):
                    valid_contracts.append(contract)
            
            # Sort by delivery_date
            tmf_sorted = sorted(valid_contracts, key=lambda c: c.delivery_date)
            
            if not tmf_sorted:
                console.print("[bold red]⚠️ No valid contracts available after settlement time[/bold red]")
                return
                
            first_contract = tmf_sorted[0]
            
            # Check if we're still on the first (front month) contract
            if first_contract.code != current_code:
                console.print(f"[bold yellow]🔄 Contract rollover detected: {current_code} → {first_contract.code}[/bold yellow]")
                
                # Unsubscribe from old contract
                try:
                    self.api.quote.unsubscribe(self.contract, quote_type='tick')
                except Exception as e:
                    console.print(f"[dim]Unsubscribe old {current_code}: {e}[/dim]")
                
                # Switch to new contract
                self.contract = first_contract
                self._last_contract_code = first_contract.code
                
                # Re-subscribe to new contract
                self.api.quote.subscribe(first_contract, quote_type='tick')
                console.print(f"[bold green]✅ Re-subscribed to {first_contract.code}[/bold green]")
                
                # Reset tick timestamp to force immediate data freshness check
                self.last_tick_at = time.time()
            else:
                # Contract is correct, issue may be API connection
                # Try re-subscribing to force refresh
                console.print(f"[dim]⚠️ Contract {current_code} is correct but no ticks, re-subscribing...[/dim]")
                try:
                    self.api.quote.unsubscribe(self.contract, quote_type='tick')
                    time.sleep(0.5)
                    self.api.quote.subscribe(self.contract, quote_type='tick')
                    console.print(f"[dim]✅ Re-subscription complete[/dim]")
                except Exception as e:
                    console.print(f" [yellow]⚠️ Re-subscription failed: {e}[/yellow] ")
        except Exception as e:
            console.print(f" [yellow]⚠️ Contract rollover check error: {e}[/yellow] ")

    # ── [GSD Data Safety] Raw tick CSV writer ──
    def _write_raw_tick(self, tick) -> None:
        """Write a single tick to the raw CSV store BEFORE any in-memory cache.

        Each subscribed leg has an independent writer.  Session date follows the
        shared TAIFEX 15:00 rollover helper so the two legs are replayable as one
        pair, including the after-hours portion of a trading day.
        """
        try:
            code = str(getattr(tick, "code", "") or self.ticker)
            writers = getattr(self, "_raw_tick_writers", None)
            if writers is None:
                writers = self._raw_tick_writers = {}
            writer = writers.get(code)
            if writer is None:
                writer = RawTickWriter(code, get_session_date_str(datetime.now()))
                writers[code] = writer
                # Retain the legacy alias for startup bar rebuild callers.
                if self.contract and code == self.contract.code:
                    self._tick_writer = writer
            writer.write(tick)
        except Exception:
            # Never let a CSV write failure crash the tick callback
            pass

    def _rebuild_bars_from_raw_ticks(self) -> None:
        """[GSD Data Safety] Rebuild tick-based 5m bars from raw tick CSV on startup.

        If the process crashed mid-session, the in-memory tick deque is lost.
        This method reads today's raw tick CSV (if it exists) and rebuilds the
        5m bars into self._tick_bars_deque so indicators can warm up immediately
        without waiting for fresh ticks.
        """
        try:
            if self.dry_run:
                return

            # Determine today's trading day and code
            trading_day = get_trading_day_str(datetime.now())
            code = getattr(self.contract, "code", self.ticker) if self.contract else self.ticker

            from squeeze_futures.data.tick_writer import read_raw_ticks
            df_ticks = read_raw_ticks(code, trading_day)
            if df_ticks.empty:
                console.print(f"[dim][FuturesMonitor] No raw tick CSV for {code} / {trading_day}, skipping rebuild[/dim]")
                return

            console.print(f"[cyan][FuturesMonitor] Rebuilding 5m bars from {len(df_ticks)} raw ticks...[/cyan]")

            # Ensure timestamp is sorted
            df_ticks = df_ticks.sort_values("timestamp")

            # Bucket into 5-minute bars
            df_ticks["ts_bucket"] = df_ticks["timestamp"].dt.floor("5min")

            rebuilt_bars = []
            for ts_bucket, group in df_ticks.groupby("ts_bucket"):
                bar = {
                    "open": float(group["price"].iloc[0]),
                    "high": float(group["price"].max()),
                    "low": float(group["price"].min()),
                    "close": float(group["price"].iloc[-1]),
                    "volume": int(group["volume"].sum()),
                    "ts": ts_bucket,
                }
                rebuilt_bars.append(bar)

            if rebuilt_bars:
                # Clear and repopulate the deque
                self._tick_bars_deque.clear()
                for bar in rebuilt_bars:
                    self._tick_bars_deque.append(bar)
                self._tick_bars_cache = None  # Invalidate cache so it rebuilds
                console.print(f"[bold green]✅ Rebuilt {len(rebuilt_bars)} bars from raw tick CSV[/bold green]")

                # Set the last bar timestamp to the latest bar to prevent re-processing
                if rebuilt_bars:
                    last_bar = rebuilt_bars[-1]
                    if last_bar["ts"] is not None:
                        self._last_bar_ts = int(last_bar["ts"].timestamp() / 300) * 300
                        # 2026-07-01 Gemini CLI: Pop the last bar from the deque and make it the current active bar.
                        # This prevents the last bar from being duplicated on subsequent ticks in the same 5m bucket.
                        self._tick_bars_deque.pop()
                        self._current_bar["ts"] = last_bar["ts"]
                        self._current_bar["open"] = last_bar["open"]
                        self._current_bar["high"] = last_bar["high"]
                        self._current_bar["low"] = last_bar["low"]
                        self._current_bar["close"] = last_bar["close"]
                        self._current_bar["volume"] = last_bar["volume"]

        except Exception as e:
            console.print(f" [yellow][FuturesMonitor] Tick CSV rebuild failed (non-fatal): {e}[/yellow] ")

    # ── [P0b] Quote Integrity Gate ────────────────────────────────────
    def _init_quote_guard(self):
        """Lazy init — contract resolution happens after __init__."""
        try:
            from core.quote_integrity import QuoteIntegrityGuard
            _csv = getattr(self, "csv_path", None)
            _log_dir = os.path.dirname(str(_csv)) if _csv else runtime_logs()
            self._quote_guard = QuoteIntegrityGuard(
                near_code=getattr(self.contract, "code", "TMFH6"),
                far_code=getattr(self.far_contract, "code", "TMFI6"),
                ticker=self.ticker,
                anomalous_quotes_path=os.path.join(_log_dir, "anomalous_quotes.jsonl"),
            )
            self._quote_integrity_stats = self._quote_guard.stats
            self.anomalous_quotes_path = self._quote_guard.anomalous_quotes_path
        except Exception as _exc:  # non-fatal — degrade to existing gates
            print(f"[QUOTE_GUARD_INIT_FAILED] {_exc}", flush=True)
            self._quote_guard = None

    def _build_quote_envelope(self, tick):
        from core.quote_integrity import QuoteEnvelope
        _close = float(getattr(tick, "close", 0) or 0)
        _p = _close
        return QuoteEnvelope(
            raw_contract=getattr(tick, "code", "") or "",
            normalized_contract=(getattr(tick, "code", "") or "").upper(),
            expected_leg=None,
            callback_source="shioaji_on_tick",
            exchange_timestamp=str(getattr(tick, "datetime", "") or ""),
            receive_timestamp=time.time(),
            receive_sequence=0,
            subscription_generation=self._quote_guard.generation,
            source_kind="live",
            price=_p,
            close=_close,
            bid=float(getattr(tick, "buy_price", _close) or _close),
            ask=float(getattr(tick, "sell_price", _close) or _close),
        )

    def _f_shadow(self):
        """Lazy FShadowCollector (fail-open; read-only shadow)."""
        try:
            if getattr(self, "_f_shadow_c", None) is None:
                from core.exit_shadow_f import FShadowCollector
                _d = "data/telemetry/shadow_f"
                os.makedirs(_d, exist_ok=True)
                _c = FShadowCollector(f"{_d}/shadow_f_{datetime.now():%Y%m%d}.jsonl")
                _c.bind_contracts(
                    str(getattr(getattr(self, "contract", None), "code", "") or "").split("/")[-1].strip(),
                    str(getattr(getattr(self, "far_contract", None), "code", "") or "").split("/")[-1].strip())
                _c._load_existing()
                self._f_shadow_c = _c
                self._f_shadow_funnel = {
                    "hook_enter": 0, "target_contract": 0, "bbos_extracted": 0,
                    "bbos_valid": 0, "envelope_created": 0, "pair_cache_updated": 0,
                    "pair_ready": 0, "pair_rejected": 0,
                    # 2026-08-05: mc_eval_called/mc_eval_returned removed —
                    # Model C has no evaluate() (dead counters looked like
                    # runtime failures at 0). f_eval_* are REAL (wired to the
                    # exit_shadow_f evaluate() call site below).
                    "f_eval_called": 0, "f_eval_returned": 0,
                    "telemetry_enqueued": 0, "telemetry_write_ok": 0,
                    "telemetry_write_err": 0, "telemetry_dropped": 0,
                    "reasons": {},
                    "probe_start": datetime.now().isoformat(timespec="seconds"),
                    "git_sha": "85b8e7de",
                }
                import atexit
                atexit.register(self._f_shadow_flush)
                self._install_signal_flush()
            return self._f_shadow_c
        except Exception as _e:
            try:
                _f = getattr(self, "_f_shadow_funnel", None)
                if _f is None:
                    _f = {"hook_enter": 0, "reasons": {}, "probe_start": "n/a", "git_sha": "41cde57a"}
                    self._f_shadow_funnel = _f
                _f["reasons"][f"shadow_init_error:{type(_e).__name__}"] =                     _f["reasons"].get(f"shadow_init_error:{type(_e).__name__}", 0) + 1
            except Exception:
                pass
            return None

    def _install_signal_flush(self):
        """SIGTERM/SIGINT -> bounded idempotent flush, then resume normal
        shutdown. Idempotent: repeated signals flush at most once more."""
        import signal as _sig
        for _s in (_sig.SIGTERM, _sig.SIGINT):
            try:
                _old = _sig.getsignal(_s)
                if _old in (_sig.SIG_DFL, None):
                    _old = None
                _sig.signal(_s, lambda signum, frame, _old=_old: self._f_shadow_signal_flush(signum, _old))
            except Exception:
                pass

    def _f_shadow_signal_flush(self, signum, old_handler):
        """Bounded flush (<=1s), idempotent; then resume prior disposition."""
        import time as _t
        _t0 = _t.time()
        try:
            _c = getattr(self, "_f_shadow_c", None)
            if _c is not None:
                _c.flush()
            self._f_shadow_flush_log = {"attempted": True, "success": True,
                                        "unflushed": 0}
        except Exception:
            self._f_shadow_flush_log = {"attempted": True, "success": False,
                                        "unflushed": -1}
        if _t.time() - _t0 > 1.0:
            # exceeded bound — still proceed to shutdown
            self._f_shadow_flush_log["bounded"] = False
        if old_handler is not None:
            try:
                import signal as _sig
                _sig.signal(signum, _sig.SIG_DFL)
                import os as _os
                _os.kill(_os.getpid(), signum)
            except Exception:
                pass

    def _f_shadow_flush(self):
        """Shutdown flush (SIGTERM/SIGINT/exit). Time-bounded; failure must
        not block shutdown. Records attempted/success/unflushed."""
        try:
            _c = getattr(self, "_f_shadow_c", None)
            if _c is None:
                return
            _buf_before = _c.buffer_stats().get("buffer_depth", 0)
            _c.flush()
            _buf_after = _c.buffer_stats().get("buffer_depth", 0)
            self._f_shadow_flush_log = {"attempted": True, "success": _buf_after == 0,
                                        "unflushed": _buf_after}
            if _buf_after > 0:
                try:
                    import logging
                    logging.getLogger("FuturesMonitor").warning(
                        "[SHADOW_F] unclean shutdown: %d unflushed events", _buf_after)
                except Exception:
                    pass
        except Exception:
            self._f_shadow_flush_log = {"attempted": True, "success": False,
                                        "unflushed": _buf_before if "_buf_before" in dir() else -1}

    def _f_funnel(self, stage, reason=None):
        try:
            _f = getattr(self, "_f_shadow_funnel", None)
            if _f is None:
                _f = {
                    "hook_enter": 0, "target_contract": 0, "bbos_extracted": 0,
                    "bbos_valid": 0, "envelope_created": 0, "pair_cache_updated": 0,
                    "pair_ready": 0, "pair_rejected": 0,
                    # 2026-08-05: mc_eval_called/mc_eval_returned removed —
                    # Model C has no evaluate() (dead counters looked like
                    # runtime failures at 0). f_eval_* are REAL (wired to the
                    # exit_shadow_f evaluate() call site below).
                    "f_eval_called": 0, "f_eval_returned": 0,
                    "telemetry_enqueued": 0, "telemetry_write_ok": 0,
                    "telemetry_write_err": 0, "telemetry_dropped": 0,
                    "reasons": {},
                    "probe_start": datetime.now().isoformat(timespec="seconds"),
                    "git_sha": "41cde57a",
                }
                self._f_shadow_funnel = _f
            _f[stage] = _f.get(stage, 0) + 1
            if reason:
                _k = f"{stage}:{reason}"
                _f["reasons"][_k] = _f["reasons"].get(_k, 0) + 1
            _now = time.time()
            if _now - float(getattr(self, "_f_shadow_funnel_dump_ts", 0.0)) > 60.0:
                self._f_shadow_funnel_dump_ts = _now
                try:
                    import logging
                    logging.getLogger("FuturesMonitor").info(
                        "[SHADOW_F_PROBE] %s",
                        json.dumps({"funnel": _f, "as_of": datetime.now().isoformat(timespec="seconds"),
                                    "session": "NIGHT" if datetime.now().hour >= 15 or datetime.now().hour < 5 else "DAY"},
                                   ensure_ascii=False))
                except Exception:
                    pass
        except Exception:
            pass

    def on_bidask(self, exchange, bidask):
        """Model C BBO feed (2026-08-04 review item 1).

        BidAskFOPv1 objects carry real bid/ask — the executable BBO contract
        Model C requires. Flag-gated (data/model_c_canary.flag) exactly like
        the tick path; _extract_bbo enforces BBO_VALID-only consumption.
        Never falls back to last/close or buy_price/sell_price.
        """
        # Cache real BBO for both legs regardless of the Model C canary.  Policy J
        # and post-trade replay need this evidence even while Model C is disabled.
        try:
            _code_cache = str(getattr(bidask, "code", "") or "").split("/")[-1].strip()
            _bbo_cache = _extract_bbo(bidask) if bidask is not None else None
            _nset_cache = getattr(self, "_canonical_near_codes", set())
            _fset_cache = getattr(self, "_canonical_far_codes", set())
            if _bbo_cache and _bbo_cache[2] == "BBO_VALID":
                _key = (str(getattr(self, "ticker", "TMF")) if _code_cache in _nset_cache
                        else str(getattr(self, "ticker", "TMF")) + "_FAR" if _code_cache in _fset_cache
                        else None)
                if _key:
                    _slot = self.market_data.setdefault(_key, {})
                    _slot.update({"bid": _bbo_cache[0], "ask": _bbo_cache[1],
                                  "bidask_at": time.time(), "bidask_exchange_ts": getattr(bidask, "datetime", None)})
                    # [S2 repair] dedicated EXIT_ONLY BBO evidence cache —
                    # immutable-per-update; written ONLY here (validated
                    # BidAskFOPv1/on_bidask).  Ticks never create or
                    # overwrite it; _exit_only_bbo_slots reads ONLY this.
                    _leg = ("near"
                            if _key == str(getattr(self, "ticker", "TMF"))
                            else "far")
                    _xdt = getattr(bidask, "datetime", None)
                    _xms = None
                    from datetime import datetime as _dtt
                    if isinstance(_xdt, _dtt):
                        # [S2 audit] datetime object only, converted ONCE
                        # to integer epoch-ms; never float() numeric/
                        # string timestamps, never scale seconds.
                        try:
                            _xms = int(_xdt.timestamp() * 1000)
                        except Exception:
                            _xms = None
                    if _xms is None or _xms <= 0:
                        # no valid evidence: preserve the previous entry
                        # (a bad quote never replaces good evidence)
                        _xms = None
                    if _xms is not None:
                        _cache = dict(
                            getattr(self, "_exit_only_bbo_cache", None)
                            or {})
                        _cache[_leg] = {
                            "code": _code_cache,
                            "bid": _bbo_cache[0],
                            "ask": _bbo_cache[1],
                            "exchange_ts_ms": _xms,
                            "received_at_ms": int(time.time() * 1000),
                            "source": "shioaji_bidask",
                            "seq": getattr(bidask, "seq", None),
                        }
                        self._exit_only_bbo_cache = _cache
        except Exception:
            pass
        try:
            _mc_flag = os.path.join(_repo_root(), "data", "model_c_canary.flag")
            if not os.path.exists(_mc_flag):
                return
            if bidask is None or not hasattr(bidask, "code"):
                return
            _code = str(getattr(bidask, "code", "") or "").split("/")[-1].strip()
            _nset = getattr(self, "_canonical_near_codes", None) or {getattr(getattr(self, "contract", None), "code", "")}
            _fset = getattr(self, "_canonical_far_codes", None) or {getattr(getattr(self, "far_contract", None), "code", "")}
            if _code in _nset:
                _leg = "NEAR"
            elif _code in _fset:
                _leg = "FAR"
            else:
                return
            _bbo = _extract_bbo(bidask)
            if not _bbo or _bbo[2] != "BBO_VALID":
                return  # TICK_ONLY / DATA_QUALITY_BLOCKED never feed Model C
            if not hasattr(self, "_model_c"):
                from core.model_c_collector import ModelCCollector
                _td = os.path.join(_repo_root(), "data", "telemetry", "model_c")
                os.makedirs(_td, exist_ok=True)
                _day = datetime.now().strftime("%Y%m%d")
                self._model_c = ModelCCollector(
                    os.path.join(_td, f"model_c_{_day}.jsonl"),
                    bbo_raw_path=os.path.join(_td, f"bbo_raw_{_day}.jsonl"))
            self._model_c.on_quote(
                _leg, _bbo[0], _bbo[1],
                bid_size=getattr(bidask, "bid_volume", None),
                ask_size=getattr(bidask, "ask_volume", None),
                receive_ts=datetime.now().isoformat(),
                # 2026-08-04: BidAskFOPv1.datetime = naive TAIFEX local
                # wall-clock (probe-verified); on Mini (CST+0800) shares the
                # UTC epoch contract with receive time. Collector classifies
                # timestamp_quality; never silently cross-domains.
                exchange_ts=getattr(bidask, "datetime", None),
                seq=getattr(bidask, "seq", None),
                contract_code=_code,
                source="shioaji_bidask")
        except Exception:
            import sys as _sys
            if not hasattr(self, "_mc_bidask_err_log"):
                self._mc_bidask_err_log = True
                logger.warning("[MODEL_C] on_bidask error: %r",
                               _sys.exc_info()[1])
        # also refresh market_data bid/ask caches used by downstream consumers
        try:
            _bid = float(getattr(bidask, "bid_price", [None])[0] if hasattr(getattr(bidask, "bid_price", None), "__getitem__") else getattr(bidask, "bid_price", None))
            _ask = float(getattr(bidask, "ask_price", [None])[0] if hasattr(getattr(bidask, "ask_price", None), "__getitem__") else getattr(bidask, "ask_price", None))
            if _bid and _ask and _bid > 0 and _ask > 0:
                _code2 = str(getattr(bidask, "code", "") or "")
                _k = _code2
                if _code2 and _code2 in getattr(self, "_canonical_near_codes", set()):
                    _k = str(getattr(self, "ticker", "TMF"))
                elif _code2 and _code2 in getattr(self, "_canonical_far_codes", set()):
                    _k = str(getattr(self, "ticker", "TMF")) + "_FAR"
                if _k:
                    self.market_data.setdefault(_k, {})["bid"] = _bid
                    self.market_data.setdefault(_k, {})["ask"] = _ask
                    self.market_data.setdefault(_k, {})["bidask_at"] = time.time()
        except Exception:
            pass

    def on_tick(self, exchange, tick):
        self.last_tick_at = time.time()
        self._f_shadow_t0 = time.time()
        try:
            self._f_funnel("hook_enter")
            _c = self._f_shadow()
            if _c is None:
                self._f_funnel("hook_enter", "collector_none")
                return
            self._f_funnel("hook_enter", "collector_ok")
            if _c is not None:
                _code = str(getattr(tick, "code", "") or "").split("/")[-1].strip()
                _bbo = _extract_bbo(tick)
                _bid = _bbo[0] if _bbo else None
                _ask = _bbo[1] if _bbo else None
                _bq = _bbo[2] if _bbo else "DATA_QUALITY_BLOCKED"
                _near_c = str(getattr(getattr(self, "contract", None), "code", "") or "").split("/")[-1].strip()
                _far_c = str(getattr(getattr(self, "far_contract", None), "code", "") or "").split("/")[-1].strip()
                # 2026-08-04 review item 2: canonical alias sets (TMFH6/MXFH6
                # are the same near contract). Code outside both sets =
                # CONTRACT_MISMATCH -> block, never classify as generic other.
                _nset = getattr(self, "_canonical_near_codes", None) or {_near_c}
                _fset = getattr(self, "_canonical_far_codes", None) or {_far_c}
                if _code in _nset or _code in _fset:
                    self._f_funnel("target_contract")
                else:
                    self._f_funnel("target_contract", f"contract_mismatch:{_code}")
                if _bq == "BBO_VALID":
                    self._f_funnel("bbos_extracted")
                    self._f_funnel("bbos_valid")
                elif _bq == "TICK_ONLY":
                    self._f_funnel("bbos_extracted", "tick_only")
                else:
                    self._f_funnel("bbos_extracted", "data_quality_blocked")
                if _bid is not None or _ask is not None:
                    self._f_funnel("envelope_created")
                    _c.on_quote(_code, _bid, _ask, contract_code=_code,
                                receive_ts=datetime.now().isoformat(timespec="milliseconds"))
                    self._f_funnel("pair_cache_updated")
                    self._f_funnel("pair_ready" if _c.pair_ready() else "pair_rejected")
                    _now_t = time.time()
                    _st = getattr(self, "_f_shadow_state", None) or {}
                    if _now_t - getattr(self, "_f_shadow_state_ts", 0) > 1.0:
                        _t_st = time.time()
                        try:
                            with open(MTS_POSITION_STATE_PATH) as _fst:
                                self._f_shadow_state = json.load(_fst)
                                self._f_shadow_state_mtime = os.path.getmtime(MTS_POSITION_STATE_PATH)
                        except Exception:
                            self._f_shadow_state = {}
                        self._f_shadow_state_ts = _now_t
                        _c._latency["state_snapshot"].append((time.time() - _t_st) * 1000.0)
                    _st = getattr(self, "_f_shadow_state", None) or {}
                    _was_pos = getattr(self, "_f_shadow_had_pos", False)
                    _has_pos = bool(_st.get("has_position"))
                    self._f_shadow_had_pos = _has_pos
                    if _has_pos and not _was_pos:
                        self._f_shadow_gen = _st.get("trade_id")
                    if _was_pos and not _has_pos and self._f_shadow_gen:
                        # canonical settlement tap — position finalized
                        _c.record_actual(
                            {"trade_id": self._f_shadow_gen,
                             "position_generation": self._f_shadow_gen},
                            float(_st.get("total_realized_pnl") or
                                  (_st.get("near_realized_pnl") or 0) + (_st.get("far_realized_pnl") or 0)),
                            float(_st.get("naked_leg_exposure_min") or 0.0),
                            exit_type=_st.get("last_exit_type") or "UNKNOWN",
                            settlement_id=f"{self._f_shadow_gen}:{_st.get('settlement_ts') or _now_t}")
                    _age_ms = (time.time() - self._f_shadow_state_ts) * 1000.0 if self._f_shadow_state_ts else 999999.0
                    if _has_pos and _c.pair_ready() and _age_ms < 5000.0:
                        self._f_funnel("f_eval_called")
                        _ev = _c.evaluate({
                            "trade_id": _st.get("trade_id"),
                            "position_generation": _st.get("trade_id"),
                            "near_side": _st.get("near_side"),
                            "far_side": _st.get("far_side"),
                            "near_entry": _st.get("near_entry"),
                            "far_entry": _st.get("far_entry"),
                            "near_contract": str(getattr(getattr(self, "contract", None), "code", "") or "").split("/")[-1].strip(),
                            "far_contract": str(getattr(getattr(self, "far_contract", None), "code", "") or "").split("/")[-1].strip(),
                            "release_threshold_pts": _st.get("release_stop_points", 88.0),
                            "atr": _st.get("atr"),
                            "mark_source": "PRODUCTION_RUNTIME",
                            "point_value": 10.0,
                            "snapshot_source": "STATE_FILE",
                            "state_snapshot_ts": self._f_shadow_state_ts,
                            "state_age_ms": round(_age_ms, 1),
                            "state_file_mtime": getattr(self, "_f_shadow_state_mtime", None),
                        })
                        self._f_funnel("f_eval_returned")
                        if _ev:
                            _c.record_production_decision(
                                {"trade_id": _st.get("trade_id"),
                                 "position_generation": _st.get("trade_id")},
                                triggered=_ev.get("event") == "EXECUTABLE_CANDIDATE",
                                breached_leg=_ev.get("breached_leg"),
                                adverse_move_pts=abs(_ev.get("near_executable_pnl", 0) / 10.0)
                                if _ev.get("breached_leg") == "NEAR" else abs(_ev.get("far_executable_pnl", 0) / 10.0),
                                threshold_pts=_st.get("release_stop_points", 88.0),
                                atr=_st.get("atr"),
                                mark_source="STATE_FILE",
                                evaluation_id=f"{_st.get('trade_id')}:{_now_t:.3f}")
                    elif _has_pos:
                        _c._write({"event": "REJECTED", "reason": "STALE_POSITION_SNAPSHOT",
                                   "trade_id": _st.get("trade_id"),
                                   "position_generation": _st.get("trade_id"),
                                   "state_age_ms": round(_age_ms, 1),
                                   "ts": datetime.now().isoformat(timespec="milliseconds"),
                                   "mode": "SHADOW_ONLY", "execution_influence": False,
                                   "adr": "ADR-026", "adr_status": "PROPOSED"})
        except Exception:
            pass
        try:
            _c = getattr(self, "_f_shadow_c", None)
            if _c is not None and getattr(self, "_f_shadow_t0", None) is not None:
                _c._latency["monitor_total"].append((time.time() - self._f_shadow_t0) * 1000.0)
        except Exception:
            pass
        # [Model C canary 2026-08-03] synchronized BBO executable marking —
        # shadow only. Flag-gated dynamically (data/model_c_canary.flag).
        try:
            _mc_flag = os.path.join(_repo_root(), "data", "model_c_canary.flag")
            if os.path.exists(_mc_flag):
                _code = str(getattr(tick, "code", "") or "").split("/")[-1].strip()
                _near_c = str(getattr(getattr(self, "contract", None), "code", "") or "").split("/")[-1].strip()
                _far_c = str(getattr(getattr(self, "far_contract", None), "code", "") or "").split("/")[-1].strip()
                _leg = None
                if _code and _code in _nset:
                    _leg = "NEAR"
                elif _code and _code in _fset:
                    _leg = "FAR"
                if not _leg and not hasattr(self, "_mc_leg_miss_log"):
                    self._mc_leg_miss_log = True
                    logger.warning("[MODEL_C] leg mismatch code=%r near=%r far=%r (canonical %s/%s)",
                                   _code, _near_c, _far_c,
                                   sorted(_nset), sorted(_fset))
                if _leg:
                    if not hasattr(self, "_model_c"):
                        from core.model_c_collector import ModelCCollector
                        _td = os.path.join(_repo_root(), "data", "telemetry", "model_c")
                        os.makedirs(_td, exist_ok=True)
                        _day = datetime.now().strftime("%Y%m%d")
                        self._model_c = ModelCCollector(
                            os.path.join(_td, f"model_c_{_day}.jsonl"),
                            bbo_raw_path=os.path.join(_td, f"bbo_raw_{_day}.jsonl"))
                    _bbo2 = _extract_bbo(tick)
                    _bid = _bbo2[0] if _bbo2 else None
                    _ask = _bbo2[1] if _bbo2 else None
                    _bq2 = _bbo2[2] if _bbo2 else "DATA_QUALITY_BLOCKED"
                    # 2026-08-04 review: Model C consumes BBO only when quality
                    # is BBO_VALID. TICK_ONLY / DATA_QUALITY_BLOCKED never feed
                    # on_quote (no last/close fallback, no buy/sell fallback).
                    if _bq2 == "BBO_VALID":
                        self._model_c.on_quote(
                            _leg, _bid, _ask,
                            bid_size=getattr(tick, "buy_volume", None),
                            ask_size=getattr(tick, "sell_volume", None),
                            receive_ts=datetime.now().isoformat(),
                            seq=getattr(tick, "seq", None),
                            contract_code=_code,
                            source="shioaji_tick")
                        _mcst = getattr(self, "_f_shadow_state", None) or {}
                        if _mcst.get("has_position"):
                            try:
                                _ms = getattr(self, "_mts_strategy", None)
                                if _ms is None:
                                    # locate strategy from state path owners
                                    for _s2 in getattr(self, "_strategies", {}).values() if hasattr(self, "_strategies") else []:
                                        _ms = _s2
                                        break
                                _nq = int(getattr(_ms, "_near_qty", getattr(_ms, "_lots", 0)) or 0) if _ms else 0
                                _fq = int(getattr(_ms, "_far_qty", getattr(_ms, "_lots", 0)) or 0) if _ms else 0
                                if _nq <= 0 or _fq <= 0:
                                    _nq = int(getattr(_ms, "_near_qty", getattr(_ms, "_lots", 1)) or 1) if _ms else 1
                                    _fq = int(getattr(_ms, "_far_qty", getattr(_ms, "_lots", 1)) or 1) if _ms else 1
                                    _qsrc = "strategy_lots_default_1" if _ms else "no_strategy_default_1"
                                else:
                                    _qsrc = "strategy_near_far_qty"
                                self._model_c.mark_position(
                                    _mcst.get("near_side"), _mcst.get("far_side"),
                                    _mcst.get("near_entry"), _mcst.get("far_entry"),
                                    _nq, _fq, qty_source=_qsrc)
                                _mk = getattr(self._model_c, "latest_accepted", None)
                                if _mk is not None:
                                    _mk["qty_source"] = _qsrc
                            except Exception as _me:
                                logger.warning("[MODEL_C] mark_position error: %r", _me)
        except Exception as _mc_err:
            if not hasattr(self, "_mc_err_log"):
                self._mc_err_log = True
                logger.warning("[MODEL_C] hook error: %r", _mc_err)
        # [P2] Spread shadow bridge — accepted-tick shadow path (disabled by default)
        try:
            self._shadow_seq = getattr(self, "_shadow_seq", 0) + 1
            from core.spread_shadow_bridge import _shadow_enabled
            if _shadow_enabled():
                from core.spread_shadow_bridge import get_bridge
                _br = get_bridge()
                _code = str(getattr(tick, "code", "") or "")
                _px = float(getattr(tick, "close", 0) or 0)
                if _br.enabled and _code and _px > 0:
                    _near_code = str(getattr(getattr(self, "contract", None), "code", "") or "")
                    _far_code = str(getattr(getattr(self, "far_contract", None), "code", "") or "")
                    if _code == _near_code:
                        _leg = "near"
                    elif _code == _far_code:
                        _leg = "far"
                    else:
                        _leg = None  # unknown/virtual code — never enters synchronizer
                    if _leg is not None:
                        _br.on_tick(_leg, {
                            "code": _code,
                            "price": _px,
                            "seq": getattr(self, "_shadow_seq", 0),
                            "ts_ms": time.time() * 1000.0,
                            "session_id": "UNKNOWN",
                        })
        except Exception:
            pass  # shadow path must never affect the trading loop  # [gstack] 更新數據更新時間

        # [P0b] Quote Integrity Gate — one decision, one destination
        if getattr(self, "_quote_guard", None) is None:
            self._init_quote_guard()
        if getattr(self, "_quote_guard", None) is not None:
            try:
                _decision = self._quote_guard.decide(self._build_quote_envelope(tick))
                self._last_quote_decision = _decision
                if _decision.destination.value == "NONE":
                    return  # rejected quote — no cache write, no downstream
            except Exception as _exc:
                print(f"[QUOTE_GUARD_DECIDE_ERR] {_exc}", flush=True)
        
        # 2026-07-31 Antigravity P0 Guard: Only near-month primary ticks (tick.code == self.contract.code)
        # update _last_tmf_price and NEAR market_data slots to prevent far-month tick price pollution.
        if getattr(tick, 'close', None) and float(tick.close) > 0:
            _p = float(tick.close)
            _t_code = getattr(tick, 'code', None)
            _tick_info = {
                "close": _p,
                "datetime": getattr(tick, 'datetime', None),
                "local_arrival_at": self.last_tick_at,
                "bid": float(getattr(tick, 'buy_price', _p) or _p),
                "ask": float(getattr(tick, 'sell_price', _p) or _p),
            }
            if _t_code:
                self.market_data[_t_code] = _tick_info
            
            _is_near_tick = (self.contract and _t_code == self.contract.code) or (not self.contract and _t_code and not _t_code.endswith("I6") and not _t_code.endswith("J6"))
            if _is_near_tick:
                self._last_tmf_price = _p
                self.market_data[self.ticker] = _tick_info
                self.market_data[f"{self.ticker}_NEAR"] = _tick_info
            elif self.far_contract and _t_code == self.far_contract.code:
                self.market_data[f"{self.ticker}_FAR"] = _tick_info

        # ── [Manual Trade Flag] Check on every tick ──
        # 2026-06-05 JVS Claw: Step 4 — gate flag check with is_primary (C4).
        # Only near-month ticks consume the flag. Far-month ticks don't populate
        # market_data[self.ticker] so they would always trigger NO_LIVE_TICK.
        _flag_path = getattr(self, "manual_trade_flag_path", "/tmp/futures_manual_trade.flag")
        _processing_path = _flag_path + ".processing"
        _is_primary_tick = self.contract and tick.code == self.contract.code
        # 2026-06-22 Gemini CLI: Check for both new and pending retry flags
        if _is_primary_tick and (os.path.exists(_flag_path) or os.path.exists(_processing_path)):
            from core.date_utils import is_day_session, is_night_session
            _now = datetime.now()
            if is_day_session(_now) or is_night_session(_now):
                try:
                    self._process_manual_trade_flag()
                except Exception as _fe:
                    console.print(f"[red][MANUAL_TRADE_FLAG] on_tick handler failed: {_fe}[/red]")

        # [Debug] fingerprint every tick (config: debug.feed)
        if self._debug_feed:
            console.print(f"[dim][FuturesMonitor][ON_TICK] code={tick.code} close={getattr(tick, 'close', None)} ts={getattr(tick, 'datetime', None)}[/dim]")

        # [Far Month] Handle far-month tick accumulation (independent from near-month)
        is_primary_tick = self.contract and tick.code == self.contract.code
        is_far_tick = False
        if self.far_contract and tick.code == self.far_contract.code:
            is_far_tick = True
        elif not is_primary_tick and self.contract and tick.code != self.contract.code:
            _target_prefix = "MXF" if str(self.ticker).upper() == "MTX" else str(self.ticker).upper()
            if str(tick.code).upper().startswith(_target_prefix):
                is_far_tick = True

        if is_far_tick:
            self._accumulate_far_tick(tick)
            # Far ticks are first-class audit evidence, not merely bar input.
            self._write_raw_tick(tick)
            # 2026-05-27 Gemini CLI: Real-time MTS Execution on Far Tick (Contract 1)
            _mts_enabled = self.cfg.get("mts", {}).get("enabled", False)
            if _mts_enabled and not self.dry_run:
                _rt_bar = dict(self._current_bar)
                # Ensure near bar has ts
                _rt_bar["ts"] = _rt_bar.get("ts") or pd.Timestamp(int(pd.Timestamp(tick.datetime).timestamp() / 300) * 300, unit='s')
                _rt_bar["near_close_rt"] = self._current_bar.get("close", 0)
                _rt_bar["near_high_rt"] = self._current_bar.get("high", 0)
                _rt_bar["near_low_rt"] = self._current_bar.get("low", 0)
                
                # 2026-06-25 Hermes Agent: extract last known ATR from processed data for dynamic stop calculations
                _last_atr = 0.0
                if hasattr(self, '_last_processed_data') and self._last_processed_data:
                    _df_5m = self._last_processed_data.get("5m")
                    if _df_5m is not None and not _df_5m.empty and "atr" in _df_5m.columns:
                        _val = _df_5m["atr"].iloc[-1]
                        if pd.notna(_val):
                            try:
                                _last_atr = float(_val)
                            except (ValueError, TypeError):
                                pass
                _rt_bar["atr"] = _last_atr
                
                # Far bar is definitely updated now
                _rt_bar["far_close_rt"] = self._far_current_bar.get("close", 0)
                _rt_bar["far_high_rt"] = self._far_current_bar.get("high", 0)
                _rt_bar["far_low_rt"] = self._far_current_bar.get("low", 0)
                
                # 2026-06-26 Gemini CLI: calculate tick ages and confirm ticks
                _now_t = time.time()
                _near_arrival = self.market_data.get(self.ticker, {}).get("local_arrival_at", 0.0)
                _far_arrival = self.market_data.get(f"{self.ticker}_FAR", {}).get("local_arrival_at", 0.0)
                _rt_bar["near_tick_age_ms"] = (_now_t - _near_arrival) * 1000 if _near_arrival > 0 else 0.0
                _rt_bar["far_tick_age_ms"] = (_now_t - _far_arrival) * 1000 if _far_arrival > 0 else 0.0
                _rt_bar["confirm_ticks"] = self.cfg.get("mts", {}).get("params", {}).get("confirm_ticks", 2)
                
                # Cache and propagate bid/ask prices for spread width checks
                _rt_bar["near_bid"] = self.market_data.get(self.ticker, {}).get("bid", _rt_bar.get("near_close", 0.0))
                _rt_bar["near_ask"] = self.market_data.get(self.ticker, {}).get("ask", _rt_bar.get("near_close", 0.0))
                _rt_bar["far_bid"] = self.market_data.get(f"{self.ticker}_FAR", {}).get("bid", _rt_bar.get("far_close", 0.0))
                _rt_bar["far_ask"] = self.market_data.get(f"{self.ticker}_FAR", {}).get("ask", _rt_bar.get("far_close", 0.0))
                
                self._mts_tick(enriched_bar=_rt_bar)
            return

        # 💡 GSD: Data Continuity Fix
        # Use strict matching for the primary contract (TMF or MXF)
        is_primary = self.contract and tick.code == self.contract.code

        # [Heartbeat] Match against common futures prefixes to update feed age
        _code = str(tick.code).upper()
        is_common_futures = _code.startswith(("MXF", "MTX", "TMF", "TXF"))
        # 2026-05-22 Gemini CLI: Defined is_tmf and is_mtx to prevent NameError in logging
        is_tmf = _code.startswith("TMF")
        is_mtx = _code.startswith(("MTX", "MXF"))

        if not is_primary and not is_common_futures:
            return

        # [GSD Data Safety] Write raw tick to CSV FIRST — before any in-memory use
        # Only write real primary ticks (not MTX/secondary ticks which might use stale price)
        if is_primary:
            # [REAL_TICK_SEEN] Real near-month MXF/TMF tick — updates ground truth age
            self._write_raw_tick(tick)
            self._last_real_tmf_tick_at = time.time()
            price = float(tick.close)
            self._last_tmf_price = price  # Cache for heartbeat
            self._refresh_runtime_status()
        else:
            # It's a secondary heartbeat tick (MTX/MXF/TMF from another contract)
            if not hasattr(self, '_last_tmf_price') or self._last_tmf_price <= 0:
                # No primary price yet, can't build bar
                return
            price = self._last_tmf_price
            # [DEGRADED FIX] Refresh runtime status even on non-primary ticks to
            # prevent stale DEGRADED status when primary tick contract is misaligned
            self._refresh_runtime_status()

        # 2026-05-27 Gemini CLI: Update market data cache for manual trade integrity checks (P0-P3)
        # Use time.time() as local_arrival_at to avoid exchange-local clock drift issues.
        # 2026-06-24 Gemini CLI: Maintain near/far/code-specific market data caches for spread execution price integrity.
        # 2026-07-31 Antigravity P0 Guard Phase 2: Gate NEAR slot updates strictly on is_primary tick.
        # Prevent far-month/secondary ticks from contaminating NEAR bid/ask/close cache at Line 1693.
        if is_primary:
            self.market_data[self.ticker] = {
                "close": price, 
                "datetime": tick.datetime,
                "local_arrival_at": time.time(),
                # 2026-06-26 Gemini CLI: cache bid/ask prices
                "bid": float(getattr(tick, 'buy_price', price) or price),
                "ask": float(getattr(tick, 'sell_price', price) or price)
            }
            self.market_data[f"{self.ticker}_NEAR"] = {
                "close": price,
                "datetime": tick.datetime,
                "local_arrival_at": time.time(),
                "bid": float(getattr(tick, 'buy_price', price) or price),
                "ask": float(getattr(tick, 'sell_price', price) or price)
            }
        elif is_far_tick:
            self.market_data[f"{self.ticker}_FAR"] = {
                "close": float(getattr(tick, 'close', price) or price),
                "datetime": tick.datetime,
                "local_arrival_at": time.time(),
                "bid": float(getattr(tick, 'buy_price', price) or price),
                "ask": float(getattr(tick, 'sell_price', price) or price)
            }

        if getattr(tick, 'code', None):
            self.market_data[tick.code] = {
                "close": price,
                "datetime": tick.datetime,
                "local_arrival_at": time.time(),
                "bid": float(getattr(tick, 'buy_price', price) or price),
                "ask": float(getattr(tick, 'sell_price', price) or price)
            }

        # Only count volume for primary ticker to keep indicators accurate
        vol = int(getattr(tick, "volume", 0)) if is_primary else 0        
        # [Wave 1 optimization] Use integer time bucketing to avoid expensive pd.Timestamp().floor()
        # Only compute Timestamp when bar changes (every 5 minutes)
        tick_ts = pd.Timestamp(tick.datetime)
        ts_int = int(tick_ts.timestamp() / 300) * 300
        
        bar = self._current_bar
        debug_skip = bar["ts"] is None
        if bar["ts"] is None or ts_int > self._last_bar_ts:
            # 💡 GSD: Only flip the bar if we have a NEW time bucket
            if bar["ts"] is not None and bar["open"] > 0:
                # [Debug] log bar close (config: debug.tickbar)
                if self._debug_tickbar:
                    console.print(f"[dim][TickBar][CLOSE] bucket={pd.Timestamp(self._last_bar_ts, unit='s').strftime('%H:%M')} close={bar['close']:.0f} vol={bar['volume']} deque={len(self._tick_bars_deque)} -> append[/dim]")
                bar_dict = {
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ts": bar["ts"],
                }
                self._tick_bars_deque.append(bar_dict)
                self._tick_bars_cache = None
            
            # Start new bar
            ts = pd.Timestamp(ts_int, unit='s')
            if self._debug_tickbar:
                console.print(f"[dim][TickBar][NEWBAR] bucket={ts.strftime('%H:%M')} price={price:.0f} vol={vol} is_tmf={is_tmf} is_mtx={is_mtx}[/dim]")
            bar["ts"] = ts
            self._last_bar_ts = ts_int
            bar["open"] = bar["high"] = bar["low"] = bar["close"] = price
            bar["volume"] = vol
        elif ts_int == self._last_bar_ts:
            # Accumulate into current bar
            if self._debug_tickbar and debug_skip:
                console.print(f"[dim][TickBar][ACCUM-first] bucket={pd.Timestamp(ts_int, unit='s').strftime('%H:%M')} price={price:.0f} vol={vol}[/dim]")
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += vol
        else:
            # Old data packet, ignore
            return

        # 2026-05-27 Gemini CLI: Real-time stop loss and MTS execution on EVERY tick
        _mts_enabled = self.cfg.get("mts", {}).get("enabled", False)
        if not self.dry_run and self.trader.position != 0 and not _mts_enabled:
            # 1. Update trailing stop peak/floor
            self.trader.update_trailing_stop(price)
            # 2. Check for SL breach
            self._check_stop_loss(tick.datetime, price)
        
        if _mts_enabled and not self.dry_run:
            _rt_bar = dict(bar)
            # Fallback to current time if ts is None
            _rt_bar["ts"] = _rt_bar.get("ts") or pd.Timestamp(ts_int, unit='s')
            
            # 2026-05-27 Gemini CLI: Pass real-time prices explicitly to override CSV staleness
            _rt_bar["near_close_rt"] = price
            _rt_bar["near_high_rt"] = bar["high"]
            _rt_bar["near_low_rt"] = bar["low"]
            
            # 2026-06-25 Hermes Agent: extract last known ATR from processed data for dynamic stop calculations
            _last_atr = 0.0
            if hasattr(self, '_last_processed_data') and self._last_processed_data:
                _df_5m = self._last_processed_data.get("5m")
                if _df_5m is not None and not _df_5m.empty and "atr" in _df_5m.columns:
                    _val = _df_5m["atr"].iloc[-1]
                    if pd.notna(_val):
                        try:
                            _last_atr = float(_val)
                        except (ValueError, TypeError):
                            pass
            _rt_bar["atr"] = _last_atr
            
            # 2026-07-31 Antigravity: Multi-tier fallback for far_close_rt (check _far_current_bar then market_data FAR cache)
            _far_rt = 0.0
            _far_h = 0.0
            _far_l = 0.0
            if hasattr(self, '_far_current_bar') and self._far_current_bar.get("close", 0) > 0:
                _far_rt = self._far_current_bar["close"]
                _far_h = self._far_current_bar.get("high", _far_rt)
                _far_l = self._far_current_bar.get("low", _far_rt)
            elif self.market_data.get(f"{self.ticker}_FAR", {}).get("close", 0) > 0:
                _far_rt = self.market_data[f"{self.ticker}_FAR"]["close"]
                _far_h = _far_rt
                _far_l = _far_rt

            if _far_rt > 0:
                _rt_bar["far_close_rt"] = _far_rt
                _rt_bar["far_high_rt"] = _far_h
                _rt_bar["far_low_rt"] = _far_l
            else:
                # 💡 [Fixed 2026-05-27] Log warning if RT far price is missing
                if _mts_enabled:
                    console.print(f"[dim][MTS] Warning: No real-time far-month price for {self.far_contract.code if self.far_contract else 'UNKNOWN'}, relying on CSV[/dim]")
            
            # 2026-06-26 Gemini CLI: calculate tick ages and confirm ticks
            _now_t = time.time()
            _near_arrival = self.market_data.get(self.ticker, {}).get("local_arrival_at", 0.0)
            _far_arrival = self.market_data.get(f"{self.ticker}_FAR", {}).get("local_arrival_at", 0.0)
            _rt_bar["near_tick_age_ms"] = (_now_t - _near_arrival) * 1000 if _near_arrival > 0 else 0.0
            _rt_bar["far_tick_age_ms"] = (_now_t - _far_arrival) * 1000 if _far_arrival > 0 else 0.0
            _rt_bar["confirm_ticks"] = self.cfg.get("mts", {}).get("params", {}).get("confirm_ticks", 2)

            # Cache and propagate bid/ask prices for spread width checks
            _rt_bar["near_bid"] = self.market_data.get(self.ticker, {}).get("bid", _rt_bar.get("near_close", 0.0))
            _rt_bar["near_ask"] = self.market_data.get(self.ticker, {}).get("ask", _rt_bar.get("near_close", 0.0))
            _rt_bar["far_bid"] = self.market_data.get(f"{self.ticker}_FAR", {}).get("bid", _rt_bar.get("far_close", 0.0))
            _rt_bar["far_ask"] = self.market_data.get(f"{self.ticker}_FAR", {}).get("ask", _rt_bar.get("far_close", 0.0))

            self._mts_tick(enriched_bar=_rt_bar)

        cb = self.client._tick_callbacks.get(tick.code)
        if cb:
            cb(exchange, tick)

        # 2026-05-22 Gemini CLI: Removed _maybe_close_selftest() call from here.

    # [Far Month] Accumulate far-month ticks into independent 5-min bars
    def _accumulate_far_tick(self, tick):
        """Accumulate far-month MXF ticks into _far_tick_bars_deque (5-min bars).
        Does NOT affect strategy signals, stop loss, or orders."""
        if getattr(tick, 'close', None) is None:
            return
        try:
            price = float(tick.close)
            if price <= 0:
                return
        except (ValueError, TypeError):
            return
        vol = int(getattr(tick, "volume", 0))
        tick_ts = pd.Timestamp(tick.datetime)
        ts_int = int(tick_ts.timestamp() / 300) * 300

        # 2026-06-24 Gemini CLI: Maintain far/code-specific market data caches for spread execution price integrity.
        self.market_data[f"{self.ticker}_FAR"] = {
            "close": price,
            "datetime": tick.datetime,
            "local_arrival_at": time.time(),
            # 2026-06-26 Gemini CLI: cache bid/ask prices
            "bid": float(getattr(tick, 'buy_price', price) or price),
            "ask": float(getattr(tick, 'sell_price', price) or price)
        }
        if getattr(tick, 'code', None):
            self.market_data[tick.code] = {
                "close": price,
                "datetime": tick.datetime,
                "local_arrival_at": time.time(),
                "bid": float(getattr(tick, 'buy_price', price) or price),
                "ask": float(getattr(tick, 'sell_price', price) or price)
            }

        # [Debug] Periodic far tick log (every 30s)
        now_s = time.time()
        if not hasattr(self, '_last_far_tick_log') or now_s - self._last_far_tick_log > 30:
            self._last_far_tick_log = now_s
            console.print(f"[dim]📥 Far tick: {tick.code} close={price} ts={tick_ts.strftime('%H:%M:%S')}[/dim]")

        bar = self._far_current_bar
        if bar["ts"] is None or ts_int > self._last_far_bar_ts:
            # Flip completed bar into deque
            if bar["ts"] is not None and bar["open"] > 0:
                self._far_tick_bars_deque.append({
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ts": bar["ts"],
                })
                # [Far Month] Persist completed bar to shared CSV for dashboard consumption
                self._save_far_bar({
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ts": bar["ts"],
                })
            # Start new bar
            ts = pd.Timestamp(ts_int, unit='s')
            bar["ts"] = ts
            self._last_far_bar_ts = ts_int
            bar["open"] = bar["high"] = bar["low"] = bar["close"] = price
            bar["volume"] = vol
        elif ts_int == self._last_far_bar_ts:
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += vol
        # else: old data, ignore


    def _refresh_far_snapshot(self) -> None:
        """Periodically refresh far-month price from API snapshot.
        
        Far-month futures often have no tick callbacks during day session.
        This fallback ensures TMF_FAR price stays current even without ticks.
        Called from the main strategy loop when far data is stale.
        """
        if not self.far_contract or not self.api or self.dry_run:
            return
        try:
            _snaps = self.api.snapshots([self.far_contract])
            if _snaps and len(_snaps) > 0:
                _s = _snaps[0]
                if _s.close and float(_s.close) > 0:
                    _price = float(_s.close)
                    _now = time.time()
                    # Update market_data cache
                    _far_key = f"{self.ticker}_FAR"
                    if _far_key not in self.market_data:
                        self.market_data[_far_key] = {}
                    self.market_data[_far_key]["close"] = _price
                    self.market_data[_far_key]["snapshot_at"] = _now
                    self.market_data[_far_key]["local_arrival_at"] = _now
                    # Also update far_current_bar
                    if hasattr(self, "_far_current_bar"):
                        self._far_current_bar["close"] = _price
                        self._far_current_bar["open"] = _price if self._far_current_bar.get("open", 0) == 0 else self._far_current_bar.get("open", _price)
                        self._far_current_bar["high"] = max(float(self._far_current_bar.get("high", 0) or 0), _price)
                        self._far_current_bar["low"] = min(float(self._far_current_bar.get("low", 0) or _price), _price)
                    self._last_far_snapshot_ts = time.time()
                    print(f"[FAR_SNAP] {self.far_contract.code} price={_price}")
        except Exception as e:
            print(f"[yellow][FAR_SNAP] fail: {e}")

    def _save_far_bar(self, bar):
        """Append a completed far-month bar to shared CSV for dashboard consumption.
        Writes to: logs/market_data/{ticker}_far_{date_str}_{tag}.csv"""
        try:
            log_dir = runtime_logs("market_data")
            os.makedirs(log_dir, exist_ok=True)
            from core.date_utils import get_session_date_str
            date_str = get_session_date_str(datetime.now())
            tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
            path = Path(log_dir) / f"{self.ticker}_far_{date_str}{tag}.csv"

            ts_str = str(bar["ts"])
            row_data = {
                "timestamp": ts_str,
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
            }
            cols = ["timestamp", "open", "high", "low", "close", "volume"]
            if not path.exists():
                pd.DataFrame([row_data])[cols].to_csv(path, index=False)
            else:
                pd.DataFrame([row_data]).reindex(columns=cols).to_csv(path, mode='a', header=False, index=False)
        except Exception as e:
            console.print(f"[dim][FuturesMonitor] Far bar save failed (non-fatal): {e}[/dim]")

    # ── Safety Stop (exchange-side protection) ──
    def _persist_execution_context(self) -> bool:
        """[Step 6] persist the execution context to the canonical
        dashboard-readable file ({TRADING_RUNTIME_DIR}/execution_context.json)
        atomically. A failure never enables LIVE — the reader is
        file-based and keeps the last good state.
        [Step 8] also syncs the broker-adapter gate reference so the
        adapter chokepoint enforces the same context.
        [P0 fix] canonical persistence succeeds FIRST; only then are
        BOTH client._execution_context and order_mgr.execution_context
        synchronized to the exact same current immutable object.  On
        persist failure both stay on the PRIOR context (fail-closed) —
        a new allow state is never exposed through the chokepoints and
        the OrderManager never builds orders against a stale mode.
        [P1] returns True only when the canonical persist succeeded and
        both consumers were synced; False on persist failure (prior
        context retained everywhere)."""
        _ctx = getattr(self, "_execution_context", None)
        # [P1 all-or-nothing] each existing consumer must accept context
        # assignment; a KNOWN inability is a typed failure BEFORE any
        # canonical persist (nothing is written, nothing changes).
        _consumers = []
        _client = getattr(self, "client", None)
        if _client is not None:
            _consumers.append((_client, "_execution_context"))
        _order_mgr = getattr(self, "order_mgr", None)
        if _order_mgr is not None:
            _consumers.append((_order_mgr, "execution_context"))

        def _set_and_verify(consumer, attr, value) -> bool:
            try:
                setattr(consumer, attr, value)
            except (AttributeError, TypeError):
                return False
            return getattr(consumer, attr, None) is value

        _prior_refs = [(c, a, getattr(c, a, None)) for c, a in _consumers]
        for _consumer, _attr, _prior in _prior_refs:
            if not _set_and_verify(_consumer, _attr, _prior):
                # known inability to sync a consumer: fail-closed,
                # nothing persisted, no success event
                return False

        # capture the prior canonical state for best-effort rollback
        _prior_payload = None
        _prior_file_existed = False
        _rf = None
        try:
            _rf = runtime_path("execution_context.json")
            _prior_file_existed = os.path.exists(_rf)
            if _prior_file_existed:
                with open(_rf, "r", encoding="utf-8") as _fr:
                    _prior_payload = json.loads(_fr.read() or "{}") or {}
        except Exception:
            _prior_payload = None
            _prior_file_existed = False

        try:
            from core.execution_context_state import persist_execution_context
            _payload = _ctx.to_dict()
            _gw = getattr(self, "_order_intent_gateway", None)
            if _gw is not None:
                _payload["gateway_intents"] = _gw.durable_view()
            persist_execution_context(_payload)
        except Exception as _pexc:
            console.print(f"[dim]⚠️ exec ctx persist failed: {_pexc} "
                          f"(file keeps last good state)[/dim]")
            # fail-closed: client/order_mgr keep the PRIOR context
            return False
        for _consumer, _attr, _prior in _prior_refs:
            if not _set_and_verify(_consumer, _attr, _ctx):
                # [P1] unexpected post-persist sync failure: restore ALL
                # in-memory consumers to their prior references (never a
                # success event with split context) and best-effort
                # restore the prior canonical state.
                for _c2, _a2, _p2 in _prior_refs:
                    try:
                        setattr(_c2, _a2, _p2)
                    except Exception:
                        pass
                try:
                    from core.execution_context_state import (
                        persist_execution_context as _persist_ctx_fn)
                    if _prior_file_existed and _prior_payload is not None:
                        # canonical ATOMIC re-persist of the prior state
                        _persist_ctx_fn(_prior_payload)
                    elif _rf is not None and os.path.exists(_rf):
                        # no prior state: remove the new file so nothing
                        # EXIT_ONLY survives a restart (no split-brain)
                        os.remove(_rf)
                except Exception:
                    # [P1 fatal] rollback of the committed canonical write
                    # FAILED: memory was restored to prior (possibly
                    # LIVE_READY) while disk holds the new state.  Force
                    # fail-closed quarantine in memory (never reuse prior
                    # authority), then raise — the process must not
                    # continue order-capable.
                    _qctx = None
                    try:
                        from dataclasses import replace
                        from core.mode_transition import ModeTransitionState
                        _reasons = tuple(
                            getattr(_ctx, "audit_reasons", ())) + (
                                "EXECUTION_CONTEXT_SYNC_FATAL",)
                        _qctx = replace(
                            _ctx,
                            effective_mode=(
                                ModeTransitionState.LIVE_QUARANTINED.value),
                            live_order_allowed=False,
                            audit_reasons=_reasons,
                        )
                    except Exception:
                        _qctx = None
                    if _qctx is not None:
                        try:
                            self._execution_context = _qctx
                        except Exception:
                            pass
                        for _c2, _a2 in _consumers:
                            try:
                                setattr(_c2, _a2, _qctx)
                            except Exception:
                                # [P1 fatal] this consumer rejected the
                                # quarantine assignment — it may still
                                # hold stale LIVE authority.  HARD-DISABLE
                                # its broker capability so no direct
                                # client route can trade (never swallow).
                                try:
                                    setattr(_c2, "api", None)
                                except Exception:
                                    pass
                                try:
                                    setattr(
                                        _c2,
                                        "_broker_capability_disabled", True)
                                except Exception:
                                    pass
                    raise ExecutionContextSyncFatal(
                        "execution-context rollback failed; in-memory "
                        "forced to fail-closed quarantine")
                return False
        return True

    def _quarantine_mts_entry_partial_submission(
            self, *, trade_id: str, submitted_order, failed_order) -> None:
        """Contain a sequential MTS entry after only one leg was accepted.

        Acknowledgement of the first leg creates a real exposure or pending
        broker order.  The second-leg rejection is therefore not an ordinary
        local failure: live trading is quarantined and the durable event
        identifies both legs for operator reconciliation.  This method never
        sends an automatic broker cancel or compensating order.
        """
        _reason = "MTS_ENTRY_PARTIAL_SUBMISSION"
        _ctx = getattr(self, "_execution_context", None)
        if _ctx is not None and getattr(_ctx, "requested_mode", None) == "live":
            # The acknowledged leg remains visible to callbacks and broker
            # reconciliation, but a watchdog must never cancel it blindly
            # after the second leg failed locally.
            self._mts_stale_order_cancels = getattr(
                self, "_mts_stale_order_cancels", set())
            self._mts_stale_order_cancels.add(submitted_order.order_id)
            self._record_mts_entry_reconcile(trade_id)
            from core.mode_transition import ModeTransitionState, with_effective_mode
            self._execution_context = with_effective_mode(
                _ctx, ModeTransitionState.LIVE_QUARANTINED.value,
                live_order_allowed=False,
                audit_reasons=(_reason,) + tuple(
                    getattr(_ctx, "audit_reasons", ()) or ()))
            self._persist_execution_context()

        self._append_mts_event(
            _reason,
            trade_id=trade_id,
            submitted_order_id=getattr(submitted_order, "order_id", None),
            submitted_broker_order_id=getattr(
                submitted_order, "exchange_order_id", None),
            failed_order_id=getattr(failed_order, "order_id", None),
            reason=getattr(failed_order, "reject_reason", None)
                   or "ADAPTER_SUBMIT_FAILED",
        )
        console.print(
            "[red]🚫 [MTS_ENTRY] partial submission: live trading "
            "QUARANTINED; no compensating order was sent. Reconcile the "
            "submitted leg with the broker before re-certification.[/red]")

    def _on_session_logout(self):
        """[Step 7] real broker logout: the current session registry
        generation is invalidated BEFORE the broker logout (centralized
        in shioaji_session.logout — unregister; failure -> invalidate_all
        + re-raise). This monitor must NOT retain a LIVE_READY context
        across it: quarantine + persist (SESSION_LOGOUT)."""
        _ctx = getattr(self, "_execution_context", None)
        if _ctx is None:
            return
        from core.live_route_certificate import session_registry
        _gen = session_registry.current_generation()
        from core.mode_transition import ModeTransitionState, with_effective_mode
        self._execution_context = with_effective_mode(
            _ctx, ModeTransitionState.LIVE_QUARANTINED.value,
            live_order_allowed=False,
            audit_reasons=("SESSION_LOGOUT",) + tuple(
                getattr(_ctx, "audit_reasons", ()) or ()))
        self._persist_execution_context()
        console.print(f"[dim]🔒 Session logout: ctx QUARANTINED "
                      f"(SESSION_LOGOUT; registry gen="
                      f"{'valid' if _gen else 'revoked'})[/dim]")

    def _record_reconcile_intent(self, trade_id: str, reason: str) -> bool:
        """Record one canonical, restart-safe reconciliation requirement."""
        try:
            from core.exit_intent import IntentLog as _MTSIntentLog
            _ilog = _MTSIntentLog(_mts_intent_log_dir())
            for _iid in _ilog.list_active():
                _row = _ilog.get(_iid)
                if _row.get("reason") == reason and \
                        _row.get("trade_id") == trade_id:
                    return True
            _iid = _ilog.create(trade_id, reason)
            console.print(f"[yellow]🛡️ {reason} intent recorded: {_iid} "
                          f"(trade {trade_id}) — QUARANTINED until broker "
                          f"state reconciles[/yellow]")
            return True
        except Exception as _exc:
            console.print(f"[dim]⚠️ reconcile intent record failed: "
                          f"{_exc}[/dim]")
            return False

    def _record_safety_stop_reconcile(self):
        """[orphan reconciliation] when quarantine blocks the safety-stop
        cancel / emergency and an exchange-side safety stop is outstanding,
        record a DURABLE SAFETY_STOP_RECONCILE intent via the canonical
        core/exit_intent protocol (no parallel ledger, no bypass). The
        pending intent survives restart and keeps QUARANTINED until the
        broker state reconciles."""
        _trade = getattr(self, "_safety_stop_trade", None)
        if _trade is None:
            return False
        return self._record_reconcile_intent(
            str(getattr(_trade, "id", None) or "SAFETY_STOP"),
            "SAFETY_STOP_RECONCILE")

    def _record_mts_entry_reconcile(self, trade_id: str) -> bool:
        """Persist an acknowledged-first-leg entry requiring reconciliation."""
        return self._record_reconcile_intent(trade_id, "MTS_ENTRY_RECONCILE")

    def _pending_reconcile_intent(self, reason: str) -> bool:
        try:
            from core.exit_intent import IntentLog as _MTSIntentLog
            _ilog = _MTSIntentLog(_mts_intent_log_dir())
            return any(_ilog.get(i).get("reason") == reason
                       for i in _ilog.list_active())
        except Exception:
            return False

    def _pending_safety_stop_reconcile(self) -> bool:
        """True if a durable SAFETY_STOP_RECONCILE intent is still active
        (restart read — the canonical exit_intent log)."""
        return self._pending_reconcile_intent("SAFETY_STOP_RECONCILE")

    def _capture_exit_only_snapshot(self) -> dict:
        """Fresh read-only broker snapshot for the exit-only attestation.

        Uses the same read methods as the post-startup gate
        (list_positions / list_trades) - zero place/cancel/update calls.
        """
        _api = getattr(self, "api", None)
        _ctx = getattr(self, "_execution_context", None)
        if _api is None or _ctx is None or not hasattr(_api, "list_positions"):
            return {"source": "unavailable", "capture_error": True}
        positions, open_orders = [], []
        captured_at = int(time.time() * 1000)
        _acct = getattr(_api, "futopt_account", None)
        if _acct is None or not hasattr(_api, "list_trades"):
            return {"source": "unavailable", "capture_error": True}
        try:
            try:
                _pos_rows = _api.list_positions(account=_acct)
            except TypeError:
                # same SDK signature quirk the preflight guards against
                _pos_rows = _api.list_positions()
            try:
                _trades_rows = _api.list_trades(account=_acct)
            except TypeError:
                _trades_rows = _api.list_trades()
            for _p in (_pos_rows or []):
                _raw_direction = (getattr(
                    getattr(_p, "direction", None), "name", None)
                    or str(getattr(_p, "direction", "")))
                positions.append({
                    "code": str(getattr(_p, "code", "")),
                    # Unknown directions remain non-canonical and are
                    # rejected later by the exit-only capability validator.
                    "direction": str(_raw_direction).rsplit(".", 1)[-1].strip().lower(),
                    "quantity": int(getattr(_p, "quantity", 0) or 0),
                    "avg_cost": float(getattr(_p, "price", 0) or 0),
                })
            for _t in (_trades_rows or []):
                _st = getattr(getattr(_t, "status", None), "status", "")
                if str(_st) not in ("Filled", "Cancelled", "Expired", "Done"):
                    open_orders.append({"ordno": getattr(_t, "ordno", None)})
        except Exception:
            # Failure to prove either positions or open orders is not an
            # empty account.  Return typed unavailable evidence instead.
            return {"source": "unavailable", "capture_error": True}
        return {
            "source": "live_broker",
            "captured_at": captured_at,
            "account_id_hash": getattr(_ctx, "account_id_hash", None),
            "session_id": getattr(_ctx, "session_id", None),
            "config_hash": getattr(_ctx, "config_hash", None),
            "release_sha": os.environ.get("LRC_RELEASE_SHA", ""),
            "positions": positions,
            "open_orders": open_orders,
        }

    def _bind_reconciled_exit_identity(self) -> bool:
        """Bind the *current authenticated* identity while quarantined.

        A non-flat account must fail normal certification and can therefore
        never reach ``LIVE_READY`` first.  This helper supplies only the
        account/config/registry bindings needed to validate an independently
        attested, exact closing capability.  It does not relax any normal
        gate, does not change the effective mode, and never enables orders.
        """
        try:
            import dataclasses
            import hashlib
            from core.live_broker_preflight import _account_hash
            from core.live_route_certificate import session_registry

            _api = getattr(self, "api", None)
            _ctx = getattr(self, "_execution_context", None)
            _acct = getattr(_api, "futopt_account", None)
            _gen = session_registry.generation(_api) if _api is not None else None
            if _ctx is None or _acct is None or not _gen:
                return False
            _cfg_path = getattr(self, "config_path", "")
            if not _cfg_path or not os.path.exists(_cfg_path):
                return False
            _cfg_hash = hashlib.sha256(Path(_cfg_path).read_bytes()).hexdigest()
            if not _cfg_hash:
                return False
            self._execution_context = dataclasses.replace(
                _ctx,
                account_id_hash=_account_hash(_acct),
                session_id=str(_gen),
                config_hash=_cfg_hash,
                live_order_allowed=False,
            )
            self._persist_execution_context()
            return True
        except Exception:
            return False

    def _operator_attest_exit_only(self, *, operator: str, trade_id: str,
                                   evidence: str = "",
                                   expected_legs=None,
                                   attested_at: str = "") -> tuple:
        """RECONCILED_EXIT_ONLY attestation flow (P0 completion).

        Hard requirement: operator attestation (who/when/evidence, no
        secrets) + a fresh matching live_broker snapshot.  On success the
        context transitions to the distinct exit-only mode (never
        LIVE_READY) and an OPERATOR_ATTESTATION event is emitted; on any
        failure returns (None, typed_code) - N/A + zero orders.
        """
        from datetime import datetime, timezone
        from core.reconciled_exit import (
            AttestationError,
            apply_exit_only,
            build_exit_only_capability,
        )
        _ctx = getattr(self, "_execution_context", None)
        # LIVE_READY already carries a bound identity.  The actual recovery
        # case is position quarantine, so bind the same current account and
        # registry generation before taking its read-only snapshot.
        if _ctx is None or not _ctx.is_live_ready():
            if not self._bind_reconciled_exit_identity():
                return None, "EXIT_ONLY_CONTEXT_INVALID"
        _snap = self._capture_exit_only_snapshot()
        _att = {
            "operator": operator,
            "attested_at": attested_at or datetime.now(
                timezone.utc).isoformat(),
            "trade_id": trade_id,
            "evidence": evidence,
            "expected_legs": list(expected_legs or []),
        }
        try:
            _cap, _record = build_exit_only_capability(
                _att, _snap, ctx=self._execution_context)
        except AttestationError as _exc:
            console.print(
                f"[red]⛔ [OPERATOR_ATTESTATION] rejected: "
                f"{_exc.code}[/red]")
            return None, _exc.code
        _prior_ctx = self._execution_context
        self._execution_context = apply_exit_only(_prior_ctx, _cap)
        try:
            _persisted = self._persist_execution_context()
        except ExecutionContextSyncFatal:
            # [P1 fatal] rollback + quarantine could not be completed:
            # this is process-terminating, NOT a normal attestation
            # failure — re-raise so the process supervision stops an
            # order-capable process (a stale authority must never keep
            # trading).  No success event was emitted.
            raise
        if _persisted is False:
            # [P1] persist failed: restore the exact prior immutable
            # context (client/order_mgr were never synced — fail-closed)
            # and emit NO OPERATOR_ATTESTATION success event.
            self._execution_context = _prior_ctx
            return None, "EXECUTION_CONTEXT_PERSIST_FAILED"
        self._append_mts_event("OPERATOR_ATTESTATION", **_record)
        console.print(
            f"[green]✅ [OPERATOR_ATTESTATION] {trade_id} -> "
            f"RECONCILED_EXIT_ONLY[/green]")
        return _record, None

    def _gateway_authority(self, strategy=None):
        """[S0] canonical gateway policy authority bundle."""
        _ctx = getattr(self, "_execution_context", None)
        _gw_mode = getattr(_ctx, "effective_mode", "")
        return {
            "live": bool(
                (getattr(self, "live_trading", False)
                 and not getattr(self, "dry_run", False))
                or _gw_mode == "reconciled_exit_only"),
            "mode": _gw_mode,
            "live_order_allowed": getattr(_ctx, "live_order_allowed", False),
            "capability": getattr(_ctx, "exit_only_capability", None),
            "hydrated_position": getattr(self, "_exit_only_position", None),
            "strategy_reconciliation_id": getattr(
                strategy, "_reconciliation_id", None),
            "near_code": getattr(
                getattr(self, "contract", None), "code", None),
            "far_code": getattr(
                getattr(self, "far_contract", None), "code", None),
            "bbo_slots": self._exit_only_bbo_slots(),
            "position_has_position": \
                self._exit_only_position_has_position(),
        }

    def _authorize_intent(self, action, strategy_name, strategy_obj=None):
        """[S0] single authorize entry: every MTS order path (auto,
        manual, emergency) authorizes through the gateway policy."""
        _gw = self._gateway()
        _ok, _binding, _reason = _gw.authorize_intent(
            action=action, strategy=strategy_name,
            authority=self._gateway_authority(strategy_obj))
        if _ok and _binding is not None:
            self._exit_only_decision_binding = _binding
        return _ok, _binding, _reason

    def _quarantine_mts_exit_leg_failure(self, *, trade_id, leg, reason):
        """[S0] a combined-exit leg failed (GatewaySubmitError):
        atomically force LIVE_QUARANTINED (reconciliation-required)
        and persist.  The intent leg stays durable UNKNOWN; never
        auto-retry, never cancel."""
        _ctx = getattr(self, "_execution_context", None)
        if _ctx is not None and getattr(_ctx, "requested_mode", None) == "live":
            from dataclasses import replace
            from core.mode_transition import ModeTransitionState
            # [S0 verdict P0-2] durable restart-safe marker FIRST: even if
            # the execution-context persist fails below, the pending
            # MTS_EXIT_RECONCILE intent keeps the restart gate fail-closed
            # (never log-only).
            self._record_reconcile_intent(trade_id, "MTS_EXIT_RECONCILE")
            try:
                _reasons = list(getattr(_ctx, "audit_reasons", ()) or ())
                _reasons.append(f"MTS_EXIT_LEG_FAILED:{leg}:{reason}")
                self._execution_context = replace(
                    _ctx,
                    effective_mode=ModeTransitionState.LIVE_QUARANTINED.value,
                    live_order_allowed=False,
                    audit_reasons=tuple(_reasons[-64:]))
                self._persist_execution_context()
            except Exception as _exc:
                console.print(
                    f"[red]⚠️ [MTS_EXIT] quarantine persist failed: "
                    f"{_exc} — durable MTS_EXIT_RECONCILE intent "
                    f"remains the restart gate[/red]")
        from core.exit_only_position import build_bbo_failure_evidence
        _cap = getattr(getattr(self, "_execution_context", None),
                       "exit_only_capability", None)
        self._append_mts_event(
            "EXIT_ONLY_QUARANTINED", action="COMBINED_EXIT",
            reason=f"{leg}:{reason}", trade_id=trade_id,
            bbo_input_v2=build_bbo_failure_evidence(
                self._exit_only_bbo_slots(), _cap, f"{leg}:{reason}"))

    def _gateway(self):
        """[S0] lazy OrderIntentGateway: replays the durable intent
        ledger (execution-context payload) and injects the registry
        into the ACTUAL broker_adapter the OrderManager uses."""
        _gw = getattr(self, "_order_intent_gateway", None)
        if _gw is None:
            from core.order_intent_gateway import OrderIntentGateway
            _durable = {}
            try:
                from core.execution_context_state import read_execution_context
                _st = read_execution_context()
                _durable = (_st or {}).get("gateway_intents") or {}
            except Exception:
                pass
            _gw = self._order_intent_gateway = OrderIntentGateway(
                durable_intents=_durable,
                record_cb=self._record_gateway_intent)
        # [S0] re-inject on every call: the broker_adapter may be
        # replaced (tests/restarts); the registry stays current.
        _gw_target = getattr(
            getattr(self, "order_mgr", None), "broker_adapter", None)
        if _gw_target is not None:
            try:
                setattr(_gw_target, "_gateway_registry", _gw.registry)
            except Exception:
                pass
        return _gw

    def _record_gateway_intent(self, durable_view):
        """[S0] persist the gateway intent ledger via the
        execution-context payload.  Raises GatewayIntentPersistFailed
        when the persist fails — the gateway must not issue an
        authorization or call the adapter unless the durable
        PENDING_SUBMIT is confirmed."""
        from core.execution_context_state import persist_execution_context
        from core.order_intent_gateway import GatewayIntentPersistFailed
        _ctx = getattr(self, "_execution_context", None)
        try:
            if _ctx is None:
                raise RuntimeError("no execution context")
            _payload = _ctx.to_dict()
            _payload["gateway_intents"] = durable_view
            persist_execution_context(_payload)
        except Exception as _pexc:
            raise GatewayIntentPersistFailed(str(_pexc)) from _pexc

    def _submit_via_gateway(self, order, exchange_ordno=None,
                            raise_on_failure=False):
        """[S0] Route every MTS signal submission through the gateway.

        Returns the canonical receipt dict on success, False on failure
        (or raises GatewaySubmitError when raise_on_failure is set —
        the exit-intent submit_leg contract: a failed leg must never
        be marked SUBMITTED)."""
        _gw = self._gateway()
        _ctx = getattr(self, "_execution_context", None)
        _live = bool(getattr(self, "live_trading", False)
                     and not getattr(self, "dry_run", False))
        _mode = "live" if _live else "paper"
        _sg = getattr(_ctx, "session_id", "") or ""
        _ok, _payload = _gw.submit_with_authorization(
            order, mode=_mode, session_generation=_sg,
            exchange_ordno=exchange_ordno,
            submit_callable=self.order_mgr.submit)
        if not _ok:
            if raise_on_failure:
                from core.order_intent_gateway import GatewaySubmitError
                raise GatewaySubmitError(_payload)
            return False
        return _payload

    # [auto re-reconciliation] read-only renewal of the CURRENT
    # capability's freshness: 30s cadence, bounded backoff (cap 300s).
    # Monitoring display TTL = 1800s; execution submit proof TTL = 60s.
    EXIT_ONLY_RENEWAL_INTERVAL_S = 30
    EXIT_ONLY_RENEWAL_MAX_BACKOFF_S = 300
    EXIT_ONLY_MONITOR_TTL_S = 1800
    EXIT_ONLY_EXECUTION_TTL_S = 60

    def _exit_only_renewal_provenance_path(self):
        try:
            return Path(runtime_path("exit_only_renewal_provenance.json"))
        except Exception:
            return None

    def _read_exit_only_renewal_provenance(self) -> dict:
        _p = self._exit_only_renewal_provenance_path()
        try:
            if _p is not None and _p.exists():
                _d = json.loads(_p.read_text(encoding="utf-8"))
                if isinstance(_d, dict):
                    return _d
        except Exception:
            pass
        return {}

    def _persist_exit_only_renewal_provenance(self, **kw) -> None:
        _p = self._exit_only_renewal_provenance_path()
        if _p is None:
            return
        _d = self._read_exit_only_renewal_provenance()
        _d.update(kw)
        _d.setdefault("renewal_count", 0)
        _d.setdefault("monitor_ttl_s", self.EXIT_ONLY_MONITOR_TTL_S)
        _d.setdefault("execution_ttl_s", self.EXIT_ONLY_EXECUTION_TTL_S)
        try:
            _p.parent.mkdir(parents=True, exist_ok=True)
            _tmp = f"{_p}.tmp.{os.getpid()}"
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(_d, f, ensure_ascii=False, sort_keys=True)
            os.replace(_tmp, _p)
        except Exception:
            try:
                if os.path.exists(_tmp):
                    os.remove(_tmp)
            except Exception:
                pass

    def _exit_only_renewal_due(self) -> bool:
        _prov = self._read_exit_only_renewal_provenance()
        _last = (_prov.get("renewed_at_ms")
                 or _prov.get("attempted_at_ms") or 0)
        _backoff = int(_prov.get("backoff_s") or 0)
        _interval_s = self.EXIT_ONLY_RENEWAL_INTERVAL_S + _backoff
        return (int(time.time() * 1000) - int(_last)) >= _interval_s * 1000

    def _exit_only_renewal_fail(self, reason: str, *,
                                quarantine: bool = True) -> tuple:
        """[auto re-reconciliation] a renewal failed.  Mismatch / open
        order / session change => quarantine the capability with the
        typed reason (LIVE_QUARANTINED + audit reason).  A transient
        QUERY failure => DEGRADED status + bounded backoff only —
        monitoring continues (the pre-submit query is decisive and a
        failure THERE quarantines).  Zero orders by construction."""
        _now_ms = int(time.time() * 1000)
        _prov = self._read_exit_only_renewal_provenance()
        _backoff = min(
            (int(_prov.get("backoff_s") or 0) * 2)
            or self.EXIT_ONLY_RENEWAL_INTERVAL_S,
            self.EXIT_ONLY_RENEWAL_MAX_BACKOFF_S)
        _status = "QUARANTINED" if quarantine else "DEGRADED"
        self._persist_exit_only_renewal_provenance(
            attempted_at_ms=_now_ms,
            last_failed_at_ms=_now_ms,
            last_reason=reason,
            backoff_s=_backoff,
            status=_status,
            next_renewal_at_ms=(
                _now_ms + (self.EXIT_ONLY_RENEWAL_INTERVAL_S + _backoff)
                * 1000))
        if quarantine:
            _ctx = getattr(self, "_execution_context", None)
            if _ctx is not None and getattr(
                    _ctx, "effective_mode", "") == "reconciled_exit_only":
                from dataclasses import replace
                from core.mode_transition import ModeTransitionState
                _reasons = list(getattr(_ctx, "audit_reasons", ()) or ())
                _reasons.append(f"EXIT_ONLY_RENEWAL:{reason}")
                _new_ctx = replace(
                    _ctx,
                    effective_mode=(
                        ModeTransitionState.LIVE_QUARANTINED.value),
                    live_order_allowed=False,
                    audit_reasons=tuple(_reasons[-64:]))
                def _hard_disable_broker_routes():
                    """api=None + _broker_capability_disabled on EVERY
                    order-capable route: this monitor, the direct
                    client, the order manager and its broker adapter.
                    Nothing can re-enable it except an operator restart
                    / reconciliation."""
                    _targets = [self]
                    _client = getattr(self, "client", None)
                    if _client is not None:
                        _targets.append(_client)
                    _mgr = getattr(self, "order_mgr", None)
                    if _mgr is not None:
                        _targets.append(_mgr)
                        _adapter = getattr(_mgr, "broker_adapter", None)
                        if _adapter is not None:
                            _targets.append(_adapter)
                    for _t in _targets:
                        try:
                            setattr(_t, "api", None)
                        except Exception:
                            pass
                        try:
                            setattr(_t, "_broker_capability_disabled", True)
                        except Exception:
                            pass

                # [P0c] the fatal/all-or-nothing persistence contract:
                # persist FIRST then sync client + order_mgr to the SAME
                # immutable object.  A safety-critical mismatch MUST
                # land LIVE_QUARANTINED; if the canonical state cannot
                # record it (False or raise) the broker is HARD-DISABLED
                # so no direct/client/order_mgr route can trade and no
                # later fresh snapshot can re-enable it — operator
                # restart/reconciliation is required.  The prior
                # EXIT_ONLY authority is NEVER restored after a
                # mismatch.
                try:
                    self._execution_context = _new_ctx
                    _persist_ok = self._persist_execution_context()
                except Exception:
                    _hard_disable_broker_routes()
                    try:
                        self._execution_context = _new_ctx
                    except Exception:
                        pass
                    raise
                if _persist_ok is False:
                    # the canonical state could NOT record the mismatch:
                    # hard-disable every order-capable route and keep
                    # the in-memory quarantine — EXIT_ONLY authority
                    # must not survive an unrecorded safety-critical
                    # mismatch.
                    _hard_disable_broker_routes()
                    try:
                        self._execution_context = _new_ctx
                    except Exception:
                        pass
        self._append_mts_event(
            "EXIT_ONLY_RENEWAL_FAILED", reason=reason,
            renewal_provenance={
                "attempted_at_ms": _now_ms, "backoff_s": _backoff,
                "status": _status})
        return False, reason

    def _verify_exit_only_snapshot(self, _snap, _cap) -> tuple:
        """[auto re-reconciliation] SHARED read-only snapshot contract:
        live_broker source, exact identity lock, exact two legs, no open
        orders.  Returns (True, None) or (False, typed_reason)."""
        if not isinstance(_snap, dict) or _snap.get("source") != "live_broker":
            return False, "EXIT_ONLY_RENEWAL_QUERY_FAILED"
        for _k in ("account_id_hash", "session_id", "config_hash",
                   "release_sha"):
            if _snap.get(_k) != _cap.get(_k):
                return False, "EXIT_ONLY_IDENTITY_MISMATCH"
        _pos = sorted(
            (str(p.get("code", "")), str(p.get("direction", "")),
             int(p.get("quantity", 0) or 0))
            for p in (_snap.get("positions") or []))
        _legs = sorted(
            (str(l.get("symbol", "")), str(l.get("side", "")),
             int(l.get("remaining_qty", 0) or 0))
            for l in (_cap.get("legs") or []))
        if _pos != _legs:
            return False, "EXIT_ONLY_POSITION_MISMATCH"
        if _snap.get("open_orders"):
            return False, "EXIT_ONLY_OPEN_ORDERS"
        return True, None

    def _pre_submit_exit_only_proof(self) -> tuple:
        """[auto re-reconciliation] the ORDER SAFETY BOUNDARY: a
        synchronous FRESH read-only broker snapshot taken immediately
        BEFORE authorization.  Requires the exact capability rid /
        locked two legs / account / session / config / release and
        open_orders == [].  Returns (True, None) to proceed or
        (False, typed_reason) — the failure path quarantines with the
        typed reason, zero orders, NO retry."""
        _ctx = getattr(self, "_execution_context", None)
        _cap = getattr(_ctx, "exit_only_capability", None)
        if not isinstance(_cap, dict):
            return False, "EXIT_ONLY_CONTEXT_INVALID"
        _snap = self._capture_exit_only_snapshot()
        _ok, _reason = self._verify_exit_only_snapshot(_snap, _cap)
        if not _ok:
            return self._exit_only_renewal_fail(_reason)
        return True, None

    def _renew_exit_only_capability(self) -> tuple:
        """[auto re-reconciliation] read-only renewal of the CURRENT
        capability's freshness.  Success requires the exact same locked
        two legs / account / session / config / release and
        open_orders == [].  NEVER creates a new capability, never
        widens, never resumes LIVE/entry."""
        _ctx = getattr(self, "_execution_context", None)
        _cap = getattr(_ctx, "exit_only_capability", None)
        if not isinstance(_cap, dict):
            return False, "EXIT_ONLY_CONTEXT_INVALID"
        _snap = self._capture_exit_only_snapshot()
        _verify_ok, _verify_reason = self._verify_exit_only_snapshot(
            _snap, _cap)
        if not _verify_ok:
            # a transient query failure is DEGRADED (monitoring
            # continues); mismatch/open-order/session change quarantines
            return self._exit_only_renewal_fail(
                _verify_reason,
                quarantine=(_verify_reason
                            != "EXIT_ONLY_RENEWAL_QUERY_FAILED"))
        _legs = sorted(
            (str(l.get("symbol", "")), str(l.get("side", "")),
             int(l.get("remaining_qty", 0) or 0))
            for l in (_cap.get("legs") or []))
        _now_ms = int(time.time() * 1000)
        _count = int(self._read_exit_only_renewal_provenance().get(
            "renewal_count") or 0) + 1
        _prov = {
            "renewed_at_ms": _now_ms,
            "next_renewal_at_ms": (
                _now_ms + self.EXIT_ONLY_RENEWAL_INTERVAL_S * 1000),
            "attempted_at_ms": _now_ms,
            "snapshot_captured_at_ms": _snap.get("captured_at") or _now_ms,
            "snapshot_hash": _cap.get("snapshot_hash", ""),
            "reconciliation_id": _cap.get("reconciliation_id", ""),
            "identity": {k: _cap.get(k) for k in (
                "account_id_hash", "session_id", "config_hash",
                "release_sha")},
            "renewal_count": _count,
            "backoff_s": 0,
            "last_reason": None,
            "last_failed_at_ms": None,
            "status": "ACTIVE",
            "legs": [list(x) for x in _legs],
        }
        self._persist_exit_only_renewal_provenance(**_prov)
        return True, _prov

    def _maybe_renew_exit_only(self) -> None:
        """[auto re-reconciliation] tick-driven hook: renew the CURRENT
        capability's freshness on the 30s cadence (bounded backoff)."""
        _ctx = getattr(self, "_execution_context", None)
        if _ctx is None or getattr(
                _ctx, "effective_mode", "") != "reconciled_exit_only":
            return
        if not self._exit_only_renewal_due():
            return
        self._renew_exit_only_capability()

    def _observe_exit_only_bbo_evidence(self) -> None:
        """[P1 dashboard-gap] observation-only persistent BBO evidence.

        When the fresh valid dual BBO cache builds a version-2 binding
        for the CURRENT exit_only_capability (identical code/identity/
        15s-freshness/hash semantics as order binding — the same
        build_bbo_binding over the same _exit_only_bbo_slots, no
        duplicated quote validation), emit/refresh an
        EXIT_ONLY_BBO_OBSERVED event (bbo_hash + bbo_payload) so the
        presentation layer has identity-matched evidence after a fresh
        attestation.  Observation-only: NEVER authorizes, creates,
        submits, alters or cancels orders and NEVER changes strategy
        decisions.  Deduped by (reconciliation_id, bbo_hash): at most
        one event per distinct binding — never one per callback and no
        artificial periodic refresh.  Invalid/missing/stale BBO emits
        nothing; the Dashboard stays N/A."""
        try:
            _ctx = getattr(self, "_execution_context", None)
            _cap = getattr(_ctx, "exit_only_capability", None)
            if not isinstance(_cap, dict):
                return
            from core.exit_only_position import build_bbo_binding
            _binding, _reason = build_bbo_binding(
                self._exit_only_bbo_slots(),
                now_ms=int(time.time() * 1000),
                near_code=getattr(
                    getattr(self, "contract", None), "code", None),
                far_code=getattr(
                    getattr(self, "far_contract", None), "code", None),
                identity=_cap)
            if not isinstance(_binding, dict):
                return  # invalid/stale/missing: no false evidence
            _hash = _binding.get("bbo_hash") or ""
            _payload = _binding.get("bbo_payload")
            if not _hash or not isinstance(_payload, dict):
                return
            _rid = _cap.get("reconciliation_id", "")
            if (_rid, _hash) == getattr(
                    self, "_exit_only_bbo_observed_key", None):
                return  # dedupe: same binding, no re-emit
            self._exit_only_bbo_observed_key = (_rid, _hash)
            self._append_mts_event(
                "EXIT_ONLY_BBO_OBSERVED",
                bbo_hash=_hash,
                bbo_payload=_payload,
                reconciliation_id=_rid)
        except Exception:
            pass  # observation must never be fatal

    def _exit_only_bbo_slots(self):
        """[S2 repair] dedicated EXIT_ONLY BBO evidence only (written by
        validated on_bidask).  The generic tick market_data is NEVER read
        here: ticks carry last/close (no BBO) and must not satisfy the
        binding.  Tick-only => empty slots => BBO_MISSING."""
        _cache = getattr(self, "_exit_only_bbo_cache", None) or {}
        return {"near": _cache.get("near"), "far": _cache.get("far")}

    def _exit_only_position_has_position(self) -> bool:
        try:
            _p = _mts_position_state_path()
            if _p.exists():
                return bool(json.loads(_p.read_text())
                            .get("has_position", False))
        except Exception:
            pass
        return False

    def _hydrate_exit_only_position(self):
        """Hydrate the managed exit-only position from the active capability.

        Only when RECONCILED_EXIT_ONLY is effective; paper/normal live is
        untouched.  The position carries broker-attested costs + trade_id,
        never synthetic PnL.  Idempotent: never re-hydrates.
        """
        _ctx = getattr(self, "_execution_context", None)
        if getattr(_ctx, "effective_mode", None) != "reconciled_exit_only":
            return None
        _cap = getattr(_ctx, "exit_only_capability", None)
        _cached = getattr(self, "_exit_only_position", None)
        _cap_hash = (_cap or {}).get("snapshot_hash")
        if _cached is not None and _cached.get("snapshot_hash") == _cap_hash:
            return _cached
        try:
            from core.exit_only_position import hydrate_exit_only_position
            _position = hydrate_exit_only_position(_cap)
        except Exception as _exc:
            _code = getattr(_exc, "code", "EXIT_ONLY_CAPABILITY_INVALID")
            self._exit_only_position = None
            self._append_mts_event("EXIT_ONLY_HYDRATION_FAILED",
                                   reason=_code)
            return None
        self._exit_only_position = _position
        self._append_mts_event(
            "EXIT_ONLY_POSITION_HYDRATED",
            trade_id=_position["trade_id"],
            reconciliation_id=_position["reconciliation_id"],
            snapshot_hash=_position["snapshot_hash"],
            legs=_position["legs"],
        )
        return _position

    def _hydrate_strategy_position(self, strategy) -> bool:
        """Give the exit evaluation the broker-attested position attrs.

        legs[0] = near, legs[1] = far (canonical allowed_orders order).
        Sets open sides, qty, broker entry costs, trade_id and
        _has_position so the existing Policy J / combined / single
        release evaluation can run; entries stay blocked by the gate.
        """
        _position = getattr(self, "_exit_only_position", None)
        if _position is None or strategy is None:
            return False
        _legs = _position.get("legs") or []
        if len(_legs) != 2:
            return False
        _near, _far = _legs[0], _legs[1]
        _near_side = "SHORT" if _near["side"] == "sell" else "LONG"
        _far_side = "SHORT" if _far["side"] == "sell" else "LONG"
        for _attr, _value in (
                ("_near_side", _near_side), ("_far_side", _far_side),
                ("_near_qty", _near["quantity"]),
                ("_far_qty", _far["quantity"]),
                ("_near_entry", _near["avg_cost"]),
                ("_far_entry", _far["avg_cost"]),
                ("_trade_id", _position.get("trade_id")),
                ("_has_position", True),
                ("_reconciliation_id",
                 _position.get("reconciliation_id")),
                ("_snapshot_hash", _position.get("snapshot_hash"))):
            try:
                setattr(strategy, _attr, _value)
            except Exception:
                return False
        return True

    def _build_exit_only_authority(self, position):
        """[S1 repair] capability authority override for the pre/post
        signal gates: OPEN MtsAuthorityState from the attested legs."""
        _legs = position.get("legs") or []
        if len(_legs) != 2:
            return None
        _near, _far = _legs[0], _legs[1]
        from strategies.futures.mts_ledger_authority import (
            MtsAuthority, MtsAuthorityState)
        return MtsAuthorityState(
            status=MtsAuthority.OPEN,
            trade_id=position["trade_id"],
            near_qty=_near["quantity"] * (1 if _near["side"] == "buy" else -1),
            far_qty=_far["quantity"] * (1 if _far["side"] == "buy" else -1),
            near_side="LONG" if _near["side"] == "buy" else "SHORT",
            far_side="LONG" if _far["side"] == "buy" else "SHORT",
            near_entry=_near["avg_cost"],
            far_entry=_far["avg_cost"],
            current_trade_id=position["trade_id"],
        )

    def _validate_exit_only_position(self):
        """[S1 repair] ONE shared EXIT_ONLY capability validation.

        Called at the start of every _mts_tick (before ANY risk gate /
        strategy evaluation) and again by _submit_mts_order_signal (risk-
        gate direct submits consume the SAME rule set — no divergent
        copies).  Returns (valid, position, reason); on failure emits ONE
        typed EXIT_ONLY_HYDRATION_BLOCKED event and clears the authority
        override.  Non-EXIT_ONLY modes return (True, None, None) — Paper /
        LIVE_READY untouched, and any lingering override is cleared on a
        mode switch.
        """
        _ctx = getattr(self, "_execution_context", None)
        if getattr(_ctx, "effective_mode", None) != "reconciled_exit_only":
            self._exit_only_auth_override = None
            return True, None, None
        # cleared until validated: a stale previous-tick override must
        # never influence gates
        self._exit_only_auth_override = None
        _cap = getattr(_ctx, "exit_only_capability", None)
        if not isinstance(_cap, dict):
            self._append_mts_event("EXIT_ONLY_HYDRATION_BLOCKED",
                                   reason="EXIT_ONLY_CAPABILITY_MISSING")
            return False, None, "EXIT_ONLY_CAPABILITY_MISSING"
        _cap_session = _cap.get("session_id") or ""
        _cur_session = getattr(_ctx, "session_id", "") or ""
        if (not _cap_session or not _cur_session
                or _cap_session != _cur_session):
            self._append_mts_event("EXIT_ONLY_HYDRATION_BLOCKED",
                                   reason="EXIT_ONLY_SESSION_MISMATCH")
            return False, None, "EXIT_ONLY_SESSION_MISMATCH"
        _captured = _cap.get("snapshot_captured_at")
        _now_ms = int(time.time() * 1000)
        # [auto re-reconciliation] monitoring is NOT gated by snapshot
        # age: the capability scope is immutable and the ORDER safety
        # boundary is the synchronous pre-submit fresh broker
        # reconciliation.  Only a future-dated capability (corruption)
        # is rejected.
        if (not isinstance(_captured, int) or _captured > _now_ms + 1_000):
            self._append_mts_event("EXIT_ONLY_HYDRATION_BLOCKED",
                                   reason="EXIT_ONLY_SNAPSHOT_FUTURE")
            return False, None, "EXIT_ONLY_SNAPSHOT_FUTURE"
        _cached = getattr(self, "_exit_only_position", None)
        if (_cached is None
                or _cached.get("snapshot_hash")
                != _cap.get("snapshot_hash")):
            try:
                from core.exit_only_position import hydrate_exit_only_position
                _position = hydrate_exit_only_position(_cap)
            except Exception as _exc:
                _code = getattr(_exc, "code", "EXIT_ONLY_CAPABILITY_INVALID")
                self._exit_only_position = None
                self._append_mts_event("EXIT_ONLY_HYDRATION_BLOCKED",
                                       reason=_code)
                return False, None, _code
            self._exit_only_position = _position
            self._append_mts_event(
                "EXIT_ONLY_POSITION_HYDRATED",
                trade_id=_position["trade_id"],
                reconciliation_id=_position["reconciliation_id"],
                snapshot_hash=_position["snapshot_hash"],
                legs=_position["legs"],
            )
        _position = self._exit_only_position
        _legs = _position.get("legs") or []
        if len(_legs) != 2:
            self._append_mts_event("EXIT_ONLY_HYDRATION_BLOCKED",
                                   reason="EXIT_ONLY_LEG_COUNT_INVALID")
            return False, None, "EXIT_ONLY_LEG_COUNT_INVALID"
        _near_code = getattr(getattr(self, "contract", None), "code", None)
        _far_code = getattr(getattr(self, "far_contract", None), "code", None)
        if (_legs[0]["code"] != _near_code or _legs[1]["code"] != _far_code
                or _legs[0]["side"] == _legs[1]["side"]):
            self._append_mts_event("EXIT_ONLY_HYDRATION_BLOCKED",
                                   reason="EXIT_ONLY_LEG_MISMATCH")
            return False, None, "EXIT_ONLY_LEG_MISMATCH"
        return True, _position, None

    def _exit_only_pre_evaluation_hydration(self, strategy) -> bool:
        """[S1] EXIT_ONLY: build the strategy position from the validated
        capability/attested broker snapshot BEFORE every strategy
        evaluation (never inside submit).  Consumes the shared
        _validate_exit_only_position result (no duplicate rules); returns
        False (evaluator skipped) with one typed blocked event on any
        failure — zero strategy order submission.  Idempotent per snapshot
        hash; refreshes when the hash changes.  Normal LIVE/PAPER flow
        returns True untouched (never overwrites strategy state)."""
        _ctx = getattr(self, "_execution_context", None)
        if getattr(_ctx, "effective_mode", None) != "reconciled_exit_only":
            return True
        _ok, _position, _reason = self._validate_exit_only_position()
        if not _ok:
            return False
        _legs = _position["legs"]
        if getattr(self, "_exit_only_strategy_hydrated_hash", None) \
                != _position["snapshot_hash"]:
            if not self._hydrate_strategy_position(strategy):
                self._append_mts_event("EXIT_ONLY_HYDRATION_BLOCKED",
                                       reason="EXIT_ONLY_STRATEGY_HYDRATION_FAILED")
                return False
            self._exit_only_strategy_hydrated_hash = \
                _position["snapshot_hash"]
        self._exit_only_auth_override = \
            self._build_exit_only_authority(_position)
        return True


    def _exit_only_decision_guard(self, action, strategy=None):
        """Thin delegate to the S0 OrderIntentGateway policy."""
        from core.order_intent_gateway import OrderIntentGateway
        _gw = getattr(self, "_order_intent_gateway", None)
        if _gw is None:
            _gw = self._order_intent_gateway = OrderIntentGateway()
        _ctx = getattr(self, "_execution_context", None)
        self._exit_only_decision_binding = None
        if getattr(_ctx, "effective_mode", None) != "reconciled_exit_only":
            return True, None, None
        self._hydrate_exit_only_position()
        if strategy is not None:
            self._hydrate_strategy_position(strategy)
        _intent_strategy = ("MTS_ENTRY"
                           if action in ("BUY_NEAR_SELL_FAR",
                                          "SELL_NEAR_BUY_FAR")
                           else "MTS_EXIT" if action == "EXIT"
                           else "MTS_RELEASE")
        _ok, _binding, _reason = self._authorize_intent(
            action, _intent_strategy, strategy)
        return _ok, _binding, _reason

    def _process_live_upl_refresh_command(self) -> bool:
        """[dashboard refresh] consume commands/live_upl_refresh.json
        ONCE in the existing loop: re-capture the CURRENT-session
        read-only broker truth through the SAME authenticated api
        (list_positions / list_trades / margin; zero place/cancel/
        update) and re-persist the canonical artifact on success.
        Query failure records the typed status and leaves live UPL N/A
        (no canonical overwrite).  Paper mode: no-op — the paper ledger
        is its own truth.  Never creates a second Shioaji session."""
        from core.runtime_paths import runtime_path
        _path = Path(runtime_path("commands", "live_upl_refresh.json"))
        _processing = Path(str(_path) + ".processing")
        if not _path.exists() and not _processing.exists():
            return False
        if _path.exists():
            try:
                _path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(_path, _processing)
            except OSError:
                return False
        try:
            _raw = json.loads(_processing.read_text(encoding="utf-8"))
        except Exception:
            _raw = None
        try:
            _processing.unlink(missing_ok=True)
        except OSError:
            pass
        if not isinstance(_raw, dict):
            return False
        _allowed = {"command_id", "action", "created_at"}
        if (not isinstance(_raw.get("command_id"), str)
                or not _raw.get("command_id")
                or set(_raw) - _allowed
                or _raw.get("action") != "LIVE_UPL_REFRESH"):
            return False
        _ctx = getattr(self, "_execution_context", None)
        _mode = getattr(_ctx, "effective_mode", "")
        if str(_mode).startswith("paper"):
            return True  # paper no-op: the paper ledger is its own truth
        _snap = self._capture_post_startup_snapshot()
        if ((_snap.get("fetch_status") or {}).get("capture") != "OK"):
            return True  # typed failure — live UPL stays N/A
        self._persist_current_session_canonical(_snap)
        return True

    def _process_reconciled_exit_attestation_command(self) -> bool:
        """[EXIT_ONLY flow removed 2026-08-14] the operator
        attestation flow no longer exists as an execution mode —
        the command is NEVER consumed and no EXIT_ONLY capability
        can be authorized through it (fail-closed: a legacy pending
        command stays untouched)."""
        return False
    def _clear_mts_entry_reconcile_intents(self, trade_id: str) -> bool:
        """Resolve only the incident intent bound to the reconciled trade.

        This is called only after the broker proves the account flat.  A
        failure stays quarantined; it never re-opens ordinary MTS entry.
        """
        try:
            from core.exit_intent import IntentLog as _MTSIntentLog
            _ilog = _MTSIntentLog(_mts_intent_log_dir())
            for _iid in _ilog.list_active():
                _row = _ilog.get(_iid)
                if (_row.get("reason") == "MTS_ENTRY_RECONCILE"
                        and _row.get("trade_id") == trade_id):
                    _ilog.mark_terminal(_iid, "RECONCILED")
            return True
        except Exception:
            return False

    def _maybe_finalize_exit_only_reconciliation(self) -> tuple:
        """Turn two confirmed closing fills plus broker-flat proof into a
        quarantined, re-certifiable context.

        This method deliberately never calls ``transition_with_certificate``.
        A local callback is not broker reconciliation and cannot resume MTS
        entry.  Any partial/cancel/reject/expiry becomes quarantine without
        canceling or reissuing either broker leg.
        """
        from core.mode_transition import ModeTransitionState, with_effective_mode
        from core.reconciled_exit import (
            AttestationError, capability_exit_completed,
            revoke_exit_only_after_flat_snapshot,
        )

        _ctx = getattr(self, "_execution_context", None)
        _cap = getattr(_ctx, "exit_only_capability", None)
        if (_ctx is None
                or getattr(_ctx, "effective_mode", None)
                != ModeTransitionState.RECONCILED_EXIT_ONLY.value
                or not isinstance(_cap, dict)):
            return None, None
        _orders = list(getattr(getattr(self, "order_mgr", None),
                               "active_orders", {}).values()) + list(
                                   getattr(getattr(self, "order_mgr", None),
                                           "completed", []))
        _rid = _cap.get("reconciliation_id")
        _matching = [o for o in _orders
                     if getattr(o, "reconciliation_id", None) == _rid]
        _terminal_bad = {"partial_filled", "cancelled", "rejected", "expired"}
        if any(str(getattr(getattr(o, "status", None), "value",
                           getattr(o, "status", ""))).lower() in _terminal_bad
               for o in _matching):
            _reason = "EXIT_ONLY_TERMINAL_AMBIGUITY"
            self._execution_context = with_effective_mode(
                _ctx, ModeTransitionState.LIVE_QUARANTINED.value,
                live_order_allowed=False, audit_reasons=(_reason,))
            self._persist_execution_context()
            self._append_mts_event(_reason, reconciliation_id=_rid)
            return None, _reason
        if not capability_exit_completed(_ctx, _matching):
            return None, None
        try:
            _next, _record = revoke_exit_only_after_flat_snapshot(
                _ctx, self._capture_exit_only_snapshot())
        except AttestationError as _exc:
            _reason = _exc.code
            self._execution_context = with_effective_mode(
                _ctx, ModeTransitionState.LIVE_QUARANTINED.value,
                live_order_allowed=False, audit_reasons=(_reason,))
            self._persist_execution_context()
            self._append_mts_event("EXIT_ONLY_RECONCILE_FAILED",
                                   reconciliation_id=_rid, reason=_reason)
            return None, _reason
        self._execution_context = _next
        if not self._clear_mts_entry_reconcile_intents(_cap.get("trade_id", "")):
            self._execution_context = with_effective_mode(
                self._execution_context,
                ModeTransitionState.LIVE_QUARANTINED.value,
                live_order_allowed=False,
                audit_reasons=("EXIT_ONLY_FLAT_RECONCILED",
                               "MTS_ENTRY_RECONCILE_PENDING"))
            self._persist_execution_context()
            self._append_mts_event("EXIT_ONLY_RECONCILE_FAILED",
                                   reconciliation_id=_rid,
                                   reason="MTS_ENTRY_RECONCILE_PENDING")
            return None, "MTS_ENTRY_RECONCILE_PENDING"
        self._persist_execution_context()
        self._append_mts_event("EXIT_ONLY_FLAT_RECONCILED", **_record)
        return _record, None


    def _pending_mts_entry_reconcile(self) -> bool:
        """True when a partial MTS entry needs broker reconciliation."""
        return self._pending_reconcile_intent("MTS_ENTRY_RECONCILE")

    def _pending_mts_exit_reconcile(self) -> bool:
        """True when a combined-exit leg failure needs broker
        reconciliation (restart read — the canonical exit_intent log)."""
        return self._pending_reconcile_intent("MTS_EXIT_RECONCILE")

    def _pending_reconcile_reason(self):
        """Return the canonical reason that must block re-certification."""
        if self._pending_safety_stop_reconcile():
            return "SAFETY_STOP_RECONCILE_PENDING"
        if self._pending_mts_entry_reconcile():
            return "MTS_ENTRY_RECONCILE_PENDING"
        if self._pending_mts_exit_reconcile():
            return "MTS_EXIT_RECONCILE_PENDING"
        return None

    def _apply_reconcile_pending_gate(self) -> None:
        """[orphan reconciliation] a pending SAFETY_STOP_RECONCILE intent
        keeps the context QUARANTINED (SAFETY_STOP_RECONCILE_PENDING,
        persisted) until the broker state reconciles — LIVE is never
        retained across a pending reconciliation."""
        _ctx = getattr(self, "_execution_context", None)
        _pending_reason = self._pending_reconcile_reason()
        if _ctx is None or _pending_reason is None:
            return
        from core.mode_transition import ModeTransitionState, with_effective_mode
        self._execution_context = with_effective_mode(
            _ctx, ModeTransitionState.LIVE_QUARANTINED.value,
            live_order_allowed=False,
            audit_reasons=(_pending_reason,) + tuple(
                getattr(_ctx, "audit_reasons", ()) or ()))
        self._persist_execution_context()

    def _bind_session_generation(self) -> None:
        """[post_startup session gate; D1] the session_registry creates the
        registry-bound generation at login — bind it into the (QUARANTINED)
        ctx.session_id BEFORE certification/post_startup gate. This avoids
        the circular LIVE_READY -> session -> gate -> LIVE_READY loop: the
        gate validates generation/session/snapshot consistency on the
        quarantined ctx, and only a passing post_startup gate + cert flow
        may transition to LIVE_READY. Standalone account-hash identity
        NEVER satisfies the gate. No generation -> no binding (fail-closed).
        """
        try:
            _api = getattr(self, "api", None)
            _ctx = getattr(self, "_execution_context", None)
            if _api is None or _ctx is None:
                return
            from core.live_route_certificate import session_registry
            _gen = session_registry.generation(_api)
            if not _gen:
                return
            import dataclasses
            if getattr(_ctx, "session_id", None) != _gen:
                self._execution_context = dataclasses.replace(
                    _ctx, session_id=str(_gen))
                self._persist_execution_context()
        except Exception:
            return

    def _confirm_session_generation(self) -> bool:
        """[D1 race guard] re-confirm the registry-bound generation right
        BEFORE the LIVE_READY transition: a logout/relogin between the
        binding and the certification invalidates the old generation —
        the transition must NOT promote with a stale session binding.
        Returns True only if the ctx.session_id still matches the CURRENT
        registry generation for this api."""
        try:
            _api = getattr(self, "api", None)
            _ctx = getattr(self, "_execution_context", None)
            if _api is None or _ctx is None:
                return False
            from core.live_route_certificate import session_registry
            _gen = session_registry.generation(_api)
            return bool(_gen) and str(_ctx.session_id) == str(_gen)
        except Exception:
            return False

    # ── [P0 post-startup gate] in-process, unavoidable ─────────────────────

    @staticmethod
    def _normalize_snapshot_positions(raw_positions, account_tag="futures") \
            -> list:
        """Normalize shioaji positions into the canonical snapshot shape
        (account/code/quantity/direction); NEVER raw account numbers."""
        out = []
        for p in raw_positions or []:
            try:
                _px = getattr(p, "price", None)
                _pnl = getattr(p, "pnl", None)
                _row = {
                    "account": account_tag,
                    "code": str(getattr(p, "code", "")),
                    "quantity": int(getattr(p, "quantity", 0) or 0),
                    "direction": getattr(getattr(p, "direction", None),
                                         "name", None)
                        or str(getattr(p, "direction", "")),
                }
                if _px is not None:
                    _row["avg_cost"] = float(_px)
                if _pnl is not None:
                    _row["pnl"] = float(_pnl)
                out.append(_row)
            except Exception:
                continue
        return out

    @staticmethod
    def _stable_broker_trade_id(account_identity_hash, legs) -> str:
        """Return a restart-stable identity for one broker-held spread.

        The live snapshot hash includes capture-time data (and therefore
        changes on every refresh).  It is evidence identity, not position
        identity.  Position identity is deliberately limited to the account
        and the exact broker legs/directions/quantities so a fresh snapshot
        cannot mint a new trade on every tick.
        """
        _legs = []
        for _leg in legs or ():
            if not isinstance(_leg, dict):
                continue
            _legs.append({
                "code": str(_leg.get("code") or ""),
                "direction": str(_leg.get("direction") or "").lower(),
                "quantity": int(_leg.get("quantity") or 0),
            })
        _legs.sort(key=lambda item: item["code"])
        _payload = json.dumps({
            "account_identity_hash": str(account_identity_hash or ""),
            "legs": _legs,
        }, sort_keys=True, separators=(",", ":"))
        return "broker-reconciled-" + hashlib.sha256(
            _payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _normalize_snapshot_orders(raw_trades) -> list:
        """Open orders only: terminal states are dropped (same canonical
        terminal-state set as the standalone preflight); the shape keeps
        order_id/code/status so the gate can identify pending orders."""
        terminal = {"Filled", "Canceled", "Cancelled", "Failed",
                    "PartiallyFailed"}
        out = []
        for t in raw_trades or []:
            _order = getattr(t, "order", None)
            _contract = getattr(t, "contract", None) or getattr(
                _order, "contract", None)
            st = getattr(t, "status", None)
            _inner = getattr(st, "status", None)
            name = (getattr(_inner, "name", None)
                    or getattr(_inner, "value", None)
                    or (str(_inner) if _inner is not None else None)
                    or getattr(st, "name", None)
                    or getattr(st, "value", None)
                    or str(st))
            name = str(name).split(".")[-1]
            if name in terminal:
                continue
            row = {
                "order_id": str(getattr(_order, "id", "") or
                                  getattr(t, "id", "")),
                "code": str(getattr(t, "code", "")
                             or getattr(_contract, "code", "")),
                "status": name,
            }
            # Preserve the historical shape when SDK identity fields are not
            # present, while retaining every broker identity that is actually
            # supplied by Shioaji for watchdog matching.
            for key, value in (
                    ("broker_order_id", getattr(t, "id", None)
                     or getattr(t, "broker_order_id", None)
                     or getattr(_order, "id", None)),
                    ("ordno", getattr(t, "ordno", None)
                     or getattr(_order, "ordno", None)),
                    ("seqno", getattr(t, "seqno", None)
                     or getattr(_order, "seqno", None))):
                if value not in (None, ""):
                    row[key] = str(value)
            _action = (getattr(t, "action", None)
                       or getattr(getattr(t, "order", None), "action", None))
            _direction = (getattr(_action, "name", None)
                          or getattr(_action, "value", None)
                          or (_action if isinstance(_action, str) else None))
            if str(_direction or "").lower() in {"buy", "sell"}:
                row["direction"] = str(_direction).lower()
            _qty = getattr(t, "quantity", None)
            if _qty is not None:
                row["qty"] = _qty
            out.append(row)
        return out

    def _watchdog_broker_truth(self, order):
        """Protect timeout decisions with a fresh broker read.

        A local watchdog timeout is never broker evidence.  Before any local
        cancel/expire transition, require a successful canonical capture and
        check the exact broker identity or a unique matching futures position.
        Capture failure is conservative: retain the order and record the
        unavailable read rather than guessing that it is unfilled.
        """
        try:
            snapshot = self._capture_post_startup_snapshot()
        except Exception as exc:
            snapshot = {"fetch_status": {"capture": "FAIL"},
                        "errors": {"capture": type(exc).__name__}}
        capture_ok = ((snapshot.get("fetch_status") or {}).get("capture")
                      == "OK")
        if not capture_ok:
            reason = "BROKER_QUERY_UNAVAILABLE"
            try:
                self._append_mts_event("WATCHDOG_BROKER_QUERY_UNAVAILABLE",
                                       order_id=order.order_id,
                                       reason=reason)
            except Exception:
                pass
            return {"protect": True, "reason": reason, "snapshot": snapshot}

        def _ids(row):
            return {str(row.get(key)) for key in
                    ("order_id", "broker_order_id", "ordno", "seqno")
                    if row.get(key) not in (None, "", "None")}

        order_ids = {str(value) for value in (
            getattr(order, "order_id", None),
            getattr(order, "exchange_order_id", None),
            getattr(order, "broker_order_id", None),
            getattr(order, "ordno", None),
            getattr(order, "seqno", None),
        ) if value not in (None, "", "None")}
        open_match = any(order_ids & _ids(row)
                         for row in (snapshot.get("open_orders") or [])
                         if isinstance(row, dict))

        side = str(getattr(getattr(order, "side", None), "value", "")
                   or getattr(getattr(order, "side", None), "name", "")
                   or "").lower()
        side_aliases = {"buy": {"buy", "long"},
                        "sell": {"sell", "short"}}
        code = str(getattr(order, "symbol", "") or "")
        try:
            qty = int(getattr(order, "quantity", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        position_match = False
        for row in snapshot.get("positions") or []:
            if not isinstance(row, dict) or str(row.get("code") or "") != code:
                continue
            try:
                row_qty = abs(int(row.get("quantity") or 0))
            except (TypeError, ValueError):
                continue
            direction = str(row.get("direction") or "").lower().split(".")[-1]
            if row_qty >= max(qty, 1) and direction in side_aliases.get(side, {side}):
                position_match = True
                break

        if open_match or position_match:
            reason = "BROKER_HAS_POSITION_OR_ORDER"
            try:
                self._append_mts_event("WATCHDOG_BROKER_HAS_POSITION_OR_ORDER",
                                       order_id=order.order_id,
                                       reason=reason,
                                       broker_order_id=getattr(order, "broker_order_id", None),
                                       symbol=code)
            except Exception:
                pass
            return {"protect": True, "reason": reason, "snapshot": snapshot,
                    "open_match": open_match, "position_match": position_match}
        # A successful empty read is still not an explicit broker terminal
        # receipt.  Keep the local order pending; session-close reconciliation
        # or a later Cancelled/Failed receipt owns the terminal transition.
        reason = "BROKER_NO_POSITION_OR_ORDER"
        try:
            self._append_mts_event("WATCHDOG_BROKER_NO_POSITION_OR_ORDER",
                                   order_id=order.order_id, reason=reason)
        except Exception:
            pass
        return {"protect": True, "reason": reason, "snapshot": snapshot}

    def _restore_terminal_watchdog_order(self, order, truth):
        """Move a locally-terminal order back to broker-pending state.

        This is a local reconciliation only.  It never submits or cancels;
        the broker's identity remains the sole identity and the order stays
        pending until an explicit broker terminal receipt arrives.
        """
        if not truth.get("protect") or order is None:
            return False
        manager = getattr(self, "order_mgr", None)
        restore = getattr(manager, "restore_pending_from_broker_truth", None)
        if not callable(restore):
            return False
        return bool(restore(order.order_id, reason=truth.get("reason", ""),
                            source="live_broker_watchdog"))

    @staticmethod
    def _normalize_snapshot_trades(raw_trades) -> list:
        """Return JSON-safe terminal broker receipts for local backfill.

        ``list_trades`` is the broker's authoritative read path when a live
        callback was missed.  Keep only identity, terminal status and fill
        facts; never persist the SDK object (which can contain account/CA
        details or enum values that are not JSON serializable).
        """
        out = []
        for trade in raw_trades or []:
            broker_order = getattr(trade, "order", None)
            status = getattr(trade, "status", None)
            nested = getattr(status, "status", None)
            raw_status = nested if nested is not None else status
            if isinstance(raw_status, dict):
                raw_status = raw_status.get("status")
            status_name = (getattr(raw_status, "name", None)
                           or getattr(raw_status, "value", None)
                           or str(raw_status or ""))
            status_name = str(status_name).split(".")[-1]
            _trade_action = getattr(trade, "action", None) or getattr(
                broker_order, "action", None)
            _direction = (getattr(_trade_action, "name", None)
                          or getattr(_trade_action, "value", None)
                          or (_trade_action if isinstance(_trade_action, str)
                              else None))
            row = {
                "id": getattr(trade, "id", None)
                      or getattr(trade, "broker_order_id", None)
                      or getattr(trade, "exchange_order_id", None)
                      or getattr(broker_order, "id", None),
                "broker_order_id": getattr(trade, "broker_order_id", None)
                                   or getattr(broker_order, "id", None),
                "ordno": getattr(trade, "ordno", None)
                        or getattr(broker_order, "ordno", None),
                "seqno": getattr(trade, "seqno", None)
                        or getattr(broker_order, "seqno", None),
                "code": getattr(trade, "code", None),
                "direction": (str(_direction).lower()
                              if _direction is not None else None),
                "status": status_name,
                "price": getattr(status, "price", None)
                         or getattr(trade, "price", None),
                "avg_price": getattr(status, "avg_price", None)
                             or getattr(trade, "avg_price", None),
                "quantity": getattr(status, "quantity", None)
                            or getattr(trade, "quantity", None),
                "filled_quantity": getattr(status, "filled_quantity", None)
                                   or getattr(trade, "filled_quantity", None),
                "ts": getattr(trade, "ts", None) or getattr(status, "ts", None),
                "deals": [],
            }
            deals = getattr(status, "deals", None) or getattr(trade, "deals", None) or []
            for deal in deals if isinstance(deals, (list, tuple)) else []:
                if isinstance(deal, dict):
                    out_deal = dict(deal)
                else:
                    out_deal = {
                        key: getattr(deal, key, None)
                        for key in ("deal_id", "trade_id", "fill_id",
                                    "exchange_fill_id", "exchange_seq",
                                    "price", "avg_price", "quantity", "qty",
                                    "ordno")
                    }
                row["deals"].append(out_deal)
            out.append(row)
        return out

    @staticmethod
    def _normalize_order_deal_records(raw_records) -> list:
        """Normalize Shioaji ``order_deal_records`` terminal deal rows.

        ``list_trades()`` is an active-order view and can be empty after a
        fill.  Shioaji's historical receipt API returns nested
        ``(OrderState, OrderEventDict)`` tuples; only FuturesDeal/FDEAL rows
        are terminal fill evidence.  FuturesOrder/FORDER rows are deliberately
        ignored here and remain the responsibility of the active-order path.
        """
        out = []
        for item in raw_records or []:
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            state, payload = item[0], item[1]
            _name = getattr(state, "name", None)
            _value = getattr(state, "value", None)
            if not (_name == "FuturesDeal" or _value == "FDEAL"
                    or str(_name or _value or "").split(".")[-1] == "FuturesDeal"):
                continue
            if not isinstance(payload, dict):
                continue
            _broker_id = (payload.get("trade_id") or payload.get("id")
                          or payload.get("broker_order_id"))
            _price = payload.get("price")
            _qty = payload.get("quantity") or payload.get("qty")
            if not _broker_id or _price is None or not _qty:
                continue
            _code = str(payload.get("full_code") or payload.get("code") or "")
            _delivery = str(payload.get("delivery_month") or "")
            # Shioaji may return the product code plus a numeric delivery
            # month rather than the monitor's canonical TMFH6/TMFI6 code.
            if _code and _delivery.isdigit() and len(_delivery) == 6:
                _month = int(_delivery[-2:])
                if _month in range(1, 13) and _code == "TMF":
                    _code = f"{_code}{chr(64 + _month)}{_delivery[-3]}"
            _deal = {
                "trade_id": str(_broker_id),
                "broker_trade_id": str(_broker_id),
                "exchange_fill_id": str(_broker_id),
                "exchange_seq": payload.get("exchange_seq"),
                "price": _price,
                "quantity": _qty,
                "ordno": payload.get("ordno"),
                "deal_ts": payload.get("ts"),
            }
            out.append({
                "id": str(_broker_id),
                "broker_order_id": str(_broker_id),
                "ordno": payload.get("ordno"),
                "seqno": payload.get("seqno"),
                "code": _code,
                "delivery_month": _delivery,
                "direction": payload.get("action"),
                "status": "Filled",
                "price": _price,
                "quantity": _qty,
                "filled_quantity": _qty,
                "trade_id": str(_broker_id),
                "ts": payload.get("ts"),
                "deals": [_deal],
            })
        return out

    @staticmethod
    def _normalize_order_state_records(raw_records) -> list:
        """Normalize Shioaji ``order_deal_records`` FORDER rows into
        trade-shaped terminal ORDER receipts.

        FDEAL rows are fills (fills ledger); FORDER rows carry the order's
        own nested status (``payload['status'].status``) plus optional
        deals.  The nested status is authoritative for the terminal ORDER
        state; deals embedded in the order snapshot feed the same
        identity-deduped fill path.  A row without an order identity is
        skipped (never a synthetic order).
        """
        out = []
        for item in raw_records or []:
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            state, payload = item[0], item[1]
            _name = getattr(state, "name", None)
            _value = getattr(state, "value", None)
            if not (_name == "FuturesOrder" or _value == "FORDER"
                    or str(_name or _value or "").split(".")[-1]
                    == "FuturesOrder"):
                continue
            if not isinstance(payload, dict):
                continue
            _order = payload.get("order")
            if not isinstance(_order, dict):
                _order = payload
            _broker_id = (_order.get("id") or _order.get("broker_order_id")
                          or payload.get("trade_id") or payload.get("id"))
            if not _broker_id:
                continue
            # nested order status: payload["status"] may be an OrderState
            # object (status.status -> enum) or a dict with its own status
            _st = payload.get("status")
            _inner = getattr(_st, "status", None)
            if isinstance(_inner, dict):
                _inner = _inner.get("status")
            _raw = (_inner if _inner is not None
                    else (_st.get("status") if isinstance(_st, dict) else _st))
            _status_name = (getattr(_raw, "name", None)
                            or getattr(_raw, "value", None)
                            or (_raw if isinstance(_raw, str) else None)
                            or str(_raw or ""))
            _status_name = str(_status_name).split(".")[-1]
            _contract = payload.get("contract")
            if not isinstance(_contract, dict):
                _contract = {}
            _code = str(payload.get("full_code")
                        or _contract.get("code") or payload.get("code") or "")
            _delivery = str(_contract.get("delivery_month")
                            or payload.get("delivery_month") or "")
            if _code and _delivery.isdigit() and len(_delivery) == 6:
                _month = int(_delivery[-2:])
                if _month in range(1, 13) and _code == "TMF":
                    _code = f"{_code}{chr(64 + _month)}{_delivery[-3]}"
            _price = (payload.get("price") or payload.get("avg_price")
                      or _order.get("price") or _order.get("avg_price"))
            _qty = (payload.get("quantity") or payload.get("filled_quantity")
                    or _order.get("quantity") or _order.get("filled_quantity"))
            _action = payload.get("action") or _order.get("action")
            _deals = payload.get("deals")
            _normalized_deals = []
            for _d in _deals if isinstance(_deals, list) else []:
                if not isinstance(_d, dict):
                    continue
                _did = (_d.get("deal_id") or _d.get("trade_id")
                        or _d.get("fill_id"))
                if not _did:
                    continue
                _dp = _d.get("price") or _d.get("avg_price")
                _dq = _d.get("quantity") or _d.get("qty")
                if _dp is None or not _dq:
                    continue
                _normalized_deals.append({
                    "deal_id": str(_did),
                    "trade_id": str(_did),
                    "broker_trade_id": str(_did),
                    "exchange_fill_id": str(_did),
                    "exchange_seq": _d.get("exchange_seq"),
                    "price": _dp,
                    "quantity": _dq,
                    "ordno": _d.get("ordno") or _order.get("ordno"),
                })
            out.append({
                "id": str(_broker_id),
                "broker_order_id": str(_broker_id),
                "ordno": _order.get("ordno"),
                "seqno": _order.get("seqno"),
                "code": _code,
                "direction": _action,
                "status": _status_name or "Unknown",
                "price": _price,
                "avg_price": _price,
                "quantity": _qty,
                "filled_quantity": _qty,
                "ts": payload.get("ts"),
                "deals": _normalized_deals,
            })
        return out

    def _capture_post_startup_snapshot(self) -> dict:
        """[P0 post-startup gate] fresh READ-ONLY capture from the SAME
        authenticated api/session (no new login): futures positions +
        open orders + margin + the bound registry generation as
        session_id, with a canonical epoch-ms INT + canonical input
        hash. Uses the shioaji 1.x read methods exactly like the
        standalone preflight: api.list_positions(account) /
        api.list_trades(account) / api.margin(futopt_account) with
        available_margin (fallback deposit_balance). Zero
        place/cancel/update calls; capture errors are recorded in the
        payload (the gate fails closed)."""
        import hashlib as _hl
        import json as _json
        captured_at = int(time.time() * 1000)
        _api = getattr(self, "api", None)
        _ctx = getattr(self, "_execution_context", None)
        session_id = getattr(_ctx, "session_id", None) or None
        positions, open_orders, broker_trades, margin = [], [], [], None
        acct_hash = None
        errors = {}
        try:
            _acct = getattr(_api, "futopt_account", None)
            if _acct is not None:
                _aid = getattr(_acct, "account_id", None) \
                    or getattr(_acct, "account_no", "") or ""
                if _aid:
                    acct_hash = _hl.sha256(str(_aid).encode()).hexdigest()
            # positions: list_positions per account (stock + futures)
            if hasattr(_api, "list_positions"):
                for _tag, _acct in (("stock", getattr(_api, "stock_account",
                                                      None)),
                                    ("futures", getattr(
                                        _api, "futopt_account", None))):
                    if _acct is None:
                        continue
                    try:
                        try:
                            _rows = _api.list_positions(account=_acct)
                        except TypeError:
                            # Some Shioaji-compatible adapters expose the
                            # account as a positional-only parameter.  Keep
                            # the same compatibility contract as the
                            # standalone preflight rather than marking a
                            # valid capture as failed.
                            _rows = _api.list_positions(_acct)
                    except Exception as exc:
                        errors[f"positions:{_tag}"] = \
                            f"{type(exc).__name__}: {exc}"
                        _rows = []
                    positions.extend(
                        self._normalize_snapshot_positions(_rows, _tag))
            else:
                errors["capture"] = "api has no list_positions"
            # open orders: list_trades, drop terminal states
            if hasattr(_api, "list_trades"):
                for _tag, _acct in (("stock", getattr(_api, "stock_account",
                                                      None)),
                                    ("futures", getattr(
                                        _api, "futopt_account", None))):
                    if _acct is None:
                        continue
                    try:
                        try:
                            # Shioaji 1.7's no-argument stream is the current
                            # broker order view.  The account-scoped overload
                            # can retain stale PendingSubmit rows on Mini;
                            # prefer no-arg and use account only for SDKs that
                            # reject the no-argument form.
                            _rows = _api.list_trades() or []
                        except TypeError:
                            try:
                                _rows = _api.list_trades(account=_acct) or []
                            except TypeError:
                                _rows = _api.list_trades(_acct) or []
                    except Exception as exc:
                        errors[f"open_orders:{_tag}"] = \
                            f"{type(exc).__name__}: {exc}"
                        _rows = []
                    open_orders.extend(self._normalize_snapshot_orders(_rows))
                    broker_trades.extend(self._normalize_snapshot_trades(_rows))
            else:
                errors["capture"] = "api has no list_trades"
            # Historical terminal receipts are separate from list_trades.
            # Reconcile them by broker identity, but never treat them as open
            # orders.  A missing/failed history query is observable and keeps
            # the existing active-order evidence path intact.
            _futopt_acct = getattr(_api, "futopt_account", None)
            if hasattr(_api, "order_deal_records") and _futopt_acct is not None:
                try:
                    try:
                        _deal_rows = _api.order_deal_records(
                            account=_futopt_acct)
                    except TypeError:
                        _deal_rows = _api.order_deal_records()
                    broker_trades.extend(
                        self._normalize_order_deal_records(_deal_rows))
                    # FORDER rows: nested ORDER status receipts (terminal
                    # order state + optional deals) — reconciled beside
                    # FDEAL fill receipts (P0-B, no callback-only).
                    broker_trades.extend(
                        self._normalize_order_state_records(_deal_rows))
                except Exception as exc:
                    errors["order_deal_records"] = (
                        f"{type(exc).__name__}: {exc}")
            # margin: api.margin(futopt_account) — the authenticated
            # futures margin (NOT an attribute on the account object)
            if hasattr(_api, "margin") and _acct is not None:
                try:
                    try:
                        _m = _api.margin(account=_acct)
                    except TypeError:
                        _m = _api.margin(_acct)
                    _avail = getattr(_m, "available_margin", None)
                    if _avail is None:
                        _avail = getattr(_m, "deposit_balance", None)
                    margin = float(_avail) if _avail is not None else None
                except Exception as exc:
                    errors["margin"] = f"{type(exc).__name__}: {exc}"
            else:
                errors["margin"] = "api has no margin / no futopt account"
            if _acct is None:
                errors["capture"] = "no futopt account available"
        except Exception as exc:
            errors["capture"] = f"{type(exc).__name__}: {exc}"
        try:
            from core.live_broker_preflight import _position_covered_orders
            _covered_positions = [
                {"code": row.get("code"), "qty": row.get("quantity"),
                 "direction": str(row.get("direction") or "").lower()
                              .split(".")[-1]}
                for row in positions if isinstance(row, dict)
                and row.get("account") == "futures"]
            open_orders, covered = _position_covered_orders(
                _covered_positions, open_orders)
        except Exception:
            covered = []
        payload = {
            "source": "live_broker",
            "mode": "live",
            "scope": "futopt",
            "account_identity_hash": acct_hash,
            "session_id": session_id,
            "captured_at": captured_at,
            "positions": positions,
            "open_orders": open_orders,
            "position_covered_orders": covered,
            "broker_trades": broker_trades,
            "available_margin": margin,
            "canonical_input_hash": None,
            "fetch_status": {"capture": "OK" if not errors else "FAIL"},
            "errors": errors,
        }
        blob = _json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           default=str)
        payload["canonical_input_hash"] = _hl.sha256(blob.encode()).hexdigest()
        # A successful snapshot is the authoritative boundary for repairing
        # local lifecycle rows.  This also covers startup/watchdog captures
        # where no strategy tick has run yet.
        try:
            self._reconcile_local_orders_from_snapshot(payload)
        except Exception:
            pass
        return payload

    def _reconcile_local_orders_from_snapshot(self, snapshot) -> int:
        """Backfill local order state from broker terminal receipts.

        Exact broker identity matching is delegated to OrderManager; an
        unmatched receipt must never create a synthetic MTS order.  This
        path is read-only with respect to the broker and only repairs the
        local ledger/export after a successful canonical snapshot.
        """
        if not snapshot or (snapshot.get("source") != "live_broker"):
            return 0
        if ((snapshot.get("fetch_status") or {}).get("capture") != "OK"):
            return 0
        manager = getattr(self, "order_mgr", None)
        if manager is None or not hasattr(manager, "reconcile_broker_state"):
            return 0
        try:
            result = manager.reconcile_broker_state(
                filled_trades=snapshot.get("broker_trades") or [],
                source="live_broker_reconcile",
                reason="callback_gap_snapshot",
            )
        except ValueError as _fill_ve:
            _oid = "unknown"
            _m = re.search(r"for\s+(\S+)", str(_fill_ve))
            if _m:
                _oid = _m.group(1)
            self._emit_fill_rejected(order_id=_oid, reason=str(_fill_ve))
            result = {"reconciled": []}
        reconciled = result.get("reconciled") or []
        position_result = manager.reconcile_position_covered_orders(
            snapshot.get("positions") or [],
            captured_at=snapshot.get("captured_at"),
        )
        changed = sum(1 for item in reconciled
                      if item.get("fills_added") or item.get("action") == "reconciled")
        changed += len(position_result.get("reconciled") or [])
        if changed and hasattr(self, "_save_orders_file_wrapper"):
            self._save_orders_file_wrapper()
        # P0-B: broker-confirmed MTS release fills must close the
        # lifecycle even without the streaming callback.  Best-effort;
        # exactly-once via sync_release SINGLE_LEG guard + fills ledger.
        try:
            self._close_mts_lifecycle_from_reconciled_fills(
                result, manager, snapshot)
        except Exception:
            pass
        return changed


    def _lookup_reconciled_order(self, manager, order_id):
        """Resolve a reconciled order from active or completed collections."""
        if not order_id or manager is None:
            return None
        try:
            _o = manager.get_order(order_id)
            if _o is not None:
                return _o
        except Exception:
            pass
        for _o in (getattr(manager, "completed", None) or []):
            if getattr(_o, "order_id", None) == order_id:
                return _o
        return None

    def _close_mts_lifecycle_from_reconciled_fills(self, result, manager,
                                                   snapshot) -> int:
        """[P0-B] Broker-confirmed fills must close the MTS lifecycle even
        when the streaming callback was missed (no callback-only
        assumption).

        ``manager.reconcile_broker_state`` already applied identity-deduped
        fills (fills_added > 0).  Only a FULLY FILLED MTS release order
        (OrderStatus.FILLED and filled_quantity >= quantity) is
        authoritative fill evidence: advance the strategy to SINGLE_LEG via
        ``sync_release`` exactly once — its SINGLE_LEG phase guard plus the
        fills-ledger RELEASE row make repeats no-ops.  Partial fills stay
        pending (no early lifecycle advance).  Never synthesizes an
        order or a leg; a failed capture never reaches here (the caller
        returns before reconciling).  Also never resends/cancels: this is
        strictly read-only broker evidence consumption.
        """
        _strat = getattr(self, "_mts_strategy", None)
        if _strat is None:
            try:
                _strat = getattr(self, "_registry", None).get("tmf_spread")
            except Exception:
                _strat = None
        if _strat is None or not hasattr(_strat, "sync_release"):
            return 0
        _near_code = str(getattr(
            getattr(self, "contract", None), "code", "") or "")
        _far_code = str(getattr(
            getattr(self, "far_contract", None), "code", "") or "")
        _closed = 0
        for _r in (result.get("reconciled") or []):
            if not _r.get("fills_added"):
                continue
            _order = self._lookup_reconciled_order(
                manager, _r.get("order_id"))
            if _order is None:
                continue
            if str(getattr(_order, "strategy", "")) != "MTS_RELEASE":
                continue
            # Terminal-fill gate (P0): only a fully FILLED order may close
            # the release lifecycle.  A partial fill keeps the order
            # pending — never an early single-leg release, never a hidden
            # remaining quantity, never a premature trail.
            from core.order_management.order import OrderStatus
            if getattr(_order, "status", None) is not OrderStatus.FILLED:
                continue
            try:
                _filled_qty = int(getattr(_order, "filled_quantity", 0) or 0)
                _order_qty = int(getattr(_order, "quantity", 0) or 0)
            except (TypeError, ValueError):
                continue
            if _filled_qty < _order_qty:
                continue
            _symbol = str(getattr(_order, "symbol", "") or "")
            if _symbol == _near_code:
                _leg = "near"
            elif _symbol == _far_code:
                _leg = "far"
            else:
                continue
            _fills = getattr(_order, "fills", None) or []
            if not _fills:
                continue
            _fill = _fills[-1]
            try:
                _fill_price = float(getattr(_fill, "fill_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if _fill_price <= 0:
                continue
            # remaining-leg price: same fallback chain as the release-fill
            # callback (market data first, then the strategy's own entry)
            _rem_price = 0.0
            try:
                _md = getattr(self, "market_data", None) or {}
                _rem_key = (f"{self.ticker}_FAR" if _leg == "near"
                            else f"{self.ticker}_NEAR")
                _rem_price = float(
                    (_md.get(_rem_key, {}) or {}).get("close") or 0.0)
            except Exception:
                _rem_price = 0.0
            if _rem_price <= 0:
                _rem_price = float(getattr(
                    _strat,
                    "_far_entry" if _leg == "near" else "_near_entry",
                    0.0) or 0.0)
            _ev_time = (getattr(_fill, "fill_time", None)
                        or getattr(_fill, "timestamp", None))
            try:
                _strat.sync_release(
                    leg=_leg, price=_rem_price,
                    release_price=_fill_price,
                    order_id=_r.get("order_id"),
                    event_time=_ev_time)
                _closed += 1
            except Exception:
                continue
        return _closed

    def _emit_release_telemetry(self, signal: str, strategy, bar_dict: dict) -> None:
        """Emit RELEASE_CONDITION_MET when the strategy signals a release/exit.

        Best-effort telemetry: the release condition was met by the strategy;
        this fires before the submit attempt so a later gate rejection is
        distinguishable from a silent non-evaluation.  Never affects orders.
        """
        try:
            _release_signals = ("RELEASE_NEAR", "RELEASE_FAR", "EXIT",
                                "COMBINED_EXIT_NEAR", "COMBINED_EXIT_FAR")
            if signal not in _release_signals:
                return
            _st = {}
            try:
                _st = getattr(strategy, "state", None) or {}
            except Exception:
                pass
            self._append_mts_event(
                "RELEASE_CONDITION_MET",
                signal=signal,
                trade_id=(getattr(strategy, "_trade_id", None)
                          or getattr(strategy, "trade_id", None)),
                release_stop=(_st.get("release_stop_points")
                              if isinstance(_st, dict) else None),
            )
        except Exception:
            pass

    def _resolve_mts_local_position_qty(self):
        """Resolve the strategy's local position quantities.

        Sources in order: the strategy reference (self._mts_strategy, then the
        strategy registry), then the persisted MTS position state file.
        Returns (None, None) when the local position is unknown -- callers
        must not emit divergence claims without a position source.
        """
        _ms = getattr(self, "_mts_strategy", None)
        if _ms is None:
            try:
                _ms = getattr(self, "_registry", None).get("tmf_spread")
            except Exception:
                _ms = None
        if _ms is None:
            for _s in (getattr(self, "_strategies", {}) or {}).values():
                _ms = _s
                break
        if _ms is not None:
            try:
                return (int(getattr(_ms, "_near_qty", 0) or 0),
                        int(getattr(_ms, "_far_qty", 0) or 0))
            except (TypeError, ValueError):
                return None, None
        try:
            with open(_mts_position_state_path(), "r", encoding="utf-8") as _f:
                _st = json.load(_f)
            if _st.get("has_position"):
                return 1, 1
            return 0, 0
        except Exception:
            return None, None

    def _emit_release_eval_skip_no_local_position(self, snapshot) -> None:
        """Emit RELEASE_EVAL_SKIP_NO_LOCAL_POSITION when broker holds TMF legs
        but the strategy sees no local position (fills gap).  Rate-limited to
        30s.  Fail-closed: when the local position is unknown (no strategy
        reference, no persisted state) no event is emitted.
        """
        try:
            _legs = [p for p in (snapshot.get("positions") or [])
                     if isinstance(p, dict)
                     and str(p.get("code") or "") in ("TMFH6", "TMFI6")
                     and int(p.get("quantity") or 0) > 0]
            if not _legs:
                return
            _nq, _fq = self._resolve_mts_local_position_qty()
            if _nq is None and _fq is None:
                return  # unknown -- fail-closed, no divergence claim
            if _nq > 0 or _fq > 0:
                return
            _now = time.monotonic()
            if _now - getattr(self, "_release_eval_skip_last_emit", 0.0) < 30.0:
                return
            self._release_eval_skip_last_emit = _now
            self._append_mts_event(
                "RELEASE_EVAL_SKIP_NO_LOCAL_POSITION",
                broker_legs=[f"{p.get('code')}:{p.get('direction')}:{p.get('quantity')}"
                             for p in _legs],
                local_near_qty=_nq,
                local_far_qty=_fq,
            )
        except Exception:
            pass

    def _emit_fill_rejected(self, order_id: str, reason: str) -> None:
        """Emit FILL_REJECTED_REMAINING when a broker fill receipt is rejected
        locally (remaining=0).  Rate-limited to 30s.
        """
        try:
            _now = time.monotonic()
            if _now - getattr(self, "_fill_rejected_last_emit", 0.0) < 30.0:
                return
            self._fill_rejected_last_emit = _now
            self._append_mts_event("FILL_REJECTED_REMAINING",
                                   order_id=order_id, reason=reason)
        except Exception:
            pass

    def _persist_current_session_canonical(self, snapshot) -> None:
        """[P0] persist the CURRENT-session canonical artifact so the
        dashboard live UPL reconciles against this runtime session
        (never the standalone preflight's request id).  Telemetry-only:
        capture failures / missing session never overwrite the artifact
        and never block the gate."""
        try:
            from core.runtime_paths import runtime_path
            if not (snapshot or {}).get("session_id"):
                return
            if ((snapshot.get("fetch_status") or {})
                    .get("capture") != "OK"):
                return
            _diag = Path(runtime_path("exports", "trades", "live",
                                      "diagnostics"))
            _diag.mkdir(parents=True, exist_ok=True)
            _canon = _diag / "broker_snapshot_canonical.json"
            _tmp = _canon.with_name(_canon.name + ".tmp")
            _tmp.write_text(json.dumps(snapshot, ensure_ascii=False,
                                       default=str), encoding="utf-8")
            os.replace(_tmp, _canon)
        except Exception:
            pass  # never block the post-startup gate on telemetry

    def _write_live_session_upl(self, positions, ctx) -> None:
        """The trading-system session's read-only list_positions().pnl,
        written for the dashboard (which NEVER opens its own Shioaji
        session).  Best-effort; absence only leaves the dashboard N/A."""
        try:
            _legs = {}
            for p in positions or []:
                _code = str(p.get("code") or "")
                if _code.startswith("TMF"):
                    # Per-field safe conversion: missing/non-numeric values
                    # become None (reader renders N/A) — the artifact is
                    # ALWAYS written fresh so a stale artifact can never
                    # serve old UPL during the freshness window.
                    def _fnum(v):
                        if v is None:
                            return None
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            return None

                    _legs[_code] = {
                        "direction": str(p.get("direction") or ""),
                        "quantity": int(p.get("quantity") or 0),
                        "avg_cost": (_fnum(p.get("avg_cost"))
                                     if _fnum(p.get("avg_cost")) is not None
                                     else _fnum(p.get("avg_price"))),
                        "pnl": _fnum(p.get("pnl")),
                    }
            _payload = {
                "source": "live_broker_session",
                "session_id": getattr(ctx, "session_id", None),
                "captured_at": int(time.time() * 1000),
                "legs": _legs,
                "total_pnl": sum(float(l.get("pnl") or 0)
                                 for l in _legs.values()),
            }
            from core.runtime_paths import runtime_path
            from pathlib import Path
            _p = Path(runtime_path("exports", "trades", "live",
                                   "diagnostics", "live_session_upl.json"))
            _p.parent.mkdir(parents=True, exist_ok=True)
            _p.write_text(json.dumps(_payload, default=str))
        except Exception:
            pass

    @staticmethod
    def _to_epoch_ms(value):
        """Normalize a timestamp to epoch milliseconds (decision domain).

        Accepts epoch seconds (<1e11) or milliseconds (>=1e11) as int/float,
        ISO-8601 strings, and datetime objects.  Returns None when the input
        is missing or untrustworthy (0/NaN/parse failure) so callers stay
        fail-closed.
        """
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, datetime):
            try:
                return value.timestamp() * 1000.0
            except Exception:
                return None
        if isinstance(value, (int, float)):
            _v = float(value)
            if _v <= 0 or _v != _v:
                return None
            return _v * 1000.0 if _v < 1e11 else _v
        if isinstance(value, str):
            _s = value.strip()
            try:
                _f = float(_s)
                if _f > 0 and _f == _f:
                    return _f * 1000.0 if _f < 1e11 else _f
            except ValueError:
                pass
            try:
                return (datetime.fromisoformat(
                    _s.replace("Z", "+00:00")).timestamp() * 1000.0)
            except Exception:
                return None
        return None

    def _resolve_broker_hydration_entry_epoch(self, snapshot, legs):
        """Resolve a broker-observed position's entry time (epoch ms),
        bound to the CURRENT position generation.

        A candidate deal/trade must match the position's code, direction,
        quantity and — when the position carries an avg_cost — price
        within 1% of that cost basis; a candidate MISSING a binding field
        cannot bind (no wildcards).  Only terminal fill evidence counts
        (Filled/PartiallyFilled status or explicit deals — a
        PendingSubmit order row is order state, never a fill).  The
        earliest matching timestamp is the entry time.  Without a cost
        basis, multiple same-code/same-direction candidates cannot be
        told apart (old generation vs current) and fail closed to None.
        Falls back to the durable local fills ledger (ENTRY rows, same
        binding), then the persisted state-file entry clock — only when
        the state legs match the current generation (role + side +
        cost).  None = no trustworthy anchor — fail-closed: Policy J
        stays suppressed while release/ATR/emergency exits keep working.
        """

        def _norm_side(text):
            text = str(text or "").lower()
            if "sell" in text or "short" in text:
                return "SHORT"
            if "buy" in text or "long" in text:
                return "LONG"
            return None

        def _row_side(row):
            return _norm_side(row.get("direction") or row.get("side"))

        def _ts(row):
            return self._to_epoch_ms(
                row.get("ts") or row.get("deal_ts") or row.get("timestamp"))

        def _terminal_fill_evidence(row):
            """Fill evidence only: fills-ledger rows, Filled/PartiallyFilled
            statuses, or rows carrying explicit deals.  A PendingSubmit /
            Submitted / Canceled order row is order state, never a fill."""
            if row.get("fill_type"):
                return True
            _st = str(row.get("status") or "").lower()
            if "fill" in _st:
                return True
            if row.get("deals"):
                return True
            return False

        def _matches(row, code, side, qty, avg):
            if not _terminal_fill_evidence(row):
                return False
            if str(row.get("code") or row.get("contract") or "") != code:
                return False
            if _row_side(row) != side:
                return False
            # Fail-closed: a candidate missing a binding field cannot be
            # proven to belong to the current generation — no wildcards.
            _rq = (row.get("quantity") or row.get("filled_quantity")
                   or row.get("qty"))
            if qty is not None:
                if _rq is None:
                    return False
                try:
                    if int(_rq) != int(qty):
                        return False
                except (TypeError, ValueError):
                    return False
            _rp = row.get("price") or row.get("avg_price")
            if avg and avg > 0:
                if _rp is None:
                    return False
                try:
                    if abs(float(_rp) - float(avg)) / float(avg) > 0.01:
                        return False
                except (TypeError, ValueError, ZeroDivisionError):
                    return False
            return True

        _best = None
        _ambiguous = False
        for _leg in legs or ():
            _code = str(_leg.get("code") or "")
            _side = _norm_side(_leg.get("direction"))
            if not _code or _side is None:
                continue
            try:
                _qty = int(_leg.get("quantity"))
            except (TypeError, ValueError):
                _qty = None
            try:
                _avg = float(_leg.get("avg_cost") or 0.0)
            except (TypeError, ValueError):
                _avg = 0.0
            _cands = [
                _row for _row in (snapshot.get("broker_trades") or [])
                if isinstance(_row, dict)
                and _matches(_row, _code, _side, _qty, _avg)]
            if not _cands:
                continue
            if _avg <= 0 and len(_cands) > 1:
                # no cost basis -> cannot distinguish old generation from
                # the current one; never anchor on a guess
                _ambiguous = True
                continue
            for _row in _cands:
                _ms = _ts(_row)
                if _ms and (_best is None or _ms < _best):
                    _best = _ms
        if _best is None and not _ambiguous:
            try:
                _rt = os.getenv("TRADING_RUNTIME_DIR")
                _fills = os.getenv("MTS_FILL_LOG_PATH") or (
                    os.path.join(_rt, "logs", "mts_trade_fills.jsonl")
                    if _rt else "logs/mts_trade_fills.jsonl")
                if os.path.exists(_fills):
                    _fill_rows = []
                    with open(_fills, "r", encoding="utf-8") as _f:
                        for _line in _f:
                            try:
                                _r = json.loads(_line)
                            except Exception:
                                continue
                            if str(_r.get("fill_type") or "") == "ENTRY":
                                _fill_rows.append(_r)
                    for _leg in legs or ():
                        _code = str(_leg.get("code") or "")
                        _side = _norm_side(_leg.get("direction"))
                        if not _code or _side is None:
                            continue
                        try:
                            _qty = int(_leg.get("quantity"))
                        except (TypeError, ValueError):
                            _qty = None
                        try:
                            _avg = float(_leg.get("avg_cost") or 0.0)
                        except (TypeError, ValueError):
                            _avg = 0.0
                        _cands = [_row for _row in _fill_rows
                                  if _matches(_row, _code, _side, _qty, _avg)]
                        if not _cands:
                            continue
                        if _avg <= 0 and len(_cands) > 1:
                            _ambiguous = True
                            continue
                        for _row in _cands:
                            _ms = _ts(_row)
                            if _ms and (_best is None or _ms < _best):
                                _best = _ms
            except Exception:
                pass
        if _best is None:
            try:
                with open(_mts_position_state_path(), "r",
                          encoding="utf-8") as _f:
                    _st = json.load(_f)
                if _st.get("has_position"):
                    # The persisted clock is usable only when the state file
                    # describes the CURRENT broker generation: every position
                    # leg maps to a near/far role whose side matches and
                    # whose entry cost matches the broker avg_cost (1%).
                    _near_code = str(getattr(
                        getattr(self, "contract", None), "code", "") or "")
                    _far_code = str(getattr(
                        getattr(self, "far_contract", None), "code", "") or "")
                    _gen_ok = True
                    for _leg in legs or ():
                        _code = str(_leg.get("code") or "")
                        if _code == _near_code:
                            _role = "near"
                        elif _code == _far_code:
                            _role = "far"
                        else:
                            _gen_ok = False
                            break
                        _pos_side = _norm_side(_leg.get("direction"))
                        _st_side = _norm_side(_st.get(f"{_role}_side"))
                        if _pos_side is None or _st_side != _pos_side:
                            _gen_ok = False
                            break
                        try:
                            _avg = float(_leg.get("avg_cost") or 0.0)
                        except (TypeError, ValueError):
                            _avg = 0.0
                        _st_entry = _st.get(f"{_role}_entry")
                        if _avg > 0:
                            if _st_entry is None:
                                _gen_ok = False
                                break
                            try:
                                if (abs(float(_st_entry) - _avg) / _avg
                                        > 0.01):
                                    _gen_ok = False
                                    break
                            except (TypeError, ValueError, ZeroDivisionError):
                                _gen_ok = False
                                break
                    if _gen_ok:
                        _ms = self._to_epoch_ms(
                            _st.get("entry_guard_start_ms")
                            or _st.get("entry_ts_ms") or _st.get("entry_ts"))
                        if _ms:
                            _best = _ms
            except Exception:
                pass
        return _best

    def _refresh_live_broker_authority(self, strategy):
        """Use the current authenticated broker snapshot as LIVE MTS truth.

        Callbacks and local fills are telemetry; a missing FDEAL must not
        make an actually-held spread appear FLAT.  This is read-only and is
        never used in PAPER or legacy EXIT_ONLY mode.
        """
        # LIVE detection MUST use the ctx requested_mode (the live INTENT,
        # set at ctx creation): the config's live_trading key is absent in
        # futures_live.yaml (monitor defaults to False), and effective_mode
        # is not yet LIVE_READY at startup (the certificate transition
        # happens later).  An effective_mode/live_trading-keyed guard returns
        # None on the first ticks, falling back to the stale fills-ledger
        # authority and resurrecting a ghost (reason=authority_rebuild).
        if (getattr(getattr(self, "_execution_context", None),
                    "requested_mode", "") != "live"
                or strategy is None):
            return None
        _now = time.monotonic()
        if _now - getattr(self, "_live_broker_authority_at", 0.0) < 5.0:
            return getattr(self, "_live_broker_authority", None)
        _snap = self._capture_post_startup_snapshot()
        if ((_snap.get("fetch_status") or {}).get("capture") != "OK"):
            # An unavailable snapshot is unknown, not flat.  Revoke any
            # earlier flat proof and keep entry fail-closed; do not mutate
            # the strategy's known-open local state.
            self._live_broker_flat_proven = False
            self._broker_authority_degraded = True
            self._broker_position_observed = True
            self._live_broker_authority = None
            self._live_broker_authority_at = _now
            return None
        try:
            self._reconcile_local_orders_from_snapshot(_snap)
        except Exception as _reconcile_exc:
            # Local receipt replay is best-effort and cannot override a
            # successful broker snapshot.  In particular, a duplicate FDEAL
            # must not conceal a proven broker-flat account.
            logger.warning(
                "[LIVE_BROKER_AUTHORITY] local reconcile failed: %s",
                _reconcile_exc,
            )
        _has_open_orders = bool(_snap.get("open_orders"))
        # open orders leave the lifecycle unresolved for ENTRY, but they
        # must NOT block the exit authority: when the snapshot shows both
        # spread legs, the OPEN authority is still built below so release
        # / trail evaluation keeps working.  open_orders keep new entries
        # blocked via _broker_position_observed and the pending gate.
        self._persist_current_session_canonical(_snap)
        _codes = {str(getattr(self.contract, "code", "")),
                  str(getattr(self.far_contract, "code", ""))}
        from strategies.futures.mts_ledger_authority import (
            MtsAuthority, MtsAuthorityState)
        _rows = [p for p in (_snap.get("positions") or [])
                 if p.get("account") == "futures"
                 and p.get("code") in _codes
                 and type(p.get("quantity")) is int
                 and p.get("quantity", 0) > 0]
        _other_futures = [p for p in (_snap.get("positions") or [])
                          if p.get("account") == "futures"
                          and p.get("code") not in _codes
                          and type(p.get("quantity")) is int
                          and p.get("quantity", 0) > 0]
        self._write_live_session_upl(
            _rows, getattr(self, "_execution_context", None))
        if not _rows:
            if _other_futures:
                # A non-target futures position means the account is not
                # provably flat for this MTS process.  Do not silently turn
                # an unknown contract into FLAT authority.
                self._broker_position_observed = True
                self._live_broker_flat_proven = False
                self._broker_authority_degraded = True
                strategy._broker_truth_flat = False
                self._live_broker_authority = None
                self._live_broker_authority_at = _now
                return None
            if _has_open_orders:
                # Stale PendingSubmit rows are session-cache residue only
                # when every open order is explained one-to-one by a local
                # FILLED entry (identity + symbol + direction + qty).  Any
                # unmatched/ambiguous pending keeps the unresolved verdict.
                _covered_flat = False
                try:
                    from core.broker_evidence import \
                        open_orders_fully_covered_by_filled
                    _om = getattr(self, "order_mgr", None)
                    _filled_rows = []
                    if _om is not None:
                        _filled_rows = [
                            _o.to_dict() for _o in _om.completed
                            if hasattr(_o, "to_dict")]
                    _covered_flat = open_orders_fully_covered_by_filled(
                        _snap.get("open_orders") or [], _filled_rows)
                except Exception:
                    _covered_flat = False  # fail-closed: keep unresolved
                if not _covered_flat:
                    # open orders with NO positions: unresolved, never flat.
                    self._broker_position_observed = True
                    self._live_broker_flat_proven = False
                    self._broker_authority_degraded = True
                    strategy._broker_truth_flat = False
                    self._live_broker_authority = None
                    self._live_broker_authority_at = _now
                    return None
                # All open orders are covered by explicit local fills: the
                # broker has no position and no unexplained pending.  Fall
                # through to the authoritative flat evidence below.
            # A successful, empty futures snapshot is authoritative flat
            # evidence.  Capture failures return above and must not clear the
            # marker, because an unknown broker state is not flat evidence.
            self._broker_position_observed = False
            self._live_broker_flat_proven = True
            self._broker_authority_degraded = False
            strategy._broker_truth_flat = True
            self._live_broker_authority = MtsAuthorityState(
                status=MtsAuthority.FLAT,
                trade_id=None,
                near_qty=0,
                far_qty=0,
                near_side=None,
                far_side=None,
                current_trade_id=None)
            self._live_broker_authority_at = _now
            return self._live_broker_authority
        def _side(row):
            text = str(row.get("direction") or "").lower()
            if "sell" in text or "short" in text:
                return "SHORT"
            if "buy" in text or "long" in text:
                return "LONG"
            return None

        _snapshot_hash = _snap.get("canonical_input_hash")
        _trade_id = self._stable_broker_trade_id(
            _snap.get("account_identity_hash"), _rows)

        if len(_rows) == 1:
            # A broker-verified remaining leg is an exit-only authority.  Do
            # not synthesize the missing leg or treat this as FLAT: the
            # strategy must continue trailing/releasing the real position,
            # while broker_position_observed keeps all new entries blocked.
            _row = _rows[0]
            _code = str(_row.get("code") or "")
            _qty = _row.get("quantity")
            _side_value = _side(_row)
            try:
                _entry = float(_row.get("avg_cost"))
            except (TypeError, ValueError):
                _entry = 0.0
            if (_code not in _codes or type(_qty) is not int or _qty <= 0
                    or _side_value is None or _entry <= 0):
                self._broker_position_observed = True
                self._live_broker_flat_proven = False
                self._broker_authority_degraded = True
                strategy._broker_truth_flat = False
                self._live_broker_authority = None
                self._live_broker_authority_at = _now
                self._emit_release_eval_skip_no_local_position(_snap)
                return None
            _is_near = _code == str(getattr(self.contract, "code", ""))
            _released_leg = "far" if _is_near else "near"
            for _name, _value in (
                    ("_near_side", _side_value if _is_near else None),
                    ("_far_side", None if _is_near else _side_value),
                    ("_near_qty", _qty if _is_near else 0),
                    ("_far_qty", 0 if _is_near else _qty),
                    ("_near_entry", _entry if _is_near else 0.0),
                    ("_far_entry", 0.0 if _is_near else _entry),
                    ("_trade_id", _trade_id), ("_has_position", True),
                    ("_released_leg", _released_leg),
                    ("_lifecycle", "SINGLE_LEG"),
                    ("_snapshot_hash", _snapshot_hash)):
                setattr(strategy, _name, _value)
            # P0-A: broker-truth entry-time hydration — Policy J guard
            # clock must never stay ENTRY_TIME_MISSING for a broker-observed
            # position.  Broker deal/trade timestamps are truth; durable
            # local fills / state file are fallbacks; None stays fail-closed.
            _entry_ms = self._resolve_broker_hydration_entry_epoch(_snap, _rows)
            if _entry_ms:
                strategy._entry_guard_start_ms = _entry_ms
                strategy._entry_ts_ms = _entry_ms
                try:
                    strategy._entry_ts = datetime.fromtimestamp(_entry_ms / 1000.0)
                except Exception:
                    strategy._entry_ts = None
            else:
                # Fail-closed: a broker-valid position with no trustworthy
                # anchor must not inherit a stale clock from a previous
                # trade (that would bypass ENTRY_TIME_MISSING and corrupt
                # the Policy-J guard clock).
                strategy._entry_guard_start_ms = None
                strategy._entry_ts_ms = None
                strategy._entry_ts = None
            self._broker_position_observed = True
            self._live_broker_flat_proven = False
            self._broker_authority_degraded = False
            strategy._broker_truth_flat = False
            _auth = MtsAuthorityState(
                status=MtsAuthority.SINGLE_LEG, trade_id=_trade_id,
                near_qty=_qty if _is_near and _side_value == "LONG" else
                         (-_qty if _is_near else 0),
                far_qty=_qty if (not _is_near and _side_value == "LONG") else
                        (-_qty if not _is_near else 0),
                near_side=_side_value if _is_near else None,
                far_side=_side_value if not _is_near else None,
                near_entry=_entry if _is_near else 0.0,
                far_entry=_entry if not _is_near else 0.0,
                current_trade_id=_trade_id)
            self._live_broker_authority = _auth
            self._live_broker_authority_at = _now
            return _auth

        if len({p.get("code") for p in _rows}) != 2:
            # More than one malformed/duplicate relevant row is ambiguous;
            # it is not safe to infer either a spread or a single leg.
            self._broker_position_observed = True
            self._live_broker_flat_proven = False
            self._broker_authority_degraded = True
            strategy._broker_truth_flat = False
            self._live_broker_authority = None
            self._live_broker_authority_at = _now
            self._emit_release_eval_skip_no_local_position(_snap)
            return None
        _by_code = {p["code"]: p for p in _rows}
        _near = _by_code.get(str(getattr(self.contract, "code", "")))
        _far = _by_code.get(str(getattr(self.far_contract, "code", "")))
        _near_side, _far_side = _side(_near), _side(_far)
        if _near_side is None or _far_side is None:
            # Relevant broker rows exist but are ambiguous.  Keep entry
            # blocked until a subsequent successful snapshot proves flat or
            # provides two unambiguous legs.
            self._broker_position_observed = True
            self._live_broker_flat_proven = False
            self._broker_authority_degraded = True
            strategy._broker_truth_flat = False
            self._live_broker_authority = None
            self._live_broker_authority_at = _now
            self._emit_release_eval_skip_no_local_position(_snap)
            return None
        self._broker_position_observed = True
        self._live_broker_flat_proven = False
        self._broker_authority_degraded = False
        strategy._broker_truth_flat = False
        try:
            _near_cost = float(_near.get("avg_cost") or 0.0)
        except (TypeError, ValueError):
            _near_cost = 0.0
        try:
            _far_cost = float(_far.get("avg_cost") or 0.0)
        except (TypeError, ValueError):
            _far_cost = 0.0
        for _name, _value in (
                ("_near_side", _near_side), ("_far_side", _far_side),
                ("_near_qty", _near["quantity"]),
                ("_far_qty", _far["quantity"]),
                ("_near_entry", _near_cost),
                ("_far_entry", _far_cost),
                ("_trade_id", _trade_id), ("_has_position", True),
                ("_snapshot_hash", _snapshot_hash)):
            setattr(strategy, _name, _value)
        # P0-A: broker-truth entry-time hydration (see single-leg branch).
        _entry_ms = self._resolve_broker_hydration_entry_epoch(_snap, _rows)
        if _entry_ms:
            strategy._entry_guard_start_ms = _entry_ms
            strategy._entry_ts_ms = _entry_ms
            try:
                strategy._entry_ts = datetime.fromtimestamp(_entry_ms / 1000.0)
            except Exception:
                strategy._entry_ts = None
        else:
            # Fail-closed: a broker-valid position with no trustworthy
            # anchor must not inherit a stale clock from a previous
            # trade (that would bypass ENTRY_TIME_MISSING and corrupt
            # the Policy-J guard clock).
            strategy._entry_guard_start_ms = None
            strategy._entry_ts_ms = None
            strategy._entry_ts = None
        _auth = MtsAuthorityState(
            status=MtsAuthority.OPEN, trade_id=_trade_id,
            near_qty=-_near["quantity"] if _near_side == "SHORT" else _near["quantity"],
            far_qty=-_far["quantity"] if _far_side == "SHORT" else _far["quantity"],
            near_side=_near_side, far_side=_far_side,
            near_entry=float(_near.get("avg_cost") or 0.0),
            far_entry=float(_far.get("avg_cost") or 0.0),
            current_trade_id=_trade_id)
        self._live_broker_authority = _auth
        self._live_broker_authority_at = _now
        return _auth

    def _finalize_local_orders_at_session_close(self) -> int:
        """Mirror broker session close locally without issuing cancellations.

        The broker removes unfilled orders at the session boundary.  First
        prove that no futures order remains open; only then expire/cancel the
        corresponding local active records.  Filled history is retained.
        """
        manager = getattr(self, "order_mgr", None)
        if manager is None or getattr(self, "dry_run", False):
            return 0
        session_key = datetime.now().strftime("%Y%m%d")
        if getattr(self, "_session_close_finalized_for", None) == session_key:
            return 0
        if getattr(self, "live_trading", False):
            snapshot = self._capture_post_startup_snapshot()
            if ((snapshot.get("fetch_status") or {}).get("capture") != "OK"):
                return 0
            codes = {str(getattr(getattr(self, "contract", None), "code", "")),
                     str(getattr(getattr(self, "far_contract", None), "code", ""))}
            if any((row.get("code") in codes or not row.get("code"))
                   for row in (snapshot.get("open_orders") or [])):
                return 0
        finalized = manager.finalize_session_orders(
            source="session_close_reconcile",
            reason="BROKER_SESSION_CLOSED_UNFILLED",
        )
        if finalized and hasattr(self, "_save_orders_file_wrapper"):
            self._save_orders_file_wrapper()
        self._session_close_finalized_for = session_key
        return finalized

    def _run_post_startup_gate(self):
        """[P0 post-startup gate] the in-process, UNAVOIDABLE gate run
        BEFORE any transition_with_certificate / LIVE_READY (startup AND
        reconnect-recertify). Fresh read-only snapshot from the SAME
        authenticated api/session; the evidence is archived under the
        runtime logs; then the core gate (check_deployment) runs with
        phase=post_startup + the bound generation. NO operator CLI
        subprocess — nothing skippable. Returns (gate, evidence_path)."""
        _pending_reason = self._pending_reconcile_reason()
        if _pending_reason is not None:
            from core.deployment_safety_gate import (
                DeploymentCheck, GuardResult)
            return DeploymentCheck(
                ok=False,
                results=(GuardResult(
                    guard="reconciliation", ok=False,
                    reasons=(_pending_reason,)),)), None
        snapshot = self._capture_post_startup_snapshot()
        self._persist_current_session_canonical(snapshot)
        _ev = None
        try:
            from core.runtime_paths import runtime_path
            _ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            _ev = Path(runtime_path("logs", f"post_startup_{_ts}.json"))
            _ev.parent.mkdir(parents=True, exist_ok=True)
            _ev.write_text(json.dumps(snapshot, ensure_ascii=False,
                                      default=str), encoding="utf-8")
        except Exception:
            _ev = None
        try:
            from core.deployment_safety_gate import check_deployment
            import hashlib as _hl
            _prof_hash = ""
            try:
                if os.path.exists(self.config_path):
                    _prof_hash = _hl.sha256(
                        Path(self.config_path).read_bytes()).hexdigest()
            except Exception:
                _prof_hash = ""
            _gen = None
            try:
                from core.live_route_certificate import session_registry
                _gen = session_registry.generation(getattr(self, "api", None))
            except Exception:
                _gen = None
            _closure = [
                "config/futures.yaml", "config/futures_live.yaml",
                "core/execution_context_state.py", "core/release_identity.py",
                "main.py", "strategies/futures/monitor.py",
                "core/deployment_safety_gate.py",
            ]
            # [P0 fix] the canonical manifest paths + exclude semantics
            # come from the DEPLOYED release_dir and match the production
            # CLI EXACTLY — a worktree/dir change must not lose the
            # manifest guard, and the exclude keeps the exclude-self tree
            # identity stable (recording the freeze must not invalidate
            # itself -> GUARD_MANIFEST_STALE)
            _rel_dir = str(Path(__file__).resolve().parents[2])
            _manifests = [
                os.path.join(_rel_dir, "PHASE1_RC_CANDIDATE.md"),
                os.path.join(_rel_dir, "PHASE2_DEPLOYMENT_MANIFEST.md"),
                os.path.join(_rel_dir, "PHASE1_FINAL_FREEZE.md"),
            ]
            _exclude = [
                "PHASE1_RC_CANDIDATE.md", "PHASE2_DEPLOYMENT_MANIFEST.md",
                "PHASE1_FINAL_FREEZE.md",
            ]
            # [P0 fix] the fresh snapshot's available_margin VALUE must be
            # passed explicitly (guard_margin requires margin_available;
            # the evidence alone was present but the guard received None)
            _margin_available = None
            try:
                _mv = snapshot.get("available_margin")
                if _mv is not None:
                    _margin_available = float(_mv)
            except (TypeError, ValueError):
                _margin_available = None
            gate = check_deployment(
                release_dir=_rel_dir,
                closure_files=_closure,
                runtime_dir=None,
                pid_file=os.environ.get("PM2_PID_FILE",
                                        "/tmp/trading-unified.pid"),
                position_state_path=str(_ev) if _ev is not None else None,
                margin_available=_margin_available,
                margin_evidence=snapshot,
                session_generation=_gen, session_revoked=False,
                config_profile_path=self.config_path,
                config_profile_hash=_prof_hash,
                manifest_paths=_manifests,
                manifest_exclude_paths=_exclude,
                expected_sha=os.environ.get("LRC_RELEASE_SHA", ""),
                phase="post_startup",
                allow_existing_mts_position=True)
            return gate, _ev
        except Exception:
            from core.deployment_safety_gate import (
                DeploymentCheck, GuardResult)
            _g = GuardResult(guard="post_startup", ok=False,
                             reasons=("POST_STARTUP_GATE_CRASHED",))
            _c = DeploymentCheck(ok=False, results=(_g,))
            return _c, _ev

    def _place_safety_stop(self, entry_price, direction, lots, stop_loss_pts):
        """Place a far-limit order at exchange as safety stop for disconnect protection."""
        if not self.live_trading or self.dry_run or not self.contract or not self.api:
            return
        # [Live wiring Step 2] execution-context gate: QUARANTINED/PREFLIGHT
        # makes ZERO place_order calls and returns a structured blocked
        # reason (audit); only LIVE_READY reaches the broker.
        _ctx = getattr(self, "_execution_context", None)
        if _ctx is not None and not _ctx.is_live_ready():
            return {"blocked": True, "reason": "LIVE_QUARANTINED",
                    "audit_reasons": tuple(
                        getattr(_ctx, "audit_reasons", ()) or ())}
        try:
            import shioaji as sj
            # Safety stop is wider than strategy stop (2x) to avoid premature fills
            safety_pts = stop_loss_pts * 2 if stop_loss_pts > 0 else 200
            if direction == "LONG":
                safety_price = entry_price - safety_pts
                action = sj.constant.Action.Sell
            else:
                safety_price = entry_price + safety_pts
                action = sj.constant.Action.Buy

            order = self.api.Order(
                price=safety_price,
                quantity=lots,
                action=action,
                price_type=sj.constant.FuturesPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                octype=sj.constant.FuturesOCType.Cover,
                account=self.api.futopt_account,
            )
            trade = self.api.place_order(self.contract, order)
            if trade and trade.status.status != sj.constant.Status.Failed:
                self._safety_stop_trade = trade
                console.print(f"[bold yellow]🛡️ Safety stop placed: {action.value} @ {safety_price:.0f} ({safety_pts:.0f}pts from entry)[/bold yellow]")
            else:
                console.print("[red]Safety stop failed to place[/red]")
        except Exception as e:
            console.print(f" [yellow]Safety stop error: {e}[/yellow] ")

    def _cancel_safety_stop(self):
        """Cancel the exchange-side safety stop after normal exit."""
        if not self._safety_stop_trade or not self.api:
            return
        # [Live wiring Step 3] execution-context gate: QUARANTINED/PREFLIGHT
        # makes ZERO cancel_order calls and returns a structured blocked
        # reason (audit); only LIVE_READY reaches the broker.
        _ctx = getattr(self, "_execution_context", None)
        if _ctx is not None and not _ctx.is_live_ready():
            self._record_safety_stop_reconcile()   # [orphan] never silent
            return {"blocked": True, "reason": "LIVE_QUARANTINED",
                    "audit_reasons": tuple(
                        getattr(_ctx, "audit_reasons", ()) or ())}
        try:
            self.api.cancel_order(self._safety_stop_trade)
            console.print("[dim]🛡️ Safety stop cancelled[/dim]")
        except Exception as e:
            # [exit failure-side] NEVER swallow: the caller (ordinary EXIT)
            # must not silently place the exit — structured failure
            console.print(f" [yellow]🛡️ Safety stop cancel error: {e}[/yellow] ")
            return {"blocked": True, "reason": "SAFETY_STOP_CANCEL_FAILED",
                    "error": str(e)}
        self._safety_stop_trade = None

    # ── GSD Phase 0d: Hourly No-Trade Audit (V-Model during session) ──
    def _hourly_no_trade_audit(self, timestamp, df_5m):
        """
        Every hour: if no trades in the past hour, diagnose WHY.
        Three possible verdicts:
          1. DATA_FAILURE → API down, stale data (alert)
          2. NO_VALID_SIGNALS → data OK, strategy found no signals (expected)
          3. COOLDOWN → strategy blocked by cooldown (expected)
        
        [ENHANCED] Also monitors trade records integrity and backups.
        """
        now_hour = datetime.now().hour  # Use system clock to prevent duplicate audits
        if now_hour == self._last_audit_hour:
            return  # Already audited this hour
        self._last_audit_hour = now_hour
        
        secs_since_tick = self._tmf_feed_age_secs()
        data_stale = secs_since_tick > 120  # 2+ min without tick

        # Use actual kbar count if available, fallback to _bars_since_trade
        actual_bars = len(df_5m) if df_5m is not None else 0

        # Diagnose
        if data_stale or df_5m is None or actual_bars < 30:
            verdict = "DATA_FAILURE"
            note = f"Data stale {secs_since_tick/60:.1f}min, bars={actual_bars}"
            console.print(f"[red]🚨 {verdict}: {note}[/red]")
        elif self.cooldown_until > 0:
            verdict = "COOLDOWN"
            note = f"Cooldown active (remaining={self.cooldown_until}), signals={self._signals_generated}"
            console.print(f"[dim]🔵 {verdict}: {note}[/dim]")
        elif self._signals_generated == 0:
            verdict = "NO_VALID_SIGNALS"
            note = f"Data OK, {actual_bars} bars, 0 signals generated. Strategy may be too strict for current conditions."
            console.print(f" [yellow]⚠️  {verdict}: {note}[/yellow] ")
        else:
            verdict = "NORMAL"
            note = f"{self._signals_generated} signals, data healthy"
        
        # [ENHANCED] Monitor trade records integrity
        trade_check_result = self._monitor_trade_records(timestamp)
        if trade_check_result:
            console.print(f"[green]✓ Trade records check: {trade_check_result}[/green]")

        options_audit_result = self._audit_options_data_health(timestamp)
        if options_audit_result:
            tone = "green" if options_audit_result.startswith("healthy") else "yellow"
            console.print(f"[{tone}]🩺 Options data audit: {options_audit_result}[/{tone}]")
            note = f"{note}; options={options_audit_result}"
        
        # Log audit
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
        save_signal_audit({
            "timestamp": str(timestamp),
            "signal": "HOURLY_AUDIT",
            "price": 0,
            "reason": verdict,
            "rejection": note,
            "lots": 0,
        })
        
        # Reset counters for next hour
        self._signals_generated = 0
        self._bars_since_trade = 0  # GAP-2 fix: reset bars counter too

    def _audit_options_data_health(self, timestamp):
        monitor = getattr(self, "options_monitor", None)
        if monitor is None:
            return ""
        try:
            return monitor.audit_indicator_health_and_repair(timestamp)
        except Exception as exc:
            return f"options_audit_error:{type(exc).__name__}:{str(exc)[:80]}"

    def _monitor_trade_records(self, timestamp):
        """
        Monitor trade records integrity and perform hourly checks.
        
        Returns:
            str: Summary of trade records status
        """
        try:
            from pathlib import Path
            import pandas as pd
            # datetime already imported at module top (datetime, timedelta)
            
            # Get current date for file naming
            current_date = timestamp.strftime("%Y%m%d") if hasattr(timestamp, "strftime") else datetime.now().strftime("%Y%m%d")
            
            # Check futures trade records
            futures_trade_file = Path(runtime_logs("market_data")) / f"TMF_{current_date}_trades.csv"
            futures_audit_file = Path(runtime_logs("market_data")) / f"TMF_{current_date}_signals_audit.csv"
            
            # Check stock trade records
            stock_trade_dir = Path(runtime_logs("stocks"))
            stock_trade_files = list(stock_trade_dir.glob("*_trades.csv")) if stock_trade_dir.exists() else []
            
            # Check options trade records
            options_trade_file = Path(runtime_logs("market_data")) / f"TXO_{current_date}_trades.csv"
            
            results = []
            
            # 1. Check futures trade records
            if futures_trade_file.exists():
                try:
                    df = pd.read_csv(futures_trade_file)
                    futures_trades = len(df)
                    results.append(f"Futures: {futures_trades} trades")
                    
                    # Check for recent trades (last hour)
                    if 'timestamp' in df.columns:
                        df['timestamp'] = pd.to_datetime(df['timestamp'])
                        recent_trades = df[df['timestamp'] > timestamp - timedelta(hours=1)]
                        if len(recent_trades) > 0:
                            results.append(f"  Recent: {len(recent_trades)} in last hour")
                except Exception as e:
                    results.append(f"Futures: Error reading ({str(e)[:50]})")
            else:
                results.append("Futures: No trade file")
            
            # 2. Check futures audit records
            if futures_audit_file.exists():
                try:
                    df = pd.read_csv(futures_audit_file)
                    audit_records = len(df)
                    results.append(f"Audit: {audit_records} records")
                except:
                    results.append("Audit: Error reading")
            
            # 3. Check stock trade records
            if stock_trade_files:
                total_stock_trades = 0
                for file in stock_trade_files:
                    try:
                        df = pd.read_csv(file)
                        total_stock_trades += len(df)
                    except:
                        pass
                results.append(f"Stocks: {total_stock_trades} trades in {len(stock_trade_files)} files")
            
            # 4. Check options trade records
            if options_trade_file.exists():
                try:
                    df = pd.read_csv(options_trade_file)
                    options_trades = len(df)
                    results.append(f"Options: {options_trades} trades")
                except:
                    results.append("Options: Error reading")
            
            # 5. Backup check (create backup if needed)
            self._backup_trade_records_if_needed(timestamp)
            
            return "; ".join(results)
            
        except Exception as e:
            return f"Trade monitor error: {str(e)[:100]}"
    
    def _backup_trade_records_if_needed(self, timestamp):
        """
        Create backup of trade records if last backup was >6 hours ago.
        """
        try:
            from pathlib import Path
            import shutil
            # datetime already imported at module top (datetime)
            
            backup_dir = Path(runtime_logs("backups", "trade_records"))
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            # Check last backup time
            backup_marker = backup_dir / "last_backup.txt"
            should_backup = True
            
            if backup_marker.exists():
                try:
                    with open(backup_marker, 'r') as f:
                        last_backup_str = f.read().strip()
                        last_backup = datetime.strptime(last_backup_str, "%Y-%m-%d %H:%M:%S")
                        hours_since = (datetime.now() - last_backup).total_seconds() / 3600
                        should_backup = hours_since >= 6  # Backup every 6 hours
                except:
                    pass
            
            if should_backup:
                # Backup futures trade records
                current_date = timestamp.strftime("%Y%m%d") if hasattr(timestamp, "strftime") else datetime.now().strftime("%Y%m%d")
                futures_trade_file = Path(runtime_logs("market_data")) / f"TMF_{current_date}_trades.csv"
                futures_audit_file = Path(runtime_logs("market_data")) / f"TMF_{current_date}_signals_audit.csv"
                
                backup_files = []
                
                if futures_trade_file.exists():
                    backup_path = backup_dir / f"TMF_{current_date}_trades_{timestamp.strftime('%H%M')}.csv"
                    shutil.copy2(futures_trade_file, backup_path)
                    backup_files.append("futures_trades")
                
                if futures_audit_file.exists():
                    backup_path = backup_dir / f"TMF_{current_date}_audit_{timestamp.strftime('%H%M')}.csv"
                    shutil.copy2(futures_audit_file, backup_path)
                    backup_files.append("futures_audit")
                
                # Update backup marker
                with open(backup_marker, 'w') as f:
                    f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                
                if backup_files:
                    console.print(f"[dim]📂 Trade records backed up: {', '.join(backup_files)}[/dim]")
                    
        except Exception as e:
            console.print(f" [yellow]⚠️ Trade backup failed: {e}[/yellow] ")

    def _save_orders_file_wrapper(self) -> set:
        """Export all orders to JSON for dashboard consumption.

        Returns the set of OCO release order IDs that were persisted via
        lifecycle fallbacks (strategy-based or state-file).  Callers can
        use this to decide whether _mts_release_orders_flushed should be set.
        """
        if not self.order_mgr:
            return set()
        try:
            from core.order_management.order import OrderSide
            import math
            import json
            from pathlib import Path

            # 2026-07-07 Gemini CLI / Hermes Agent: Determine session date consistently
            _date = getattr(self.order_mgr, "_session_date", None) if self.order_mgr else None
            if not _date:
                try:
                    from core.date_utils import get_session_date_str
                    _date = get_session_date_str()
                except Exception:
                    _date = datetime.now().strftime("%Y%m%d")
            # Shioaji enum values can leak into test/recovery objects.  They
            # are not timestamps; never pass them to pandas/date helpers.
            # A malformed timestamp is unscoped rather than fatal: retain the
            # order for export and let its own serialized fields be audited.
            if not isinstance(_date, str) or not _date.isdigit() or len(_date) != 8:
                _date = datetime.now().strftime("%Y%m%d")

            # Get current market price for unrealized PnL
            cur_price = 0.0
            try:
                cur_price = float(self.market_data.get(self.ticker, {}).get("close", 0))
            except Exception:
                cur_price = 0.0

            all_orders = self.order_mgr.get_completed() + self.order_mgr.get_pending()
            # 2026-08-03 Gemini CLI: Filter exported orders by active session date to prevent historical memory leakage across days
            try:
                import pandas as _pd_import
                from core.date_utils import get_session_date_str
                filtered_orders = []
                for o in all_orders:
                    o_created = getattr(o, 'created_at', None)
                    if o_created:
                        o_dt = _pd_import.to_datetime(o_created, errors='coerce')
                        o_sess = get_session_date_str(o_dt) if _pd_import.notna(o_dt) else None
                        if o_sess and o_sess != _date:
                            continue
                    filtered_orders.append(o)
                if filtered_orders:
                    all_orders = filtered_orders
            except Exception:
                pass
            export_data = []
            for o in all_orders:
                d = o.to_dict()
                # Add unrealized PnL for open positions
                d["unrealized_pnl"] = None
                d["unrealized_pnl_pts"] = None
                d["current_price"] = cur_price if cur_price > 0 else None

                if o.status in ("filled", "partial_filled") and self.trader.position != 0:
                    entry = self.trader.entry_price
                    qty = abs(self.trader.position)
                    if cur_price > 0 and entry > 0:
                        if self.trader.position > 0:  # LONG
                            pnl_pts = cur_price - entry
                        else:  # SHORT
                            pnl_pts = entry - cur_price
                        # 2026-07-07 Gemini CLI: P2: PnL multiplier contract-aware
                        point_value = get_point_value(self.ticker)
                        pnl_cash = pnl_pts * point_value * qty
                        d["unrealized_pnl"] = round(pnl_cash, 0)
                        d["unrealized_pnl_pts"] = round(pnl_pts, 1)

                export_data.append(d)

            # 2026-07-07 Hermes Agent: Track OCO order IDs persisted via
            # lifecycle fallbacks so callers can conditionally set
            # _mts_release_orders_flushed without re-reading files.
            _persisted_oco_ids: set = set()

            # [Fix 2026-07-06] Include release OCO orders from lifecycle state.
            # The order manager is reset on PM2 restart, but release_group persists
            # in the state file. Without this, release orders are invisible until
            # the next order_mgr event (fill/cancel) triggers a flush.
            # 2026-07-07 Hermes Agent: Also check order_mgr.completed for duplicate
            # order IDs, preventing ghost OCO entries when both legs already filled
            # but release_group.status is still SUBMITTED (e.g., after restart).
            _completed_ids = {o.order_id for o in self.order_mgr.completed} if self.order_mgr else set()
            _strat = self._registry.get("tmf_spread")
            if _strat and hasattr(_strat, "_lifecycle_oca"):
                _rg = _strat._lifecycle_oca.release_group
                if hasattr(_rg, 'status') and getattr(_rg.status, 'value', '') in ("SUBMITTED", "SUBMITTING"):
                    for _label, _oid, _side_attr, _price_attr, _entry_side_attr in [
                        ("NEAR", _rg.near_order_id, "near_side", "near_price", "_near_side"),
                        ("FAR", _rg.far_order_id, "far_side", "far_price", "_far_side"),
                    ]:
                        if not _oid:
                            continue
                        if any(d.get("order_id") == _oid for d in export_data):
                            continue
                        if _oid in _completed_ids:
                            continue
                        _side = getattr(_rg, _side_attr, None) or ""
                        if _side not in ("buy", "sell"):
                            _entry_side = getattr(_strat, _entry_side_attr, None)
                            _es = str(getattr(_entry_side, "value", _entry_side)).upper()
                            if _es == "LONG":
                                _side = "sell"
                            elif _es == "SHORT":
                                _side = "buy"
                            else:
                                continue
                        _price = getattr(_rg, _price_attr, 0) or 0
                        _otype = getattr(_rg, "order_type", "MKP")
                        export_data.append({
                            "order_id": _oid,
                            "symbol": f"{self.ticker}_{_label}",
                            "side": _side,
                            "order_type": _otype,
                            "quantity": 1,
                            "filled_quantity": 0,
                            "price": _price if _price > 0 else 0,
                            "avg_fill_price": 0,
                            "status": "submitted",
                            "strategy": "MTS_RELEASE_OCO",
                        })
                        _persisted_oco_ids.add(_oid)
                elif hasattr(_rg, 'status') and getattr(_rg.status, 'value', '') in ("PARTIALLY_FILLED", "CANCELING_SIBLING", "SIBLING_CANCELED"):
                    for _label, _oid in [("NEAR", _rg.near_order_id), ("FAR", _rg.far_order_id)]:
                        if _oid and not any(d.get("order_id") == _oid for d in export_data):
                            export_data.append({
                                "order_id": _oid,
                                "symbol": f"{self.ticker}_{_label}",
                                "side": getattr(_rg, f"{_label.lower()}_side", None) or "",
                                "order_type": getattr(_rg, "order_type", "MKP"),
                                "quantity": 1,
                                "filled_quantity": 0,
                                "price": 0,
                                "avg_fill_price": 0,
                                "status": ("filled" if _label == "NEAR" and getattr(_rg, "filled_leg", None)
                                          and getattr(_rg.filled_leg, "value", "") == "NEAR"
                                          else "cancelled"),
                                "strategy": "MTS_RELEASE_OCO",
                            })

            # 2026-07-07 Hermes Agent: State-file fallback — read
            # /tmp/mts_position_state.json directly as a last resort.
            # The strategy-based fallback above relies on _lifecycle_oca
            # being restored on the plugin instance, which may not have
            # happened yet after a PM2 restart. This fallback guarantees
            # that OCO release orders are always persisted to the orders
            # JSON file when release_group.status is SUBMITTED.
            _existing_ids = {d.get("order_id") for d in export_data if d.get("order_id")}
            try:
                # 2026-07-07 Gemini CLI: Risk 5: Use isolated state path instead of module constant to prevent test pollution
                # 2026-07-07 Gemini CLI / Hermes Agent: Call top-level _mts_position_state_path directly (not as instance method)
                _state_path = _mts_position_state_path()
                if _state_path.exists():
                    _state_data = json.loads(_state_path.read_text())
                    _lc = _state_data.get("lifecycle", {})
                    _rg_sf = _lc.get("release_group") or _state_data.get("release_group")
                    if _rg_sf and isinstance(_rg_sf, dict):
                        _sf_status = _rg_sf.get("status", "")
                        if _sf_status in ("SUBMITTED", "PARTIALLY_FILLED", "CANCELING_SIBLING"):
                            for _leg_key in ("near", "far"):
                                _leg_oid = _rg_sf.get(f"{_leg_key}_order_id")
                                if _leg_oid and _leg_oid not in _existing_ids:
                                    export_data.append({
                                        "order_id": _leg_oid,
                                        "symbol": f"{self.ticker}_{_leg_key.upper()}",
                                        "side": _rg_sf.get(f"{_leg_key}_side", ""),
                                        "order_type": _rg_sf.get(f"{_leg_key}_order_type", _rg_sf.get("order_type", "MKP")),
                                        "quantity": 1,
                                        "filled_quantity": 0,
                                        "price": _rg_sf.get(f"{_leg_key}_price", 0) or 0,
                                        "avg_fill_price": 0,
                                        "status": "submitted",
                                        "strategy": "MTS_RELEASE_OCO",
                                    })
                                    _persisted_oco_ids.add(_leg_oid)
                                    console.print(
                                        f"[cyan]📄 [ORDERS_SAVE] state-file fallback: "
                                        f"{_leg_oid} ({_leg_key}) from position_state[/cyan]"
                                    )
            except Exception as _sf_exc:
                console.print(
                    f"[dim yellow]⚠️ [ORDERS_SAVE] state-file fallback failed: "
                    f"{_sf_exc}[/dim yellow]"
                )

            # 2026-07-08 Gemini CLI: Use session-date-aware orders_file path with test isolation to prevent test leakage
            import sys
            orders_dir = runtime_path("exports", "trades")
            if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
                current_cwd = Path.cwd().resolve()
                if (current_cwd / "RULES.md").exists() and (current_cwd / "exports").exists():
                    orders_dir = "tests/temp_exports_trades"
            orders_file = Path(orders_dir) / f"{self.ticker}_{_date}_orders.json"
            orders_file.parent.mkdir(parents=True, exist_ok=True)

            # [export fix] Shioaji enum values (Action/… ) and other
            # non-serializable values must never crash the export: the
            # JSON-safe normalization turns them into strings WITHOUT
            # changing order identity or the dedupe keys.
            try:
                export_data = json.loads(
                    json.dumps(export_data, ensure_ascii=False,
                               default=str))
            except Exception:
                pass  # never block the export on normalization

            # 2026-07-07 Hermes Agent: Deduplicate by order_id against
            # existing file content.  The state-file fallback adds OCO
            # orders unconditionally; if _save_orders_file_wrapper is
            # called repeatedly (e.g. from multiple tick paths), the
            # file would grow unbounded with duplicate entries.
            # No size limit — always dedup to prevent corruption when
            # the file grows large from rapid-fire saves.
            _seen_ids = {d.get("order_id") for d in export_data if d.get("order_id")}
            try:
                if orders_file.exists():
                    _existing = json.loads(orders_file.read_text())
                    if isinstance(_existing, list):
                        for _entry in _existing:
                            if isinstance(_entry, dict):
                                _eid = _entry.get("order_id")
                                if _eid and _eid not in _seen_ids:
                                    _seen_ids.add(_eid)
                                    export_data.append(_entry)
            except Exception:
                pass  # corrupt existing file → overwrite with fresh data

            # Projection-only correction: a successful live canonical
            # position match must not be overwritten by stale in-memory
            # submitted rows.  This never authorizes or submits an order.
            if getattr(self, "live_trading", False):
                try:
                    _canon_path = Path(runtime_path(
                        "exports", "trades", "live", "diagnostics",
                        "broker_snapshot_canonical.json"))
                    _snap = json.loads(_canon_path.read_text(encoding="utf-8"))
                    if (_snap.get("source") == "live_broker"
                            and (_snap.get("fetch_status") or {}).get("capture") == "OK"):
                        _pos = {}
                        for _p in _snap.get("positions") or []:
                            if (_p.get("account") == "futures"
                                    and int(_p.get("quantity", 0) or 0) > 0):
                                _side = str(_p.get("direction") or "").lower().split(".")[-1]
                                if _side in ("buy", "sell"):
                                    _pos[(str(_p.get("code")), _side,
                                          int(_p.get("quantity")))] = _p
                        for _d in export_data:
                            _side = str(_d.get("side") or "").lower().split(".")[-1]
                            _key = (str(_d.get("symbol") or ""), _side,
                                    int(_d.get("quantity", 0) or 0))
                            _p = _pos.get(_key)
                            if _p and _d.get("status") != "filled":
                                _d.update({
                                    "status": "filled",
                                    "filled_quantity": int(_p["quantity"]),
                                    "remaining_quantity": 0,
                                    "avg_fill_price": float(_p["avg_cost"]),
                                    "cancelled_at": None,
                                    "cancel_reason": None,
                                })
                except Exception:
                    pass

            # 2026-07-07 Hermes Agent: atomic write via temp + rename.
            # Dashboard reads this file on every refresh; a direct write
            # races with the read and produces corrupted JSON.  Writing
            # to a temp file then atomically renaming avoids truncation.
            import random
            _tmp_orders = f"{orders_file}.tmp.{os.getpid()}.{random.randint(1000,9999)}"
            try:
                with open(_tmp_orders, "w", encoding="utf-8") as f:
                    json.dump(export_data, f, ensure_ascii=False, indent=2)
                os.replace(_tmp_orders, orders_file)
            except Exception:
                if os.path.exists(_tmp_orders):
                    os.remove(_tmp_orders)
                raise
            return _persisted_oco_ids
        except Exception as e:
            console.print(f"[yellow]⚠️ Failed to save futures orders file: {e}[/yellow]")
            return set()

    def _append_filled_lifecycle_order(self, side, price, ts, lots, *, strategy="futures", comment="", order_id=None):
        """Append a filled lifecycle order record without re-executing trade logic."""
        if not self.order_mgr:
            return None
        try:
            from core.order_management.order import Order, OrderStatus, OrderType

            qty = int(lots or 1)
            fill_price = float(price or 0)
            order_ts = ts if hasattr(ts, "strftime") else datetime.now()
            lifecycle_order = Order(
                symbol=self.ticker,
                side=side,
                order_type=OrderType.MARKET,
                quantity=qty,
                price=fill_price,
                order_id=order_id or f"LIFECYCLE-{order_ts.strftime('%Y%m%d-%H%M%S-%f')}",
                strategy=strategy,
                comment=comment,
            )
            lifecycle_order.status = OrderStatus.FILLED
            lifecycle_order.filled_quantity = qty
            lifecycle_order.avg_fill_price = fill_price
            lifecycle_order.created_at = order_ts
            lifecycle_order.submitted_at = order_ts
            lifecycle_order.filled_at = order_ts
            lifecycle_order.updated_at = order_ts
            lifecycle_order.exchange_order_id = f"RECOV-{lifecycle_order.order_id}"
            self.order_mgr.completed.append(lifecycle_order)
            return lifecycle_order
        except Exception as e:
            console.print(f" [yellow]⚠️ Failed to append lifecycle order: {e}[/yellow] ")
            return None

    def _recover_orders_from_trades_csv(self):
        """Recover all orders from trades CSV to rebuild OrderManager state on startup."""
        if not self.order_mgr:
            return
        
        try:
            import csv
            import json
            from pathlib import Path
            from core.order_management.order import Order, OrderStatus, OrderType, OrderSide

            # Restore the current session's JSON lifecycle before broker
            # reconciliation.  The export is the durable source for orders
            # submitted before a restart; broker truth will subsequently
            # promote position-covered pending rows to FILLED.
            try:
                from core.runtime_paths import runtime_path
                orders_file = Path(runtime_path(
                    "exports", "trades",
                    f"{self.ticker}_{datetime.now().strftime('%Y%m%d')}_orders.json"))
                if orders_file.exists():
                    saved = json.loads(orders_file.read_text(encoding="utf-8"))
                    if isinstance(saved, list):
                        restored = self.order_mgr.restore_orders(saved)
                        if restored.get("active") or restored.get("completed"):
                            console.print(
                                f"[dim]♻️ Restored {restored.get('active', 0)} active / "
                                f"{restored.get('completed', 0)} completed orders[/dim]"
                            )
            except Exception as _restore_exc:
                console.print(f"[dim yellow]⚠️ Order JSON restore skipped: {_restore_exc}[/dim yellow]")
            
            # Find today's trades CSV
            today = datetime.now().strftime("%Y%m%d")
            trades_file = Path(f"exports/trades/TMF_{today}_trades.csv")
            
            if not trades_file.exists():
                console.print("[dim]No trades file to recover orders from[/dim]")
                return
            
            with open(trades_file) as f:
                rows = list(csv.DictReader(f))
            
            if not rows:
                return
            
            recovered_count = 0
            for row in rows:
                try:
                    trade_type = row.get("type", "")
                    direction = row.get("direction", "")
                    price = float(row.get("price", 0))
                    lots = int(row.get("lots", 0) or 1)
                    timestamp_str = row.get("timestamp", "")
                    reason = row.get("reason", "")
                    
                    # Parse timestamp
                    try:
                        ts = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    except:
                        ts = datetime.now()
                    
                    # Determine OrderSide from type
                    if trade_type == "BUY":
                        order_side = OrderSide.BUY
                    elif trade_type == "SELL":
                        order_side = OrderSide.SELL
                    elif trade_type == "EXIT":
                        # Exit order side is opposite of direction
                        order_side = OrderSide.SELL if direction == "LONG" else OrderSide.BUY
                    else:
                        continue  # Skip unknown types
                    
                    lifecycle_order = self._append_filled_lifecycle_order(
                        side=order_side,
                        price=price,
                        ts=ts,
                        lots=lots,
                        strategy="futures",
                        comment=f"{trade_type} {reason}".strip(),
                        order_id=f"TRADES-{ts.strftime('%Y%m%d-%H%M%S')}",
                    )
                    if lifecycle_order is not None:
                        recovered_count += 1
                    
                except Exception as e:
                    console.print(f" [yellow]⚠️ Failed to recover order from row: {e}[/yellow] ")
                    continue
            
            if recovered_count > 0:
                console.print(f"[bold cyan]♻️ Recovered {recovered_count} futures orders from trades CSV[/bold cyan]")
                # Save immediately to orders JSON
                self._save_orders_file_wrapper()
            
        except Exception as e:
            console.print(f" [yellow]Futures order recovery from trades CSV failed: {e}[/yellow] ")

    # ── Order Lifecycle (L3 Integration) ──
    def _get_lifecycle_order(self, order_id):
        if not self.order_mgr:
            return None
        order = self.order_mgr.active_orders.get(order_id)
        if order is not None:
            return order
        for completed in self.order_mgr.completed:
            if completed.order_id == order_id:
                return completed
        return None

    def _clear_pending_lifecycle_order(self, order_id):
        self._pending_lifecycle_orders.pop(order_id, None)
        # 2026-05-27 Gemini CLI: Clear cancellation tracking as well
        if hasattr(self, "_mts_stale_order_cancels"):
            self._mts_stale_order_cancels.discard(order_id)

    def _check_oco_release_fill(self, event):
        """ADR-010 Sprint 4A: detect OCO release fill — PARTIALLY_FILLED only.

        Matches event.order_id against strategy release_group near/far order ids.
        On match: mark PARTIALLY_FILLED without cancel sibling or trail activation.
        Sibling cancel and SINGLE_LEG transition handled in Sprint 4B/4C.

        Invariant: PARTIALLY_FILLED → trail_group.status must NOT be ARMED.
        """
        from strategies.plugins.futures.active.tmf_spread import (
            ReleaseGroupStatus, Leg, TrailGroupStatus, CancelStatus,
            _write_mts_state, lifecycle_to_dict,
        )
        _strategy = self._registry.get("tmf_spread")
        if not _strategy or not hasattr(_strategy, "_lifecycle_oca"):
            return
        _rg = _strategy._lifecycle_oca.release_group

        # 2026-07-07 Hermes Agent: Ignore sibling fill when winner already set.
        # After the first OCO leg fills, _check_oco_release_fill sets filled_leg
        # and cancels the sibling synchronously.  If the sibling fill arrives
        # (e.g. from a stale paper_fill_sim entry or duplicate callback), it
        # must be silently ignored to prevent double-fill state corruption.
        if _rg.filled_leg is not None:
            console.print(
                f"[yellow]⚠️ [OCO_DUPLICATE_FILL_IGNORED] release_group already "
                f"filled (winner={_rg.filled_leg.value}); ignoring sibling fill "
                f"for {event.order_id}[/yellow]"
            )
            return

        _rg_status_val = _rg.status.value if hasattr(_rg.status, 'value') else str(_rg.status)
        if _rg_status_val != "SUBMITTED":
            return
        _oid = event.order_id
        if _oid == _rg.near_order_id:
            _winner = "near"
        elif _oid == _rg.far_order_id:
            _winner = "far"
        else:
            return  # not an OCO release fill

        # Dedup by deal_id
        _deal_key = event.deal_id or f"oco:{_oid}:{event.fill_qty}:{event.fill_price}"
        if _deal_key in self._applied_lifecycle_deals:
            return
        self._applied_lifecycle_deals[_deal_key] = datetime.now().isoformat()

        price = float(event.fill_price or 0)
        
        # 2026-07-07 Gemini CLI: Enforce strict OCO fill state transition sequence on_first_fill
        _rg.filled_leg = Leg.NEAR if _winner == "near" else Leg.FAR
        _rg.filled_order_id = _oid
        _rg.canceled_leg = Leg.FAR if _winner == "near" else Leg.NEAR

        # Invariant: trail must NOT be active in PARTIALLY_FILLED/CANCELING_SIBLING
        _strategy._lifecycle_oca.trail_group.status = TrailGroupStatus.INACTIVE

        _cancel_oid = _rg.far_order_id if _winner == "near" else _rg.near_order_id
        if self.order_mgr and _cancel_oid:
            try:
                # 1. Transition status directly to CANCELING_SIBLING before sending request
                _rg.status = ReleaseGroupStatus.CANCELING_SIBLING
                _rg.sibling_cancel_order_id = _cancel_oid
                _rg.sibling_cancel_status = CancelStatus.PENDING
                
                # 2. cancel_sibling
                self.order_mgr.cancel(_cancel_oid, reason=f"oco_4b_cancel_{_winner}", source="oco_bracket")
                
                console.print(
                    f"[bold cyan]🔄 [OCO_4B] CANCELING_SIBLING sent for {_cancel_oid}"
                    f" (winner={_winner})[/bold cyan]"
                )
                
                _write_mts_state(
                    has_position=True, action=f"OCO_CANCELING_{_winner.upper()}",
                    reason=f"oco_4b_cancel_{_winner}",
                    near_entry=_strategy._near_entry, far_entry=_strategy._far_entry,
                    near_last=price if _winner == "near" else float(self.market_data.get(f"{self.ticker}_NEAR", {}).get("close") or 0),
                    far_last=price if _winner == "far" else float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close") or 0),
                    near_side=_strategy._near_side, far_side=_strategy._far_side,
                    released_leg=_winner, trade_id=_strategy._trade_id,
                    ticker=self.ticker, atr=float(getattr(_strategy, "_last_atr", 0.0) or 0.0),
                    lifecycle=lifecycle_to_dict(_strategy._lifecycle_oca),
                )
            except (ValueError, RuntimeError) as _e:
                _rg.sibling_cancel_status = CancelStatus.REJECTED
                _rg.status = ReleaseGroupStatus.FAILED
                console.print(
                    f"[red]⚠️ [OCO_4B] Cancel failed: {_e} — status=FAILED[/red]"
                )
        else:
            _rg.status = ReleaseGroupStatus.PARTIALLY_FILLED
            _write_mts_state(
                has_position=True, action=f"OCO_{_winner.upper()}_PARTIAL",
                reason=f"oco_{_winner}_partially_filled",
                near_entry=_strategy._near_entry, far_entry=_strategy._far_entry,
                near_last=price if _winner == "near" else float(self.market_data.get(f"{self.ticker}_NEAR", {}).get("close") or 0),
                far_last=price if _winner == "far" else float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close") or 0),
                near_side=_strategy._near_side, far_side=_strategy._far_side,
                released_leg=_winner, trade_id=_strategy._trade_id,
                ticker=self.ticker, atr=float(getattr(_strategy, "_last_atr", 0.0) or 0.0),
                lifecycle=lifecycle_to_dict(_strategy._lifecycle_oca),
            )

    def _apply_confirmed_futures_deal(self, event):
        from core.order_management.order import OrderStatus
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit

        # 2026-07-07 Gemini CLI: P0: Stale callback guard based on lifecycle generation
        # 2026-07-07 Gemini CLI / Hermes Agent: Handle missing _lifecycle_generation gracefully for unit tests
        pending = self._pending_lifecycle_orders.get(event.order_id)
        if pending:
            _order_gen = pending.get("generation", 0)
            _curr_gen = getattr(self, "_lifecycle_generation", 0)
            if _order_gen < _curr_gen:
                import logging
                logging.getLogger("FuturesMonitor").warning(
                    f"[STALE_CALLBACK_IGNORED] Ignored stale fill for order {event.order_id} "
                    f"(order generation {_order_gen} < current generation {_curr_gen})"
                )
                return None

        # [MTS] Check if this fill completes a spread entry (automated or manual)
        # Must be called BEFORE early returns to ensure tracking dictionary is updated
        price = float(event.fill_price or 0)
        self._check_mts_multi_leg_fill(event.order_id, price)
        self._maybe_complete_emergency_command(event, price)

        pending = self._pending_lifecycle_orders.get(event.order_id)
        # 2026-06-22 Gemini CLI: Use fill_qty to match OrderEvent class definition
        if pending is None or event.fill_qty <= 0:
            # ADR-010 Sprint 4A: check if this fill matches OCO bracket order
            if pending is None and event.fill_qty > 0:
                self._check_oco_release_fill(event)
            return None

        deal_key = event.deal_id or f"{event.order_id}:{event.fill_qty}:{event.fill_price}"
        if deal_key in self._applied_lifecycle_deals:
            return None

        signal = pending.get("signal")
        # Support both standard and MTS signal types for logging/audit
        MTS_CONFIRMED_FILL_SIGNALS = {
    "BUY", "SELL", "EXIT", "PARTIAL_EXIT",
    "SELL_NEAR_BUY_FAR", "BUY_NEAR_SELL_FAR",
    "RELEASE_NEAR", "RELEASE_FAR",
    "COMBINED_EXIT_NEAR", "COMBINED_EXIT_FAR",
}
        if signal not in MTS_CONFIRMED_FILL_SIGNALS:
            import logging as _lg
            _lg.getLogger("FuturesMonitor").error(
                "[CONFIRMED_FILL_REJECTED_UNKNOWN_SIGNAL] signal=%s order_id=%s trade_id=%s fill_qty=%s fill_price=%s",
                signal, event.order_id, event.deal_id or "?", event.fill_qty, event.fill_price,
            )
            return None

        ts = datetime.now()
        # 2026-06-22 Gemini CLI: Use fill_qty to match OrderEvent
        lots = int(event.fill_qty)
        reason = pending.get("reason")
        stop_loss = pending.get("stop_loss")
        break_even_trigger = pending.get("break_even_trigger")
        trail_points = pending.get("trail_points")
        cross_policy = pending.get("cross_policy")

        # Skip directional trader execution for multi-leg spread signals (net zero or self-managed)
        if signal in ("SELL_NEAR_BUY_FAR", "BUY_NEAR_SELL_FAR", "RELEASE_NEAR", "RELEASE_FAR", "EXIT", "COMBINED_EXIT_NEAR", "COMBINED_EXIT_FAR"):
             _pending_strat = pending.get("strategy", "")
             if _pending_strat and "MTS" in str(_pending_strat):
                 # [Fix 2026-05-27] Handle strategy state reset for MTS exits upon fill
                 # 2026-07-27 Hermes Agent: COMBINED_EXIT must be checked BEFORE
                 # the MTS_EXIT catch-all (COMBINED_EXIT orders use strategy=MTS_EXIT).
                 if signal in ("COMBINED_EXIT_NEAR", "COMBINED_EXIT_FAR"):
                     self._apply_combined_exit_fill(event, pending, signal, price)
                 elif signal == "EXIT" or _pending_strat == "MTS_EXIT":
                     _mts_strat = self._registry.get("tmf_spread")
                     if _mts_strat:
                         _mts_strat._reset(reason="trail_exit_confirmed", exit_price=price)
                         console.print(f"[bold green]✅ [MTS_SYNC] Trailing exit CONFIRMED: {event.order_id}[/bold green]")
                     # 2026-07-08 Hermes Agent: only reset force exit inflight if this fill
                     # was triggered by the risk control gate (not a normal trail exit).
                     _reason = pending.get("reason", "") if pending else ""
                     if "SESSION_CLOSE_FORCE" in str(_reason) or "SETTLEMENT_FORCE_FLAT" in str(_reason):
                         self._mts_force_exit_inflight = False
                 elif signal in ("RELEASE_NEAR", "RELEASE_FAR") or _pending_strat == "MTS_RELEASE":
                     _mts_strat = self._registry.get("tmf_spread")
                     if _mts_strat:
                         _leg = "near" if "NEAR" in str(signal) else "far"
                         # 2026-06-26 Gemini CLI: sync_release requires the price of the REMAINING leg.
                         # If near is released, the remaining leg is far. If far is released, the remaining leg is near.
                         if _leg == "near":
                             _rem_price = float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close") or 0.0)
                             if _rem_price <= 0:
                                 _rem_price = float(self._far_current_bar.get("close") or 0.0)
                         else:
                             _rem_price = float(self.market_data.get(f"{self.ticker}_NEAR", {}).get("close") or 0.0)
                             if _rem_price <= 0:
                                 _rem_price = float(self._current_bar.get("close") or 0.0)
                         
                         # Fallback to the entry price of the remaining leg if still 0
                         if _rem_price <= 0:
                             _rem_price = _mts_strat._far_entry if _leg == "near" else _mts_strat._near_entry
                             
                         _mts_strat.sync_release(leg=_leg, price=_rem_price, release_price=price, event_time=getattr(event, "timestamp", None))
                         console.print(f"[bold green]✅ [MTS_SYNC] Release CONFIRMED: {event.order_id} ({_leg}) with remaining leg price {_rem_price}[/bold green]")

                 # 2026-06-09 JVS Claw: Fix symbol matching for NEAR/FAR legs
                 # Update directional trader position for spread legs.
                 # This prevents GHOST_POSITION errors in the watchdog.
                 # Match both exact contract code AND NEAR/FAR suffix patterns.
                 # 2026-07-09 Hermes Agent: Also match far from far_contract.code
                 # for live mode where real contract codes (e.g. TMFH6) are used.
                 _symbol = str(event.symbol or "")
                 _contract_code = self.contract.code if self.contract else ""
                 _far_code = self.far_contract.code if self.far_contract else ""
                 _is_near_leg = "NEAR" in _symbol or _symbol == _contract_code
                 _is_far_leg = "FAR" in _symbol or (_far_code and _symbol == _far_code)
                 
                 if _is_near_leg:
                      # 2026-06-23 Gemini CLI: Determine if this fill is an exit/release/emergency closing transaction
                      _is_closing = False
                      if pending:
                          _pending_strat = pending.get("strategy", "")
                          _pending_sig = pending.get("signal", "")
                          if _pending_strat in ("MTS_RELEASE", "MTS_EXIT", "MTS_EMERGENCY") or _pending_sig in ("RELEASE_NEAR", "EXIT"):
                              _is_closing = True
                      
                      if _is_closing:
                          _mkt_action = "EXIT"
                      else:
                          from core.order_management.order import OrderSide
                          _mkt_action = "BUY" if event.side == OrderSide.BUY else "SELL"
                      
                      # 2026-06-22 Gemini CLI: Pass ts variable to execute_signal to fix signature mismatch TypeError
                      self.trader.execute_signal(_mkt_action, price, ts, lots=lots)
                      console.print(f"[dim][MTS_SYNC] NEAR-leg synced to trader: {self.trader.position} ({_mkt_action})[/dim]")

                 # P1-B: mirror the confirmed fill into the durable exit
                 # intent (fence-aware transition, same runtime logs dir)
                 try:
                     _iid = pending.get("intent_id")
                     _leg_role = pending.get("leg_role")
                     if _iid and _leg_role:
                         from core.exit_intent import IntentLog as _MTSIntentLog
                         _ilog = _MTSIntentLog(_mts_intent_log_dir())
                         _cur = _ilog.get(_iid)
                         if _cur is not None and \
                                 _cur.get("legs", {}).get(_leg_role, {}).get("status") != "FILLED":
                             _ilog.transition(_iid, _leg_role, "FILLED")
                 except Exception:
                     import logging
                     logging.getLogger("FuturesMonitor").warning(
                         "[P1B_FILL_SYNC_FAILED] order_id=%s", event.order_id)

                 self._applied_lifecycle_deals.add(deal_key)

                 # Check Combined Exit group completion
                 _ce_group_id = pending.get("combined_exit_group_id")
                 if _ce_group_id and hasattr(self, "_combined_exit_groups"):
                     _ceg = self._combined_exit_groups.get(_ce_group_id)
                     if _ceg and not _ceg.get("completed"):
                         if signal == "COMBINED_EXIT_NEAR":
                             _ceg["near_filled"] = True
                             _ceg["near_fill_price"] = price
                         elif signal == "COMBINED_EXIT_FAR":
                             _ceg["far_filled"] = True
                             _ceg["far_fill_price"] = price

                         if _ceg.get("near_filled") and _ceg.get("far_filled"):
                             _ceg["completed"] = True
                             _mts_strat = self._registry.get("tmf_spread")
                             if _mts_strat:
                                 np = _ceg.get("near_fill_price", 0)
                                 fp = _ceg.get("far_fill_price", 0)
                                 _mts_strat._reset(reason="combined_exit_filled", exit_price=max(np, fp))
                                 lc = {"phase": "FLAT"}
                                 from strategies.plugins.futures.active.tmf_spread import _write_mts_state
                                 _write_mts_state(has_position=False, action="COMBINED_EXIT_COMPLETED",
                                     reason="COMBINED_EXIT",
                                     near_entry=_mts_strat._near_entry, far_entry=_mts_strat._far_entry,
                                     near_last=np, far_last=fp,
                                     near_side=_mts_strat._near_side, far_side=_mts_strat._far_side,
                                     trade_id=pending.get("trade_id", ""), ticker=self.ticker,
                                     lifecycle=lc)
                                 console.print("[bold green]\u2705 [MTS_ORDER] COMBINED_EXIT COMPLETED: group=" + str(_ce_group_id) + " near=" + str(np) + " far=" + str(fp) + "[/bold green]")
                             self._append_mts_event("COMBINED_EXIT_COMPLETED",
                                 group_id=_ce_group_id,
                                 near_fill_price=_ceg.get("near_fill_price", 0) if _ceg else 0,
                                 far_fill_price=_ceg.get("far_fill_price", 0) if _ceg else 0,
                                 trade_id=pending.get("trade_id", ""))

                 # Execution Quality Audit
                 _ref_ohlc = pending.get("ref_ohlc", {})
                 _ref_ohlc = pending.get("ref_ohlc", {})
                 _ref_close = float(_ref_ohlc.get("close", 0))
                 _slippage = 0.0
                 if _ref_close > 0:
                     # For BUY: slippage = fill - ref (positive is bad)
                     # For SELL: slippage = ref - fill (positive is bad)
                     _side_val = str(pending.get("side", "")).upper() or ("BUY" if "BUY" in str(signal) else "SELL")
                     if _side_val == "BUY": _slippage = price - _ref_close
                     else: _slippage = _ref_close - price

                 self._append_mts_event("LEG_FILLED", 
                                       order_id=event.order_id, symbol=event.symbol, 
                                       price=price, qty=lots, slippage=round(_slippage, 1),
                                       ref_ohlc=_ref_ohlc)

                 order = self._get_lifecycle_order(event.order_id)
                 if order is not None and order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                     self._clear_pending_lifecycle_order(event.order_id)

                 self._save_orders_file_wrapper()
                 return f"MTS_LEG_FILL:{event.symbol}"

        pnl_pts = 0.0
        pnl_cash = 0.0
        friction_cost = 0.0
        direction = "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else ""
        if signal in ("EXIT", "PARTIAL_EXIT") and self.trader.entry_price > 0 and self.trader.position != 0:
            direction = "LONG" if self.trader.position > 0 else "SHORT"
            sign = 1 if self.trader.position > 0 else -1
            pnl_pts = (price - self.trader.entry_price) * sign
            gross = pnl_pts * self.trader.point_value * lots
            fee = self.trader.fee_per_side * 2 * lots
            exch_fee = self.trader.exchange_fee_per_side * 2 * lots
            tax = (self.trader.entry_price + price) * self.trader.point_value * self.trader.tax_rate * lots
            friction_cost = fee + exch_fee + tax
            pnl_cash = gross - friction_cost
            self._session_pnl += pnl_pts

        result = self.trader.execute_signal(
            signal,
            price,
            ts,
            lots=lots,
            max_lots=self.MGMT.get("max_positions", 2),
            stop_loss=stop_loss,
            break_even_trigger=break_even_trigger,
            trail_points=trail_points,
            exit_reason=reason,
        )
        if not result:
            save_signal_audit({
                "timestamp": ts,
                "signal": signal,
                "price": price,
                "reason": reason or "",
                "rejection": "confirmed_deal_rejected",
                "lots": lots,
            })
            return None

        self._applied_lifecycle_deals.add(deal_key)
        save_signal_audit({
            "timestamp": ts,
            "signal": signal,
            "price": price,
            "reason": reason or "",
            "rejection": "",
            "lots": lots,
        })
        save_trade({
            "type": signal,
            "timestamp": ts,
            "price": price,
            "lots": lots,
            "direction": direction,
            "pnl_pts": round(pnl_pts, 1),
            "pnl_cash": round(pnl_cash, 0),
            "friction_cost": round(friction_cost, 0),
            "reason": reason or "",
            "cross_policy": cross_policy,
        })

        if signal in ("BUY", "SELL"):
            ctx = getattr(self, "_last_bar_context", {})
            self._entry_features_futures = {
                "momentum": ctx.get("momentum", 0),
                "mom_velo": ctx.get("mom_velo", 0),
                "vwap_distance_pts": round(abs(price - ctx.get("vwap", price)), 1),
                "atr": ctx.get("atr", 0),
                "regime": ctx.get("regime", "UNKNOWN"),
                "score": ctx.get("score", 0),
                "entry_price": float(price),
            }
            save_trade({
                "type": "ENTRY_DIAG",
                "timestamp": ts,
                "signal": signal,
                "price": price,
                "lots": lots,
                "direction": direction,
                "reason": reason or "",
                "entry_diag": self._entry_features_futures,
                "cross_policy": cross_policy,
            })
            if self.live_trading and not self.dry_run:
                fill_direction = "LONG" if signal == "BUY" else "SHORT"
                sl_pts = stop_loss if stop_loss else self.RISK.get("stop_loss_pts", 60)
                self._place_safety_stop(price, fill_direction, lots, sl_pts)

        if signal in ("EXIT", "PARTIAL_EXIT") and hasattr(self, "_entry_features_futures") and self._entry_features_futures:
            from core.decision_logger import DecisionLogger

            outcome = {
                "pnl": float(pnl_cash),
                "pnl_pts": float(pnl_pts),
                "exit_price": float(price),
                "exit_reason": str(reason or "SIGNAL"),
            }
            DecisionLogger.log_trade_outcome(
                trade_id=f"FUT-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                strategy=self.active_strategy_name,
                regime=self._entry_features_futures.get("regime", "NORMAL"),
                features=self._entry_features_futures,
                outcome=outcome,
            )
            if signal == "EXIT":
                self._entry_features_futures = {}

        if signal in ("EXIT", "PARTIAL_EXIT") and pnl_pts < 0:
            sess = self.session_type or "day"
            self.consecutive_losses += 1
            self.session_losses.append((ts, pnl_pts, reason or "UNKNOWN", sess))
        elif signal in ("EXIT", "PARTIAL_EXIT"):
            self.consecutive_losses = 0

        order = self._get_lifecycle_order(event.order_id)
        if order is not None and order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            self._clear_pending_lifecycle_order(event.order_id)

        self._save_orders_file_wrapper()
        return result

    def on_order_event(self, order_state, data) -> None:
        """Apply one Shioaji futures callback to the canonical lifecycle.

        ``main.order_dispatcher`` is the single registered Shioaji callback.
        It dispatches futures events here; without this bridge, a broker
        receipt remains SUBMITTED forever even after an exchange fill.
        Local order identity is never inferred from contract/side: the broker
        receipt identifiers (id/seqno/ordno) must match an active order.
        """
        if not getattr(self, "order_mgr", None):
            return

        payload = self.order_mgr._payload_to_dict(data) or {}
        # Shioaji 1.7 may wrap order identity and fill fields under
        # ``order``/``deal``.  Preserve the raw payload, but promote only
        # missing canonical fields so broker identity can resolve an active
        # local order without guessing from symbol or side.
        for _nested_key in ("order", "deal", "trade"):
            _nested = payload.get(_nested_key)
            if isinstance(_nested, dict):
                for _key, _value in _nested.items():
                    if payload.get(_key) is None and _value is not None:
                        payload[_key] = _value
        # Shioaji 1.7 enum names are FuturesDeal/FuturesOrder but their
        # values are FDEAL/FORDER.  Never filter only on the display name:
        # doing so silently drops every live futures callback.
        state_name = str(getattr(order_state, "name", ""))
        state_value = str(getattr(order_state, "value", "")).upper()
        state_text = str(order_state)
        is_futures_deal = (
            state_name == "FuturesDeal"
            or state_value == "FDEAL"
            or state_text in ("FuturesDeal", "FDEAL")
        )
        is_futures_order = (
            state_name == "FuturesOrder"
            or state_value == "FORDER"
            or state_text in ("FuturesOrder", "FORDER")
        )
        if not (is_futures_deal or is_futures_order):
            return

        broker_order_id = (payload.get("id") or payload.get("broker_order_id")
                           or payload.get("order_id"))
        ordno = payload.get("ordno")
        seqno = payload.get("seqno")
        reason = str(payload.get("errmsg") or payload.get("reason") or "")

        if is_futures_deal:
            try:
                fill_price = float(payload.get("price") or 0)
                fill_qty = int(payload.get("quantity") or 0)
            except (TypeError, ValueError):
                return
            if fill_price <= 0 or fill_qty <= 0:
                return
            _deal_id = payload.get("trade_id") or payload.get("deal_id")
            if not (broker_order_id or _deal_id or seqno):
                # Fail-closed: no broker identity to correlate — never apply.
                self._append_mts_event(
                    "FILL_REJECTED_MISSING_BROKER_IDENTITY",
                    reason="no broker order/deal/seq identity in deal callback",
                )
                return
            _dedupe_order = self.order_mgr._resolve_order(
                None, broker_order_id=broker_order_id, seqno=seqno, ordno=ordno)
            if _dedupe_order is not None and not self._fills_bridge_mark_seen(
                    _dedupe_order, _deal_id, seqno,
                    exchange_seq=payload.get("exchange_seq")):
                return  # duplicate broker receipt — already applied (session or durable)
            order = self.order_mgr.apply_deal_fill(
                None,
                deal_id=payload.get("trade_id") or payload.get("deal_id"),
                fill_price=fill_price,
                fill_qty=fill_qty,
                exchange_fill_id=payload.get("trade_id") or payload.get("deal_id"),
                broker_trade_id=payload.get("trade_id"),
                exchange_seq=payload.get("exchange_seq"),
                raw_payload=payload,
                broker_order_id=broker_order_id,
                seqno=seqno,
                ordno=ordno,
                source="shioaji_callback",
                reason=reason,
            )
            if order is not None:
                self._leg_lock_apply_order_event(
                    order, getattr(order, "status", None), fill_qty=fill_qty)
            if order is None:
                logger.warning(
                    "[FUTURES_DEAL_UNMATCHED] id=%s seqno=%s ordno=%s",
                    broker_order_id, seqno, ordno,
                )
            return

        _updated_order = self.order_mgr.apply_order_update(
            None,
            raw_status=payload.get("status") or state_value,
            reason=reason,
            raw_payload=payload,
            broker_order_id=broker_order_id,
            seqno=seqno,
            ordno=ordno,
            source="shioaji_callback",
        )
        if _updated_order is not None:
            self._leg_lock_apply_order_event(
                _updated_order, getattr(_updated_order, "status", None))

    def _fills_bridge_mark_seen(self, order, deal_id, seqno,
                               exchange_seq=None) -> bool:
        """Return True if this broker fill identity is NEW (not seen before).

        Restart-safe: checks the order's durable fills first (order.fills
        carries the broker identity via OrderFill and round-trips through the
        orders JSON), so a restored order with an already-applied deal is never
        re-applied after a restart.  Deal-id match covers deal_id-carrying
        receipts; a seqno-only receipt (no deal id) dedupes against the durable
        fill's exchange_seq on the same order.  Session dedupe: bounded map
        keyed by (order id | deal id | seqno), entries older than 1h pruned at
        5000.  Fail-open on internal error — an explicit loss-vs-duplicate
        tradeoff: a dedupe fault may let one duplicate through (a visible
        over-fill the remaining-guard still caps) rather than silently drop a
        real fill (a lost fill would strand the position without a record).
        """
        try:
            _oid = getattr(order, "order_id", None)
            for _f in (getattr(order, "fills", None) or []):
                _fid = (getattr(_f, "broker_trade_id", None)
                        or getattr(_f, "exchange_fill_id", None)
                        or getattr(_f, "deal_id", None))
                if _fid and _fid == deal_id:
                    return False  # durable — already applied (restored state)
                _fseq = getattr(_f, "exchange_seq", None)
                if _fseq and exchange_seq and _fseq == exchange_seq:
                    return False  # seqno/exchange_seq-only receipt — already applied
            _key = f"{_oid}|{deal_id}|{seqno}"
            _seen = getattr(self, "_fills_bridge_seen", None)
            if _seen is None:
                _seen = self._fills_bridge_seen = {}
            _now = time.monotonic()
            if _key in _seen:
                return False
            if len(_seen) > 5000:
                _old = _now - 3600.0
                for _k in [k for k, v in _seen.items() if v < _old]:
                    del _seen[_k]
            _seen[_key] = _now
            return True
        except Exception:
            return True

    def _wire_order_callbacks(self):
        """Wire OrderManager callbacks to PaperTrader and audit system."""
        from core.order_management.order import OrderStatus, OrderSide

        def _on_fill_callback(event):
            if event.status not in (OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED):
                return
            msg = self._apply_confirmed_futures_deal(event)
            if msg:
                action = "BUY" if event.side == OrderSide.BUY else "SELL"
                # 2026-06-22 Gemini CLI: Use fill_qty to match OrderEvent
                console.print(f"[green]📦 Confirmed deal: {action} {event.fill_qty} @ {event.fill_price:.0f} deal={event.deal_id} → {msg}[/green]")
            
            # [GSD] Always update dashboard file to reflect latest OrderManager state (e.g. FILLED)
            self._save_orders_file_wrapper()
            self._maybe_finalize_exit_only_reconciliation()

        def _on_cancel_callback(event):
            console.print(f" [yellow]🚫 Order CANCELLED: {event.order_id} ({event.reason})[/yellow] ")
            self._fail_emergency_command(event, "CANCELLED")
            # 2026-07-07 Gemini CLI: P0: Stale callback guard based on lifecycle generation
            # 2026-07-07 Gemini CLI / Hermes Agent: Handle missing _lifecycle_generation gracefully for unit tests
            pending = self._pending_lifecycle_orders.get(event.order_id)
            _curr_gen = getattr(self, "_lifecycle_generation", 0)
            if pending and pending.get("generation", 0) < _curr_gen:
                import logging
                logging.getLogger("FuturesMonitor").warning(
                    f"[STALE_CALLBACK_IGNORED] Ignored stale cancel for order {event.order_id} "
                    f"(order generation {pending.get('generation', 0)} < current generation {_curr_gen})"
                )
                return
            self._clear_pending_lifecycle_order(event.order_id)
            self._save_orders_file_wrapper()
            self._maybe_finalize_exit_only_reconciliation()

        def _on_reject_callback(event):
            console.print(f"[red]❌ Order REJECTED: {event.order_id} ({event.reason})[/red]")
            self._fail_emergency_command(event, "REJECTED")
            # 2026-07-31: COMBINED_EXIT leg rejected -> repair path, never pretend completed
            try:
                _rp = self._pending_lifecycle_orders.get(event.order_id)
                if _rp and str(_rp.get("signal", "")).startswith("COMBINED_EXIT"):
                    self._handle_combined_exit_leg_rejected(event, _rp)
            except Exception as _re:
                import logging
                logging.getLogger().warning("[COMBINED_EXIT_REJECT_HANDLER_FAILED] %s", _re)
            # 2026-07-07 Gemini CLI: P0: Stale callback guard based on lifecycle generation
            # 2026-07-07 Gemini CLI / Hermes Agent: Handle missing _lifecycle_generation gracefully for unit tests
            pending = self._pending_lifecycle_orders.get(event.order_id)
            _curr_gen = getattr(self, "_lifecycle_generation", 0)
            if pending and pending.get("generation", 0) < _curr_gen:
                import logging
                logging.getLogger("FuturesMonitor").warning(
                    f"[STALE_CALLBACK_IGNORED] Ignored stale reject for order {event.order_id} "
                    f"(order generation {pending.get('generation', 0)} < current generation {_curr_gen})"
                )
                return
            self._clear_pending_lifecycle_order(event.order_id)
            self._save_orders_file_wrapper()
            self._maybe_finalize_exit_only_reconciliation()

        def _on_status_change(event):
            self._save_orders_file_wrapper()

        self.order_mgr.register_callback("on_fill", _on_fill_callback)
        self.order_mgr.register_callback("on_cancel", _on_cancel_callback)
        self.order_mgr.register_callback("on_reject", _on_reject_callback)
        self.order_mgr.register_callback("on_status_change", _on_status_change)
        self._save_orders_file_wrapper()

    def _submit_order_via_manager(self, signal, price, ts, lots, stop_loss=None, break_even_trigger=None, trail_points=None, reason=None):
        """Submit order through OrderManager and wait for confirmed deals to mutate PaperTrader."""
        from core.order_management.order import OrderType, OrderSide

        if signal == "BUY":
            side = OrderSide.BUY
            action = "Buy"
        elif signal == "SELL":
            side = OrderSide.SELL
            action = "Sell"
        elif signal in ("EXIT", "PARTIAL_EXIT"):
            if self.trader.position == 0:
                return None
            side = OrderSide.SELL if self.trader.position > 0 else OrderSide.BUY
            action = "Sell" if self.trader.position > 0 else "Buy"
        else:
            return None

        order_type = OrderType.MARKET  # Default to market; can be configured

        order = self.order_mgr.create_order(
            symbol=self.ticker, side=side, order_type=order_type,
            quantity=lots, strategy=reason or "UNKNOWN",
            comment=f"{signal} {reason or ''}".strip(),
        )
        self._pending_lifecycle_orders[order.order_id] = {
            "intent_id": order.intent_id,
            "signal": signal,
            "reason": reason,
            "stop_loss": stop_loss or self.RISK.get("stop_loss_pts", 60),
            "break_even_trigger": break_even_trigger,
            "trail_points": trail_points,
            "ts": ts,
            "lots": lots,
            "cross_policy": getattr(self, "_last_cross_policy", None),
        }

        console.print(f"[cyan]📤 Order SUBMITTED: {signal} {lots} @ {price:.0f} ({reason}) "
                      f"[order_id={order.order_id}][/cyan]")

        if self.live_trading and not self.dry_run:
            try:
                trade = self.client.place_order(self.contract, action=action,
                                                quantity=lots)
            except AdapterOrderError as e:
                # P0: structured durable failure — never swallowed
                self._clear_pending_lifecycle_order(order.order_id)
                self.order_mgr.reject(order.order_id,
                                      f"api_order_failed:{e.code}")
                console.print(
                    f"[red][FuturesMonitor] Live order failed: {e.code} "
                    f"{e.context}[/red]")
                return None
            if trade is None:
                self._clear_pending_lifecycle_order(order.order_id)
                self.order_mgr.reject(order.order_id, "api_order_failed")
                return None
            self.order_mgr.attach_submission(
                order.order_id,
                broker_trade=trade,
                broker_order_id=getattr(trade, "id", None),
                seqno=getattr(trade, "seqno", None),
                ordno=getattr(trade, "ordno", None),
                raw_status="Submitted",
            )
            return order.order_id

        self.order_mgr.submit(order, exchange_ordno=f"PAPER-{order.order_id}")
        self.paper_fill_sim.register(order)
        self.paper_fill_sim.process_tick(self._make_synthetic_tick(price, ts, symbol=order.symbol))
        return order.order_id

    def _make_synthetic_tick(self, price, ts, symbol=None):
        """Create a synthetic tick object from price/timestamp for PaperFillSimulator."""
        tick = type("Tick", (), {})()
        tick.code = symbol or self.ticker
        tick.datetime = ts if hasattr(ts, "strftime") else datetime.now()
        tick.close = price
        tick.open = price
        tick.high = price
        tick.low = price
        tick.volume = 0
        return tick

    # ── Paper Fill Polling (ADR-010 OCO) ──

    def _reconcile_paper_oco_orders_from_state(self) -> None:
        """Re-register OCO orders from state file (no strategy dependency).
        
        Reads /tmp/mts_position_state.json directly for lifecycle.release_group.
        Used at startup before market open when strategy lifecycle isn't restored yet.
        """
        if not self.paper_fill_sim or not self.order_mgr:
            return
        # [S0] OCO is disabled at the gateway boundary (ALL modes — no
        # Order construction, no submit) until S9 routes it through
        # the gateway.  Direct OCO order/submit paths fail closed here.
        self._append_mts_event(
            "ORDER_INTENT_BLOCKED", action="OCO_RECONCILE",
            reason="GATEWAY_OCO_DISABLED", trade_id="")
        return

        _state_path = _mts_position_state_path()
        if not _state_path.exists():
            return

        try:
            _state = json.loads(_state_path.read_text())
        except Exception:
            return

        _lc = _state.get("lifecycle", {})
        _rg = _lc.get("release_group", {})
        if _rg.get("status") != "SUBMITTED":
            return
        # 2026-07-07 Hermes Agent: P0 — terminal guard (same rationale as
        # _reconcile_paper_oco_orders at L2850).  If the state file records
        # that a leg was already filled, do NOT re-register.
        if _rg.get("filled_leg") is not None:
            return

        _near_oid = _rg.get("near_order_id")
        _far_oid = _rg.get("far_order_id")
        if not _near_oid or not _far_oid:
            return

        _pending_ids = set(self.paper_fill_sim._pending_orders.keys())
        # 2026-07-07 Hermes Agent: P0 — also check completed orders to
        # prevent re-registering orders already filled in a prior poll cycle.
        _completed_ids = {o.order_id for o in (self.order_mgr.completed or [])}
        _need_near = (
            _near_oid not in _pending_ids
            and _near_oid not in self.order_mgr.active_orders
            and _near_oid not in _completed_ids
        )
        _need_far = (
            _far_oid not in _pending_ids
            and _far_oid not in self.order_mgr.active_orders
            and _far_oid not in _completed_ids
        )
        if not _need_near and not _need_far:
            return

        from core.order_management.order import Order, OrderType, OrderSide

        _near_symbol = self.contract.code if self.contract else f"{self.ticker}_NEAR"
        _far_symbol = self.far_contract.code if self.far_contract else f"{self.ticker}_FAR"
        _near_entry = _state.get("near_entry", 0)
        _far_entry = _state.get("far_entry", 0)
        _near_side = _state.get("near_side", "")
        _far_side = _state.get("far_side", "")

        if _need_near:
            _ns = OrderSide.BUY if str(_near_side).upper() == "SHORT" else OrderSide.SELL
            _no = Order(symbol=_near_symbol, side=_ns, order_type=OrderType.MKP,
                        quantity=1, strategy="MTS_RELEASE_OCO", order_id=_near_oid)
            self.order_mgr.active_orders[_near_oid] = _no
            self.order_mgr.submit(_no)
            self.paper_fill_sim.register(_no)
            console.print(f"[cyan]♻️ [OCO_RECONCILE] near={_near_oid} from state file[/cyan]")

        if _need_far:
            _fs = OrderSide.BUY if str(_far_side).upper() == "SHORT" else OrderSide.SELL
            _fo = Order(symbol=_far_symbol, side=_fs, order_type=OrderType.MKP,
                        quantity=1, strategy="MTS_RELEASE_OCO", order_id=_far_oid)
            self.order_mgr.active_orders[_far_oid] = _fo
            self.order_mgr.submit(_fo)
            self.paper_fill_sim.register(_fo)
            console.print(f"[cyan]♻️ [OCO_RECONCILE] far={_far_oid} from state file[/cyan]")

    def _reconcile_paper_oco_orders(self, strategy) -> None:
        """Re-register OCO release orders with paper_fill_sim after PM2 restart.
        
        After restart, _lifecycle_oca is restored from state file (including
        near_order_id/far_order_id in SUBMITTED status), but paper_fill_sim
        is a fresh in-memory queue. Orders exist in the lifecycle but not in
        the simulator → they become orphans that never fill.
        
        Called from _mts_tick() after strategy init, before price polling.
        """
        if not self.paper_fill_sim or not self.order_mgr:
            return
        # [S0] OCO is disabled at the gateway boundary (ALL modes — no
        # Order construction, no submit) until S9 routes it through
        # the gateway.  Direct OCO order/submit paths fail closed here.
        self._append_mts_event(
            "ORDER_INTENT_BLOCKED", action="OCO_RECONCILE",
            reason="GATEWAY_OCO_DISABLED", trade_id="")
        return
        if not hasattr(strategy, '_lifecycle_oca'):
            return

        _lc = strategy._lifecycle_oca
        _rg = _lc.release_group
        from strategies.plugins.futures.active.tmf_spread import ReleaseGroupStatus

        _status_val = _rg.status.value if hasattr(_rg.status, 'value') else str(_rg.status)
        if _status_val != "SUBMITTED":
            return
        # 2026-07-07 Hermes Agent: P0 — terminal guard.
        # If filled_leg is already set, the OCO has been partially or fully
        # resolved.  Reconciliation must NOT re-register orders that belong
        # to a resolved release_group.  This is the cross-tick idempotency
        # guard that prevents the reconcile→fill→reconcile→fill loop.
        if _rg.filled_leg is not None:
            return
        if not _rg.near_order_id or not _rg.far_order_id:
            return

        # Check if already registered (e.g., after fresh submission in same run)
        _pending_ids = set(self.paper_fill_sim._pending_orders.keys())
        # 2026-07-07 Hermes Agent: P0 — also check completed orders.
        # After fill, an OCO leg moves from active_orders → completed.
        # Without this check, the reconciliation loop re-registers the
        # same order every tick, causing infinite re-fill (runaway loop).
        _completed_ids = {o.order_id for o in (self.order_mgr.completed or [])}
        _need_near = _rg.near_order_id not in _pending_ids and _rg.near_order_id not in _completed_ids
        _need_far = _rg.far_order_id not in _pending_ids and _rg.far_order_id not in _completed_ids
        if not _need_near and not _need_far:
            return  # both already registered

        # Reconstruct orders in OrderManager if missing
        from core.order_management.order import OrderType, OrderSide

        _near_side_str = (_rg.near_side or "").upper()
        _far_side_str = (_rg.far_side or "").upper()
        _near_symbol = self.contract.code if self.contract else f"{self.ticker}_NEAR"
        _far_symbol = self.far_contract.code if self.far_contract else f"{self.ticker}_FAR"

        if _need_near and _rg.near_order_id not in self.order_mgr.active_orders:
            _ns = OrderSide.SELL if _near_side_str == "BUY" else OrderSide.BUY
            from core.order_management.order import Order
            _no = Order(
                symbol=_near_symbol, side=_ns, order_type=OrderType.MKP,
                quantity=1, strategy="MTS_RELEASE_OCO",
                order_id=_rg.near_order_id,
            )
            self.order_mgr.active_orders[_rg.near_order_id] = _no
            self.order_mgr.submit(_no)
            self.paper_fill_sim.register(_no)
            console.print(f"[cyan]♻️ [OCO_RECONCILE] near order {_rg.near_order_id} re-registered[/cyan]")

        if _need_far and _rg.far_order_id not in self.order_mgr.active_orders:
            _fs = OrderSide.SELL if _far_side_str == "BUY" else OrderSide.BUY
            from core.order_management.order import Order
            _fo = Order(
                symbol=_far_symbol, side=_fs, order_type=OrderType.MKP,
                quantity=1, strategy="MTS_RELEASE_OCO",
                order_id=_rg.far_order_id,
            )
            self.order_mgr.active_orders[_rg.far_order_id] = _fo
            self.order_mgr.submit(_fo)
            self.paper_fill_sim.register(_fo)
            console.print(f"[cyan]♻️ [OCO_RECONCILE] far order {_rg.far_order_id} re-registered[/cyan]")

        # 2026-07-07 Hermes Agent: Reindex counter after reconcile to prevent
        # backtracking on PM2 restart. Reconciled orders carry persisted IDs
        # (e.g., ORD-20260707-000003) — _next_id must resume from max+1.
        if self.order_mgr:
            self.order_mgr.reindex_orders()

    def _process_pending_paper_fills(self, near_price: float, far_price: float, ts) -> None:
        """Poll paper fill simulator with live near/far prices during MTS tick.
        
        ADR-010 OCO release bracket orders are registered with paper_fill_sim
        at submission time. Without ongoing polling, they become orphaned and
        never fill even when the market moves past their levels.
        
        Called from _mts_tick() on every poll cycle with real-time prices.

        2026-07-07 Hermes Agent: Atomic OCO guard.  After processing the
        first tick, if pending_count dropped (an OCO leg was filled or
        cancelled), break immediately instead of feeding the second tick.
        This prevents the double-fill bug where both near and far OCO
        orders fill in the same poll cycle because the callback hasn't
        had a chance to cancel the sibling yet.
        """
        if not self.paper_fill_sim or self.paper_fill_sim.get_pending_count() == 0:
            # [DEBUG 2026-07-07] Diagnose why OCO orders aren't filling
            _pc = self.paper_fill_sim.get_pending_count() if self.paper_fill_sim else -1
            if _pc == 0:
                import logging
                _log = logging.getLogger("FuturesMonitor")
                _log.info(
                    "[MTS][PAPER_FILL_DEBUG] pending_count=0 near=%.1f far=%.1f",
                    near_price, far_price,
                )
            return

        _near_symbol = self.contract.code if self.contract else f"{self.ticker}_NEAR"
        _far_symbol = self.far_contract.code if self.far_contract else f"{self.ticker}_FAR"

        ticks = [
            self._make_synthetic_tick(near_price, ts, symbol=_near_symbol),
            self._make_synthetic_tick(far_price, ts, symbol=_far_symbol),
        ]

        for tick in ticks:
            # [ADR-010] Pre-tick guard: don't feed second tick if OCO already resolved
            _rg = None
            _strat = self._registry.get("tmf_spread") if hasattr(self, "_registry") else None
            if _strat and hasattr(_strat, "_lifecycle_oca"):
                _rg = _strat._lifecycle_oca.release_group
            if _rg is not None and getattr(_rg, "filled_leg", None) is not None:
                console.print("[dim][PAPER_FILL] OCO already filled — skipping remaining ticks[/dim]")
                break

            # 2026-07-07 Hermes Agent: snapshot pending before processing.
            # If an OCO leg gets filled/cancelled during process_tick(),
            # the sibling must not be processed in the same cycle.
            _before_ids = set(self.paper_fill_sim._pending_orders.keys())

            try:
                self.paper_fill_sim.process_tick(tick)
            except Exception as exc:
                import logging
                _log = logging.getLogger("FuturesMonitor")
                _log.exception("[MTS][PAPER_FILL] process_tick failed for symbol=%s: %s",
                               getattr(tick, "code", "?"), exc)
                console.print(
                    f"[red]⚠️ [PAPER_FILL_ERR] tick={getattr(tick, 'code', '?')} "
                    f"close={getattr(tick, 'close', 0)} err={exc}[/red]"
                )

            # 2026-07-07 Hermes Agent: Post-tick guard — break if any
            # OCO order was consumed (filled or cancelled) during this tick.
            # This is more reliable than checking filled_leg because the
            # callback may not have propagated to release_group yet.
            _after_ids = set(self.paper_fill_sim._pending_orders.keys())
            if _after_ids != _before_ids:
                _consumed = _before_ids - _after_ids
                # 2026-07-31: COMBINED_EXIT legs are INDEPENDENT closes — both must
                # fill. OCO sibling protection only applies to release brackets
                # (first leg filled => cancel sibling). Breaking here would starve
                # the second CE leg's tick in this cycle.
                _ce_consumed = any(
                    str(self._pending_lifecycle_orders.get(oid, {}).get("signal", "")).startswith("COMBINED_EXIT")
                    for oid in _consumed
                )
                if _ce_consumed:
                    console.print(
                        f"[dim][PAPER_FILL] COMBINED_EXIT leg consumed={_consumed} "
                        f"— continuing to feed sibling (both legs must fill)[/dim]"
                    )
                else:
                    console.print(
                        f"[bold yellow]🔒 [OCO_ATOMIC] consumed={_consumed} "
                        f"— breaking poll loop (sibling protected)[/bold yellow]"
                    )
                    break

            # [ADR-010] Post-tick guard: break if OCO filled_leg is now set
            # (kept as backup even with the snapshot guard above)
            if _rg is not None and getattr(_rg, "filled_leg", None) is not None:
                console.print("[dim][PAPER_FILL] OCO filled_leg set — breaking poll loop[/dim]")
                break

    # ── Margin check ──
    def _margin_sufficient(self):
        """Check if account has enough margin before placing entry order."""
        try:
            margin = self.api.margin(self.api.futopt_account)
            equity = margin.equity
            reserve_pct = 0.20  # 保留 20% 不動用
            available = equity * (1 - reserve_pct)
            required = margin.initial_margin if margin.initial_margin > 0 else 17000  # MXF 一口約 17,000
            if available < required:
                console.print(f"[red]Margin check: equity={equity:.0f} available={available:.0f} < required={required:.0f}[/red]")
                return False
            console.print(f"[dim]Margin OK: equity={equity:.0f} available={available:.0f}[/dim]")
            return True
        except Exception as e:
            console.print(f" [yellow]Margin check failed: {e} — allowing order[/yellow] ")
            return True  # API 查詢失敗不擋單，讓交易所擋

    # ── Trade execution ──
    def _audit_signal(self, signal_type, side, score, rejection_reason, note=""):
        """Record signal audit trail to CSV (thread-safe, MXF file)."""
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
        save_signal_audit({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "signal": signal_type,
            "side": side,
            "score": score,
            "rejection": rejection_reason,
            "note": note,
        }, ticker=self.ticker)

    def _ensure_strategy_initialized(self, strategy_name, strategy, ctx):
        """Initialize a strategy instance once before the router calls it."""
        if not hasattr(self, "_initialized_strategy_names"):
            self._initialized_strategy_names = set()
        if strategy_name in self._initialized_strategy_names:
            return
        strategy.init(ctx)
        self._initialized_strategy_names.add(strategy_name)

    def _has_active_working_order(self):
        if not getattr(self, "_use_order_manager", False) or self.order_mgr is None:
            return False
        try:
            return any(order.symbol == self.ticker for order in self.order_mgr.get_pending())
        except Exception:
            return False

    def _get_symbol_pending_orders(self):
        if not getattr(self, "_use_order_manager", False) or self.order_mgr is None:
            return []
        try:
            return [order for order in self.order_mgr.get_pending() if order.symbol == self.ticker]
        except Exception:
            return []

    def _has_pending_flattening_order(self, pending_orders=None):
        pending_orders = pending_orders or []
        active_order_ids = {
            getattr(order, "order_id", None) for order in pending_orders if getattr(order, "order_id", None)
        }
        for order_id, meta in getattr(self, "_pending_lifecycle_orders", {}).items():
            signal = str(meta.get("signal", "")).upper()
            if signal not in {"EXIT", "PARTIAL_EXIT"}:
                continue
            if not active_order_ids or order_id in active_order_ids:
                return True
        return False

    @staticmethod
    def _format_router_audit_note(decision, bar_regime):
        parts = [
            f"reason={decision.reason}",
            f"regime={bar_regime.regime}",
            f"bias={bar_regime.bias}",
            f"session={bar_regime.session_regime}",
        ]
        if decision.selected_strategy:
            parts.append(f"selected={decision.selected_strategy}")
        if decision.candidates:
            parts.append(f"candidates={','.join(decision.candidates)}")
        if decision.notes:
            parts.append(f"notes={' | '.join(decision.notes)}")
        return "; ".join(parts)

    def _build_strategy_context(self, bar, session_regime):
        """Build strategy context from bar data."""
        # Get dataframes from the current processing pipeline
        df_5m = None
        df_15m = None
        try:
            processed = getattr(self, '_last_processed_data', None)
            if processed is not None:
                df_5m = processed.get("5m", None)
                df_15m = processed.get("15m", None)
        except Exception:
            pass
        
        # [Skew Integration] Compute option skew signal from quote store
        skew_signal = None
        skew_regime = None
        if self._skew_engine is not None:
            try:
                close_price = bar.get("close", 0) or 0
                if close_price > 0:
                    skew_signal = self._skew_engine.compute_if_ready(
                        futures_price=close_price,
                        force=False,
                    )
                    if skew_signal.is_valid():
                        skew_signal = skew_signal.to_dict()
                        logger.info(
                            "[FuturesMonitor] ctx.market.skew_signal injected: "
                            "direction=%s confidence=%.3f",
                            skew_signal.get("direction", "?"),
                            skew_signal.get("confidence", 0),
                        )
                    else:
                        skew_signal = None

                    # [Skew Integration / Phase 2] IV curve shape classification
                    try:
                        snapshot = self._skew_engine.surface_snapshot(
                            futures_price=close_price,
                        )
                        if snapshot.is_valid():
                            # Lazily init shape classifier on first valid snapshot
                            if not hasattr(self, '_skew_shape_classifier') or self._skew_shape_classifier is None:
                                from core.derivatives.shape_classifier import IVShapeClassifier
                                self._skew_shape_classifier = IVShapeClassifier()
                            # Lazily init IV percentile engine
                            if not hasattr(self, '_skew_percentile') or self._skew_percentile is None:
                                from core.derivatives.iv_percentile import IVPercentileEngine
                                self._skew_percentile = IVPercentileEngine(
                                    window_sec=7200, min_samples=30,
                                )
                            # Record ATM IV into rolling percentile window
                            self._skew_percentile.record(atm_iv=snapshot.atm_iv)

                            regime = self._skew_shape_classifier.classify(
                                atm_iv=snapshot.atm_iv,
                                otm_put_iv=snapshot.otm_put_iv,
                                otm_call_iv=snapshot.otm_call_iv,
                                underlying_price=snapshot.underlying_price,
                                timestamp=snapshot.timestamp,
                            )

                            # Merge IV percentile / z-score into the regime dict
                            pct = self._skew_percentile.get_percentile(
                                atm_iv=snapshot.atm_iv,
                            )
                            regime.iv_percentile = pct.get("iv_percentile", 0.0)
                            regime.iv_zscore = pct.get("iv_zscore", 0.0)

                            # [VolStateMachine] Lazy init + update
                            if not hasattr(self, '_skew_vol_state_machine') or self._skew_vol_state_machine is None:
                                from core.derivatives.vol_state_machine import VolatilityStateMachine
                                self._skew_vol_state_machine = VolatilityStateMachine()
                            vol_state = self._skew_vol_state_machine.update(
                                directional_skew=regime.directional_skew,
                                tension=regime.tension,
                                iv_percentile=regime.iv_percentile,
                                confidence=regime.confidence,
                                timestamp=regime.timestamp,
                            )

                            skew_regime = regime.to_dict()
                            skew_regime["vol_state"] = str(vol_state.state)
                            skew_regime["vol_state_age_sec"] = vol_state.age_sec
                            skew_regime["vol_state_transition_count"] = vol_state.transition_count
                            skew_regime["vol_state_persistent"] = vol_state.persistent

                            logger.info(
                                "[VolState] state=%s age=%ds persistent=%s "
                                "transitions=%d skew=%s tension=%s "
                                "pct=%.2f z=%.2f conf=%.2f",
                                skew_regime.get("vol_state", "?"),
                                skew_regime.get("vol_state_age_sec", 0),
                                skew_regime.get("vol_state_persistent", False),
                                skew_regime.get("vol_state_transition_count", 0),
                                skew_regime.get("directional_skew", "?"),
                                skew_regime.get("tension", "?"),
                                skew_regime.get("iv_percentile", 0),
                                skew_regime.get("iv_zscore", 0),
                                skew_regime.get("confidence", 0),
                            )

                            # [SkewRegimeLogger] Persist every decision
                            if not hasattr(self, '_skew_regime_logger') or self._skew_regime_logger is None:
                                from core.derivatives.skew_regime_logger import SkewRegimeLogger
                                self._skew_regime_logger = SkewRegimeLogger()
                            try:
                                self._skew_regime_logger.write(skew_regime)
                            except Exception:
                                pass
                        else:
                            # No option data — indicate UNKNOWN vol state
                            logger.info(
                                "[VolState] state=UNKNOWN reason=no_option_data "
                                "atm_strike=%.0f otm_put_strike=%.0f otm_call_strike=%.0f",
                                snapshot.atm_strike,
                                snapshot.otm_put_strike,
                                snapshot.otm_call_strike,
                            )
                            # Write UNKNOWN to JSONL too
                            if not hasattr(self, '_skew_regime_logger') or self._skew_regime_logger is None:
                                from core.derivatives.skew_regime_logger import SkewRegimeLogger
                                self._skew_regime_logger = SkewRegimeLogger()
                            try:
                                unknown_record = {
                                    "vol_state": "UNKNOWN",
                                    "reason": "no_option_data",
                                    "timestamp": str(snapshot.timestamp) if snapshot.timestamp else None,
                                }
                                self._skew_regime_logger.write(unknown_record)
                            except Exception:
                                pass
                    except Exception as e:
                        logger.warning("[FuturesMonitor] shape_classifier error: %s", e)
                        skew_regime = None
            except Exception:
                skew_signal = None
                skew_regime = None

        # [V-Model] Enrich bar with calendar spread data (spread_z, near_close, far_close)
        if self._spread_loaded:
            try:
                self._spread_loader.enrich_bar(bar)
            except Exception as e:
                print(f"[V-Model] enrich_bar failed: {e}")

        ctx = StrategyContext(
            market=MarketData(
                last_bar=bar,
                # 2026-05-27 Gemini CLI: Pass current ticker to strategy context
                ticker=self.ticker,
                df_5m=df_5m,
                df_15m=df_15m,
                timestamp=bar.get('timestamp', ''),
                session=int(bar.get('session', 0)),
                regime=session_regime,
                flags=self._data_flags if hasattr(self, '_data_flags') and self._data_flags else None,
                skew_signal=skew_signal,
                skew_regime=skew_regime,
            ),
            position=PositionView(
                size=self.trader.position,
                entry_price=self.trader.entry_price,
                current_stop_loss=getattr(self.trader, "current_stop_loss", None),
                unrealized_pnl=getattr(self.trader, "unrealized_pnl", 0),
                has_tp1_hit=self.has_tp1_hit,
            ),
            config=self.cfg,
            bar_counter=self._bar_counter,
        )
        return ctx

    def _route_signal(self, bar, session_regime, active_name=None, pending_orders=None, attribution_recorder=None):
        """Route signal through strategy router with optional attribution."""
        _ts = bar.get("timestamp") or (bar.name if hasattr(bar, "name") else "unknown")
        console.print(f"[ROUTE_SIGNAL_ENTER] ts={_ts} active={active_name}")
        
        # Build context
        ctx = self._build_strategy_context(bar, session_regime)

        # [Phase 2 Fix] Skip routing on prefill/warmup bars (old data from Parquet/CSV)
        # Check if bar timestamp is from current trading day
        _raw_bar_ts = bar.get("timestamp")
        if _raw_bar_ts is None and hasattr(bar, "name"):
             _raw_bar_ts = bar.name
        
        bar_ts = _raw_bar_ts
        if bar_ts is not None:
            from core.date_utils import get_trading_day
            try:
                bar_td = get_trading_day(pd.Timestamp(bar_ts))
                current_td = get_trading_day(pd.Timestamp(datetime.now()))
                if bar_td != current_td:
                    console.print(f"[dim][Router] Skip prefill bar: ts={bar_ts} trading_day={bar_td} != current={current_td}[/dim]")
                    # [V-Model] write_trace for prefill skip
                    from core.strategy_eval import RouterTrace, write_trace as _wt
                    _wt(RouterTrace(
                        ts=str(bar_ts),
                        regime="PREFILL",
                        bias="",
                        selected=None,
                        selected_action="PREFILL_SKIP",
                        strategies=[],
                    ))
                    return None, ctx, session_regime, None
            except Exception:
                pass

        # [Phase 2: Skew Filter] Gate pre-check
        skew_signal = getattr(ctx.market, "skew_signal", None)
        if skew_signal and isinstance(skew_signal, dict):
            direction = skew_signal.get("direction", "UNKNOWN")
            confidence = skew_signal.get("confidence", 0.0)
            skew_threshold = self.cfg.get("skew", {}).get("filter_threshold", 0.70)
            if direction == "BEAR" and confidence >= skew_threshold and self.trader.position == 0:
                console.print(
                    f" [yellow][SkewGate] BLOCK entry — skew BEAR "
                    f"confidence={confidence:.2f} >= {skew_threshold:.2f}[/yellow] "
                )
                bar_regime = classify_futures_bar_regime(bar, session_regime=session_regime)
                # [V-Model] write_trace for skew gate block
                from core.strategy_eval import RouterTrace, write_trace as _wt2
                _wt2(RouterTrace(
                    ts=_ts,
                    regime=bar_regime.regime,
                    bias=bar_regime.bias,
                    selected=None,
                    selected_action="SKEW_GATE_BLOCK",
                    strategies=[],
                ))
                from core.futures_strategy_router import FuturesRouterDecision
                decision = FuturesRouterDecision(
                    is_trade=False,
                    action="skip",
                    reason=f"SKEW_GATE_BEAR_conf_{confidence:.2f}",
                    selected_strategy=None,
                    signal=None,
                    regime=bar_regime.regime,
                    bias=bar_regime.bias,
                    candidates=[]
                )
                return decision, ctx, session_regime, bar_regime

        # Get pending orders if not provided
        if pending_orders is None:
            pending_orders = self._get_symbol_pending_orders()
        
        # Classify bar regime
        console.print(f"[dim][ROUTE_SIGNAL_PRE_CLASSIFY] ts={_ts} regime_from_bar={bar.get('regime', '?')} sqz_on={bar.get('sqz_on', '?')}[/dim]")
        bar_regime = classify_futures_bar_regime(bar, session_regime=session_regime)
        
        # [Patch] Override context.market.regime with bar_regime
        object.__setattr__(ctx.market, 'regime', bar_regime.regime)

        # [P1] Single Source of Truth Contract: inject into bar dict
        _b = str(bar_regime.bias).strip().upper()
        _r = str(bar_regime.regime).strip().upper()
        bar["router_bias"] = _b
        bar["router_regime"] = _r
        bar["bias"] = _b
        bar["regime"] = _r

        # ── [GSD] Schema Compliance Check ────────────────────────────
        required_cols = {"Close", "High", "Low", "Open", "Volume", "atr", "vwap", "router_regime", "router_bias"}
        missing = required_cols - set(bar.keys())
        if missing:
            logger.warning(f"[SCHEMA_VIOLATION] Bar is missing required columns: {missing}")
        # ──────────────────────────────────────────────────────────────

        # Route signal
        from core.futures_strategy_router import route_futures_signal
        decision = route_futures_signal(
            registry=self._registry,
            context=ctx,
            regime_result=bar_regime,
            active_strategy_name=active_name,
            current_working_orders=pending_orders,
            is_flattening=self._has_pending_flattening_order(pending_orders),
            prepare_strategy=lambda name, strategy: self._ensure_strategy_initialized(name, strategy, ctx),
            recorder=attribution_recorder
        )
        self.latest_router_decision = decision
        return decision, ctx, session_regime, bar_regime

    def _append_mts_event_checked(self, event_type: str, **kwargs):
        """Append an MTS event and report whether it was durably written.

        Most historical telemetry is best-effort.  Entry decision evidence is
        different: without a durable decision record we must not create broker
        intents that cannot later be explained.  Keep this primitive small so
        the normal writer remains backward-compatible while entry can enforce
        the all-or-nothing contract.
        """
        try:
            _dir = runtime_logs()
            if not os.path.exists(_dir):
                os.makedirs(_dir, exist_ok=True)
            # 2026-06-25 Gemini CLI / Hermes Agent: environmental isolation for MTS spread events
            path = os.getenv("MTS_EVENT_LOG_PATH", os.path.join(_dir, "mts_spread_events.jsonl"))
            event = {"event": event_type, "ts": datetime.now().isoformat()}
            event.update(kwargs)
            with open(path, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
            return True
        except Exception:
            return False

    def _append_mts_event(self, event_type: str, **kwargs):
        """Append an MTS-specific event to the shared event ledger.

        Legacy telemetry remains best-effort; callers that require a durable
        audit record use :meth:`_append_mts_event_checked` explicitly.
        """
        try:
            _dir = runtime_logs()
            if not os.path.exists(_dir):
                os.makedirs(_dir, exist_ok=True)
            path = os.getenv("MTS_EVENT_LOG_PATH", os.path.join(_dir, "mts_spread_events.jsonl"))
            event = {"event": event_type, "ts": datetime.now().isoformat()}
            event.update(kwargs)
            with open(path, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception:
            pass

    def _mts_ledger_reconstructed_open_qty(self, trade_id: str) -> dict[str, int] | None:
        """Rebuild per-leg open quantity from the append-only fills ledger.

        Source of truth: logs/mts_trade_fills.jsonl (every confirmed fill).
        For each (trade_id, leg): ENTRY adds qty, RELEASE/EXIT/COMBINED_EXIT*
        subtracts qty. A leg is FLAT when reconstructed open qty <= 0.

        This is the INDEPENDENT hard gate for COMBINED_EXIT: it does NOT trust
        strategy in-memory state (phase / _near_qty / _far_qty), which can be
        stale if the release-fill callback failed to sync.

        Returns dict {"NEAR": int, "FAR": int} when the ledger exists and was
        scanned, or None when the ledger is absent/unreadable (caller must
        fall back to strategy-level gates — do not block on missing evidence).
        """
        if not trade_id:
            return None
        _fills_path = os.environ.get("MTS_FILL_LOG_PATH") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "logs", "mts_trade_fills.jsonl",
        )
        if not os.path.exists(_fills_path):
            return None
        _open_qty = {"NEAR": 0, "FAR": 0}
        _found_entry = False  # any ENTRY fill for this trade_id in the ledger
        _TERMINAL_TYPES = {"RELEASE", "EXIT", "COMBINED_EXIT", "COMBINED_EXIT_NEAR", "COMBINED_EXIT_FAR"}
        try:
            with open(_fills_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _rec = json.loads(_line)
                    except Exception:
                        continue
                    if str(_rec.get("trade_id") or "") != str(trade_id):
                        continue
                    _leg = str(_rec.get("leg") or _rec.get("contract") or "").upper()
                    if _leg not in ("NEAR", "FAR"):
                        continue
                    try:
                        _qty = int(_rec.get("qty") or 0)
                    except (ValueError, TypeError):
                        continue
                    _ft = str(_rec.get("fill_type", "")).upper()
                    if _ft == "ENTRY":
                        _found_entry = True
                        _open_qty[_leg] += _qty
                    elif _ft in _TERMINAL_TYPES:
                        _open_qty[_leg] -= _qty
        except Exception:
            logger.exception("[MTS_LEDGER_SCAN_FAILED] fills ledger unreadable")
            return None
        # No ENTRY evidence for this trade -> cannot reconstruct -> caller must
        # fall back to strategy-level gates (do NOT block on missing evidence).
        if not _found_entry:
            return None
        return _open_qty

    def _reset_mts_dynamics_for_session(self, bar) -> None:
        """Reset the dynamics calculator when the bar's session changes so
        derivatives never span sessions (day -> night etc.)."""
        _sess = bar.get("session_type")
        if _sess is None or _sess == getattr(
                self, "_spread_dynamics_session", None):
            return
        self._spread_dynamics_session = _sess
        _sd = getattr(self, "_spread_dynamics", None)
        if _sd is None:
            return
        from strategies.futures.mts.spread_dynamics import (
            SpreadDynamicsCalculator)
        self._spread_dynamics = SpreadDynamicsCalculator(
            tau_sec=_sd.tau_sec, window_sec=_sd.window_sec,
            min_dt_sec=_sd.min_dt_sec,
            max_derivative_gap_sec=_sd.max_derivative_gap_sec,
            min_abs_z_for_ratio=_sd.min_abs_z_for_ratio,
            min_slope_samples=_sd.min_slope_samples,
            min_slope_duration_sec=_sd.min_slope_duration_sec)

    def _update_mts_spread_dynamics(self, bar: dict):
        """Dynamics telemetry seam (bar-timestamp clock, session-aware).

        Bar timestamps drive dt so behavior is deterministic and a
        session_type change resets the calculator so derivatives never
        span sessions.  The production tick pipeline uses
        _apply_spread_dynamics (real arrival time — the 5-minute bar
        cadence would otherwise trip max_derivative_gap_sec on every bar).
        Returns the metrics; never raises.
        """
        _sd = getattr(self, "_spread_dynamics", None)
        _z = bar.get("spread_z")
        if _sd is None or _z is None:
            return None
        try:
            self._reset_mts_dynamics_for_session(bar)
            _sd = getattr(self, "_spread_dynamics", None)
            _ts_raw = bar.get("ts") or bar.get("timestamp") or time.time()
            if hasattr(_ts_raw, "timestamp"):
                _ts_float = float(_ts_raw.timestamp())
            elif hasattr(_ts_raw, "tz"):
                _ts_float = float(pd.Timestamp(_ts_raw).timestamp())
            else:
                _ts_float = float(_ts_raw)
            _m = _sd.update(_ts_float, float(_z))
            _raw = (float(bar.get("near_close", 0.0) or 0.0)
                    - float(bar.get("far_close", 0.0) or 0.0))
            bar["raw_spread"] = _raw
            bar["dz"] = _m.z_velocity
            bar["spread_slope"] = _m.rolling_slope
            bar["velocity_ema"] = _m.velocity_ema
            bar["spread"] = _raw
            return _m
        except Exception:
            return None

    def _apply_spread_dynamics(self, bar: dict) -> None:
        """Wire the SpreadDynamicsCalculator into the bar pipeline so the
        entry_observation research records carry dz / spread_slope /
        velocity_ema (research wiring gap — the calculator was never
        called, leaving those columns NULL).  Also records the spread into
        the candidate payload.  Never raises; the research features are
        best-effort telemetry."""
        _sd = getattr(self, "_spread_dynamics", None)
        _z = bar.get("spread_z")
        if _sd is None or _z is None:
            return
        try:
            self._reset_mts_dynamics_for_session(bar)
            # Real arrival time, not bar timestamps: bars carry a 5-minute
            # cadence whose dt (300s) exceeds the calculator's
            # max_derivative_gap_sec=15, resetting the sample counter every
            # update so dz / spread_slope never form (the evaluation is
            # stuck in CANDIDATE_AWAITING_EVALUATION forever).
            _m = _sd.update(time.time(), float(_z))
            bar["dz"] = _m.z_velocity
            bar["spread_slope"] = _m.rolling_slope
            bar["velocity_ema"] = _m.velocity_ema
            bar.setdefault(
                "spread",
                float(bar.get("near_close", 0.0) or 0.0)
                - float(bar.get("far_close", 0.0) or 0.0))
        except Exception:
            pass


    def _record_mts_entry_research_candidate(self, strategy, bar_dict, ts):
        """Best-effort z-score candidate telemetry; never affects orders."""
        try:
            _z = float(bar_dict.get("spread_z"))
            _threshold = bar_dict.get("entry_z_threshold", bar_dict.get("entry_z"))
            if _threshold is None:
                _threshold = getattr(strategy, "_entry_z", None)
            _threshold = float(_threshold)
            if not (math.isfinite(_z) and math.isfinite(_threshold)
                    and _threshold > 0 and abs(_z) >= _threshold):
                return
            # P1-C: reject stale / missing quotes — a candidate whose bar
            # EXPLICITLY carries a stale quote_age or invalid BBO evidence
            # is not a trustworthy entry observation (absent keys pass, so
            # legacy bars keep recording).
            _q_age = bar_dict.get("quote_age_ms")
            if _q_age is not None:
                try:
                    _max_age = float(getattr(
                        strategy, "_max_quote_age_ms", 30000) or 30000)
                    if float(_q_age) > _max_age:
                        return
                except (TypeError, ValueError):
                    pass
            for _k in ("near_bid", "near_ask", "far_bid", "far_ask"):
                if _k not in bar_dict:
                    continue
                _v = bar_dict.get(_k)
                if (_v is None or _v == 0 or _v == ""
                        or (isinstance(_v, float) and math.isnan(_v))):
                    return
            _action = "SELL_NEAR_BUY_FAR" if _z > 0 else "BUY_NEAR_SELL_FAR"
            _audit = {
                "event_time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "action": _action, "decision": "CANDIDATE",
                "rejection_reason": "CANDIDATE_AWAITING_EVALUATION",
                "trade_id": getattr(strategy, "_trade_id", None),
                "near_contract": getattr(self, "near_code", None),
                "far_contract": getattr(self, "far_code", None),
                "spread": bar_dict.get("spread"), "spread_z": _z,
                "entry_z": _threshold, "spread_ma": bar_dict.get("spread_ma"),
                "spread_std": bar_dict.get("spread_std"), "dz": bar_dict.get("dz"),
                "spread_slope": bar_dict.get("spread_slope"),
                "velocity_ema": bar_dict.get("velocity_ema"),
                "atr": bar_dict.get("atr"), "regime": bar_dict.get("regime"),
                "near_bid": bar_dict.get("near_bid"), "near_ask": bar_dict.get("near_ask"),
                "far_bid": bar_dict.get("far_bid"), "far_ask": bar_dict.get("far_ask"),
                "quote_age_ms": bar_dict.get("quote_age_ms"),
                "pair_skew_ms": bar_dict.get("pair_skew_ms"),
            }
            from core.entry_research_store import record_entry_observation
            _ctx = getattr(self, "_execution_context", None)
            record_entry_observation(
                _audit, mode="live" if self.live_trading else "paper",
                session_id=getattr(_ctx, "session_id", None),
                config_hash=getattr(_ctx, "config_hash", None),
                release_sha=os.environ.get("LRC_RELEASE_SHA"),
                run_id=getattr(self, "run_id", None),
                source=("live_strategy" if self.live_trading else "paper_strategy"),
            )
        except Exception:
            pass

    def _submit_mts_order_signal(self, signal, strategy, bar_dict, ts):
        """Submit order via order_mgr for MTS signals (entry, release, exit).

        This is the P0 safety boundary for all MTS order submissions.
        Every code path that creates broker/paper orders for MTS must go
        through this function.  Guard checks here are the LAST line of
        defense before an order hits the broker or paper_fill_sim.
        """
        # [S0] live authorization moved into OrderIntentGateway policy
        # (merged P0 live / entry / EXIT_ONLY / FLAT).  Gate 2 (paper
        # drain) is paper-path behavior and stays.
        # [S1 repair] EXIT_ONLY submits consume the SAME shared capability
        # validation (risk-gate direct submits included) — invalid/stale/
        # missing => one typed blocked event + zero order submission.
        _exit_ok, _exit_position, _exit_reason = \
            self._validate_exit_only_position()
        if not _exit_ok:
            return
        # [auto re-reconciliation] the ORDER SAFETY BOUNDARY: when a
        # bounded EXIT/RELEASE/COMBINED_EXIT signal is about to submit,
        # synchronously take a FRESH read-only broker snapshot
        # immediately BEFORE authorization and require the exact
        # capability rid / locked two legs / account / session / config
        # / release with open_orders == [].  Failure => quarantine typed
        # reason + zero orders + NO retry.  This is NOT a timer — the
        # proof is taken only when an exit-order signal is about to
        # submit (no signal => no broker query).
        _exec_ctx = getattr(self, "_execution_context", None)
        if (_exec_ctx is not None and getattr(
                _exec_ctx, "effective_mode", "")
                == "reconciled_exit_only"):
            _sig_action = getattr(signal, "action", None)
            _action_value = getattr(_sig_action, "value", _sig_action)
            if _action_value in ("EXIT", "RELEASE", "PARTIAL_EXIT",
                                 "COMBINED_EXIT", "COMBINED_EXIT_NEAR",
                                 "COMBINED_EXIT_FAR"):
                _proof_ok, _proof_reason = self._pre_submit_exit_only_proof()
                if not _proof_ok:
                    self._append_mts_event(
                        "ORDER_INTENT_BLOCKED", action=_action_value,
                        reason=_proof_reason,
                        trade_id=getattr(strategy, "_trade_id", ""))
                    return
        _exec_ctx = getattr(self, "_execution_context", None)
        if _exec_ctx is not None:
            # Gate 2: Block new entries during paper drain
            # Even in paper mode, new positions cannot open during drain.
            # Normalize action: could be str ("ENTRY") or enum (LifecycleAction).
            _sig_action = getattr(signal, "action", None)
            _action_value = getattr(_sig_action, "value", _sig_action)
            if _action_value in ("ENTRY", "BUY", "SELL"):
                try:
                    _exec_ctx.assert_entry_allowed()
                except EntryBlocked as exc:
                    console.print(
                        f"[bold yellow]⛔ [MTS_ENTRY_REJECT_DRAIN] "
                        f"{exc}[/bold yellow]"
                    )
                    return

        if not self.order_mgr:
            console.print("[red]⚠️ [MTS_ORDER] order_mgr not available — cannot submit order[/red]")
            return

        # 2026-07-07 Hermes Agent: P0 Market-hours guard.
        # Submitting orders when TAIFEX futures market is closed produces
        # ghost orders — the broker accepts them as queued but they never
        # fill, leaving permanent pending_submit/submitted entries in
        # OrderManager memory and the orders JSON file.
        #
        # This guard is the final safety net.  The upstream _mts_tick()
        # also checks market hours (L4709), but defense-in-depth requires
        # the guard AT the submission boundary so no future code path can
        # bypass it.  Emergency flatten has its own direct submission path
        # that does NOT go through here (see _process_manual_trade_flag
        # close_all); that is intentionally unguarded for crisis scenarios.
        _action = getattr(signal, "action", "?")
        _reason = getattr(signal, "reason", "?")
        if not is_taifex_futures_market_open():
            console.print(
                f"[dim yellow]⛔ [MTS_ORDER_REJECT] market closed; "
                f"refusing to submit {_action} "
                f"(reason={_reason})[/dim yellow]"
            )
            # 2026-07-30: Clean up COMBINED_EXIT contaminated state when orders can't be submitted
            if _action == "EXIT" and "COMBINED_EXIT" in str(_reason).upper():
                try:
                    _mts_strat = strategy if hasattr(strategy, "_has_position") else None
                    if _mts_strat and getattr(_mts_strat, "_has_position", False):
                        from strategies.plugins.futures.active.tmf_spread import _write_mts_state
                        _mts_strat._reset(reason="combined_exit_rejected_market_closed")
                        _write_mts_state(
                            has_position=False, action="COMBINED_EXIT_REJECTED",
                            reason="market_closed",
                            trade_id=getattr(_mts_strat, "_trade_id", ""),
                            ticker=getattr(self, "ticker", "TMF"),
                            lifecycle={},
                        )
                        console.print(f"[yellow]⚠️ [COMBINED_EXIT_CLEANUP] Reset strategy state to FLAT (market closed)[/yellow]")
                except Exception as _ce:
                    import logging
                    logging.getLogger().warning("[COMBINED_EXIT_CLEANUP_FAILED] %s", _ce)
            return

        # [S0] Position-authority FLAT check merged into the
        # OrderIntentGateway policy (EXIT_FLAT_BLOCKED when the
        # authority says flat and no capability-bound position exists).

        from core.order_management.order import OrderType, OrderSide
        _action = signal.action
        _reason = signal.reason
        _near_close = float(bar_dict.get("near_close", 0))
        _far_close = float(bar_dict.get("far_close", 0))
        _ts = ts or datetime.now()

        # 💡 [Fixed 2026-05-27] Prioritize existing strategy trade_id for releases/exits
        _trade_id = getattr(strategy, "_trade_id", None)
        if not _trade_id or _action in ("BUY_NEAR_SELL_FAR", "SELL_NEAR_BUY_FAR"):
             _trade_id = f"mts-auto-{_ts.strftime('%H%M%S-%f')[:-3]}"

        # Helper for common fields in event log
        def _ev_meta(order):
            _meta = {
                "order_id": order.order_id, "symbol": order.symbol,
                "side": order.side.value, "type": order.order_type.value,
                "price": order.price, "qty": order.quantity, "strategy": order.strategy,
                "trade_id": _trade_id
            }
            _binding = getattr(self, "_exit_only_decision_binding", None)
            if _binding:
                from core.exit_only_position import attach_decision_binding
                _cap = getattr(getattr(self, "_execution_context", None),
                               "exit_only_capability", None)
                if isinstance(_cap, dict):
                    return attach_decision_binding(_meta, _cap, _binding)
            return _meta

        _TICK = 1.0
        _ENTRY_BUFFER = 4
        _EXIT_BUFFER = 10

        # 2026-07-07 Hermes Agent: Hard guard — refuse placeholder symbols.
        # After PM2 restart, self.contract / self.far_contract may be None
        # until Shioaji resolves the rolling contracts.  Submitting orders
        # with placeholder codes like "TMF_NEAR" / "TMF_FAR" would send
        # invalid symbols to the broker or paper_fill_sim.
        if self.contract is None:
            console.print(
                "[red]⛔ [MTS_ORDER_BLOCKED] near contract is None; "
                "refusing to submit order with placeholder symbol[/red]"
            )
            return
        if self.far_contract is None:
            console.print(
                "[red]⛔ [MTS_ORDER_BLOCKED] far_contract is None; "
                "refusing to submit order with placeholder symbol[/red]"
            )
            return

        # [GSD] Use real contract codes instead of synthetic symbols
        _near_code = self.contract.code
        _far_code = self.far_contract.code

        # [Snapshot] Capture submission-time OHLC for slippage analysis
        _snap = {
            "near": {k: bar_dict.get(f"near_{k}") for k in ("open", "high", "low", "close")},
            "far": {k: bar_dict.get(f"far_{k}") for k in ("open", "high", "low", "close")},
            "spread_z": bar_dict.get("spread_z")
        }

        # [S0 gateway] single MTS order-intent authorization boundary:
        # merged P0 live / entry / EXIT_ONLY / FLAT policy.
        _gw = self._gateway()
        self._hydrate_exit_only_position()
        _gw_intent_strategy = ("MTS_ENTRY"
                              if _action in ("BUY_NEAR_SELL_FAR",
                                             "SELL_NEAR_BUY_FAR")
                              else "MTS_EXIT" if _action == "EXIT"
                              else "MTS_RELEASE")
        _gw_ok, _gw_binding, _gw_reason = self._authorize_intent(
            _action, _gw_intent_strategy, strategy)
        self._exit_only_decision_binding = _gw_binding
        if not _gw_ok:
            # [S2 audit] persist the canonical failure-evidence payload
            # with the blocked decision (version bbo_input_v2 + JSON-safe
            # raw slots + cap identity + reason + deterministic hash) so
            # dashboard/review can reproduce the rejection.
            _block_ev = {
                "action": _action, "reason": _gw_reason,
                "trade_id": getattr(strategy, "_trade_id", ""),
            }
            from core.exit_only_position import build_bbo_failure_evidence
            _cap = getattr(getattr(self, "_execution_context", None),
                           "exit_only_capability", None)
            _block_ev["bbo_input_v2"] = build_bbo_failure_evidence(
                self._exit_only_bbo_slots(), _cap, _gw_reason)
            self._append_mts_event("ORDER_INTENT_BLOCKED", **_block_ev)
            return

        if _action == "PARTIAL_EXIT":
            # 2026-06-17 Hermes Agent: use signal reason, not strategy._released_leg (still None before sync_release)
            _is_release_near = _reason and "RELEASE_NEAR" in str(_reason).upper()
            _is_release_far = _reason and "RELEASE_FAR" in str(_reason).upper()
            if _is_release_near:
                _side = OrderSide.BUY if getattr(strategy, "_near_side") == "SHORT" else OrderSide.SELL
                _leg_key = None
                if self._leg_lock_enabled():
                    _leg_key = self._mts_leg_lock_key(
                        trade_id=_trade_id, contract=_near_code, side=_side, qty=1)
                    if self._leg_lock_check(_leg_key):
                        return
                    if not self._leg_lock_acquire(_leg_key):
                        self._append_mts_event(
                            "ORDER_INTENT_BLOCKED", action=_action,
                            reason="LEG_LOCK_ACQUIRE_FAILED",
                            trade_id=_trade_id, contract=_near_code)
                        return
                console.print(f"[yellow]📝 [MTS_ORDER] Submitting RELEASE_NEAR: {_side} (MKP Range Market)[/yellow]")
                # 2026-06-08 JVS Claw: Use MKP (範圍市價) instead of MARKET — 避免滑價
                _order = self.order_mgr.create_order(symbol=_near_code, side=_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_RELEASE")
                self._append_mts_event("ORDER_INTENT_CREATED", **_ev_meta(_order))

                # [GSD] Track in lifecycle orders so fill is not ignored
                self._pending_lifecycle_orders[_order.order_id] = {
                    "intent_id": _order.intent_id, "signal": "RELEASE_NEAR", "reason": _reason, 
                    "ts": _ts, "lots": 1, "price": _near_close, "ref_ohlc": _snap["near"],
                    "strategy": "MTS_RELEASE",
                }

                if not self._submit_via_gateway(_order):
                    if _leg_key is not None:
                        self._leg_lock_mark_terminal(_leg_key, "SUBMIT_FAILED")
                    from core.exit_only_position import (
                        build_bbo_failure_evidence)
                    _cap = getattr(
                        getattr(self, "_execution_context", None),
                        "exit_only_capability", None)
                    self._append_mts_event(
                        "ORDER_INTENT_BLOCKED", action=_action,
                        reason="SUBMIT_FAILED",
                        trade_id=getattr(strategy, "_trade_id", ""),
                        bbo_input_v2=build_bbo_failure_evidence(
                            self._exit_only_bbo_slots(), _cap,
                            "SUBMIT_FAILED"))
                    return
                if _leg_key is not None:
                    self._leg_lock_bind_order(_leg_key, _order)
                self._append_mts_event("ORDER_SUBMITTED", **{
                    **_ev_meta(_order),
                    "ref_ohlc": _snap["near"],
                    "leg_role": "RELEASED",
                    "exit_stage": "FIRST_LEG_RELEASE",
                    "release_reason": _reason or "RELEASE_STOP",
                    "reason_source": "LIFECYCLE_DECISION",
                })
                if self.paper_fill_sim:
                    self.paper_fill_sim.register(_order)
                    # 💡 [Fixed 2026-05-27] Force immediate fill in paper mode
                    self.paper_fill_sim.process_tick(self._make_synthetic_tick(_near_close, _ts, symbol=_near_code))

                    # Force fill ONLY in paper mode
                    if self.dry_run or not self.live_trading:
                        console.print(f"[bold green]✅ [MTS_ORDER] RELEASE_NEAR FILLED: {_side} (MKP)[/bold green]")
            elif _is_release_far:
                _side = OrderSide.BUY if getattr(strategy, "_far_side") == "SHORT" else OrderSide.SELL
                _leg_key = None
                if self._leg_lock_enabled():
                    _leg_key = self._mts_leg_lock_key(
                        trade_id=_trade_id, contract=_far_code, side=_side, qty=1)
                    if self._leg_lock_check(_leg_key):
                        return
                    if not self._leg_lock_acquire(_leg_key):
                        self._append_mts_event(
                            "ORDER_INTENT_BLOCKED", action=_action,
                            reason="LEG_LOCK_ACQUIRE_FAILED",
                            trade_id=_trade_id, contract=_far_code)
                        return
                console.print(f"[yellow]📝 [MTS_ORDER] Submitting RELEASE_FAR: {_side} (MKP Range Market)[/yellow]")
                # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免滑價
                _order = self.order_mgr.create_order(symbol=_far_code, side=_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_RELEASE")
                self._append_mts_event("ORDER_INTENT_CREATED", **_ev_meta(_order))

                # [GSD] Track in lifecycle orders so fill is not ignored
                self._pending_lifecycle_orders[_order.order_id] = {
                    "intent_id": _order.intent_id, "signal": "RELEASE_FAR", "reason": _reason, 
                    "ts": _ts, "lots": 1, "price": _far_close, "ref_ohlc": _snap["far"],
                    "strategy": "MTS_RELEASE",
                }

                if not self._submit_via_gateway(_order):
                    if _leg_key is not None:
                        self._leg_lock_mark_terminal(_leg_key, "SUBMIT_FAILED")
                    from core.exit_only_position import (
                        build_bbo_failure_evidence)
                    _cap = getattr(
                        getattr(self, "_execution_context", None),
                        "exit_only_capability", None)
                    self._append_mts_event(
                        "ORDER_INTENT_BLOCKED", action=_action,
                        reason="SUBMIT_FAILED",
                        trade_id=getattr(strategy, "_trade_id", ""),
                        bbo_input_v2=build_bbo_failure_evidence(
                            self._exit_only_bbo_slots(), _cap,
                            "SUBMIT_FAILED"))
                    return
                if _leg_key is not None:
                    self._leg_lock_bind_order(_leg_key, _order)
                self._append_mts_event("ORDER_SUBMITTED", **{
                    **_ev_meta(_order),
                    "ref_ohlc": _snap["far"],
                    "leg_role": "RELEASED",
                    "exit_stage": "FIRST_LEG_RELEASE",
                    "release_reason": _reason or "RELEASE_STOP",
                    "reason_source": "LIFECYCLE_DECISION",
                })
                if self.paper_fill_sim:
                    self.paper_fill_sim.register(_order)
                    # 💡 [Fixed 2026-05-27] Force immediate fill in paper mode
                    self.paper_fill_sim.process_tick(self._make_synthetic_tick(_far_close, _ts, symbol=_far_code))

                    # Force fill ONLY in paper mode
                    if self.dry_run or not self.live_trading:
                        console.print(f"[bold green]✅ [MTS_ORDER] RELEASE_FAR FILLED: {_side} (MKP)[/bold green]")
            else:
                console.print(f"[red]⚠️ [MTS_ORDER] PARTIAL_EXIT but cannot determine released leg from signal reason: {_reason}[/red]")
            return

        elif _action == "COMBINED_EXIT" or _reason in ("TMF_COMBINED_EXIT", "COMBINED_EXIT"):
            # 2026-07-28: Canonical COMBINED_EXIT path — both orders go through
            # OrderManager and fill callback pipeline. No inline paper_fill_sim.
            
            # Item 5: Execution Idempotency Claim
            _claim_key = f"{_trade_id}:POLICY_J:COMBINED_EXIT"
            if not hasattr(self, "_claimed_execution_keys"):
                self._claimed_execution_keys = set()
            if _claim_key in self._claimed_execution_keys:
                console.print(f"[yellow]⚠️ [COMBINED_EXIT_DUPLICATE_SUPPRESSED] trade_id={_trade_id} key={_claim_key}[/yellow]")
                return

            # Item 3: Fail-Closed Side Mapping
            CLOSE_SIDE = {"LONG": OrderSide.SELL, "SHORT": OrderSide.BUY}
            _near_side_raw = getattr(strategy, "_near_side", None)
            _far_side_raw = getattr(strategy, "_far_side", None)
            _near_side_str = str(_near_side_raw).upper() if _near_side_raw else ""
            _far_side_str = str(_far_side_raw).upper() if _far_side_raw else ""
            if _near_side_str not in CLOSE_SIDE:
                console.print(f"[bold red]⛔ [MTS_COMBINED_EXIT_BLOCKED] INVALID_NEAR_POSITION_SIDE: {_near_side_raw}[/bold red]")
                return
            if _far_side_str not in CLOSE_SIDE:
                console.print(f"[bold red]⛔ [MTS_COMBINED_EXIT_BLOCKED] INVALID_FAR_POSITION_SIDE: {_far_side_raw}[/bold red]")
                return

            _near_order_side = CLOSE_SIDE[_near_side_str]
            _far_order_side = CLOSE_SIDE[_far_side_str]

            # Item 4: Dynamic Quantity Derivation & Holding Verification
            _near_qty = int(getattr(strategy, "_near_qty", getattr(strategy, "_lots", 1)) or 1)
            _far_qty = int(getattr(strategy, "_far_qty", getattr(strategy, "_lots", 1)) or 1)
            if _near_qty <= 0 or _far_qty <= 0:
                console.print(f"[bold red]⛔ [MTS_COMBINED_EXIT_BLOCKED] INVALID_POSITION_QUANTITY: near={_near_qty} far={_far_qty}[/bold red]")
                return

            # 2026-07-31 Hermes Agent: Independent release-ledger hard gate (ADR-025).
            # Strategy in-memory phase/qty can be stale if release-fill sync failed.
            # Rebuild per-leg open qty from the broker-confirmed fills ledger:
            # a leg with reconstructed open qty <= 0 is FLAT and COMBINED_EXIT must
            # be blocked — otherwise we would re-enter/flip the flat leg.
            # (Ledger absent/unreadable -> None -> fall back to strategy gates.)
            _ledger_qty = self._mts_ledger_reconstructed_open_qty(_trade_id)
            if (_ledger_qty is not None
                    and getattr(self, "_exit_only_position", None) is None
                    and (_ledger_qty["NEAR"] <= 0 or _ledger_qty["FAR"] <= 0)):
                console.print(
                    f"[bold red]⛔ [MTS_COMBINED_EXIT_BLOCKED] LEDGER_RECONSTRUCTED_LEG_FLAT: "
                    f"trade_id={_trade_id} near_open={_ledger_qty['NEAR']} far_open={_ledger_qty['FAR']}. "
                    f"COMBINED_EXIT requires BOTH legs open — refusing to submit.[/bold red]"
                )
                return

            # Acquire both leg locks before creating/submitting either order.
            # A conflict on one leg blocks the whole pair; no partial local
            # order objects or broker I/O may occur.
            _near_lock_key = _far_lock_key = None
            if self._leg_lock_enabled():
                _near_lock_key = self._mts_leg_lock_key(
                    trade_id=_trade_id, contract=_near_code,
                    side=_near_order_side, qty=_near_qty)
                _far_lock_key = self._mts_leg_lock_key(
                    trade_id=_trade_id, contract=_far_code,
                    side=_far_order_side, qty=_far_qty)
                if (self._leg_lock_check(_near_lock_key)
                        or self._leg_lock_check(_far_lock_key)):
                    return
                if not self._leg_lock_acquire_pair(_near_lock_key, _far_lock_key):
                    return

            # Claim idempotency only after the durable pair lock succeeds;
            # a blocked attempt must be retryable after broker reconciliation.
            self._claimed_execution_keys.add(_claim_key)

            # Create combined_exit_group_id for correlation
            _ce_group_id = f"CE-{_trade_id}-{uuid.uuid4().hex[:8]}"
            console.print(f"[yellow]📝 [MTS_ORDER] Submitting COMBINED_EXIT group={_ce_group_id}: NEAR ({_near_order_side} x{_near_qty}) & FAR ({_far_order_side} x{_far_qty})[/yellow]")

            _near_order = self.order_mgr.create_order(symbol=_near_code, side=_near_order_side, order_type=OrderType.MKP, quantity=_near_qty, strategy="MTS_EXIT")
            _far_order = self.order_mgr.create_order(symbol=_far_code, side=_far_order_side, order_type=OrderType.MKP, quantity=_far_qty, strategy="MTS_EXIT")

            # P1-B durable-exit-intent: ids persisted BEFORE any broker I/O;
            # each leg goes through the canonical submit_leg (durable
            # SUBMIT_ATTEMPTED → I/O → SUBMITTED). This is the direct
            # countermeasure for the P1-2 submission-layer double-submit.
            from core.exit_intent import IntentLog as _MTSIntentLog
            _ilog = _MTSIntentLog(_mts_intent_log_dir())
            _iid = None
            for _i in _ilog.list_active():
                if _ilog.get(_i).get("trade_id") == _trade_id:
                    _iid = _i
                    break
            if _iid is None:
                _iid = _ilog.create(_trade_id, "COMBINED_EXIT")
            _near_order.intent_id = _iid
            _far_order.intent_id = _iid
            _near_order.client_order_id = _ilog.get(_iid)["legs"]["NEAR"]["client_order_id"]
            _far_order.client_order_id = _ilog.get(_iid)["legs"]["FAR"]["client_order_id"]
            from core.exit_intent import DuplicateSubmitError as _MTSDupSubmit
            from core.order_intent_gateway import (
                GatewaySubmitError as _MTSGWFail,
            )
            try:
                _ilog.submit_leg(_iid, "NEAR", self.order_mgr,
                                 submit_fn=lambda cid, leg: self._submit_via_gateway(_near_order, raise_on_failure=True))
            except _MTSDupSubmit:
                # P1-2 dedup: a repeated exit decision must NOT re-send
                console.print(f"[yellow]⛔ [P1B_DUP_SUBMIT] {_trade_id} NEAR already submitted — suppressing duplicate[/yellow]")
                return
            except _MTSGWFail as _gwe:
                # [S0] failed near leg: force LIVE_QUARANTINED + persist
                # (reconciliation-required); the FAR leg must never
                # submit after a near failure.
                self._quarantine_mts_exit_leg_failure(
                    trade_id=_trade_id, leg="NEAR", reason=str(_gwe))
                if _near_lock_key is not None:
                    self._leg_lock_mark_terminal(_near_lock_key, "SUBMIT_FAILED")
                if _far_lock_key is not None:
                    self._leg_lock_mark_terminal(_far_lock_key, "SUBMIT_FAILED")
                return
            if _near_lock_key is not None:
                self._leg_lock_bind_order(_near_lock_key, _near_order)
            try:
                _ilog.submit_leg(_iid, "FAR", self.order_mgr,
                                 submit_fn=lambda cid, leg: self._submit_via_gateway(_far_order, raise_on_failure=True))
            except _MTSDupSubmit:
                console.print(f"[yellow]⛔ [P1B_DUP_SUBMIT] {_trade_id} FAR already submitted — suppressing duplicate[/yellow]")
                return
            except _MTSGWFail as _gwe:
                # [S0] failed far leg: force LIVE_QUARANTINED + persist
                # (reconciliation-required); no retry, no cancel.
                self._quarantine_mts_exit_leg_failure(
                    trade_id=_trade_id, leg="FAR", reason=str(_gwe))
                if _far_lock_key is not None:
                    self._leg_lock_mark_terminal(_far_lock_key, "SUBMIT_FAILED")
                return
            if _far_lock_key is not None:
                self._leg_lock_bind_order(_far_lock_key, _far_order)
            # 2026-07-30: Write state to COMBINED_EXIT only AFTER successful submission
            try:
                from strategies.plugins.futures.active.tmf_spread import _write_mts_state
                _write_mts_state(
                    has_position=True, action="COMBINED_EXIT",
                    reason="COMBINED_EXIT",
                    trade_id=_trade_id, ticker=getattr(self, "ticker", "TMF"),
                )
            except Exception:
                import logging
                logging.getLogger().warning("[COMBINED_EXIT_STATE_WRITE_FAILED] trade_id=%s", _trade_id)

            # Register in pending lifecycle with group_id
            self._pending_lifecycle_orders[_near_order.order_id] = {
                "intent_id": _near_order.intent_id, "signal": "COMBINED_EXIT_NEAR", "reason": "COMBINED_EXIT",
                "trade_id": _trade_id,  # 2026-07-31: real trade_id for fills-ledger correlation
                "ts": _ts, "lots": _near_qty, "price": _near_close, "ref_ohlc": _snap["near"],
                "strategy": "MTS_EXIT", "combined_exit_group_id": _ce_group_id,
                "leg_role": "NEAR",  # P1-B: intent leg correlation for fills
            }
            self._pending_lifecycle_orders[_far_order.order_id] = {
                "intent_id": _far_order.intent_id, "signal": "COMBINED_EXIT_FAR", "reason": "COMBINED_EXIT",
                "trade_id": _trade_id,  # 2026-07-31: real trade_id for fills-ledger correlation
                "ts": _ts, "lots": _far_qty, "price": _far_close, "ref_ohlc": _snap["far"],
                "strategy": "MTS_EXIT", "combined_exit_group_id": _ce_group_id,
                "leg_role": "FAR",  # P1-B: intent leg correlation for fills
            }

            # Append MTS events for audit trail
            self._append_mts_event("ORDER_SUBMITTED", **{
                **_ev_meta(_near_order), "ref_ohlc": _snap["near"],
                "leg_role": "NEAR", "exit_stage": "COMBINED_EXIT",
                "exit_reason": "COMBINED_EXIT", "combined_exit_group_id": _ce_group_id,
                "reason_source": "LIFECYCLE_DECISION",
            })
            self._append_mts_event("ORDER_SUBMITTED", **{
                **_ev_meta(_far_order), "ref_ohlc": _snap["far"],
                "leg_role": "FAR", "exit_stage": "COMBINED_EXIT",
                "exit_reason": "COMBINED_EXIT", "combined_exit_group_id": _ce_group_id,
                "reason_source": "LIFECYCLE_DECISION",
            })

            # Register with paper fill simulator (fill happens in normal tick loop)
            if self.paper_fill_sim:
                self.paper_fill_sim.register(_near_order)
                self.paper_fill_sim.register(_far_order)

            # Persist orders to Dashboard-readable file
            self._save_orders_file_wrapper()

            # Track the group for fill completion monitoring
            if not hasattr(self, "_combined_exit_groups"):
                self._combined_exit_groups = {}
            self._combined_exit_groups[_ce_group_id] = {
                "trade_id": _trade_id,
                "near_order_id": _near_order.order_id,
                "far_order_id": _far_order.order_id,
                "near_filled": False, "near_fill_price": None,
                "far_filled": False, "far_fill_price": None,
                "completed": False,
                "created_at": _ts,
            }

            console.print(f"[green]📝 [MTS_ORDER] COMBINED_EXIT submitted: NEAR={_near_order.order_id} FAR={_far_order.order_id} group={_ce_group_id}[/green]")
            return
        elif _action == "EXIT":
            # Exit remaining leg — determine which one it is from strategy state
            _released = getattr(strategy, "_released_leg", None)
            _remaining_side = getattr(strategy, "_side", None)
            if _released == "near":
                _ref_price = _far_close
                _symbol = _far_code
                _leg_label = "FAR"
                _ref_ohlc = _snap["far"]
            else:
                _ref_price = _near_close
                _symbol = _near_code
                _leg_label = "NEAR"
                _ref_ohlc = _snap["near"]

            if not _remaining_side:
                return

            # ── ADR-011: Phase Isolation Guard (P0, 2026-07-16) ──
            # MTS_EXIT is only legal when the release fill has been confirmed:
            #   lifecycle.phase == SINGLE_LEG  AND
            #   release_group.status in (FILLED, COMPLETED)
            # COMPLETED is the status set by sync_release() (ARMED trigger model).
            # FILLED is the status from the old OCO bracket model — both mean
            # "release fill confirmed; remaining leg can be safely exited".
            # This prevents the 38ms double-order bug where MTS_RELEASE and
            # MTS_EXIT are submitted in the same evaluation cycle before the
            # release fill is confirmed (paper mode: synchronous fill callback
            # transitions to SINGLE_LEG, opening the gate for EXIT).
            from strategies.plugins.futures.active.tmf_spread import PositionPhase, ReleaseGroupStatus
            _lc = getattr(strategy, "_lifecycle_oca", None)
            if _lc is not None:
                _phase = _lc.phase
                _rg_status = _lc.release_group.status
                _phase_val = _phase.value if hasattr(_phase, 'value') else str(_phase)
                _rg_status_val = _rg_status.value if hasattr(_rg_status, 'value') else str(_rg_status)
                if _phase_val != "SINGLE_LEG" or _rg_status_val not in ("FILLED", "COMPLETED"):
                    console.print(
                        f"[bold red]⛔ [MTS_EXIT_BLOCKED_PHASE_ISOLATION] "
                        f"phase={_phase.value if hasattr(_phase, 'value') else _phase} "
                        f"rg_status={_rg_status.value if hasattr(_rg_status, 'value') else _rg_status} "
                        f"expected_phase={PositionPhase.SINGLE_LEG.value} "
                        f"expected_rg_status={ReleaseGroupStatus.FILLED.value} "
                        f"trade_id={getattr(strategy, '_trade_id', None)} "
                        f"exit_reason={_reason}"
                        f"[/bold red]"
                    )
                    return

            # 2026-07-07 Hermes Agent: Restart reconciliation gap guard.
            # After PM2 restart + emergency flatten, lifecycle may be None or
            # FLAT while the strategy still holds a position (restored from
            # state file).  Allowing legacy MTS_EXIT in this window creates
            # duplicate exits before OCO lifecycle is properly re-established.
            _has_pos = bool(getattr(strategy, "_has_position", False))
            _phase_val = getattr(getattr(_lc, "phase", None), "value", None) if _lc else None
            if _phase_val in (None, "FLAT") and _has_pos:
                console.print(
                    f"[yellow]⚠️ [RESTART_GAP_GUARD] Blocked legacy EXIT — "
                    f"lifecycle={_phase_val} but strategy has position; "
                    f"waiting for OCO reconciliation "
                    f"(trade_id={getattr(strategy, '_trade_id', None)})[/yellow]"
                )
                return

            _side = OrderSide.SELL if _remaining_side == "LONG" else OrderSide.BUY

            _leg_key = None
            if self._leg_lock_enabled():
                _leg_key = self._mts_leg_lock_key(
                    trade_id=_trade_id, contract=_symbol, side=_side, qty=1)
                if self._leg_lock_check(_leg_key):
                    return
                if not self._leg_lock_acquire(_leg_key):
                    self._append_mts_event(
                        "ORDER_INTENT_BLOCKED", action=_action,
                        reason="LEG_LOCK_ACQUIRE_FAILED",
                        trade_id=_trade_id, contract=_symbol)
                    return

            console.print(f"[yellow]📝 [MTS_ORDER] Submitting EXIT for {_leg_label}: {_side} (MKP Range Market)[/yellow]")
            # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免滑價
            _order = self.order_mgr.create_order(symbol=_symbol, side=_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_EXIT")
            # B48 (codex C): correlate the decision event through the order
            _order.event_id = getattr(signal, "event_id", "")
            _order.winner = getattr(signal, "winner", "")
            self._append_mts_event("ORDER_INTENT_CREATED", **{**_ev_meta(_order), "ref_ohlc": _ref_ohlc})

            # [GSD] Track in lifecycle orders so fill is not ignored
            self._pending_lifecycle_orders[_order.order_id] = {
                "intent_id": _order.intent_id, "signal": "EXIT", "reason": _reason,
                "ts": _ts, "lots": 1, "price": _ref_price, "ref_ohlc": _ref_ohlc,
                "strategy": "MTS_EXIT",
                "leg_role": _leg_label,  # P1-B: intent leg correlation for fills
                "event_id": _order.event_id, "winner": _order.winner,
            }

            # P1-B durable-exit-intent: canonical submit — the producer
            # created the in-flight intent (remaining leg); if absent (legacy
            # path), create it now. Durable SUBMIT_ATTEMPTED precedes the I/O.
            from core.exit_intent import IntentLog as _MTSIntentLog
            _ilog = _MTSIntentLog(_mts_intent_log_dir())
            _iid = None
            for _i in _ilog.list_active():
                if _ilog.get(_i).get("trade_id") == _trade_id:
                    _iid = _i
                    break
            if _iid is None:
                _iid = _ilog.create(_trade_id, "COMBINED_EXIT", leg=_leg_label)
            _order.intent_id = _iid
            _order.client_order_id = _ilog.get(_iid)["legs"][_leg_label]["client_order_id"]
            from core.exit_intent import DuplicateSubmitError as _MTSDupSubmit
            from core.order_intent_gateway import (
                GatewaySubmitError as _MTSGWFail,
            )
            try:
                _ilog.submit_leg(_iid, _leg_label, self.order_mgr,
                                 submit_fn=lambda cid, leg: self._submit_via_gateway(_order, raise_on_failure=True))
            except _MTSDupSubmit:
                # P1-2 dedup: a repeated exit decision must NOT re-send
                console.print(f"[yellow]⛔ [P1B_DUP_SUBMIT] {_trade_id} {_leg_label} already submitted — suppressing duplicate[/yellow]")
                return
            except _MTSGWFail as _gwe:
                self._quarantine_mts_exit_leg_failure(
                    trade_id=_trade_id, leg=_leg_label, reason=str(_gwe))
                if _leg_key is not None:
                    self._leg_lock_mark_terminal(_leg_key, "SUBMIT_FAILED")
                return
            if _leg_key is not None:
                self._leg_lock_bind_order(_leg_key, _order)
            self._append_mts_event("ORDER_SUBMITTED", **{**_ev_meta(_order), "ref_ohlc": _ref_ohlc,
                                                          "event_id": _order.event_id,
                                                          "winner": _order.winner})

            # ADR-009 Task 9: confirm order submit before lifecycle SUBMITTED
            # Backfill exit_order_id + set SUBMITTED + flush state immediately.
            # Prevents orphan SUBMITTED + exit_order_id=null deadlock.
            from strategies.plugins.futures.active.tmf_spread import _write_mts_state, lifecycle_to_dict, TrailGroupStatus
            _exit_lc = getattr(strategy, "_lifecycle_oca", None)
            if _exit_lc is not None and hasattr(_exit_lc, 'trail_group'):
                _exit_lc.trail_group.exit_order_id = _order.order_id
                _exit_lc.trail_group.status = TrailGroupStatus.SUBMITTED
                _write_mts_state(
                    has_position=True, action=f"TRAIL_SUBMITTED_{_leg_label}",
                    reason=f"task9_backfill_{_order.order_id}",
                    near_entry=getattr(strategy, "_near_entry", 0),
                    far_entry=getattr(strategy, "_far_entry", 0),
                    near_side=getattr(strategy, "_near_side", None),
                    far_side=getattr(strategy, "_far_side", None),
                    released_leg=getattr(strategy, "_released_leg", None),
                    trade_id=getattr(strategy, "_trade_id", _trade_id),
                    ticker=getattr(strategy, "_ticker", self.ticker),
                    atr=0.0,
                    lifecycle=lifecycle_to_dict(_exit_lc),
                )

            if self.paper_fill_sim:
                    self.paper_fill_sim.register(_order)
                    # [Fixed 2026-05-27] Force immediate fill in paper mode
                    self.paper_fill_sim.process_tick(self._make_synthetic_tick(_ref_price, _ts, symbol=_symbol))

                    # Force fill ONLY in paper mode
                    if self.dry_run or not self.live_trading:
                        console.print(f"[bold green]✅ [MTS_ORDER] EXIT_REMAINING ({_symbol}) FILLED: {_side} (MKP)[/bold green]")
            else:
                console.print(f"[red]⚠️ [MTS_ORDER] EXIT but remaining side is None[/red]")
            return

        elif _action in ("BUY_NEAR_SELL_FAR", "SELL_NEAR_BUY_FAR"):
            # 2026-07-08 Hermes Agent: P0 — Multi-source open position guard.
            # Blocks ENTRY if state file, lifecycle, fills ledger, or order_mgr
            # indicates an existing open position.  Prevents orphaned positions
            # from being silently overwritten (state file ≠ fills ledger).
            if self._mts_block_entry_if_open_position(strategy, _action):
                return

            # Entry: submit two legs
            _near_side = OrderSide.SELL if _action == "SELL_NEAR_BUY_FAR" else OrderSide.BUY
            _far_side = OrderSide.BUY if _action == "SELL_NEAR_BUY_FAR" else OrderSide.SELL

            # Decision provenance is a hard precondition for a live entry.
            # The order-intent rows alone preserve prices/sides but not the
            # signal that selected them.  Persist the complete decision before
            # constructing either leg; if the audit write fails, no order can
            # be created or submitted without an explainable entry rationale.
            _entry_audit = {
                "event_time": _ts.isoformat() if hasattr(_ts, "isoformat") else str(_ts),
                "action": _action,
                "decision": "ENTER",
                "reason": _reason,
                "trade_id": _trade_id,
                "near_contract": _near_code,
                "far_contract": _far_code,
                "near_side": "SHORT" if _action == "SELL_NEAR_BUY_FAR" else "LONG",
                "far_side": "LONG" if _action == "SELL_NEAR_BUY_FAR" else "SHORT",
                "near_price": _near_close,
                "far_price": _far_close,
                "spread_now": bar_dict.get("spread"),
                "spread_z": bar_dict.get("spread_z"),
                "entry_z": getattr(strategy, "_entry_z", None),
                "spread_ma": bar_dict.get("spread_ma"),
                "spread_std": bar_dict.get("spread_std"),
                "atr": bar_dict.get("atr"),
                "expected_reversion": (
                    "SPREAD_TO_NARROW" if _action == "SELL_NEAR_BUY_FAR"
                    else "SPREAD_TO_WIDEN"
                ),
                "near_price_source": "LIVE_TICK" if "near_close_rt" in bar_dict else "BAR_CLOSE",
                "far_price_source": "LIVE_TICK" if "far_close_rt" in bar_dict else "BAR_CLOSE",
                "near_tick_age_ms": bar_dict.get("near_tick_age_ms"),
                "far_tick_age_ms": bar_dict.get("far_tick_age_ms"),
                "signal_event_id": getattr(signal, "event_id", ""),
            }
            if self.live_trading:
                _audit_ok = self._append_mts_event_checked(
                    "ENTRY_AUDIT", **_entry_audit)
            else:
                # Preserve paper-mode behavior: telemetry remains best-effort
                # and never changes the paper execution contract.
                self._append_mts_event("ENTRY_AUDIT", **_entry_audit)
                _audit_ok = True
            if not _audit_ok:
                console.print(
                    "[bold red]⛔ [MTS_ENTRY_BLOCKED] "
                    "ENTRY_AUDIT_PERSIST_FAILED — no legs created or submitted"
                    "[/bold red]"
                )
                return

            # Research-only shadow write.  It is intentionally after the
            # durable audit and has no authority over the order path: a
            # missing/locked database must never reject or delay an entry.
            try:
                from core.entry_research_store import record_entry_observation
                _ctx_for_research = getattr(self, "_execution_context", None)
                record_entry_observation(
                    _entry_audit,
                    mode="live" if self.live_trading else "paper",
                    session_id=getattr(_ctx_for_research, "session_id", None),
                    config_hash=getattr(_ctx_for_research, "config_hash", None),
                    release_sha=os.environ.get("LRC_RELEASE_SHA"),
                    run_id=getattr(self, "run_id", None),
                    source=("live_strategy" if self.live_trading
                            else "paper_strategy"),
                )
            except Exception:
                pass

            console.print(f"[yellow]📝 [MTS_ORDER] Submitting ENTRY orders (MKP Range Market): NEAR={_near_side}, FAR={_far_side}[/yellow]")
            
            # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免滑價
            _o_near = self.order_mgr.create_order(symbol=_near_code, side=_near_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_ENTRY")
            self._append_mts_event("ORDER_INTENT_CREATED", **{**_ev_meta(_o_near), "ref_ohlc": _snap["near"]})
            
            # [GSD] Track in lifecycle orders so fill is not ignored
            self._pending_lifecycle_orders[_o_near.order_id] = {
                "intent_id": _o_near.intent_id, "signal": _action, "reason": _reason, 
                "ts": _ts, "lots": 1, "price": _near_close, "ref_ohlc": _snap["near"],
                "strategy": "MTS_ENTRY",
            }

            # 2026-06-08 JVS Claw: Use MKP (範圍市價) — 避免滑價
            _o_far = self.order_mgr.create_order(symbol=_far_code, side=_far_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_ENTRY")
            self._append_mts_event("ORDER_INTENT_CREATED", **{**_ev_meta(_o_far), "ref_ohlc": _snap["far"]})
            
            # [GSD] Track in lifecycle orders so fill is not ignored
            self._pending_lifecycle_orders[_o_far.order_id] = {
                "intent_id": _o_far.intent_id, "signal": _action, "reason": _reason,
                "ts": _ts, "lots": 1, "price": _far_close, "ref_ohlc": _snap["far"],
                "strategy": "MTS_ENTRY",
            }

            # 2026-06-26 Gemini CLI: Populate tracking dictionary BEFORE submitting orders (Deferred Sync Fix)
            # This ensures that synchronous fills in paper mode find the trade in _mts_pending_fills immediately.
            self._mts_pending_fills[_trade_id] = {
                "near_order_id": _o_near.order_id,
                "far_order_id": _o_far.order_id,
                "near_filled": False,
                "far_filled": False,
                "side": "SHORT" if _action == "SELL_NEAR_BUY_FAR" else "LONG",
                "spread_side": _action,
                "near_label": "NEAR",
                "far_label": "FAR",
                "near_ref": _near_close,
                "far_ref": _far_close,
                "ts": _ts,
                "near_price_source": "LIVE_TICK" if "near_close_rt" in bar_dict else "BAR_CLOSE",
                "near_tick_age_ms": 0,
                "far_price_source": "LIVE_TICK" if "far_close_rt" in bar_dict else "BAR_CLOSE",
                "far_tick_age_ms": 0,
            }
            
            if not self._submit_via_gateway(_o_near):
                self._pending_lifecycle_orders.pop(_o_near.order_id, None)
                # ``create_order`` registers both legs as active before the
                # first I/O attempt so paper's synchronous fills can correlate
                # them.  If near fails locally, far was never submitted and
                # must become terminal immediately, not a watchdog timeout.
                self._pending_lifecycle_orders.pop(_o_far.order_id, None)
                self.order_mgr.reject(
                    _o_far.order_id,
                    reason="PRECEDING_LEG_REJECTED",
                    source="mts_entry",
                )
                self._mts_pending_fills.pop(_trade_id, None)
                self._append_mts_event("ORDER_REJECTED_LOCAL", **{
                    **_ev_meta(_o_near), "ref_ohlc": _snap["near"],
                    "reason": getattr(_o_near, "reject_reason", "ADAPTER_SUBMIT_FAILED"),
                })
                self._append_mts_event("ORDER_REJECTED_LOCAL", **{
                    **_ev_meta(_o_far), "ref_ohlc": _snap["far"],
                    "reason": "PRECEDING_LEG_REJECTED",
                })
                return
            self._append_mts_event("ORDER_SUBMITTED", **{**_ev_meta(_o_near), "ref_ohlc": _snap["near"]})
            if self.paper_fill_sim:
                self.paper_fill_sim.register(_o_near)
                # 💡 [Fixed 2026-05-27] Force immediate fill in paper mode
                self.paper_fill_sim.process_tick(self._make_synthetic_tick(_near_close, _ts, symbol=_near_code))

            if not self._submit_via_gateway(_o_far):
                self._pending_lifecycle_orders.pop(_o_far.order_id, None)
                self._append_mts_event("ORDER_REJECTED_LOCAL", **{
                    **_ev_meta(_o_far), "ref_ohlc": _snap["far"],
                    "reason": getattr(_o_far, "reject_reason", "ADAPTER_SUBMIT_FAILED"),
                })
                self._quarantine_mts_entry_partial_submission(
                    trade_id=_trade_id,
                    submitted_order=_o_near,
                    failed_order=_o_far,
                )
                return
            self._append_mts_event("ORDER_SUBMITTED", **{**_ev_meta(_o_far), "ref_ohlc": _snap["far"]})
            if self.paper_fill_sim:
                self.paper_fill_sim.register(_o_far)
                # 💡 [Fixed 2026-05-27] Force immediate fill in paper mode
                self.paper_fill_sim.process_tick(self._make_synthetic_tick(_far_close, _ts, symbol=_far_code))

            # 2026-05-27 Gemini CLI: Removed redundant process_tick to prevent double-ordering loops
            from types import SimpleNamespace
            # 2026-05-27 Gemini CLI: Removed redundant process_tick loop
            # 2026-05-27 Gemini CLI: Removed redundant process_tick loop
            console.print(f"[bold green]✅ [MTS_ORDER] ENTRY SUBMITTED: near={_near_side} far={_far_side}[/bold green]")
            return

        else:
            console.print(f"[red]⚠️ [MTS_ORDER] Unknown signal action: {_action}[/red]")
            return



    def _leg_lock_path(self) -> str:
        _p = getattr(self, "_leg_lock_store", "")
        if _p:
            return _p
        try:
            from core.runtime_paths import runtime_path
            return runtime_path("exports", "trades", "live", "diagnostics",
                                "mts_leg_locks.json")
        except Exception:
            return "/tmp/mts_leg_locks.json"

    def _leg_lock_id(self, key: dict) -> str:
        return "|".join(str(key.get(k)) for k in (
            "trade_id", "session_generation", "contract",
            "closing_side", "qty"))

    def _mts_leg_lock_key(self, *, trade_id, contract, side, qty) -> dict:
        """Build the canonical lock key for a broker-facing MTS leg."""
        _ctx = getattr(self, "_execution_context", None)
        _side = getattr(side, "value", side)
        return {
            "trade_id": trade_id,
            "session_generation": getattr(_ctx, "session_id", "") or "",
            "contract": contract,
            "closing_side": str(_side or "").upper(),
            "qty": qty,
        }

    def _leg_lock_enabled(self) -> bool:
        """Enable broker leg locks only for the live order path.

        Paper mode has synchronous local fills and must retain its existing
        compatibility semantics; the durable broker lock is a live-side
        duplicate-submission barrier.
        """
        return bool(getattr(self, "live_trading", False)
                    and not getattr(self, "dry_run", False))

    def _leg_lock_bind_order(self, key: dict, order) -> None:
        """Attach broker identity to an already-acquired leg lock."""
        try:
            _broker_id = (getattr(order, "broker_order_id", None)
                          or getattr(order, "exchange_order_id", None)
                          or "")
            _seqno = getattr(order, "seqno", None) or ""
            if not (_broker_id or _seqno):
                return
            with self._leg_lock_flock(exclusive=True):
                _locks = self._leg_lock_read()
                _lock = _locks.get(self._leg_lock_id(key))
                if _lock is None:
                    return
                _lock["local_order_id"] = getattr(order, "order_id", "") or ""
                _lock["broker_order_id"] = str(_broker_id)
                _lock["seqno"] = str(_seqno)
                self._leg_lock_write(_locks)
        except Exception:
            # Identity enrichment must never turn a confirmed receipt into a
            # false success; the lock remains held under its canonical key.
            return

    def _leg_lock_apply_order_event(self, order, status, fill_qty=None) -> None:
        """Release/retain the lock from an authoritative order event."""
        if not self._leg_lock_enabled():
            return
        try:
            _status = str(getattr(status, "value", status) or "").upper()
            _broker_id = str(
                getattr(order, "broker_order_id", None)
                or getattr(order, "exchange_order_id", None) or "")
            _seqno = str(getattr(order, "seqno", None) or "")
            _local_id = str(getattr(order, "order_id", None) or "")
            _locks = self._leg_lock_load()
            for _record in list(_locks.values()):
                if not (
                    (_broker_id and str(_record.get("broker_order_id") or "") == _broker_id)
                    or (_seqno and str(_record.get("seqno") or "") == _seqno)
                    or (_local_id and str(_record.get("local_order_id") or "") == _local_id)
                ):
                    continue
                _key = {
                    "trade_id": _record.get("trade_id"),
                    "session_generation": _record.get("session_generation"),
                    "contract": _record.get("contract"),
                    "closing_side": _record.get("closing_side"),
                    "qty": _record.get("qty"),
                    "broker_order_id": _record.get("broker_order_id"),
                    "seqno": _record.get("seqno"),
                }
                if _status in {"FILLED", "PARTIAL_FILLED"}:
                    _filled = fill_qty
                    if _filled is None:
                        _filled = getattr(order, "filled_quantity", 0)
                    self._leg_lock_apply_broker_deal(_key, _filled)
                elif _status in {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}:
                    self._leg_lock_mark_terminal(_key, _status)
        except Exception:
            # A callback parsing failure must never release a pending lock.
            return

    def _leg_lock_save(self, locks: dict) -> None:
        """Atomic replace under the stable .lock flock (single-writer)."""
        try:
            with self._leg_lock_flock(exclusive=True):
                self._leg_lock_write(locks)
        except Exception:
            pass

    def _leg_lock_load(self) -> dict:
        try:
            with self._leg_lock_flock(exclusive=False):
                return self._leg_lock_read()
        except Exception:
            return {}  # corrupted file -> fail-safe: no locks assumed

    def _leg_lock_flock_path(self) -> str:
        """Stable flock target — NEVER replaced, so the lock survives the
        JSON's atomic os.replace (inode race fix)."""
        return self._leg_lock_path() + ".lock"

    @contextlib.contextmanager
    def _leg_lock_flock(self, exclusive: bool = True):
        import fcntl
        _p = self._leg_lock_flock_path()
        _d = os.path.dirname(_p)
        if _d:
            os.makedirs(_d, exist_ok=True)
        _f = open(_p, "a+", encoding="utf-8")
        try:
            fcntl.flock(_f, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield _f
        finally:
            try:
                fcntl.flock(_f, fcntl.LOCK_UN)
            except Exception:
                pass
            _f.close()

    def _leg_lock_read(self) -> dict:
        """Direct JSON read — callers hold the .lock flock."""
        try:
            import json as _json
            _p = self._leg_lock_path()
            if not os.path.exists(_p):
                return {}
            with open(_p, encoding="utf-8") as _f:
                return _json.load(_f) or {}
        except Exception:
            return {}

    def _leg_lock_write(self, locks: dict) -> None:
        """Atomic replace — callers hold the .lock flock."""
        _p = self._leg_lock_path()
        _d = os.path.dirname(_p)
        if _d:
            os.makedirs(_d, exist_ok=True)
        import json as _json
        _tmp = _p + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as _f:
            _json.dump(locks, _f, default=str)
            _f.flush()
            os.fsync(_f.fileno())
        os.replace(_tmp, _p)

    def _leg_lock_rebind(self, locks: dict, key: dict) -> dict | None:
        """Rebind a lock to the new session after a restart.

        Broker identity (broker_order_id + seqno) is authoritative and
        unique.  The generic 4-field match (trade_id + contract +
        closing_side + qty) is allowed ONLY when it resolves to exactly one
        candidate; multiple candidates -> fail-closed QUARANTINE
        (LEG_LOCK_REBIND_AMBIGUOUS) — never a silent pass/resend."""
        _bid = str(key.get("broker_order_id") or "")
        if _bid:
            _seq = str(key.get("seqno") or "")
            for _lock in locks.values():
                if (str(_lock.get("broker_order_id") or "") == _bid
                        and (not _seq or str(_lock.get("seqno") or "") == _seq)):
                    _lock["session_generation"] = key.get("session_generation")
                    self._leg_lock_save(locks)
                    return _lock
            return None
        _cands = [v for v in locks.values()
                  if (str(v.get("trade_id") or "") == str(key.get("trade_id") or "")
                      and str(v.get("contract") or "") == str(key.get("contract") or "")
                      and str(v.get("closing_side") or "") == str(key.get("closing_side") or "")
                      and int(v.get("qty") or 0) == int(key.get("qty") or 0))]
        if len(_cands) == 1:
            _cands[0]["session_generation"] = key.get("session_generation")
            self._leg_lock_save(locks)
            return _cands[0]
        if len(_cands) > 1:
            self._append_mts_event(
                "LEG_LOCK_REBIND_AMBIGUOUS",
                reason="LEG_LOCK_REBIND_AMBIGUOUS",
                trade_id=key.get("trade_id"),
                contract=key.get("contract"))
            return _cands[0]  # fail-closed: still block submissions
        return None

    def _leg_lock_acquire(self, key: dict,
                          status: str = "PENDING_UNCONFIRMED") -> bool:
        """Persist a leg lock BEFORE submission.  Key:
        trade_id + session_generation + contract + closing_side + qty.
        status: PENDING_UNCONFIRMED (receipt, never released early) or
        SUBMIT_FAILED (released after failure handling).

        The whole read-modify-write runs under ONE exclusive flock on the
        lock file so concurrent processes/threads cannot lose a lock."""
        try:
            import json as _json
            import fcntl
            _p = self._leg_lock_path()
            _d = os.path.dirname(_p)
            if _d:
                os.makedirs(_d, exist_ok=True)
            with self._leg_lock_flock(exclusive=True):
                _locks = self._leg_lock_read()
                _locks[self._leg_lock_id(key)] = {
                    "trade_id": key.get("trade_id"),
                    "session_generation": key.get("session_generation"),
                    "contract": key.get("contract"),
                    "closing_side": key.get("closing_side"),
                    "qty": key.get("qty"),
                    "local_order_id": key.get("local_order_id") or "",
                    "broker_order_id": key.get("broker_order_id") or "",
                    "seqno": key.get("seqno") or "",
                    "status": status,
                    "terminal": "",
                    "locked_at": datetime.now().isoformat(),
                }
                self._leg_lock_write(_locks)
            return True
        except Exception:
            return False

    def _leg_lock_acquire_pair(self, near_key: dict, far_key: dict) -> bool:
        """③ combined all-or-none: acquire BOTH leg locks atomically.

        - Both legs free -> lock both, return True.
        - Either leg already holds a non-terminal lock -> acquire nothing
          (no partial pair), record LEG_LOCK_PAIR_PARTIAL quarantine /
          reconcile intent, return False.  The other leg is never left
          locked, and neither leg is ever submitted."""
        try:
            _near_id = self._leg_lock_id(near_key)
            _far_id = self._leg_lock_id(far_key)
            _terminal = ("FILLED", "CANCELLED", "REJECTED", "CANCELED")
            with self._leg_lock_flock(exclusive=True):
                _locks = self._leg_lock_read()
                _conflicts = []
                for _kid, _k in ((_near_id, near_key), (_far_id, far_key)):
                    _existing = _locks.get(_kid)
                    if (_existing and str(_existing.get("status", "")).upper()
                            not in _terminal):
                        _conflicts.append(_k.get("contract"))
                if _conflicts:
                    self._append_mts_event(
                        "LEG_LOCK_PAIR_PARTIAL",
                        reason="LEG_LOCK_PAIR_PARTIAL",
                        trade_id=near_key.get("trade_id"),
                        contract=",".join(_conflicts))
                    return False
                _now = datetime.now().isoformat()
                for _kid, _k in ((_near_id, near_key), (_far_id, far_key)):
                    _locks[_kid] = {
                        "trade_id": _k.get("trade_id"),
                        "session_generation": _k.get("session_generation"),
                        "contract": _k.get("contract"),
                        "closing_side": _k.get("closing_side"),
                        "qty": _k.get("qty"),
                        "local_order_id": _k.get("local_order_id") or "",
                        "broker_order_id": _k.get("broker_order_id") or "",
                        "seqno": _k.get("seqno") or "",
                        "status": "PENDING_UNCONFIRMED",
                        "terminal": "",
                        "locked_at": _now,
                    }
                self._leg_lock_write(_locks)
                return True
        except Exception:
            try:
                self._append_mts_event(
                    "LEG_LOCK_PAIR_PARTIAL",
                    reason="LEG_LOCK_PAIR_PARTIAL",
                    trade_id=near_key.get("trade_id"))
            except Exception:
                pass
            return False

    def _leg_lock_mark_terminal(self, key: dict, status: str) -> None:
        """Mark a lock terminal (FILLED/CANCELLED/REJECTED) then release —
        only explicit broker terminal evidence may do this."""
        try:
            with self._leg_lock_flock(exclusive=True):
                _locks = self._leg_lock_read()
                _lid = self._leg_lock_id(key)
                if _lid in _locks:
                    _locks[_lid]["status"] = status
                    _locks[_lid]["terminal"] = status
                    self._leg_lock_write(_locks)
        except Exception:
            pass
        self._leg_lock_release(key)

    def _leg_lock_apply_broker_deal(self, key: dict, filled_qty) -> bool:
        """④ broker deal evidence: ONLY a full fill (filled_qty >= qty) is
        terminal.  A partial fill retains the lock and records the
        reconcile intent — release/resend stay forbidden."""
        try:
            _qty = int(key.get("qty") or 0)
            try:
                _filled = float(filled_qty)
            except (TypeError, ValueError):
                _filled = 0.0
            if _qty > 0 and _filled >= _qty:
                self._leg_lock_mark_terminal(key, "FILLED")
                return True
            self._append_mts_event(
                "LEG_LOCK_PARTIAL_FILL",
                reason="LEG_LOCK_PARTIAL_FILL",
                contract=key.get("contract"),
                trade_id=key.get("trade_id"),
                filled_qty=_filled, qty=_qty)
            return False
        except Exception:
            return False

    def _leg_lock_apply_broker_query(self, key: dict, trades) -> bool:
        """④ reconciliation query evidence: an empty result or a query
        exception must NOT release the lock (no terminal proof) — only an
        explicit full-fill deal in the queried trades releases."""
        try:
            if trades is None:
                self._append_mts_event(
                    "LEG_LOCK_QUERY_FAILED",
                    reason="LEG_LOCK_QUERY_FAILED",
                    contract=key.get("contract"),
                    trade_id=key.get("trade_id"))
                return False
            _qty = int(key.get("qty") or 0)
            _bid = str(key.get("broker_order_id") or "")
            for _t in trades:
                _order = getattr(_t, "order", None)
                _id = str(
                    getattr(_t, "order_id", None)
                    or getattr(_t, "id", None)
                    or getattr(_order, "id", None) or "")
                _st = getattr(_t, "status", None)
                _nested_st = getattr(_st, "status", None)
                _raw_st = _nested_st if _nested_st is not None else _st
                _status = str(
                    getattr(_raw_st, "name", None)
                    or getattr(_raw_st, "value", None)
                    or _raw_st or "").split(".")[-1].upper()
                _filled = float(
                    getattr(_t, "fill_qty", None)
                    or getattr(_t, "filled_quantity", None)
                    or getattr(_t, "quantity", 0) or 0)
                if ((not _bid or _id == _bid)
                        and _status.upper() == "FILLED"
                        and _qty > 0 and _filled >= _qty):
                    self._leg_lock_mark_terminal(key, "FILLED")
                    return True
            self._append_mts_event(
                "LEG_LOCK_QUERY_NO_TERMINAL",
                reason="LEG_LOCK_QUERY_NO_TERMINAL",
                contract=key.get("contract"),
                trade_id=key.get("trade_id"))
            return False
        except Exception:
            return False

    def _reconcile_intent_path(self) -> str:
        _p = getattr(self, "_reconcile_intent_store", "")
        if _p:
            return _p
        try:
            from core.runtime_paths import runtime_path
            return runtime_path("exports", "trades", "live", "diagnostics",
                                "mts_reconcile_intents.json")
        except Exception:
            return "/tmp/mts_reconcile_intents.json"

    def _mts_partial_submission_quarantine(self, near_key: dict,
                                           far_key: dict) -> None:
        """Partial-submission quarantine: the first leg's receipt landed
        but the second leg's submit FAILED.  Record a DURABLE reconcile
        intent (restored after restart).  No retry, no cancel, no
        compensating order — the pair is not complete."""
        try:
            import json as _json
            _p = self._reconcile_intent_path()
            _intents = {}
            if os.path.exists(_p):
                with open(_p, encoding="utf-8") as _f:
                    _intents = _json.load(_f) or {}
            _intents[self._leg_lock_id(near_key)] = {
                "reason": "PARTIAL_SUBMISSION_QUARANTINE",
                "near": near_key, "far": far_key,
                "ts": datetime.now().isoformat(),
            }
            _d = os.path.dirname(_p)
            if _d:
                os.makedirs(_d, exist_ok=True)
            with open(_p, "w", encoding="utf-8") as _f:
                _json.dump(_intents, _f, default=str)
            self._append_mts_event(
                "PARTIAL_SUBMISSION_QUARANTINE",
                reason="PARTIAL_SUBMISSION_QUARANTINE",
                contract=near_key.get("contract"),
                trade_id=near_key.get("trade_id"))
        except Exception:
            pass

    def _reconcile_intent_exists(self, key: dict) -> bool:
        try:
            import json as _json
            _p = self._reconcile_intent_path()
            if not os.path.exists(_p):
                return False
            with open(_p, encoding="utf-8") as _f:
                _intents = _json.load(_f) or {}
            return self._leg_lock_id(key) in _intents
        except Exception:
            return False

    def _submit_release_pair(self, near_key: dict, far_key: dict,
                             near_submit_ok: bool = True,
                             far_submit_ok: bool = True) -> str:
        """Release pair submission with partial-submission protection.

        Acquires both leg locks (all-or-none), submits the near leg, then
        the far leg.  If the FAR submit fails after the NEAR receipt, the
        pair enters MTS_ENTRY_RECONCILE / partial-submission quarantine:
        the near lock is retained, no retry/cancel/compensating order, and
        a durable reconcile intent is recorded."""
        try:
            if not self._leg_lock_acquire_pair(near_key, far_key):
                return "PAIR_LOCK_BLOCKED"
            if near_submit_ok:
                self._append_mts_event(
                    "ORDER_SUBMITTED", event="ORDER_SUBMITTED",
                    contract=near_key.get("contract"),
                    trade_id=near_key.get("trade_id"),
                    leg_role="RELEASED", exit_stage="FIRST_LEG_RELEASE")
            else:
                self._leg_lock_release(near_key)
                return "NEAR_SUBMIT_FAILED"
            if not far_submit_ok:
                self._mts_partial_submission_quarantine(near_key, far_key)
                return "MTS_ENTRY_RECONCILE"
            self._append_mts_event(
                "ORDER_SUBMITTED", event="ORDER_SUBMITTED",
                contract=far_key.get("contract"),
                trade_id=far_key.get("trade_id"),
                leg_role="RELEASED", exit_stage="SECOND_LEG_RELEASE")
            return "OK"
        except Exception:
            return "SUBMIT_ERROR"

    def _leg_lock_release(self, key: dict,
                          only_statuses=("FILLED", "CANCELLED", "REJECTED",
                                         "CANCELED", "EXPIRED", "SUBMIT_FAILED")) -> None:
        """Release a leg lock ONLY on terminal states or SUBMIT_FAILED.
        PENDING_UNCONFIRMED is never released early (no resend)."""
        try:
            _locks = self._leg_lock_load()
            _lid = self._leg_lock_id(key)
            _lock = _locks.get(_lid)
            if _lock is None:
                return
            _st = str(_lock.get("status", "")).upper()
            if _st in tuple(s.upper() for s in only_statuses):
                _locks.pop(_lid, None)
                self._leg_lock_save(_locks)
        except Exception:
            pass

    def _leg_lock_check(self, key: dict) -> bool:
        """True when a non-terminal lock exists for this leg — a second
        signal must NOT resubmit (zero submissions)."""
        try:
            _locks = self._leg_lock_load()
            _lock = _locks.get(self._leg_lock_id(key))
            if _lock is None:
                # restart rebinding: session_generation changed after a
                # restart — match by broker identity components
                # (trade_id + contract + closing_side + qty) and rebind.
                _lock = self._leg_lock_rebind(_locks, key)
            if _lock is None:
                return False
            if str(_lock.get("status", "")).upper() in (
                    "FILLED", "CANCELLED", "REJECTED", "CANCELED", "EXPIRED"):
                return False
            self._append_mts_event(
                "ORDER_BLOCKED_PENDING_EXISTS",
                reason="ORDER_BLOCKED_PENDING_EXISTS",
                contract=key.get("contract"),
                trade_id=key.get("trade_id"))
            return True
        except Exception:
            return False

    def _execute_trade(self, signal, price, ts, lots, *, stop_loss=None, break_even_trigger=None, trail_points=None, reason=None):
        action = None
        exit_order_side = None
        if signal == "BUY":
            action = "Buy"
        elif signal == "SELL":
            action = "Sell"
        elif signal in ("EXIT", "PARTIAL_EXIT"):
            if self.trader.position == 0:
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "no_position", "lots": lots})
                return None
            from core.order_management.order import OrderSide
            exit_order_side = OrderSide.SELL if self.trader.position > 0 else OrderSide.BUY
            action = "Sell" if self.trader.position > 0 else "Buy"

        live_ready = self.live_trading and not self.dry_run and self.contract is not None
        if live_ready and action is not None and not (self._use_order_manager and self.order_mgr):
            # [Live wiring Step 4] execution-context gate on the DIRECT
            # client.place_order path: non-LIVE_READY (PREFLIGHT/QUARANTINED)
            # AND ctx=None (no certification) are FAIL-CLOSED -> ZERO
            # place_order/cancel_order calls + structured blocked result with
            # audit reason. Manager path untouched.
            _ctx = getattr(self, "_execution_context", None)
            if _ctx is None or not _ctx.is_live_ready():
                return {"blocked": True,
                        "reason": "NO_LIVE_CERTIFICATION"
                        if _ctx is None else "LIVE_QUARANTINED",
                        "audit_reasons": tuple(
                            getattr(_ctx, "audit_reasons", ()) or ())}
            # 進場前檢查保證金（出場不擋）
            if signal in ("BUY", "SELL"):
                if not self._margin_sufficient():
                    console.print(f"[red][FuturesMonitor] ⛔ 保證金不足，取消 {signal}[/red]")
                    from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                    save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "margin_insufficient", "lots": lots})
                    return None
            # 出場前先刪 safety stop，避免庫存不足
            if signal in ("EXIT", "PARTIAL_EXIT"):
                _cancel_result = self._cancel_safety_stop()
                if isinstance(_cancel_result, dict) \
                        and _cancel_result.get("blocked"):
                    # [exit failure-side] safety-stop cancel failed — do
                    # NOT silently place the ordinary exit: structured
                    # failure + persisted dashboard reason + fail-closed
                    # context; NO subsequent place_order call.
                    _reason = _cancel_result.get(
                        "reason", "SAFETY_STOP_CANCEL_FAILED")
                    _ctx = getattr(self, "_execution_context", None)
                    if _ctx is not None:
                        from core.mode_transition import (ModeTransitionState,
                                                          with_effective_mode)
                        self._execution_context = with_effective_mode(
                            _ctx, ModeTransitionState.LIVE_QUARANTINED.value,
                            live_order_allowed=False,
                            audit_reasons=(_reason,) + tuple(
                                getattr(_ctx, "audit_reasons", ()) or ()))
                        self._persist_execution_context()
                    console.print(
                        f"[red]🚫 EXIT blocked: {_reason} — safety-stop "
                        f"cancel failed; NO exit placed[/red]")
                    return {"blocked": True, "reason": _reason,
                            "audit_reasons": list(
                                getattr(self._execution_context,
                                        "audit_reasons", ()) or ())}
            try:
                trade = self.client.place_order(self.contract, action=action,
                                                quantity=lots)
            except AdapterOrderError as e:
                # P0: structured durable failure — never swallowed
                console.print(
                    f"[red][FuturesMonitor] Live order failed: {e.code} "
                    f"{signal} {lots}[/red]")
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({
                    "timestamp": ts, "signal": signal, "price": price,
                    "reason": reason or "", "rejection": "api_order_failed",
                    "lots": lots, "error_code": e.code,
                    "error_context": e.context})
                return None
            if trade is None:
                console.print(f"[red][FuturesMonitor] Live order failed: {signal} {lots}[/red]")
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "api_order_failed", "lots": lots})
                return None

        # 計算 PnL（出場時，含手續費+稅金）
        pnl_pts = 0
        pnl_cash = 0
        friction_cost = 0
        direction = ""
        if signal == "BUY":
            direction = "LONG"
        elif signal == "SELL":
            direction = "SHORT"
        elif signal in ("EXIT", "PARTIAL_EXIT") and self.trader.entry_price > 0:
            direction = "LONG" if self.trader.position > 0 else "SHORT"
            sign = 1 if self.trader.position > 0 else -1
            pnl_pts = (price - self.trader.entry_price) * sign
            gross = pnl_pts * self.trader.point_value * lots
            fee = self.trader.fee_per_side * 2 * lots
            exch_fee = self.trader.exchange_fee_per_side * 2 * lots
            tax = (self.trader.entry_price + price) * self.trader.point_value * self.trader.tax_rate * lots
            friction_cost = fee + exch_fee + tax
            pnl_cash = gross - friction_cost

            # GSD Phase 3: Track session PnL for circuit breaker
            self._session_pnl += pnl_pts

        if signal in ("BUY", "SELL"):
            # --- Pre-entry guards (A–D checkpoints) ---
            # 1) Price sanity
            if price is None or price <= 0:
                self._audit_signal("ENTRY_BLOCKED", "", 0, "invalid_price", f"price={price}")
                console.print(f" [yellow][FuturesMonitor] Block entry: invalid price {price}[/yellow] ")
                return None
            # 2) Feed freshness (use monitor thresholds)
            try:
                if hasattr(self, 'feed_health') and self.feed_health is not None:
                    tx_age = self.feed_health.age('TX')
                    # 2026-05-27 Gemini CLI: Use dynamic ticker for status info
                    tmf_age = self.feed_health.age(self.ticker)
                    max_age = getattr(self, 'STALE_WARN_SECS', 120)
                    if tx_age > max_age or tmf_age > max_age:
                        # 2026-05-27 Gemini CLI: Use dynamic ticker in audit log
                        self._audit_signal("ENTRY_BLOCKED", "", 0, "feed_stale", f"TX={tx_age:.0f}s {self.ticker}={tmf_age:.0f}s")
                        console.print(f" [yellow][FuturesMonitor] Block entry: feed stale TX={tx_age:.0f}s {self.ticker}={tmf_age:.0f}s[/yellow] ")
                        return None
            except Exception:
                pass
            # 3) Do not enter on the same bar as last trade
            if hasattr(self, '_last_trade_ts') and self._last_trade_ts is not None:
                try:
                    if ts == self._last_trade_ts:
                        self._audit_signal("ENTRY_BLOCKED", "", 0, "same_bar", "same_bar_as_last_trade")
                        console.print(f" [yellow][FuturesMonitor] Block entry: same bar as last trade ({ts})[/yellow] ")
                        return None
                except Exception:
                    pass
            # 4) Enforce simple position guard: avoid new entry when a position exists (prevent pyramiding)
            if getattr(self, 'trader', None) is not None and self.trader.position != 0:
                self._audit_signal("ENTRY_BLOCKED", "", 0, "position_not_zero", f"position={self.trader.position}")
                console.print(f" [yellow][FuturesMonitor] Block entry: position not zero ({self.trader.position})[/yellow] ")
                return None
            # 5) Minimum stop loss check (prevent tiny stops)
            try:
                min_sl = self.RISK.get('min_stop_loss_pts', 10)
                if stop_loss is not None and stop_loss < min_sl:
                    self._audit_signal("ENTRY_BLOCKED", "", 0, "stop_loss_too_small", f"sl={stop_loss}")
                    console.print(f" [yellow][FuturesMonitor] Block entry: stop_loss {stop_loss} < min {min_sl}[/yellow] ")
                    return None
            except Exception:
                pass

            # Passed pre-entry guards — update entry bookkeeping
            self._last_entry_reason = reason
            # [Bug Fix] Initialize trail peak to entry price
            self._atr_trail_peak = price
            self._vwap_violation_bars = 0
            # GSD Phase 0b: Reset consecutive losses on new entry
            self.consecutive_losses = 0
            # GSD Phase 0d: Reset bar counter on new entry
            self._last_trade_ts = ts
            self._bars_since_trade = 0
            self._signals_generated += 1

            # ── Squeeze Fire Scout: record entry bar + time_stop_bars ──
            if reason and "SCOUT" in str(reason).upper():
                self._scout_entry_bar = self._bar_counter
                self._scout_time_stop_bars = 6  # default; overridden by signal metadata if available
                console.print(f"[cyan]🔍 Scout time stop: entry_bar={self._scout_entry_bar} time_stop={self._scout_time_stop_bars} bars[/cyan]")
            else:
                self._scout_entry_bar = -1
                self._scout_time_stop_bars = 0

        # ── [L3] Route through OrderManager if enabled ──
        if self._use_order_manager and self.order_mgr and signal in ("BUY", "SELL", "EXIT", "PARTIAL_EXIT"):
            if live_ready and signal in ("BUY", "SELL") and not self._margin_sufficient():
                console.print(f"[red][FuturesMonitor] ⛔ 保證金不足，取消 {signal}[/red]")
                from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
                save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "margin_insufficient", "lots": lots})
                return None
            if live_ready and signal in ("EXIT", "PARTIAL_EXIT"):
                self._cancel_safety_stop()
            return self._submit_order_via_manager(signal, price, ts, lots,
                                                   stop_loss=stop_loss,
                                                   break_even_trigger=break_even_trigger,
                                                   trail_points=trail_points,
                                                   reason=reason)

        # Sanitize zero values to None for PaperTrader logic
        be_trigger = break_even_trigger if break_even_trigger and break_even_trigger > 0 else None
        tp_trail = trail_points if trail_points and trail_points > 0 else None

        result = self.trader.execute_signal(
            signal, price, ts, lots=lots,
            max_lots=self.MGMT.get("max_positions", 2),
            stop_loss=stop_loss, break_even_trigger=be_trigger, 
            trail_points=tp_trail, exit_reason=reason,
        )
        if not result:
            from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
            save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "papertrader_rejected", "lots": lots})
            return None
        # 信號成功執行，記錄審計軌跡
        from strategies.futures.squeeze_futures.data.data_storage import save_signal_audit
        save_signal_audit({"timestamp": ts, "signal": signal, "price": price, "reason": reason or "", "rejection": "", "lots": lots})
        save_trade({"type": signal, "timestamp": ts, "price": price, "lots": lots,
                    "direction": direction, "pnl_pts": round(pnl_pts, 1),
                    "pnl_cash": round(pnl_cash, 0), "friction_cost": round(friction_cost, 0),
                    "reason": reason or "", "cross_policy": getattr(self, '_last_cross_policy', None)})

        if self._use_order_manager and self.order_mgr and signal in ("EXIT", "PARTIAL_EXIT") and exit_order_side is not None:
            self._append_filled_lifecycle_order(
                side=exit_order_side,
                price=price,
                ts=ts,
                lots=lots,
                strategy="futures",
                comment=f"{signal} {reason or ''}".strip(),
            )
            self._save_orders_file_wrapper()

        # GSD Phase 0c: Entry diagnostic snapshot
        if signal in ("BUY", "SELL"):
            ctx = getattr(self, "_last_bar_context", {})
            self._entry_features_futures = {
                "momentum": ctx.get("momentum", 0),
                "mom_velo": ctx.get("mom_velo", 0),
                "vwap_distance_pts": round(abs(price - ctx.get("vwap", price)), 1),
                "atr": ctx.get("atr", 0),
                "regime": ctx.get("regime", "UNKNOWN"),
                "score": ctx.get("score", 0),
                "entry_price": float(price)
            }
            save_trade({"type": "ENTRY_DIAG", "timestamp": ts, "signal": signal,
                        "price": price, "lots": lots, "direction": direction,
                        "reason": reason or "",
                        "entry_diag": self._entry_features_futures,
                        "cross_policy": getattr(self, '_last_cross_policy', None)})

        # [GSD Phase B] Log outcome attribution
        if signal in ("EXIT", "PARTIAL_EXIT") and hasattr(self, "_entry_features_futures") and self._entry_features_futures:
            from core.decision_logger import DecisionLogger
            outcome = {
                "pnl": float(pnl_cash),
                "pnl_pts": float(pnl_pts),
                "exit_price": float(price),
                "exit_reason": str(reason or "SIGNAL")
            }
            DecisionLogger.log_trade_outcome(
                trade_id=f"FUT-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                strategy=self.active_strategy_name,
                regime=self._entry_features_futures.get("regime", "NORMAL"),
                features=self._entry_features_futures,
                outcome=outcome
            )
            if signal == "EXIT":
                self._entry_features_futures = {}

        # GSD Phase 0b: Track consecutive losses on exit
        if signal in ("EXIT", "PARTIAL_EXIT") and pnl_pts < 0:
            sess = self.session_type or "day"
            self.consecutive_losses += 1
            self.session_losses.append((ts, pnl_pts, reason or "UNKNOWN", sess))
            console.print(f" [yellow]⚠️  Loss #{self.consecutive_losses}: {pnl_pts:.1f} pts ({reason or 'unknown'}) [{sess}][/yellow] ")
        elif signal in ("EXIT", "PARTIAL_EXIT") and pnl_pts >= 0:
            self.consecutive_losses = 0

        d = "🟢 BUY" if signal == "BUY" else "🔴 SELL" if signal == "SELL" else "⚪ EXIT"
        friction_note = f" (摩擦成本 {friction_cost:.0f} TWD)" if friction_cost > 0 else ""
        console.print(f"[bold green][FuturesMonitor] [{ts}] {d} {lots} lots @ {price:.0f}  {result}{friction_note}[/bold green]")
        # Safety stop management
        if live_ready:
            if signal in ("BUY", "SELL"):
                direction = "LONG" if signal == "BUY" else "SHORT"
                sl_pts = stop_loss if stop_loss else self.RISK.get("stop_loss_pts", 60)
                self._place_safety_stop(price, direction, lots, sl_pts)
            if _has_notification_system:
                from core.notification.schemas import TradeEvent as _TE
                te = _TE(
                    trade_id=f"FUT-{datetime.now().strftime('%Y%m%d%H%M%S')}-{int(time.time()*1000)%10000}",
                    action=f"LIVE_{'ENTRY' if signal in ('BUY','SELL') else 'EXIT'}_FILLED",
                    side="LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else "",
                    price=price,
                    quantity=lots,
                )
                _notify_trade_event(event=te, formatter="futures", monitor=self)
            elif _legacy_notify:
                _legacy_notify(
                    f"[MXF] {signal} {lots} lots @ {price:.0f}",
                    f"{d} {lots} lots @ {price:.0f}\n{result}",
                )
        return result

    def _check_stop_loss(self, ts, price):
        if self.trader.position == 0:
            return None
            
        self.RISK.get("stop_loss_pts", 60)
        # 如果有設定 ATR 倍數，則使用動態停損
        if self.ATR_MULT > 0:
            # 這裡需要傳入當前的 df_5m 來算最新的 ATR
            # 但為了效率，我們可以假設在 _strategy_tick 中已經算好了，或者這裡重新算
            # 這裡簡單處理：如果 trader 有 current_stop_loss 就用它
            pass

        if self.trader.position > 0 and self.trader.current_stop_loss and price <= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", price, ts, abs(self.trader.position), reason="STOP_LOSS")
        if self.trader.position < 0 and self.trader.current_stop_loss and price >= self.trader.current_stop_loss:
            return self._execute_trade("EXIT", price, ts, abs(self.trader.position), reason="STOP_LOSS")
        return None

    def _detect_squeeze_failure(self, last_5m, df_5m):
        """
        Detect squeeze breakout failure → return counter signal.
        Returns: "COUNTER_BUY", "COUNTER_SELL", or None
        """
        fired = last_5m.get("fired", False)
        momentum = last_5m.get("momentum", 0)
        close = last_5m["Close"]

        # New fire event
        if fired:
            self._fire_pending_dir = 1 if momentum > 0 else -1
            self._fire_bar_idx = self._bar_counter
            self._fire_high = close
            self._fire_low = close
            return None

        if self._fire_pending_dir == 0:
            return None

        bars_since = self._bar_counter - self._fire_bar_idx
        self._fire_high = max(self._fire_high, close)
        self._fire_low = min(self._fire_low, close)

        # Expire
        if bars_since > self.counter_confirm_bars:
            self._fire_pending_dir = 0
            return None

        if bars_since < 1:
            return None

        # Failure validation
        recent_high = last_5m.get("recent_high", close)
        recent_low = last_5m.get("recent_low", close)
        mom_velo = last_5m.get("mom_velo", 0)
        vwap = last_5m.get("vwap", close)

        if self._fire_pending_dir == 1:  # Bullish fire failed?
            no_new_high = close < recent_high
            velo_reversed = mom_velo <= 0
            vwap_reject = close < vwap
            if no_new_high and (velo_reversed or vwap_reject):
                self._fire_pending_dir = 0
                return "COUNTER_SELL"
        else:  # Bearish fire failed?
            no_new_low = close > recent_low
            velo_reversed = mom_velo >= 0
            vwap_reject = close > vwap
            if no_new_low and (velo_reversed or vwap_reject):
                self._fire_pending_dir = 0
                return "COUNTER_BUY"

        return None

    def _is_ranging_regime(self, df_5m):
        """Auto-detect ranging market: recent bars flip bullish_align frequently."""
        if len(df_5m) < 20:
            return False
        recent = df_5m["bullish_align"].iloc[-20:]
        flips = (recent != recent.shift(1)).sum()
        return flips >= 4  # 20 bars 內翻轉 4 次以上 → 盤整

    def _ensure_indicator_schema(self, path: Path, new_data_keys: list):
        """🛡️ [GSD Load-time Normalize] Ensure CSV schema is consistent ONCE at startup."""
        if not path.exists(): return
        try:
            df = pd.read_csv(path)
            unnamed_cols = [c for c in df.columns if str(c).startswith("Unnamed")]
            if "timestamp" not in df.columns and unnamed_cols:
                df = df.rename(columns={unnamed_cols[0]: "timestamp"})
                unnamed_cols = unnamed_cols[1:]
            if unnamed_cols:
                df = df.drop(columns=unnamed_cols)
            
            missing = [c for c in new_data_keys if c not in df.columns]
            if missing:
                console.print(f" [yellow]🛡️ Migrating indicator CSV: adding {missing}[/yellow] ")
                for c in missing:
                    df[c] = pd.NA
            
            # 2026-06-23 Gemini CLI: Sort columns to keep a stable order but ensure timestamp is first
            cols = sorted(list(df.columns))
            if "timestamp" in cols:
                cols.remove("timestamp")
                cols = ["timestamp"] + cols
            df = df.reindex(columns=cols)
            df.to_csv(path, index=False)
            
            # Cache the column order for subsequent appends
            self._indicator_cols = cols
            self._indicators_migrated = True
        except Exception as e:
            console.print(f"[red]Schema migration failed:[/red] {e}")

    def _save_bar(self, row, score, regime):
        log_dir = runtime_logs("market_data")
        os.makedirs(log_dir, exist_ok=True)
        
        from core.date_utils import get_session_date_str, get_session
        now = datetime.now()
        date_str = get_session_date_str(now)
        
        tag = "_DRY" if self.dry_run else ("_LIVE" if self.live_trading else "_PAPER")
        path = Path(log_dir) / f"{self.ticker}_{date_str}{tag}_indicators.csv"
        
        # 1. Prepare Data
        data = row.to_dict()
        if "trading_day" in data and data["trading_day"] is not None:
            td = data["trading_day"]
            data["trading_day"] = td.isoformat() if hasattr(td, "isoformat") else str(td)
            
        data.update({
            "timestamp": str(row.name),
            "session": get_session(now),
            "score": score,
            "regime": regime,
            "router_regime": row.get("router_regime", regime),
            "router_bias": row.get("router_bias", "UNKNOWN"),
            "volume_spike": float(row.get("volume_spike", row.get("volume", 1))),
            "trend_strength_raw": float(row.get("trend_strength_raw", row.get("trend", 0))),
            "open": row.get("Open", 0), "high": row.get("High", 0), "low": row.get("Low", 0), "close": row.get("Close", 0),
            "volume": row.get("Volume", 0), "amount": row.get("Amount", 0),
            "bull_align": row.get("bullish_align", False), "bear_align": row.get("bearish_align", False),
            "in_pb_zone": row.get("in_bull_pb_zone", False) or row.get("in_bear_pb_zone", False),
        })

        # [BUG FIX DIAGNOSTIC] Check what data dict contains before writing
        console.print(
            f"[dim][SAVE_BAR_CHECK] ts={data.get('timestamp')} "
            f"atr_in_data={'atr' in data} atr_val={data.get('atr', 'MISSING')} "
            f"vwap_in_data={'vwap' in data} vwap_val={data.get('vwap', 'MISSING')} "
            f"sqz_in_data={'sqz_on' in data} sqz_val={data.get('sqz_on', 'MISSING')} "
            f"mom_in_data={'momentum' in data} mom_val={data.get('momentum', 'MISSING')} "
            f"data_keys_sample={list(data.keys())[:5]}...[/dim]"
        )

        # 2. Schema Normalization (Once per session)
        if not hasattr(self, "_indicators_migrated") or not self._indicators_migrated:
            self._ensure_indicator_schema(path, list(data.keys()))
            self._indicators_migrated = True

        # 3. Fast Append with Timestamp Gating
        try:
            current_ts = pd.to_datetime(data["timestamp"])
            
            # [BUG FIX 2026-05-13] Canonical column order for indicator CSV.
            # Never use sorted(data.keys()) — alpha sort puts Close before timestamp,
            # causing column misalignment between first-time header and subsequent appends.
            CANONICAL_INDICATOR_COLS = [
                "timestamp", "Close", "High", "Low", "Open", "Volume", "amount",
                "atr", "atr_floor", "atr_raw", "atr_used",
                "bb_low", "bb_lower", "bb_mid", "bb_up", "bb_upper",
                "bear_align", "bear_breakout", "bear_breakout_strength", "bear_breakout_strength_atr",
                "bearish_align", "breakout_strength", "breakout_strength_atr",
                "bull_align", "bull_breakout", "bullish_align",
                "close", "d_val", "day_max", "day_min", "day_open",
                "ema_200_up", "ema_fast", "ema_filter", "ema_macro", "ema_slow",
                "fired",
                "high", "high_20_prev",
                "in_bear_pb_zone", "in_bull_pb_zone", "in_pb_zone",
                "intraday_strength_pct", "is_bear_structural_breakout", "is_bull_structural_breakout",
                "is_new_high", "is_new_low", "is_structural_breakout",
                "k_val",
                "low", "low_20_prev",
                "macd_hist", "macd_line", "macd_rising", "macd_signal",
                "mom_prev", "mom_state", "mom_velo", "momentum",
                "open", "opening_bearish", "opening_bullish",
                "price_vs_vwap", "price_vs_vwap_pct",
                "recent_high", "recent_low",
                "regime", "router_bias", "router_regime", "rsi", "rsv",
                "score", "session", "squeeze_release", "sqz_on",
                "trading_day", "trend_strength_raw",
                "volume", "volume_spike", "vwap",
            ]
            
            if not path.exists():
                # First time: Write header with canonical column order
                cols = [c for c in CANONICAL_INDICATOR_COLS if c in data]
                self._indicator_cols = cols
                pd.DataFrame([data])[cols].to_csv(path, index=False)
                self._last_saved_ts = current_ts
            else:
                # [GSD Idempotency Fix] Read last TS from file if not in memory
                if not hasattr(self, "_last_saved_ts") or self._last_saved_ts is None:
                    try:
                        # [BUG FIX 2026-05-13] Read last timestamp from actual timestamp column
                        # instead of blindly taking split(',')[0] which may be Close, not timestamp.
                        from core.date_utils import parse_csv_last_timestamp
                        self._last_saved_ts = parse_csv_last_timestamp(path)
                        if self._last_saved_ts is None or self._last_saved_ts == pd.NaT:
                            self._last_saved_ts = pd.Timestamp.min
                    except:
                        self._last_saved_ts = pd.Timestamp.min

                # Only append if this is a NEW bar
                if current_ts > self._last_saved_ts:
                    cols = getattr(self, "_indicator_cols", None)
                    if cols is None:
                        # [BUG FIX 2026-05-13] Read column order from CSV header to avoid
                        # misalignment between sorted() append and backfill column order.
                        try:
                            cols = pd.read_csv(path, nrows=0).columns.tolist()
                        except Exception:
                            cols = sorted(data.keys())
                    row_df = pd.DataFrame([data])
                    row_df.reindex(columns=cols).to_csv(path, mode='a', header=False, index=False)
                    self._last_saved_ts = current_ts
                    self._backfill_has_seen_enriched_row = True
                # else: ignore duplicate bar
        except Exception as e:
            console.print(f"[red]Fast-append failed:[/red] {e}")

    # ── P4 Hardening: Data freshness ──────────────────────────────────

    def _check_canonical_freshness(self, df_5m: pd.DataFrame | None) -> list[str]:
        """Check if canonical 5m bars are stale (no new bar within SLA).

        SLA: normal = <= 2 × bar interval (10 minutes for 5m bars).
        If stale, returns ["STALE_DATA"] — does NOT crash, does NOT fetch.

        Returns:
            list[str]: flags to attach to MarketData (empty list if fresh).
        """
        if df_5m is None or df_5m.empty:
            return []

        last_ts = df_5m.index[-1] if hasattr(df_5m.index, 'dtype') else df_5m.index[-1]
        now = datetime.now()

        # Normalise to datetime for comparison
        if isinstance(last_ts, pd.Timestamp):
            last_bar_dt = last_ts.to_pydatetime()
        elif isinstance(last_ts, datetime):
            last_bar_dt = last_ts
        else:
            return []  # Can't determine freshness

        elapsed_secs = (now - last_bar_dt).total_seconds()

        # [P4 Hardening] Canonical freshness SLA: warn at 2× bar interval
        sla_secs = getattr(self, 'CANONICAL_SLA_SECS', 600)  # Default 10 min
        if elapsed_secs > sla_secs:
            console.print(
                f" [yellow][P4] Canonical 5m data stale: last_bar_ts={last_bar_dt.strftime('%H:%M:%S')}, "
                f"age={elapsed_secs:.0f}s (>SLA {sla_secs}s). Flagging STALE_DATA.[/yellow] "
            )
            return ["STALE_DATA"]

        # If previous tick had STALE_DATA and is now fresh again, log recovery
        if getattr(self, '_was_stale', False) and elapsed_secs <= sla_secs:
            console.print(
                f"[green][P4] Canonical 5m data recovered: age={elapsed_secs:.0f}s (within SLA).[/green]"
            )
            self._was_stale = False

        return []

    def _check_tick_api_consistency(
        self,
        df_tick: pd.DataFrame | None,
        df_1min: pd.DataFrame | None,
        bar_source: dict[str, object],
    ) -> None:
        """[P4 Hardening] Compare tick-5m vs api-1m close prices at most recent bar.

        Tick-5m is the preferred source (P1). Api-1m (P2 backfill) is the fallback.
        If both are available and their latest bar's close differs by more than
        MAX_TICK_POINT_DISCREPANCY (default 5.0 MXF points), log a structured warning.

        Design:
        - Periodic only (every 30 ticks via _bar_counter guard in caller).
        - Never fetches, never crashes, never blocks trading.
        - Only warns when both sources have data and the selected source is NOT the
          one that looks fresher — indicating the pipeline may have stale data.
        """
        max_diff = getattr(self, 'MAX_TICK_POINT_DISCREPANCY', 5.0)

        if df_tick is None or df_tick.empty or df_1min is None or df_1min.empty:
            return

        # Get last bar Close from each source
        tick_last_close = None
        try:
            tick_idx = df_tick.index[-1] if hasattr(df_tick, 'index') else None
            if isinstance(tick_idx, pd.Timestamp):
                tick_last_close = float(df_tick['Close'].iloc[-1])
        except Exception:
            pass

        api_last_close = None
        try:
            # df_1min is 1-minute bars; sample the last one
            api_idx = df_1min.index[-1] if hasattr(df_1min, 'index') else None
            if isinstance(api_idx, pd.Timestamp):
                api_last_close = float(df_1min['Close'].iloc[-1])
        except Exception:
            pass

        if tick_last_close is None or api_last_close is None:
            return

        diff = abs(tick_last_close - api_last_close)
        if diff <= max_diff:
            return  # Within tolerance — no warning

        # Discrepancy detected — log structured warning
        tick_last_ts = str(df_tick.index[-1]) if hasattr(df_tick, 'index') else 'N/A'
        api_last_ts = str(df_1min.index[-1]) if hasattr(df_1min, 'index') else 'N/A'
        source_name = str(bar_source.get('source', 'unknown'))

        console.print(
            f" [yellow][IngestionWatchdog] "
            f"reason=tick_api_mismatch "
            f"tick_close={tick_last_close:.1f} "
            f"api_close={api_last_close:.1f} "
            f"diff={diff:.1f} "
            f"threshold={max_diff:.1f} "
            f"tick_last_ts={tick_last_ts} "
            f"api_last_ts={api_last_ts} "
            f"active_source={source_name} "
            f"action=none "
            f"result=warning_only[/yellow] "
        )

    # ── End P4 Hardening ──────────────────────────────────────────────

    def _periodic_backfill_bars(self):
        """[Phase 2] Rate-limited periodic backfill via IngestionService.

        Delegates to self._ingestion.fetch_backfill() which handles
        rate limiting (120s), CSV persistence, and TXFR1 pre-fetch.

        Returns DataFrame or None if rate-limited/unavailable.
        """
        if self.dry_run or not self.api or not self.contract:
            return None
        # [Fix] Safety net: sync ingestion contract (resolved after __init__)
        try:
            if self._ingestion._contract is None:
                self._ingestion.set_contract(self.contract)
        except Exception:
            pass
        _will_fetch = (time.time() - getattr(self._ingestion, "_last_kbars_fetch_at", 0)) >= 120
        _perf_started = time.perf_counter()
        if _will_fetch:
            logger.info("[PERF] periodic_backfill_start")
        _result = self._ingestion.fetch_backfill()
        _elapsed_ms = (time.perf_counter() - _perf_started) * 1000
        if _will_fetch or _elapsed_ms >= 100:
            logger.info("[PERF] periodic_backfill_done duration_ms=%.1f fetched=%s", _elapsed_ms, _result is not None)
        return _result

    def _fetch_today_kbars(self):
        """[Phase 2] Fetch today's kbars via IngestionService.

        Delegates to self._ingestion.fetch_backfill() for rate-limited
        API access with CSV persistence and TXFR1 pre-fetch.

        ═══ RESTRICTION: STARTUP / BACKFILL ONLY ═══
        This function MUST NOT be called from _strategy_tick().  The
        runtime guard below enforces this.  strategy_tick() accesses
        data via _periodic_backfill_bars() only.
        """
        # 🛡️ Runtime guard: detect if we are inside _strategy_tick() call stack
        import traceback
        for frame in traceback.extract_stack():
            if frame.name == '_strategy_tick':
                raise RuntimeError(
                    "[GUARD] _fetch_today_kbars() called from _strategy_tick() context. "
                    "Use _periodic_backfill_bars() instead."
                )
        return self._ingestion.fetch_backfill()

    def _save_raw_kbars(self, bars) -> None:
        """[GSD Data Safety] Save raw shioaji kbars response to CSV."""
        try:
            if self._kbar_writer is None:
                trading_day = get_trading_day_str(datetime.now())
                code = getattr(self.contract, "code", self.ticker)
                self._kbar_writer = RawKbarWriter(code, trading_day)

            # Convert bars NamedTuple/list to DataFrame for the writer
            df_raw = pd.DataFrame({**bars})
            if df_raw.empty:
                return
            self._kbar_writer.write_dataframe(df_raw)
        except Exception:
            # Never let a CSV write failure crash the fetch
            pass

    def run(self):
        # 2026-07-21 Gemini CLI: Bind thread-local state path for the background thread
        _thread_local.state_path = getattr(self, "_state_path", None)
        self._running = True
        mode = "dry-run" if self.dry_run else ("LIVE" if self.live_trading else "PAPER")

        # [GSD Hardening] Heartbeat for main watchdog
        self.last_heartbeat_ts = time.time()
        self._heartbeat_interval_secs = max(1, self.POLL_INTERVAL // 2)
        import threading as _thrd

        def _hb_loop():
            while self._running:
                self.last_heartbeat_ts = time.time()
                time.sleep(self._heartbeat_interval_secs)
        _thrd.Thread(target=_hb_loop, name="futures-hb", daemon=True).start()

        # [Phase A] Immediate Position Recovery & Heartbeat Start
        self._refresh_runtime_status()
        
        if not self.dry_run and self.api:
            try:
                positions = self.api.list_positions(self.api.futopt_account)
                for p in positions:
                    if self.contract and getattr(p, 'code', '') == self.contract.code:
                        qty = p.quantity if str(p.direction) == 'Buy' else -p.quantity
                        self.trader.position = qty
                        self.trader.entry_price = float(p.price)
                        
                        if self.order_mgr:
                            from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
                            rec_order = Order(
                                symbol=self.contract.code,
                                side=OrderSide.BUY if qty > 0 else OrderSide.SELL,
                                order_type=OrderType.MARKET,
                                quantity=abs(qty),
                                price=float(p.price),
                                order_id=f"RECOV-{datetime.now().strftime('%H%M%S')}",
                                strategy="RECOVERED"
                            )
                            rec_order.status = OrderStatus.FILLED
                            rec_order.filled_quantity = abs(qty)
                            rec_order.avg_fill_price = float(p.price)
                            rec_order.filled_at = datetime.now()
                            self.order_mgr.completed.append(rec_order)
                        console.print(f"[bold cyan]♻️ Recovered futures position: {qty} @ {p.price}[/bold cyan]")
                        break
            except Exception as e:
                console.print(f" [yellow]Futures position recovery failed: {e}[/yellow] ")

        # [Phase A.5] Rebuild tick bars from raw tick CSV (crash recovery)
        self._rebuild_bars_from_raw_ticks()

        # [Phase B] Async Indicator Warm-up
        import threading
        self._backfill_done = False
        def _bg_backfill():
            console.print(f"[cyan]⏳ [Phase B] Starting background K-bar backfill...[/cyan]")
            df_hist = self._fetch_today_kbars()
            if df_hist is not None and not df_hist.empty:
                self._backfill_done = True
                console.print(f"[bold green]✅ [Phase B] Backfill complete ({len(df_hist)} bars). Indicators stabilizing...[/bold green]")
            else:
                console.print(f" [yellow]⚠️ [Phase B] Backfill returned no data, will rely on tick accumulation.[/yellow] ")
        
        threading.Thread(target=_bg_backfill, daemon=True).start()

        from core.diagnostic_engine import DiagnosticEngine
        self.diag_engine = DiagnosticEngine(str(Path(runtime_logs("market_data")) / "TMF_trades.csv"))
        self._diag_counter = 0

        console.print(f"[green][FuturesMonitor] started ({mode}). Status: WARMING_UP[/green]")

        while self._running:
            try:
                self._strategy_tick()
                self._diag_counter += 1
                if self._diag_counter % 10 == 0:
                    results = self.diag_engine.check_health()
                    for r in results:
                        console.print(f"[bold red]🩺 DIAGNOSTIC ALERT: {r.action}[/bold red]")
            except Exception as e:
                import traceback, sys
                tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                console.print(f"[red][FuturesMonitor] error: {e}[/red]")
                console.print(f"[dim]{tb_str.strip()}[/dim]")
                with open("/tmp/fm_err.txt", "w") as f:
                    f.write(f"[{datetime.now()}] {e}\n{tb_str}\n")
                print(f"DEBUG TB WROTE to /tmp/fm_err.txt: {e}", file=sys.stderr)
            time.sleep(self.POLL_INTERVAL)

    def stop(self):
        self._running = False
        # 2026-07-24 Gemini CLI: Export Shadow Soak Manifest on monitor shutdown
        try:
            _strat = self._registry.get("tmf_spread") if hasattr(self, "_registry") else None
            if _strat and hasattr(_strat, "close_soak_collector"):
                _strat.close_soak_collector(reason="MONITOR_STOP")
        except Exception:
            pass

    def _cancel_all_pending_orders(self):
        """Cancel all pending orders (limit/market) when session transitions from night to day."""
        if self.dry_run:
            console.print("[dim]dry-run: skipping order cancellation[/dim]")
            return
        
        cancelled_count = 0
        try:
            # If order manager is enabled, use it
            if self.order_mgr:
                # 2026-05-27 Gemini CLI: Fixed API mismatch (get_pending and cancel)
                pending = self.order_mgr.get_pending()
                for order in pending:
                    try:
                        self.order_mgr.cancel(order.order_id, reason="SESSION_TRANSITION")
                        console.print(f" [yellow]✓ Cancelled pending order {order.order_id}[/yellow] ")
                        cancelled_count += 1
                    except Exception as e:
                        console.print(f"[red]Failed to cancel order {order.order_id}: {e}[/red]")
            else:
                # Fallback: direct API cancellation for futures orders
                # This is a simplistic implementation - may need enhancement
                console.print(" [yellow]⚠️ Order manager not enabled; manual API cancellation not implemented yet[/yellow] ")
        except Exception as e:
            console.print(f"[red]Error in _cancel_all_pending_orders: {e}[/red]")
        
        if cancelled_count == 0:
            console.print("[dim]No pending orders to cancel[/dim]")
        else:
            console.print(f"[bold green]✅ Cancelled {cancelled_count} pending order(s)[/bold green]")

    # ── [V-Model] MTS Mode: Minimal Tradable System ──
    # Bypass regime/router/policy/gates entirely. Direct path:
    #   Market → ORB Signal → Risk Check → Execution
    def _sync_mts_status(self):
        """[GSD] Synchronize MTS position and manual order state to disk for dashboard."""
        # 2026-05-27 Gemini CLI: Use isolated path if environment variable is set
        _hb_path = _mts_position_state_path()
        _mts_cfg = self.cfg.get("mts", {})
        _strat_name = _mts_cfg.get("strategy", "tmf_spread")
        strategy = self._registry.get(_strat_name)
        
        try:
            # 2026-06-26 Gemini CLI: Extract current ATR from Kbar processed data
            _last_atr = 0.0
            if hasattr(self, '_last_processed_data') and self._last_processed_data:
                _df_5m = self._last_processed_data.get("5m")
                if _df_5m is not None and not _df_5m.empty and "atr" in _df_5m.columns:
                    _val = _df_5m["atr"].iloc[-1]
                    if pd.notna(_val):
                        try: _last_atr = float(_val)
                        except: pass

            # 1. Base Strategy Info
            _has_pos_in_mem = bool(getattr(strategy, "_has_position", False)) if strategy else False
            
            # Read existing to preserve some fields (like last prices)
            existing = {}
            if _hb_path.exists():
                try:
                    existing = json.loads(_hb_path.read_text())
                except: pass

            # [GSD] Restoration Guard: don't overwrite valid disk state while strategy recovers
            if not _has_pos_in_mem and existing.get("has_position") is True:
                return

            # ADR-009 Task 10: FLAT must not inherit position fields from existing.
            # When local has_position=False, clear near_entry/far_entry/side to prevent
            # stale entry prices from self-perpetuating via the fallback chain:
            #   strategy._near_entry=0 → existing.get("near_entry")=47369 → re-write → loop
            if not _has_pos_in_mem:
                existing.pop("near_entry", None)
                existing.pop("far_entry", None)
                existing.pop("near_side", None)
                existing.pop("far_side", None)
                existing.pop("released_leg", None)
                existing.pop("remaining_side", None)

            # 2. Position Details
            # 2026-06-23 Gemini CLI: Safe parsing of float fields to prevent NoneType TypeError
            _n_entry = getattr(strategy, "_near_entry", 0.0) or float(existing.get("near_entry") or 0.0)
            _f_entry = getattr(strategy, "_far_entry", 0.0) or float(existing.get("far_entry") or 0.0)
            _n_side = getattr(strategy, "_near_side", None) or existing.get("near_side")
            _f_side = getattr(strategy, "_far_side", None) or existing.get("far_side")
            
            # 2026-06-09 JVS Claw: Read latest prices from market_data, fallback to existing
            # 2026-06-23 Gemini CLI: Safe parsing of float fields to prevent NoneType TypeError
            _n_last = float(self.market_data.get(self.ticker, {}).get("close") or 0.0) or float(existing.get("near_last") or 0.0)
            _f_last = float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close") or 0.0) or float(self._far_current_bar.get("close") or 0.0) or float(existing.get("far_last") or 0.0)
            
            # 2026-05-27 Gemini CLI: Use dynamic multiplier from constants instead of hardcoded 10.0
            _mult = float(get_point_value(self.ticker))
            _n_upl = (_n_last - _n_entry) * (-1 if _n_side == "SHORT" else 1) * _mult if _n_entry > 0 and _n_last > 0 and _n_side else 0.0
            _f_upl = (_f_last - _f_entry) * (-1 if _f_side == "SHORT" else 1) * _mult if _f_entry > 0 and _f_last > 0 and _f_side else 0.0

            # 3. Manual Order Details (Enrichment)
            _manual_order_info = {
                "manual_order_ts": existing.get("manual_order_ts", "—"),
                "manual_order_type": existing.get("manual_order_type", "—"),
                "manual_order_filled": existing.get("manual_order_filled", "—")
            }
            
            if self._pending_lifecycle_orders:
                # Find the most recent manual order
                _manual_orders = [
                    (oid, meta) for oid, meta in self._pending_lifecycle_orders.items() 
                    if meta.get("reason") == "MTS_MANUAL"
                ]
                if _manual_orders:
                    _manual_orders.sort(key=lambda x: x[1].get("ts", datetime.min), reverse=True)
                    oid, meta = _manual_orders[0]
                    _manual_order_info = {
                        "manual_order_ts": meta.get("ts").isoformat() if isinstance(meta.get("ts"), datetime) else str(meta.get("ts")),
                        "manual_order_type": "範圍市價 (MKP)",
                        "manual_order_filled": "NO"
                    }
            elif self._manual_trade_status == "FILLED":
                _manual_order_info["manual_order_filled"] = "YES"

            _hb_state = {
                "has_position": _has_pos_in_mem,
                "state": "HEARTBEAT",
                "reason": "mts_sync_status",
                "manual_trade_status": self._manual_trade_status,
                "near_side": _n_side, "far_side": _f_side,
                "near_entry": round(_n_entry, 1), "far_entry": round(_f_entry, 1),
                "near_last": round(_n_last, 1), "far_last": round(_f_last, 1),
                "near_upl": round(_n_upl, 1), "far_upl": round(_f_upl, 1),
                "total_upl": round(_n_upl + _f_upl, 1),
                "initial_balance": self.initial_balance,
                "balance": getattr(self.trader, "balance", 0) if hasattr(self, "trader") else 0,
                # Live mode: real account equity from Shioaji API
                "live_equity": (
                    float(self.api.margin(self.api.futopt_account).equity)
                    if self.live_trading and self.api and hasattr(self.api, "margin")
                    else None
                ),
                "atr": round(_last_atr, 2), # 2026-06-26 Gemini CLI: pass current ATR to state writer
                "_updated": datetime.now().isoformat(),
            }
            # 2026-07-07 Hermes Agent: Do not write simplified MTS position state
            # while strategy owns a live position. Full lifecycle-aware state is
            # written by tick heartbeat / strategy.write_state().
            # _sync_mts_status writes a flat schema (no lifecycle/release_group/
            # trail_group) which would strip the state machine fields on every
            # poll cycle, causing the dashboard to briefly flash a degraded view.
            if _has_pos_in_mem:
                return

            _hb_state.update(_manual_order_info)
            
            # 2026-06-23 Gemini CLI: Use unique temporary filename to avoid race conditions with other writers
            import random
            _tmp_file = f"{_hb_path}.tmp.{os.getpid()}.{random.randint(1000, 9999)}"
            try:
                with open(_tmp_file, "w") as f:
                    json.dump(_hb_state, f, default=str)
                os.replace(_tmp_file, str(_hb_path))
            except Exception as e:
                if os.path.exists(_tmp_file): os.remove(_tmp_file)
                raise e
        except Exception as e:
            console.print(f"[red]⚠️ MTS Status Sync failed: {e}[/red]")

    def _run_mts_watchdog(self):
        """
        2026-05-27 Gemini CLI: Tiered MTS Safety Watchdog (P4).
        - High-Freq (10s): EXITING state-lock & Pending order timeouts.
        - Low-Freq (30s): Broker reconciliation & Feed health.
        """
        if not self.order_mgr or self.dry_run:
            return

        now_mono = time.monotonic()
        
        # ── Tier 1: High-Frequency Check (Every 10s) ──
        _last_hi_check = getattr(self, "_mts_watchdog_last_hi", 0.0)
        if (now_mono - _last_hi_check) < 10.0:
            return
        self._mts_watchdog_last_hi = now_mono

        _mts_cfg = self.cfg.get("mts", {})
        strategy = self._registry.get(_mts_cfg.get("strategy", "tmf_spread"))
        if not strategy: return

        now_dt = datetime.now()
        
        # 1.1 Pending Order Timeout
        # 2026-06-08 JVS Claw: Extended timeout coverage for all MTS order types.
        # EXIT/RELEASE: 15s (urgent — need to close position quickly)
        # ENTRY/MANUAL: 30s (single leg) or 60s (one leg filled, waiting for other)
        to_resubmit = []
        to_cancel_notify = []  # Orders to cancel + notify user (ENTRY/MANUAL)
        for order_id, meta in list(self._pending_lifecycle_orders.items()):
            _strat_label = meta.get("strategy") or ""
            _is_exit = "MTS_EXIT" in _strat_label or "MTS_RELEASE" in _strat_label
            _is_entry = "MTS_ENTRY" in _strat_label or "MTS_MANUAL" in _strat_label
            if not _is_exit and not _is_entry:
                continue
            if order_id in self._mts_stale_order_cancels:
                continue

            _submit_ts = meta.get("ts")
            if not _submit_ts:
                continue

            age_secs = (now_dt - _submit_ts).total_seconds()

            # Determine timeout based on order type and partial fill status
            if _is_exit:
                _timeout = 15
            else:
                # Check if the other leg of the same trade is already filled
                _trade_id = meta.get("trade_id")
                _has_partial = False
                if _trade_id and _trade_id in self._mts_pending_fills:
                    _fill_data = self._mts_pending_fills[_trade_id]
                    _has_partial = _fill_data.get("near_filled", False) or _fill_data.get("far_filled", False)
                _timeout = 60 if _has_partial else 30

            if age_secs > _timeout:
                order = self.order_mgr.get_order(order_id)
                from core.order_management.order import OrderStatus
                if order is None:
                    order = next((candidate for candidate in
                                  getattr(self.order_mgr, "completed", [])
                                  if candidate.order_id == order_id), None)
                if order and order.status in (
                        OrderStatus.PENDING_SUBMIT,
                        OrderStatus.PRE_SUBMITTED,
                        OrderStatus.SUBMITTED,
                        OrderStatus.PARTIAL_FILLED,
                        OrderStatus.CANCELLED,
                        OrderStatus.EXPIRED):
                    _truth = self._watchdog_broker_truth(order)
                    if _truth.get("protect"):
                        if (order.status in (OrderStatus.CANCELLED,
                                             OrderStatus.EXPIRED)
                                and _truth.get("reason") in (
                                    "BROKER_HAS_POSITION_OR_ORDER",
                                    "BROKER_QUERY_UNAVAILABLE")):
                            self._restore_terminal_watchdog_order(order, _truth)
                        # Broker position/open-order truth wins.  Do not put
                        # this order into either cancellation queue.
                        continue
                    if _is_exit:
                        console.print(f"[bold yellow]⚠️ [WATCHDOG] MTS Order {order_id} hanging >{_timeout}s. Cancelling...[/bold yellow]")
                        to_resubmit.append(order_id)
                    else:
                        console.print(f"[bold red]🚨 [WATCHDOG] MTS Order {order_id} timeout >{_timeout}s. Cancelling and notifying...[/bold red]")
                        to_cancel_notify.append(order_id)

        for order_id in to_resubmit:
            try:
                self._mts_stale_order_cancels.add(order_id)
                self.order_mgr.cancel(order_id)
            except Exception as e:
                console.print(f"[red]❌ [WATCHDOG] Stale order cancel failed: {e}[/red]")

        # 2026-06-08 JVS Claw: Cancel + notify for ENTRY/MANUAL timeouts
        for order_id in to_cancel_notify:
            try:
                self._mts_stale_order_cancels.add(order_id)
                self.order_mgr.cancel(order_id)
                self._manual_trade_status = f"FAILED: ORDER_TIMEOUT ({order_id})"
                self._append_mts_event("ORDER_TIMEOUT", order_id=order_id)
                console.print(f"[bold red]🚨 [MTS_TIMEOUT] Order {order_id} cancelled — exceeded 30/60s timeout[/bold red]")
            except Exception as e:
                console.print(f"[red]❌ [WATCHDOG] Entry timeout cancel failed: {e}[/red]")

        # 1.2 EXITING State Lock (15s)
        _lifecycle = getattr(strategy, "_lifecycle", "FLAT")
        _exit_start = getattr(strategy, "_exit_start_time", 0.0)
        if _lifecycle == "EXITING" and _exit_start > 0:
            if (now_mono - _exit_start) > 15.0:
                # 2026-05-27 Gemini CLI: Enhanced Alert Logic
                _broker_pos = self.trader.position
                if _broker_pos == 0:
                    console.print(f"[bold green]♻️ [WATCHDOG] EXITING stuck but Broker is FLAT. Self-healing state.[/bold green]")
                    strategy._reset(reason="WATCHDOG_EXITING_HEAL")
                else:
                    console.print(f"[bold red]🚨 [WATCHDOG] ALERT: EXITING stuck >15s and Broker STILL HAS POSITION ({_broker_pos}). Manual attention required![/bold red]")
                    
                    # 2026-05-27 Gemini CLI: P5: Forensic Forensic Metadata Contract
                    # Find potential pending order ID for this exit
                    _pending_oid = next((oid for oid, meta in self._pending_lifecycle_orders.items() 
                                       if "MTS_EXIT" in (meta.get("strategy") or "")), "NONE")
                    
                    self._append_mts_event("WATCHDOG_ALERT", 
                                          reason="EXIT_FAILED_ATTENTION_REQUIRED", 
                                          lifecycle=_lifecycle,
                                          broker_position=_broker_pos,
                                          local_position=bool(getattr(strategy, "_has_position", False)),
                                          pending_order_id=_pending_oid,
                                          elapsed_secs=round(now_mono - _exit_start, 1))
                    
                    # We don't reset here to avoid losing the "stuck" visibility in logs, 
                    # but we mark the status for dashboard
                    self._manual_trade_status = "FAILED_EXIT_REQUIRES_ATTENTION"

        # ── Tier 2: Low-Frequency Check (Every 30s) ──
        _last_lo_check = getattr(self, "_mts_watchdog_last_lo", 0.0)
        if (now_mono - _last_lo_check) < 30.0:
            return
        self._mts_watchdog_last_lo = now_mono

        # 2.1 Broker Reconciliation
        _has_pos_in_mem = bool(getattr(strategy, "_has_position", False))
        
        # 2026-07-01 Gemini CLI: Sync paper trader position from restored strategy state on startup to prevent immediate reconciliation reset
        if not self.live_trading and self.trader.position == 0 and _has_pos_in_mem:
            _released_leg = getattr(strategy, "_released_leg", None)
            if _released_leg != "near":
                self.trader.position = 1
                self.trader.entry_price = getattr(strategy, "_near_entry", 0.0) or getattr(strategy, "_far_entry", 0.0)
                console.print(f"[bold cyan]♻️ [MTS_SYNC] Initialized paper trader position to 1 from restored strategy state[/bold cyan]")
                
        _broker_pos = self.trader.position 
        _entry_mono = getattr(strategy, "_entry_time_monotonic", 0.0)
        _released_leg = getattr(strategy, "_released_leg", None)
        
        # 💡 [Fixed 2026-05-27] Spread-aware reconciliation
        _should_be_flat_at_broker = (_released_leg == "near")
        _is_out_of_sync = False
        
        if _has_pos_in_mem:
            if _should_be_flat_at_broker:
                if _broker_pos != 0: _is_out_of_sync = True
            else:
                if _broker_pos == 0: _is_out_of_sync = True

        if _is_out_of_sync and (now_mono - _entry_mono) > 60.0:
            console.print(f"[bold red]🚨 [WATCHDOG] Reconciliation: Memory state ({_has_pos_in_mem}, released={_released_leg}) mismatch with Broker ({_broker_pos}) >60s. Syncing...[/bold red]")
            self._append_mts_event("RECONCILIATION_FAILURE", reason="GHOST_POSITION", mem_pos=_has_pos_in_mem, released=_released_leg, broker_pos=_broker_pos)
            strategy._reset(reason="WATCHDOG_RECONCILIATION_SYNC")

    # ── Broker Snapshot Probe ──────────────────────────────────────────
    # Read-only: captures positions/open-orders using existing Shioaji
    # session. Triggered by request file at /tmp/mts_broker_snapshot_request.json
    # Response written to exports/trades/live/diagnostics/
    # ────────────────────────────────────────────────────────────────────

    _BROKER_SNAPSHOT_REQUEST_PATH = "/tmp/mts_broker_snapshot_request.json"

    def capture_broker_snapshot(self) -> dict:
        """Capture a read-only BrokerSnapshot using the existing Shioaji session.

        Returns a dict matching BrokerSnapshot schema.
        Raises RuntimeError on any query failure (never returns partial data).
        """
        from datetime import timezone
        now = datetime.now(timezone.utc)

        raw_api = getattr(self, "api", None)
        if raw_api is None:
            raise RuntimeError("BROKER_CLIENT_UNAVAILABLE")

        account = getattr(raw_api, "futopt_account", None)
        if account is None:
            raise RuntimeError("BROKER_ACCOUNT_UNAVAILABLE")

        # Positions (must succeed)
        try:
            positions = list(raw_api.list_positions(account))
        except Exception as exc:
            raise RuntimeError(f"BROKER_POSITION_QUERY_FAILED: {exc}") from exc

        pos_time = datetime.now(timezone.utc)

        # Open orders (must succeed, but empty is valid)
        try:
            all_trades = list(raw_api.list_trades())
        except Exception:
            try:
                all_trades = list(raw_api.list_trades(account))
            except Exception as exc:
                raise RuntimeError(f"BROKER_ORDER_QUERY_FAILED: {exc}") from exc

        ord_time = datetime.now(timezone.utc)

        open_orders = [
            t for t in all_trades
            if getattr(t.status, "status", "") not in ("Filled", "Cancelled", "Expired", "Done")
        ]

        # Account identity (for preflight match check)
        account_id_raw = f"{account.person_id}:{account.broker_id}:{account.account_id}"
        import hashlib
        account_id_hash = hashlib.sha256(account_id_raw.encode()).hexdigest()

        return {
            "connected": True,
            "authenticated": True,
            "account_id_hash": account_id_hash,
            "position_count": len(positions),
            "open_order_count": len(open_orders),
            "position_snapshot_time": pos_time.isoformat(),
            "order_snapshot_time": ord_time.isoformat(),
            # Raw data for audit (no PII)
            "_positions": [
                {"code": p.code, "qty": p.quantity, "pnl": p.pnl}
                for p in positions
            ],
            "_open_orders": [
                {"code": t.code, "qty": t.quantity, "status": getattr(t.status, "status", "?")}
                for t in open_orders
            ],
        }

    def _check_broker_snapshot_request(self) -> bool:
        """Check for a broker snapshot request file and process it.

        Returns True if a request was processed (caller should skip tick),
        False if no request was present.
        """
        import json, os
        from datetime import timezone

        request_path = self._BROKER_SNAPSHOT_REQUEST_PATH
        if not os.path.exists(request_path):
            return False

        try:
            with open(request_path, "r") as f:
                request = json.load(f)
        except Exception:
            # Malformed request: remove and ignore
            try:
                os.remove(request_path)
            except Exception:
                pass
            return False

        request_id = request.get("request_id", "UNKNOWN")
        operation = request.get("operation", "")

        if operation != "CAPTURE_BROKER_SNAPSHOT":
            console.print(f"[yellow]⚠️ [BROKER_PROBE] Unknown operation: {operation}[/yellow]")
            try:
                os.remove(request_path)
            except Exception:
                pass
            return False

        console.print(f"[cyan]📷 [BROKER_PROBE] {request_id}: capturing snapshot...[/cyan]")

        # Create diagnostic output directory
        diag_dir = Path(runtime_path("exports", "trades", "live", "diagnostics"))
        diag_dir.mkdir(parents=True, exist_ok=True)

        try:
            snapshot = self.capture_broker_snapshot()

            # Evaluate preflight (pure function, no side effects)
            from core.mode_transition import evaluate_broker_preflight, BrokerSnapshot

            preflight_snapshot = BrokerSnapshot(
                connected=snapshot["connected"],
                authenticated=snapshot["authenticated"],
                account_id_hash=snapshot["account_id_hash"],
                position_count=snapshot["position_count"],
                open_order_count=snapshot["open_order_count"],
                position_snapshot_time=datetime.fromisoformat(snapshot["position_snapshot_time"]),
                order_snapshot_time=datetime.fromisoformat(snapshot["order_snapshot_time"]),
            )
            result = evaluate_broker_preflight(preflight_snapshot)

            # Build response
            response = {
                "request_id": request_id,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "process_start_id": (
                    getattr(getattr(self, "_execution_context", None), "process_start_id", None)
                ),
                "read_only": True,
                "snapshot": snapshot,
                "preflight": {
                    "passed": result.passed,
                    "failed_checks": list(result.failed_checks),
                },
            }

            # Atomic write: tmp → fsync → replace
            tmp_path = diag_dir / f"broker_snapshot_{request_id}.tmp"
            final_path = diag_dir / f"broker_snapshot_{request_id}.json"
            tmp_path.write_text(json.dumps(response, indent=2, default=str))
            tmp_path.replace(final_path)

            # Also update the canonical response file
            latest = diag_dir / "broker_snapshot_latest.json"
            latest_tmp = diag_dir / "broker_snapshot_latest.tmp"
            latest_tmp.write_text(json.dumps(response, indent=2, default=str))
            latest_tmp.replace(latest)

            if result.passed:
                console.print(f"[green]✅ [BROKER_PROBE] {request_id}: PASSED[/green]")
            else:
                console.print(f"[yellow]⚠️ [BROKER_PROBE] {request_id}: FAILED - {result.failed_checks}[/yellow]")

        except RuntimeError as exc:
            failure = {
                "request_id": request_id,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True,
                "error": str(exc),
                "snapshot": None,
                "preflight": {"passed": False, "failed_checks": [str(exc)]},
            }
            final_path = diag_dir / f"broker_snapshot_{request_id}.json"
            final_path.write_text(json.dumps(failure, indent=2, default=str))
            console.print(f"[red]❌ [BROKER_PROBE] {request_id}: ERROR - {exc}[/red]")

        except Exception as exc:
            console.print(f"[red]❌ [BROKER_PROBE] {request_id}: UNEXPECTED ERROR - {type(exc).__name__}: {exc}[/red]")
            import traceback
            traceback.print_exc()

        finally:
            # Always consume the request
            try:
                os.remove(request_path)
            except Exception:
                pass

        # Signal caller to skip this tick (probe takes priority)
        return True

    def _mts_tick(self, enriched_bar: dict | None = None):
        # [P1] Periodic far snapshot refresh (60s cooldown) - inline
        _now = time.time()
        if not hasattr(self, '_last_far_snap_ts') or _now - self._last_far_snap_ts > 60:
            try:
                if self.far_contract and self.api and not self.dry_run:
                    _s = self.api.snapshots([self.far_contract])
                    if _s and len(_s) > 0 and _s[0].close and float(_s[0].close) > 0:
                        _p = float(_s[0].close)
                        print(f"[FAR_SNAP] {self.far_contract.code} price={_p}", flush=True)
                        self.market_data[f'{self.ticker}_FAR']['close'] = _p
                        self._last_far_snap_ts = _now
            except Exception as e:
                print(f"[FAR_SNAP_ERR] {e}", flush=True)
        
        """MTS minimal execution path. Uses enriched bar from pipeline when available,
        falls back to building bar from tick deque if none provided."""
        print("MTS_ALIVE", flush=True)

        # [P1] observation-only: refresh persistent EXIT_ONLY_BBO_OBSERVED
        # evidence when the current capability has fresh valid dual BBO
        # (deduped by reconciliation_id + bbo_hash; never alters orders/
        # decisions; invalid/stale emits nothing).
        # [auto re-reconciliation] read-only renewal of the CURRENT
        # capability freshness (30s cadence, bounded backoff; failures
        # quarantine with a typed reason, zero orders).

        # ── Read-only broker snapshot probe (request file) ──
        if self._check_broker_snapshot_request():
            return

        _mts = self.cfg.get("mts", {})
        _strat_name = _mts.get("strategy", "tmf_spread")

        # 2026-07-15 Gemini CLI: Ensure strategy is initialized before recovery or OCO checks
        # to prevent AttributeError on uninitialized fields like _release_price during state writes.
        strategy = self._registry.get(_strat_name)
        if strategy is not None and not hasattr(strategy, "_has_position"):
            ctx = StrategyContext(
                market=MarketData(
                    last_bar={}, 
                    timestamp="",
                    ticker=self.ticker
                ),
                position=PositionView(size=self.trader.position), 
                config=_mts
            )
            strategy.init(ctx)

        # ── [ADR-010] OCO reconciliation: runs even when market closed ──
        # Must reconcile paper_fill_sim after restart before first market tick,
        # otherwise SUBMITTED OCO orders from previous session become orphans.
        # Reads state file directly because strategy lifecycle isn't restored
        # until on_bar() runs (which requires market open).
        if not getattr(self, "_oco_reconciled", False):
            self._reconcile_paper_oco_orders_from_state()
            self._oco_reconciled = True

        # ═══════════════════════════════════════════════════════════════
        # P0: Split-brain detection + auto-recovery
        # fills 有持倉但 state 說 FLAT → 嘗試 fills-led recovery
        # （不 freeze，避免系統永遠卡住）
        # ───────────────────────────────────────────────────────────────
        # state 有持倉但 fills 說 FLAT → reset to FLAT
        # ───────────────────────────────────────────────────────────────
        # 2026-07-16 修復: 移除舊版的直接 return (freeze)，
        # 改成嘗試 fills-led recovery。成功後寫入 lifecycle state
        # 使後續 tick 不再觸發 recovery。
        # ═══════════════════════════════════════════════════════════════
        _fills_open = self._mts_has_open_position_from_fills()
        _state_path = _mts_position_state_path()
        _state_has_pos = False
        _state_trade_id = None
        try:
            if _state_path.exists():
                _disk = json.loads(_state_path.read_text())
                _state_has_pos = bool(_disk.get("has_position", False))
                _state_trade_id = _disk.get("trade_id")
        except Exception:
            pass

        # A broker-attested EXIT_ONLY capability is the authority for the
        # reconciled position.  A stale/local fills ledger may still say
        # FLAT after restart; the legacy split-brain reset must not erase
        # that hydrated position before the shared EXIT_ONLY validator runs.
        # PAPER/LIVE retain the legacy reconciliation behavior.
        _exit_only_authority_active = (
            getattr(getattr(self, "_execution_context", None),
                    "effective_mode", "") == "reconciled_exit_only"
            and isinstance(
                getattr(getattr(self, "_execution_context", None),
                        "exit_only_capability", None), dict)
        )

        # LIVE broker truth is authoritative.  Never resurrect a local
        # position from historical fills in live mode — the recovery path is
        # PAPER-only.  Gate on the execution context's requested_mode (the
        # live INTENT, set at ctx creation): the config's live_trading key is
        # absent in futures_live.yaml (monitor defaults to False), and
        # effective_mode is not yet "live_ready" at startup (the certificate
        # transition happens later) — either signal alone let the fills-led
        # recovery resurrect a ghost (reason=fills_recovery observed).
        _live_authority_runtime = bool(
            getattr(getattr(self, "_execution_context", None),
                    "requested_mode", "") == "live")
        if _fills_open and not _state_has_pos and not _live_authority_runtime:
            # Split-brain: fills says open, state says closed.
            # Try fills-led recovery via strategy's _restore_from_fills_log
            console.print(
                f"[bold yellow]🚨 [MTS_SPLIT_BRAIN] fills_has_open={_fills_open} "
                f"state_has_pos={_state_has_pos} — attempting fills-led recovery...[/bold yellow]"
            )
            strategy = self._registry.get(self.cfg.get("mts", {}).get("strategy", "tmf_spread"))
            if strategy and hasattr(strategy, '_restore_from_fills_log'):
                if strategy._restore_from_fills_log():
                    strategy._mts_recovery_state = "RECOVERED"
                    strategy._mts_state_write_enabled = True
                    # Persist lifecycle state so next tick doesn't re-trigger recovery
                    if hasattr(strategy, 'write_state'):
                        strategy.write_state(
                            action=str(getattr(strategy, '_lifecycle', 'OPEN')),
                            reason='fills_recovery',
                        )
                    console.print("[bold green]✅ [MTS_RECOVERY] Fills-led recovery succeeded![/bold green]")
                    _state_has_pos = True
                else:
                    console.print(
                        f"[bold red]🚨 [MTS_SPLIT_BRAIN] Fills recovery failed — "
                        f"emergency flatten still available[/bold red]"
                    )
        elif _state_has_pos and not _fills_open \
                and not _exit_only_authority_active:
            console.print(
                f"[bold yellow]⚠️ [MTS_SPLIT_BRAIN] State says POSITION but fills says closed. "
                f"Resetting to FLAT.[/bold yellow]"
            )
            strategy = self._registry.get(self.cfg.get("mts", {}).get("strategy", "tmf_spread"))
            if strategy:
                if hasattr(strategy, '_mts_recovery_state'):
                    strategy._mts_recovery_state = "FLAT_CONFIRMED"
                # 2026-07-21 Gemini CLI: Reset strategy state and trigger reentry cooldown to prevent immediate trading after split-brain recovery
                if hasattr(strategy, '_reset'):
                    strategy._reset(reason="MTS_SPLIT_BRAIN_RESET")
            _state_has_pos = False
            _fills_open = False

        # 1. Market hours check
        if not is_taifex_futures_market_open():
            return

        # 2. Get bar
        _bar_dict = enriched_bar
        _df_5m = None
        if _bar_dict is None:
            _df_5m = self._get_tick_bars_df()
            if _df_5m is None or _df_5m.empty: return
            last_5m = _df_5m.iloc[-1]
            _bar_dict = last_5m.to_dict()
            _bar_dict["ts"] = last_5m.name if hasattr(last_5m, "name") else None

        if not _bar_dict: return
        if hasattr(self, '_spread_loader') and self._spread_loaded:
            try: self._spread_loader.enrich_bar(_bar_dict)
            except: pass
            
        # 2026-05-27 Gemini CLI: Override CSV with real-time prices for tick-level MTS management
        if "near_close_rt" in _bar_dict:
            _bar_dict["near_close"] = _bar_dict["near_close_rt"]
            _bar_dict["near_high"] = _bar_dict.get("near_high_rt", _bar_dict["near_close"])
            _bar_dict["near_low"] = _bar_dict.get("near_low_rt", _bar_dict["near_close"])
        if "far_close_rt" in _bar_dict:
            _bar_dict["far_close"] = _bar_dict["far_close_rt"]
            _bar_dict["far_high"] = _bar_dict.get("far_high_rt", _bar_dict["far_close"])
            _bar_dict["far_low"] = _bar_dict.get("far_low_rt", _bar_dict["far_close"])

        # 💡 [Fixed 2026-05-27] Dynamic Real-Time Spread Z Calculation
        # The background CSV job runs only 3 times a day. To trade between cron jobs,
        # we calculate spread_z dynamically using RT prices and the latest available MA/STD.
        if _bar_dict.get("near_close", 0) > 0 and _bar_dict.get("far_close", 0) > 0:
            _spread_ma = _bar_dict.get("spread_ma", 0.0)
            _spread_std = _bar_dict.get("spread_std", 0.0)
            if _spread_std > 0:
                _rt_spread = _bar_dict["near_close"] - _bar_dict["far_close"]
                _bar_dict["spread_z"] = (_rt_spread - _spread_ma) / _spread_std

        # Research wiring: dz / spread_slope / velocity_ema into the bar
        # (entry_observation payload reads these; previously always NULL).
        self._apply_spread_dynamics(_bar_dict)

        # 2026-07-08 Hermes Agent: Pass sqz_on to bar dict for BB filter gate.
        # Read from squeeze indicator pipeline (_last_processed_data["5m"]).
        # If sqz_on=False, BB filter is skipped entirely (squeeze not active).
        _bar_dict["sqz_on"] = False
        if hasattr(self, "_last_processed_data") and self._last_processed_data:
            _df5 = self._last_processed_data.get("5m")
            if _df5 is not None and not _df5.empty and "sqz_on" in _df5.columns:
                _bar_dict["sqz_on"] = bool(_df5["sqz_on"].iloc[-1])

        # 2026-07-08 Hermes Agent: Compute per-leg VWAP for trail tightening (_apply_vwap_exit).
        # near_vwap from near tick bars, far_vwap from far tick bars.
        # Guard: VWAP=None on any failure → _apply_vwap_exit gracefully no-ops.
        _near_vwap = None
        _far_vwap = None
        try:
            _df_near_all = self._get_tick_bars_df()
            if _df_near_all is not None and not _df_near_all.empty:
                _near_cum_vol = _df_near_all["Volume"].sum()
                if _near_cum_vol > 0:
                    _near_vwap = float((_df_near_all["Close"] * _df_near_all["Volume"]).sum() / _near_cum_vol)
        except Exception:
            pass
        try:
            _df_far_all = self.get_far_tick_bars_df()
            if _df_far_all is not None and not _df_far_all.empty:
                _far_cum_vol = _df_far_all["Volume"].sum()
                if _far_cum_vol > 0:
                    _far_vwap = float((_df_far_all["Close"] * _df_far_all["Volume"]).sum() / _far_cum_vol)
        except Exception:
            pass
        _bar_dict["near_vwap"] = _near_vwap
        _bar_dict["far_vwap"] = _far_vwap

        # 2026-07-08 Hermes Agent: Compute BB bands for near/far release filter.
        # Only computed when BB filter is enabled AND sqz_on is active.
        _mts_params = _mts.get("params", {})
        _bb_cfg = _mts_params.get("release_filter", {})
        if _bb_cfg.get("bb_enabled", False) and _bar_dict.get("sqz_on", False):
            try:
                _bb_period = int(_bb_cfg.get("bb_period", 20))
                _bb_std = float(_bb_cfg.get("bb_std_mult", 2.0))
                _df_near = self._get_tick_bars_df()
                _df_far = self.get_far_tick_bars_df()
                if _df_near is not None and not _df_near.empty and len(_df_near) >= _bb_period:
                    _near_close = _df_near["Close"].rolling(_bb_period)
                    _near_mid = _near_close.mean().iloc[-1]
                    _near_std = _near_close.std().iloc[-1]
                    _bar_dict["near_bb_mid"] = float(_near_mid)
                    _bar_dict["near_bb_upper"] = float(_near_mid + _bb_std * _near_std)
                    _bar_dict["near_bb_lower"] = float(_near_mid - _bb_std * _near_std)
                if _df_far is not None and not _df_far.empty and len(_df_far) >= _bb_period:
                    _far_close = _df_far["Close"].rolling(_bb_period)
                    _far_mid = _far_close.mean().iloc[-1]
                    _far_std = _far_close.std().iloc[-1]
                    _bar_dict["far_bb_mid"] = float(_far_mid)
                    _bar_dict["far_bb_upper"] = float(_far_mid + _bb_std * _far_std)
                    _bar_dict["far_bb_lower"] = float(_far_mid - _bb_std * _far_std)
            except Exception:
                pass  # BB unavailable → strategy will bypass filter
        _n_close = float(_bar_dict.get("near_close") or 0)
        _f_close = float(_bar_dict.get("far_close") or 0)
        # [TEMP-DEBUG-20260731] pollution source identification
        if _n_close > 0 and hasattr(self, "_dbg_last_near") and abs(_n_close - self._dbg_last_near) > 50:
            print("[POLLUTE_DEBUG] near=%.1f prev=%.1f near_close_rt=%s near_high_rt=%s near_low_rt=%s far_close_rt=%s ts=%s keys=%s"
                  % (_n_close, self._dbg_last_near,
                     _bar_dict.get("near_close_rt"), _bar_dict.get("near_high_rt"),
                     _bar_dict.get("near_low_rt"), _bar_dict.get("far_close_rt"),
                     _bar_dict.get("ts"),
                     [k for k in ("near_close_rt", "near_high_rt", "near_low_rt", "far_close_rt", "near_close") if k in _bar_dict]), flush=True)
        self._dbg_last_near = _n_close

        # 2026-07-14 Gemini CLI: Inject MTF snapshot into the bar dictionary for ADR-009 Phase 1
        self._inject_mtf_snapshot(_bar_dict)

        # 3. Strategy setup
        strategy = self._registry.get(_strat_name)
        if strategy is None:
            console.print(f"[red][MTS] Strategy {_strat_name} not registered[/red]")
            return

        # 2026-07-01 Gemini CLI: Ensure strategy is initialized before heartbeat to prevent AttributeError on attributes like _last_atr
        ctx = StrategyContext(
            market=MarketData(
                last_bar=_bar_dict, 
                timestamp=_bar_dict.get("ts", ""),
                ticker=self.ticker
            ),
            position=PositionView(size=self.trader.position), 
            config=_mts
        )
        if not hasattr(strategy, "_has_position"):
            strategy.init(ctx)

        # ═══════════════════════════════════════════════════════════════
        # P0: MTS Heartbeat — TELEMETRY ONLY
        # 2026-07-16 修復: heartbeat 不再呼叫 strategy.write_state()。
        # 改用 _write_mts_telemetry()，只更新價格/UPL/報價時效，
        # 完全不碰 has_position / lifecycle / trade_id / entry_price。
        # 即使記憶體 _has_position=None (INITIALIZING)，也無法洗掉持倉。
        # ───── Guard 邏輯 ─────
        # 1. _mts_recovery_state 非 RECOVERED/FLAT_CONFIRMED → 只寫 telemetry
        # 2. _has_position=None 但磁碟有持倉 → 只寫 telemetry (不碰 lifecycle)
        # 3. 正常狀態 → 價格/UPL 更新 (telemetry only)
        # ───────────────────────────────────────────────────────────────
        _hb_path = _mts_position_state_path()
        try:
            _has_pos_in_mem = getattr(strategy, "_has_position", None)
            _recovery_state = getattr(strategy, "_mts_recovery_state", None)
            existing = {}
            if _hb_path.exists():
                try:
                    existing = json.loads(_hb_path.read_text())
                except: pass

            # P0: Recovery state guard — no lifecycle writes until RECOVERED or FLAT_CONFIRMED
            # 2026-07-15 Gemini CLI: Calculate fallback entry prices and sides from disk state
            # to calculate UPL even when in-memory state is initializing/unknown.
            _n_entry = getattr(strategy, "_near_entry", 0) or 0
            _f_entry = getattr(strategy, "_far_entry", 0) or 0
            _n_side = getattr(strategy, "_near_side", None)
            _f_side = getattr(strategy, "_far_side", None)
            
            if not _n_entry: _n_entry = existing.get("near_entry", 0) or 0
            if not _f_entry: _f_entry = existing.get("far_entry", 0) or 0
            if not _n_side: _n_side = existing.get("near_side")
            if not _f_side: _f_side = existing.get("far_side")
            
            _n_last = float(_bar_dict.get('near_close') or 0.0)
            _f_last = float(_bar_dict.get('far_close') or 0.0)
            
            # Fall back to last known prices on disk if current prices are missing/0
            _n_last_calc = _n_last if _n_last > 0 else (existing.get("near_last", 0) or 0)
            _f_last_calc = _f_last if _f_last > 0 else (existing.get("far_last", 0) or 0)
            
            _mult = float(get_point_value(self.ticker))
            _rel_leg = getattr(strategy, "_released_leg", None) or existing.get("released_leg")
            
            _n_upl = 0.0
            _f_upl = 0.0
            
            _has_pos_eval = (_has_pos_in_mem is True) or (_has_pos_in_mem is None and existing.get("has_position") is True)
            if _has_pos_eval:
                if _n_entry > 0 and _n_last_calc > 0 and _n_side and _rel_leg != "near":
                    _n_pts = (_n_last_calc - _n_entry) * (-1 if _n_side == "SHORT" else 1)
                    _n_upl = _n_pts * _mult
                if _f_entry > 0 and _f_last_calc > 0 and _f_side and _rel_leg != "far":
                    _f_pts = (_f_last_calc - _f_entry) * (-1 if _f_side == "SHORT" else 1)
                    _f_upl = _f_pts * _mult

            # 2026-07-25 Gemini CLI: Typed Enum comparison for RecoveryState — eliminate string guessing
            from strategies.plugins.futures.active.tmf_spread import RecoveryState
            _recovery_state = getattr(strategy, "_mts_recovery_state", None)
            _is_active_state = False
            if _recovery_state is None:
                _is_active_state = True
            elif isinstance(_recovery_state, RecoveryState):
                _is_active_state = _recovery_state in (RecoveryState.RECOVERED, RecoveryState.FLAT_CONFIRMED)
            elif isinstance(_recovery_state, str):
                _is_active_state = _recovery_state.upper() in ("RECOVERED", "FLAT_CONFIRMED")
            elif hasattr(_recovery_state, "name"):
                _is_active_state = _recovery_state.name.upper() in ("RECOVERED", "FLAT_CONFIRMED")

            if not _is_active_state:
                # Allow telemetry (prices, UPL) but don't write lifecycle
                try:
                    from strategies.plugins.futures.active.tmf_spread import _write_mts_telemetry as _hb_telemetry
                    _hb_telemetry(
                        near_last=_n_last, far_last=_f_last,
                        near_upl=_n_upl, far_upl=_f_upl, total_upl=_n_upl + _f_upl,
                        quote_age_ms=_bar_dict.get("quote_age_ms", 0),
                        spread_z=_bar_dict.get("spread_z", 0),
                    )
                except Exception:
                    pass
                console.print(f"[dim][MTS] Heartbeat: recovery_state={_recovery_state} — telemetry only[/dim]")
            elif _has_pos_in_mem is None and existing.get("has_position") is True:
                # UNKNOWN memory but POSITION on disk — suppress lifecycle write
                console.print("[dim][MTS] Heartbeat suppressed: _has_position is UNKNOWN, disk says POSITION[/dim]")
                # Still write telemetry
                try:
                    from strategies.plugins.futures.active.tmf_spread import _write_mts_telemetry as _hb_telemetry
                    _hb_telemetry(
                        near_last=_n_last, far_last=_f_last,
                        near_upl=_n_upl, far_upl=_f_upl, total_upl=_n_upl + _f_upl,
                        quote_age_ms=_bar_dict.get("quote_age_ms", 0),
                        spread_z=_bar_dict.get("spread_z", 0),
                    )
                except Exception:
                    pass
            else:
                # Normal heartbeat: only telemetry — never lifecycle
                try:
                    from strategies.plugins.futures.active.tmf_spread import _write_mts_telemetry as _hb_telemetry
                    _hb_telemetry(
                        near_last=_n_last, far_last=_f_last,
                        near_upl=_n_upl, far_upl=_f_upl, total_upl=_n_upl + _f_upl,
                        quote_age_ms=_bar_dict.get("quote_age_ms", 0),
                        spread_z=_bar_dict.get("spread_z", 0),
                    )
                except Exception:
                    pass

        # 2026-05-22 Gemini CLI: Fixed except block indentation to resolve syntax error
        except Exception as e:
            console.print(f"[red]⚠️ Heartbeat failed: {e}[/red]")

        # ADR-009 Task 10: broker position reconciliation.
        # If local lifecycle says FLAT but broker/trader has open spread position,
        # reconstruct lifecycle from broker state to prevent split-brain.
        # Guard: only reconcile when local FLAT + broker has position + strategy is MTS-capable.
        _broker_pos = getattr(self.trader, "position", 0)
        _has_pos = bool(getattr(strategy, "_has_position", False))
        _lc = getattr(strategy, "_lifecycle_oca", None)
        
        # 2026-07-21 Gemini CLI: Prevent broker position reconciliation race condition immediately after exit
        _last_exit = getattr(strategy, "_last_exit_ts", None)
        _in_cooldown = False
        if _last_exit:
            if isinstance(_last_exit, datetime) and (datetime.now() - _last_exit).total_seconds() < 10:
                _in_cooldown = True

        if (
            not _has_pos
            and _broker_pos != 0
            and not _in_cooldown
            and _lc is not None
            and hasattr(_lc, 'phase')
            and str(_lc.phase.value) == "FLAT"
            and getattr(strategy, "_ticker", "").startswith("TMF")
            # 2026-07-22 Gemini CLI: Only recover if the local fills ledger indicates there is an active trade
            and self._mts_has_open_position_from_fills()
        ):
            from strategies.plugins.futures.active.tmf_spread import (
                PositionPhase, infer_lifecycle_from_legacy_state,
                _write_mts_state, lifecycle_to_dict,
            )
            # 2026-07-21 Gemini CLI: Load entry prices, sides, trade_id from the state file on disk
            # to prevent entry prices from remaining 0.0 in memory after recovery.
            _disk = {}
            _state_path = _mts_position_state_path()
            if _state_path.exists():
                try:
                    _disk = json.loads(_state_path.read_text())
                except Exception:
                    pass

            strategy._near_entry = float(_disk.get("near_entry") or getattr(strategy, "_near_entry", 0) or 0.0)
            strategy._far_entry = float(_disk.get("far_entry") or getattr(strategy, "_far_entry", 0) or 0.0)
            # 2026-08-06 Hermes Agent P1: sides must be authoritative — the
            # reconcile previously copied whatever the state file held (once
            # leg labels "NEAR"/"FAR"), and close_all's `else BUY` mapping then
            # sent wrong-direction orders. Near derives from the broker
            # position; far accepts only valid LONG/SHORT (else None, and
            # close_all fails closed instead of sending a wrong order).
            _near_side = _disk.get("near_side") or getattr(strategy, "_near_side", None)
            _far_side = _disk.get("far_side") or getattr(strategy, "_far_side", None)
            if _broker_pos > 0:
                _near_side = "LONG"
            elif _broker_pos < 0:
                _near_side = "SHORT"
            else:
                _near_side = None
            if _far_side not in ("LONG", "SHORT"):
                console.print(
                    f"[red]⚠️ [BROKER_RECONCILED] invalid far_side={_far_side!r} → None; "
                    f"close_all will fail-closed[/red]"
                )
                _far_side = None
            strategy._near_side = _near_side
            strategy._far_side = _far_side
            strategy._released_leg = _disk.get("released_leg") or getattr(strategy, "_released_leg", None)
            strategy._trade_id = _disk.get("trade_id") or getattr(strategy, "_trade_id", None)

            strategy._has_position = True
            strategy._lifecycle = "RECOVERED_BROKER"
            _legacy_hint = {
                "has_position": True,
                "released_leg": None,
                "release_state": "BOTH_HELD",
            }
            strategy._lifecycle_oca = infer_lifecycle_from_legacy_state(_legacy_hint)
            _write_mts_state(
                has_position=True, action="BROKER_RECONCILED",
                reason="broker_position_recovery",
                near_entry=strategy._near_entry,
                far_entry=strategy._far_entry,
                near_side=strategy._near_side,
                far_side=strategy._far_side,
                released_leg=strategy._released_leg,
                trade_id=strategy._trade_id,
                ticker=self.ticker,
                atr=0.0,
                lifecycle=lifecycle_to_dict(strategy._lifecycle_oca),
            )
            console.print(f"[bold yellow]♻️ [BROKER_RECONCILED] broker_pos={_broker_pos} → lifecycle restored to {strategy._lifecycle_oca.phase.value}[/bold yellow]")

        # ADR-010 Sprint 4C: CANCELING_SIBLING → SIBLING_CANCELED → SINGLE_LEG + trail ARMED
        # Paper mode: cancel is sync-confirmed, transition immediately on next tick.
        if (
            _lc is not None
            and hasattr(_lc, 'phase')
            and hasattr(_lc, 'release_group')
            and _lc.phase.value == "SPREAD"
            and _lc.release_group.status.value == "CANCELING_SIBLING"
        ):
            if _lc.release_group.sibling_cancel_status is not None \
               and _lc.release_group.sibling_cancel_status.value == "PENDING":
                # Paper: broker cancel already succeeded; promote CONFIRMED
                from strategies.plugins.futures.active.tmf_spread import (
                    CancelStatus, ReleaseGroupStatus, PositionPhase, TrailGroupStatus,
                    _write_mts_state, lifecycle_to_dict,
                )
                _lc.release_group.sibling_cancel_status = CancelStatus.CONFIRMED
                _lc.release_group.status = ReleaseGroupStatus.SIBLING_CANCELED
                _lc.phase = PositionPhase.SINGLE_LEG
                _lc.trail_group.status = TrailGroupStatus.ARMED
                _released = str(_lc.release_group.filled_leg.value) if _lc.release_group.filled_leg else None
                _write_mts_state(
                    has_position=True, action="OCO_SIBLING_CANCELED",
                    reason=f"oco_sibling_canceled_winner={_released}",
                    near_entry=getattr(strategy, "_near_entry", 0) or 0,
                    far_entry=getattr(strategy, "_far_entry", 0) or 0,
                    near_last=getattr(strategy, "_near_last", 0) or 0,
                    far_last=getattr(strategy, "_far_last", 0) or 0,
                    near_side=getattr(strategy, "_near_side", None),
                    far_side=getattr(strategy, "_far_side", None),
                    released_leg=_released,
                    trade_id=getattr(strategy, "_trade_id", None),
                    ticker=self.ticker,
                    lifecycle=lifecycle_to_dict(_lc),
                )
                console.print(
                    f"[bold green]✅ [OCO_4C] CANCELING_SIBLING → SIBLING_CANCELED → SINGLE_LEG/{_released} → trail ARMED[/bold green]"
                )

        # 2026-07-07 Hermes Agent: P0 — Position authority gate.
        # Strategy runtime flags (_has_position, _released_leg) can desync
        # from the state file after PM2 restart, clear_records, or emergency
        # flatten.  If the persistent authority says FLAT but the strategy
        # still believes it holds a position, force-sync and block any
        # EXIT/RELEASE signals for this tick.
        _state_path = _mts_position_state_path()
        _authority_has_pos = False
        _lc_authority = None
        try:
            if _state_path.exists():
                _disk = json.loads(_state_path.read_text())
                # 💡 Gemini CLI: State identity validation — fail-closed on mismatch (block entry, preserve position, require reconciliation)
                _disk_ticker = _disk.get("ticker")
                if _disk_ticker and str(_disk_ticker).upper() != str(self.ticker).upper():
                    console.print(
                        f"[bold red]⛔ [POSITION_AUTHORITY_MISMATCH] State file ticker ({_disk_ticker}) "
                        f"mismatches monitor ticker ({self.ticker}) — fail-closed: blocking new entries[/bold red]"
                    )
                    self._mts_entry_blocked = True
                    self._mts_reconciliation_pending = True
                    _authority_has_pos = True  # Prevent erroneous force-reset of in-memory position
                else:
                    _authority_has_pos = bool(_disk.get("has_position", False))
                    _lc_authority = _disk.get("lifecycle", {})
        except Exception:
            pass

        _strat_has_pos = bool(getattr(strategy, "_has_position", False))
        _strat_tid = getattr(strategy, "_trade_id", None)
        # 2026-08-06 codex v2 audit: capture the PREVIOUS completed tick's age
        # BEFORE stamping this tick, so a whole-loop stall is observed on the
        # first tick after recovery (same-thread checkers cannot fire during
        # an active C-level/API/I/O hang).
        self._mts_stamp_tick_loop()

        # 2026-08-06 Hermes Agent P1: three-state LEDGER authority drives both
        # the pre-signal reset and the post-signal exit gate. The state file
        # lags the ledger during multi-leg fills — a plain bool read of it
        # forced a strategy._reset() on a freshly entered position (incident
        # 2026-08-06 o-091403-145, 2h unmanaged). See
        # strategies/futures/mts_ledger_authority.py for the pure decisions.
        if time.monotonic() - self._ledger_projection_sync_ts > 2.0:
            self._ledger_projection.sync_from_ledger()
            self._ledger_projection_sync_ts = time.monotonic()
        _auth = self._ledger_projection.snapshot()
        # LIVE broker truth supersedes a silent/missing callback ledger.  The
        # helper is read-only and leaves PAPER and legacy EXIT_ONLY untouched.
        from strategies.futures.mts_ledger_authority import MtsAuthority
        _broker_auth = self._refresh_live_broker_authority(strategy)
        if _broker_auth is not None:
            _auth = _broker_auth
            _authority_has_pos = (_broker_auth.status in (
                MtsAuthority.OPEN, MtsAuthority.SINGLE_LEG))
            _state_has_pos = _authority_has_pos
            # The strategy's own restore hook runs inside on_bar, before the
            # next monitor gate.  Carry the current broker-flat proof into
            # that hook so historical fills cannot resurrect a ghost.
            strategy._broker_truth_flat = not _authority_has_pos
        # [S1 repair] ONE shared EXIT_ONLY capability validation at tick
        # start — before ANY risk gate / strategy evaluation.  The
        # previous tick's authority override is cleared here; when valid,
        # the capability position IS the authority for the pre/post
        # signal gates.  Invalid/stale/missing => risk gates, evaluator
        # and submit all blocked (zero), with one typed blocked event.
        _exit_ok, _exit_position, _exit_reason = \
            self._validate_exit_only_position()
        if _exit_ok and _exit_position is not None:
            self._exit_only_auth_override = \
                self._build_exit_only_authority(_exit_position)
        _exit_override = getattr(self, "_exit_only_auth_override", None)
        _gate_auth = _exit_override if _exit_override is not None else _auth
        _gate_state_has_pos = (_authority_has_pos
                               if _exit_override is None else True)
        _pre_action = gate_decision_pre_signal(
            _gate_auth, _gate_state_has_pos, _strat_has_pos, _strat_tid,
        )
        if _pre_action == MtsGateAction.RESET_STRATEGY:
            console.print(
                f"[bold yellow]⚠️ [POSITION_AUTHORITY] Ledger FLAT + memory open "
                f"(trade={_strat_tid}) — force-syncing strategy to FLAT[/bold yellow]"
            )
            strategy._reset(reason="POSITION_AUTHORITY_FLAT")
        elif _pre_action == MtsGateAction.RECONSTRUCT:
            self._reconstruct_position_from_ledger(strategy, _auth)

        # 2026-08-06 codex audit: evaluator-lag SLO — runs every tick so a
        # stalled evaluator is caught even when the gates skip on_bar.
        self._mts_check_evaluator_lag(strategy, _strat_has_pos)

        if _exit_ok:
            self._record_mts_entry_research_candidate(strategy, _bar_dict, datetime.now())

        signal = None
        if _pre_action != MtsGateAction.RESET_STRATEGY and _exit_ok:
            # 2026-07-08 Hermes Agent: Risk control gates (settlement > SINGLE_LEG > normal)
            if not self._mts_risk_gate_settlement(strategy):
                if not self._mts_risk_gate_single_leg_preclose(strategy, _bar_dict):
                    # [S1] EXIT_ONLY: hydrate the strategy position from the
                    # validated capability BEFORE every evaluation (never
                    # inside submit).  On any missing/stale/session/leg-
                    # mismatch/hydration failure an explicit
                    # EXIT_ONLY_HYDRATION_BLOCKED event fires and the
                    # evaluator is skipped — zero order submission.
                    if self._exit_only_pre_evaluation_hydration(strategy):
                        signal = strategy.on_bar(ctx)
                        self._mts_note_strategy_evaluated()

        # 2026-07-07 Hermes Agent: P0 — Post-signal authority gate.
        # 2026-08-06 Hermes Agent P1: driven by the LEDGER authority (not the
        # state file), so a lagging state file can no longer suppress a real
        # exit. Block only when the ledger itself says FLAT; UNKNOWN stays
        # fail-open for exits.
        if signal is not None:
            _sig_action = getattr(signal, "action", "?")
            _post_override = getattr(self, "_exit_only_auth_override", None)
            _post_action = gate_decision_post_signal(
                _post_override if _post_override is not None else _auth,
                _sig_action)
            if _post_action == MtsGateAction.BLOCK_SIGNAL:
                console.print(
                    f"[bold red]⛔ [MTS_ORDER_REJECT] Ledger FLAT; "
                    f"blocking signal={_sig_action} reason={getattr(signal, 'reason', '?')}[/bold red]"
                )
                signal = None

        # [Fix 2026-07-06] Narrow guard: flush release OCO orders to orders JSON
        # after PM2 restart. Only fires once when SUBMITTED release_group with
        # both order ids is detected, preventing stale/partial state pollution.
        if getattr(self, "_mts_release_orders_flushed", False) is False:
            _rg = getattr(strategy, "_lifecycle_oca", None)
            if _rg is not None:
                _rg_rel = getattr(_rg, "release_group", None)
                _rg_phase = getattr(_rg, "phase", None)
                _rg_status = getattr(_rg_rel, "status", None) if _rg_rel else None
                _rg_near = getattr(_rg_rel, "near_order_id", None) if _rg_rel else None
                _rg_far = getattr(_rg_rel, "far_order_id", None) if _rg_rel else None
                if (
                    str(getattr(_rg_phase, "value", "")) == "SPREAD"
                    and str(getattr(_rg_status, "value", "")) == "SUBMITTED"
                    and _rg_near
                    and _rg_far
                ):
                    # 2026-07-07 Hermes Agent: Re-register OCO orders BEFORE saving
                    # orders file, so get_pending() returns them and the duplicate
                    # guard in _save_orders_file_wrapper prevents ghost injection.
                    # _save_orders_file_wrapper() returns the set of OCO release
                    # order IDs it persisted via lifecycle fallbacks.
                    self._reconcile_paper_oco_orders(strategy)
                    _persisted = self._save_orders_file_wrapper()
                    if _persisted:
                        self._mts_release_orders_flushed = True

        # ── [ADR-010] Poll paper OCO fills with live prices (every tick) ──
        _n_close = float(_bar_dict.get("near_close") or 0)
        _f_close = float(_bar_dict.get("far_close") or 0)
        if _n_close > 0 and _f_close > 0:
            self._process_pending_paper_fills(
                near_price=_n_close, far_price=_f_close, ts=datetime.now(),
            )

        if signal:
            self._emit_release_telemetry(signal, strategy, _bar_dict)
            self._submit_mts_order_signal(signal, strategy, _bar_dict, datetime.now())
            # 💡 [Fixed 2026-05-27] Removed premature strategy._reset(). 
            # Reset now happens in _apply_confirmed_futures_deal upon fill to prevent runaway re-entry loops.


    # ═══════════════════════════════════════════════════════════════
    # 2026-07-08 Hermes Agent: MTS risk control gates
    # ═══════════════════════════════════════════════════════════════

    def _mts_risk_gate_settlement(self, strategy) -> bool:
        """Settlement day force flat: all phases, full close on the contract's last trading day after 13:30.

        Only triggers when today IS the delivery date AND past 13:30.
        Does NOT trigger on already-expired contracts (delivery in the past).
        Idempotent: fires at most once per session.

        Returns True if gate triggered (position force-closed).
        """
        if not self.contract:
            return False

        # Only trigger on the ACTUAL settlement day, not on long-expired contracts
        try:
            from datetime import datetime as _dt
            _delivery = _dt.strptime(self.contract.delivery_date, "%Y/%m/%d").date()
            _today = _dt.now().date()
            if _delivery != _today:
                return False  # not today's settlement
        except Exception:
            return False

        if not self._is_contract_expired(self.contract.delivery_date):
            return False  # before 13:30

        # Idempotency guard: only fire once
        if getattr(self, '_mts_settlement_flat_done', False):
            return True

        _has_pos = bool(getattr(strategy, '_has_position', False))
        if not _has_pos:
            return False

        self._mts_settlement_flat_done = True
        self._mts_force_exit_inflight = True

        console.print(
            f"[bold red]📅 [RISK_SETTLEMENT] Contract {self.contract.code} "
            f"expired — force closing all positions[/bold red]"
        )
        # Emergency flatten: close both legs if still held
        self._emergency_flatten_mts(strategy)
        return True

    def _mts_risk_gate_single_leg_preclose(self, strategy, bar_dict) -> bool:
        """SINGLE_LEG pre-close force flat: exit remaining leg within 5 min of session close.

        Returns True if gate triggered (remaining leg exit submitted).
        """
        from core.date_utils import _minutes_to_session_close
        from strategies.plugins.futures.active.tmf_spread import PositionPhase

        # Check phase
        _lc = getattr(strategy, '_lifecycle_oca', None)
        if _lc is None:
            return False
        _phase = getattr(_lc, 'phase', None)
        _phase_val = _phase.value if hasattr(_phase, 'value') else str(_phase)
        if _phase_val != "SINGLE_LEG":
            return False

        # Check time
        _mins = _minutes_to_session_close()
        if _mins is None or _mins > 5:
            return False

        # Idempotency guard
        _inflight = getattr(self, '_mts_force_exit_inflight', False)
        if _inflight:
            return True  # already submitted, skip on_bar

        # Don't double-fire if trail exit already in progress
        _is_exiting = getattr(strategy, '_lifecycle', '') == 'EXITING'
        if _is_exiting:
            return False

        # Don't fire if no remaining leg info
        if getattr(strategy, '_released_leg', None) is None:
            return False
        if getattr(strategy, '_side', None) is None:
            return False

        self._mts_force_exit_inflight = True
        console.print(
            f"[bold red]⏰ [RISK_PRECLOSE] SINGLE_LEG force close: "
            f"{_mins:.1f}min to session close, remaining={getattr(strategy, '_side', '?')}[/bold red]"
        )
        signal = Signal("EXIT", "SESSION_CLOSE_FORCE", confidence=1.0, stop_loss=0)
        self._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        return True

    # ═══════════════════════════════════════════════════════════════
    # 2026-07-08 Hermes Agent: P0 — Multi-source open position detection
    # ═══════════════════════════════════════════════════════════════

    def _mts_has_open_position_from_fills(self) -> bool:
        """Three-state ledger authority (incremental projection, 2026-08-06 P1).

        The old implementation re-scanned the whole fills JSONL on every call
        (a per-tick full scan). The projection is maintained incrementally
        (tail-read of new bytes only); this is now a thin snapshot read.
        """
        self._ledger_projection.sync_from_ledger()
        return self._ledger_projection.snapshot().status == MtsAuthority.OPEN

    def _reconstruct_position_from_ledger(self, strategy, auth):
        """Rebuild the strategy position state from the ledger authority and
        write it back to the state file (2026-08-06 P1).

        Called when the state file lags the ledger (or the strategy holds a
        stale trade_id): the position is reconstructed to the LEDGER truth —
        correct side / qty / lifecycle — then persisted. This is not a
        skipped reset; it is an authoritative rebuild.
        """
        from strategies.futures.mts_ledger_authority import MtsAuthority
        strategy._has_position = True
        strategy._trade_id = auth.trade_id
        strategy._near_side = auth.near_side
        strategy._far_side = auth.far_side
        strategy._near_qty = abs(auth.near_qty)
        strategy._far_qty = abs(auth.far_qty)
        strategy._near_entry = auth.near_entry
        strategy._far_entry = auth.far_entry
        _single_leg = getattr(auth, "status", None) is MtsAuthority.SINGLE_LEG
        strategy._released_leg = (
            "near" if _single_leg and auth.far_qty else
            "far" if _single_leg and auth.near_qty else None
        )
        console.print(
            f"[bold yellow]♻️ [POSITION_AUTHORITY] Ledger reconstruct: "
            f"trade={auth.trade_id} near={auth.near_side}x{abs(auth.near_qty)} "
            f"far={auth.far_side}x{abs(auth.far_qty)} — writing back state[/bold yellow]"
        )
        try:
            from strategies.plugins.futures.active.tmf_spread import (
                _write_mts_state,
                infer_lifecycle_from_legacy_state,
            )
            strategy._lifecycle_oca = infer_lifecycle_from_legacy_state(
                {"has_position": True,
                 "released_leg": strategy._released_leg,
                 "release_state": (
                     "NEAR_RELEASED" if strategy._released_leg == "near"
                     else "FAR_RELEASED" if strategy._released_leg == "far"
                     else "BOTH_HELD")}
            )
            strategy._lifecycle = (
                "SINGLE_LEG" if _single_leg else "RECOVERED_LEDGER")
            _write_mts_state(
                has_position=True, action="LEDGER_RECONSTRUCTED",
                reason="authority_rebuild",
                near_entry=auth.near_entry,
                far_entry=auth.far_entry,
                near_side=auth.near_side,
                far_side=auth.far_side,
                released_leg=strategy._released_leg,
                trade_id=auth.trade_id,
                ticker=self.ticker,
                atr=0.0,
                lifecycle={"phase": "SINGLE_LEG" if _single_leg else "SPREAD",
                           "release_group": {"status": "INACTIVE"},
                           "trail_group": {"status": "INACTIVE"}},
            )
        except Exception as _e:
            import logging
            logging.getLogger("FuturesMonitor").warning(
                "[POSITION_AUTHORITY] state write-back failed: %s", _e
            )

    def _mts_note_strategy_evaluated(self) -> None:
        """Record the strategy evaluator heartbeat (evaluator-lag SLO)."""
        self._last_strategy_evaluation_mono = time.monotonic()
        self._last_strategy_evaluation_wall = datetime.now().isoformat()
        self._strategy_evaluated_once = True

    def _mts_stamp_tick_loop(self) -> None:
        """Record the tick-loop heartbeat, keeping the previous tick's age.

        The tick_loop SLO clock compares the PREVIOUS completed tick (set
        before this stamp) so a stall between ticks is observed on the first
        tick after recovery.
        """
        self._prev_mts_tick_mono = self._last_mts_tick_mono
        self._last_mts_tick_mono = time.monotonic()
        self._last_mts_tick_wall = datetime.now().isoformat()

    def _mts_evaluator_lag_slo(self, key: str, default: float) -> float:
        """Read an SLO threshold from mts.params; invalid/<=0 falls back."""
        try:
            v = float(self.cfg.get("mts", {}).get("params", {}).get(key, default))
        except (TypeError, ValueError):
            v = default
        if v <= 0 or v != v:  # zero / negative / NaN
            v = default
        return v

    def _mts_pending_orders_age(self):
        """Return (has_pending_mts_order, oldest_age_secs_or_None).

        Age is None when a pending order carries no usable timestamp — in
        that case the caller suppresses (cannot judge staleness).
        """
        if not self.order_mgr:
            return False, None
        try:
            _active = getattr(self.order_mgr, "active_orders", []) or []
            if isinstance(_active, dict):
                _active = list(_active.values())
            _mts_strategies = {"MTS_ENTRY", "MTS_MANUAL", "MTS_RELEASE", "MTS_EXIT"}
            _ages = []
            _now_dt = datetime.now()
            for _o in _active:
                if str(getattr(_o, "strategy", "") or "") not in _mts_strategies:
                    continue
                _ts = getattr(_o, "created_at", None)
                if _ts is None:
                    return True, None
                try:
                    if isinstance(_ts, datetime):
                        _ages.append((_now_dt - _ts).total_seconds())
                    else:
                        _ages.append(time.time() - float(_ts))
                except (TypeError, ValueError):
                    return True, None
            if not _ages:
                return False, None
            return True, min(_ages)
        except Exception:
            return False, None

    def _mts_check_evaluator_lag(self, strategy, strat_has_pos: bool) -> None:
        """Evaluator-lag SLO v2.1 (2026-08-06 codex v2 audit corrections).

        Split clocks: tick-loop (PREVIOUS completed tick) and on_bar
        heartbeat. Suppressed while FLAT or market closed. Fresh pending MTS
        orders (transition window) suppress; orders older than
        pending_order_stall_secs alert as TRANSITION_STALL (hung order must
        not suppress evaluator alerts indefinitely). Baselines seeded at
        startup so a restored OPEN position with no first evaluation alerts
        after grace (first_eval=True).
        """
        if not strat_has_pos:
            return
        try:
            if not is_taifex_futures_market_open():
                return  # market closed — evaluator legitimately idle
        except Exception:
            pass  # fail-open on helper error (no false alarm)
        _now = time.monotonic()
        _tid = getattr(strategy, "_trade_id", None)
        try:
            _has_pending, _pending_age = self._mts_pending_orders_age()
        except Exception:
            _has_pending, _pending_age = False, None
        if _has_pending:
            if _pending_age is not None and                     _pending_age > self._mts_evaluator_lag_slo(
                        "pending_order_stall_secs", 120.0):
                # hung order — alert, don't suppress forever
                self._mts_slo_alert(
                    "pending_stall", "pending_order_stall_secs", 120.0,
                    _pending_age, _now, _tid,
                    first_eval=False,
                    last_wall=self._last_strategy_evaluation_wall,
                )
            return  # fresh or unjudgeable pending order — suppress this tick
        self._mts_slo_clock(
            "tick_loop", "tick_loop_slo_secs", 90.0,
            self._prev_mts_tick_mono, _now, _tid,
        )
        self._mts_slo_clock(
            "on_bar", "on_bar_slo_secs", 90.0,
            self._last_strategy_evaluation_mono, _now, _tid,
        )

    def _mts_slo_clock(
        self, clock: str, cfg_key: str, default: float,
        last_mono, now: float, tid,
    ) -> None:
        """Evaluate one SLO clock; emit a rate-limited alert when stale."""
        if last_mono is None:
            return
        self._mts_slo_alert(
            clock, cfg_key, default, now - last_mono, now, tid,
            first_eval=clock == "on_bar" and not self._strategy_evaluated_once,
            last_wall=(
                self._last_strategy_evaluation_wall
                if clock == "on_bar" else self._last_mts_tick_wall
            ),
        )

    def _mts_slo_alert(
        self, clock: str, cfg_key: str, default: float,
        lag: float, now: float, tid, first_eval: bool, last_wall,
    ) -> None:
        """Emit a rate-limited MTS_EVALUATOR_LAG alert when lag > SLO."""
        _slo = self._mts_evaluator_lag_slo(cfg_key, default)
        if lag <= _slo:
            return
        if now - self._last_slo_alert_mono.get(clock, 0.0) < 60.0:
            return  # rate limit: at most one alert per minute per clock
        self._last_slo_alert_mono[clock] = now
        logger.warning(
            "[MTS_EVALUATOR_LAG] clock=%s position open trade=%s silent for "
            "%.0fs (SLO %.0fs) first_eval=%s last_eval=%s",
            clock, tid, lag, _slo, first_eval, last_wall,
        )
        try:
            self._append_mts_event(
                "MTS_EVALUATOR_LAG",
                clock=clock,
                trade_id=tid,
                lag_secs=round(lag, 1),
                slo_secs=_slo,
                first_eval=first_eval,
                last_eval_at=last_wall,
            )
        except Exception as _e:  # never let telemetry break the tick path
            logger.warning("[MTS_EVALUATOR_LAG] event write failed: %s", _e)

    def _mts_has_pending_mts_orders(self) -> bool:
        """Check if order_mgr has any pending MTS lifecycle orders.

        Pending ENTRY/RELEASE/EXIT orders indicate an in-flight lifecycle
        transition that hasn't completed yet.  A new ENTRY must not be
        submitted while any MTS order is pending.
        """
        if not self.order_mgr:
            return False
        try:
            _active = getattr(self.order_mgr, "active_orders", []) or []
            if isinstance(_active, dict):  # 2026-08-06: active_orders is Dict[str, Order]
                _active = list(_active.values())
            _mts_strategies = {"MTS_ENTRY", "MTS_MANUAL", "MTS_RELEASE", "MTS_EXIT"}  # 2026-07-09 Hermes Agent: include MTS_MANUAL so pending manual orders block duplicate entries
            for _o in _active:
                _strat = str(getattr(_o, "strategy", "") or "")
                if _strat in _mts_strategies:
                    return True
            return False
        except Exception:
            return False

    def _canonical_confirms_flat(self) -> bool:
        """Fresh broker snapshot is direct flat evidence for the entry guard.

        The in-memory _live_broker_flat_proven flag can be stale during
        degraded windows; the canonical snapshot (fresh + OK capture + no
        futures position + no open orders) is authoritative.  Guards the
        fills-ledger orphan case: a broker-side close the system never saw
        (phone flatten) leaves an orphan ENTRY in the fills ledger that must
        not block a fresh entry when the broker truth confirms flat.
        """
        try:
            from core.runtime_paths import runtime_path
            _p = Path(runtime_path("exports", "trades", "live",
                                   "diagnostics",
                                   "broker_snapshot_canonical.json"))
            if not _p.exists():
                return False
            _d = json.loads(_p.read_text())
            _age = time.time() - (int(_d.get("captured_at") or 0) / 1000.0)
            if _age > 60:
                return False
            if ((_d.get("fetch_status") or {}).get("capture") != "OK"):
                return False
            if _d.get("open_orders"):
                return False
            _fut = [p for p in (_d.get("positions") or [])
                    if p.get("account") == "futures"
                    and int(p.get("quantity", 0) or 0) > 0]
            return not _fut
        except Exception:
            return False

    def _mts_block_entry_if_open_position(
        self, strategy, signal_action: str
    ) -> bool:
        """P0 guard: block MTS ENTRY if ANY source indicates an open position.

        Returns True if entry is blocked (caller must abort).
        Returns False if entry is safe to proceed.

        Checks in priority order:
        1. state file: has_position == True
        2. lifecycle phase != FLAT
        3. fills ledger: ENTRY without matching EXIT
        4. order_mgr: pending MTS lifecycle orders
        """
        # Guard 1: state file authority
        _state_path = _mts_position_state_path()
        _state_has_pos = False
        _lifecycle_phase = None
        try:
            if _state_path.exists():
                _disk = json.loads(_state_path.read_text())
                _state_has_pos = bool(_disk.get("has_position", False))
                _lc = _disk.get("lifecycle", {})
                _lifecycle_phase = _lc.get("phase") if isinstance(_lc, dict) else None
        except Exception:
            pass

        # Guard 2: fills ledger
        _fills_has_open = self._mts_has_open_position_from_fills()

        # Guard 3: pending orders
        _has_pending = self._mts_has_pending_mts_orders()

        _blocked = False
        _reasons = []

        _live_flat_proven = (
            getattr(getattr(self, "_execution_context", None),
                    "requested_mode", "") == "live"
            and (bool(getattr(self, "_live_broker_flat_proven", False))
                 or self._canonical_confirms_flat())
            and not bool(getattr(self, "_broker_authority_degraded", False))
        )
        if bool(getattr(self, "_broker_authority_degraded", False)):
            _blocked = True
            _reasons.append("broker_authority_degraded")
        elif not _live_flat_proven:
            if _state_has_pos:
                _blocked = True
                _reasons.append("state.has_position=True")
            if bool(getattr(strategy, "_has_position", False)):
                _blocked = True
                _reasons.append("strategy.has_position=True")
            if _lifecycle_phase and _lifecycle_phase != "FLAT":
                _blocked = True
                _reasons.append(f"lifecycle.phase={_lifecycle_phase}")
            if _fills_has_open:
                _blocked = True
                _reasons.append("fills_ledger_has_open_entry")
        if _has_pending:
            _blocked = True
            _reasons.append("pending_mts_orders")
        if bool(getattr(self, "_broker_position_observed", False)):
            _blocked = True
            _reasons.append("broker_snapshot_has_position")

        if _blocked:
            console.print(
                f"[bold red]⛔ [MTS_ENTRY_BLOCKED_OPEN_POSITION] "
                f"state_has_pos={_state_has_pos} "
                f"lifecycle_phase={_lifecycle_phase} "
                f"fills_has_open={_fills_has_open} "
                f"pending_orders={_has_pending} "
                f"reasons={_reasons} "
                f"action={signal_action}[/bold red]"
            )
            return True

        return False


    def _resolve_close_all_position(self):
        """Resolve the position to close.  The BROKER canonical (actual
        positions) is the authority — local state is used only when the
        broker capture is unavailable.  Duplicate contracts or unknown
        directions fail closed (no close info).  Returns
        (has_pos, near_side, far_side, released_leg, trade_id, disk)."""
        _has_pos = False
        _near_side = None
        _far_side = None
        _released_leg = None
        _trade_id = "mts-emergency"
        _disk = None
        _canon_ok = False

        def _side(row):
            if row is None:
                return None
            text = str(row.get("direction") or "").lower()
            if "sell" in text or "short" in text:
                return "SHORT"
            if "buy" in text or "long" in text:
                return "LONG"
            return None

        # 1. broker canonical first (broker facts are the authority).
        #    capture == OK is authoritative IMMEDIATELY — even an empty
        #    positions list proves flat and must never fall back to a
        #    possibly-stale local state (ghost resurrection).
        try:
            _snap = self._capture_post_startup_snapshot()
            if (_snap and (_snap.get("fetch_status") or {})
                    .get("capture") == "OK"):
                _canon_ok = True
                _codes = {str(getattr(self.contract, "code", "")),
                          str(getattr(self.far_contract, "code", ""))}
                _rows = [p for p in (_snap.get("positions") or [])
                         if p.get("account") == "futures"
                         and str(p.get("code") or "") in _codes
                         and int(p.get("quantity") or 0) > 0]
                if not _rows:
                    # broker flat, or no target MTS contracts -> flat,
                    # zero close info, zero orders
                    return (False, None, None, None,
                            "mts-emergency", None)
                if _rows:
                    if len(_rows) != len({str(p.get("code")) for p in _rows}):
                        # duplicate contract: fail closed
                        return (False, None, None, None,
                                "mts-emergency", None)
                    _by_code = {str(p.get("code")): p for p in _rows}
                    _near = _by_code.get(
                        str(getattr(self.contract, "code", "")))
                    _far = _by_code.get(
                        str(getattr(self.far_contract, "code", "")))
                    _near_side = _side(_near)
                    _far_side = _side(_far)
                    if _near_side is None and _far_side is None:
                        # unknown directions: fail closed
                        return (False, None, None, None,
                                "mts-emergency", None)
                    if len(_rows) == 1 and _near_side is not None:
                        _released_leg = "far"
                    elif len(_rows) == 1 and _far_side is not None:
                        _released_leg = "near"
                    _has_pos = True
                    _trade_id = "mts-emergency-broker"
                    _canon_ok = True
                    console.print(
                        "[yellow]📝 [MANUAL_TRADE] close_all: "
                        "sides from broker canonical[/yellow]")
        except Exception as _bc_e:
            console.print(
                f"[red]⚠️ [MANUAL_TRADE] close_all: broker canonical "
                f"fallback failed: {_bc_e}[/red]")
        # 2. local state fallback ONLY when the canonical is unavailable
        if not _canon_ok:
            try:
                _state_path = _mts_position_state_path()
                if _state_path.exists():
                    _disk = json.loads(_state_path.read_text())
                if _disk and _disk.get("has_position") is True:
                    _has_pos = True
                    _near_side = _disk.get("near_side")
                    _far_side = _disk.get("far_side")
                    _released_leg = _disk.get("released_leg")
                    _trade_id = _disk.get("trade_id", "mts-emergency")
            except Exception as _sf_e:
                console.print(
                    f"[red]⚠️ [MANUAL_TRADE] close_all: disk read failed: {_sf_e}[/red]")
        return (_has_pos, _near_side, _far_side, _released_leg,
                _trade_id, _disk)

    def _write_manual_command_status(self, command_id, status, message, **extra) -> None:
        """2026-07-31: Write manual-command audit status for the dashboard.

        Status machine: COMMAND_SENT (dashboard) -> RECEIVED (monitor consumed
        flag) -> PROCESSING -> COMPLETED | FAILED. Every transition carries a
        timestamp; COMPLETED carries position_before/position_after/order_ids.
        """
        try:
            _path = "/tmp/futures_manual_trade_status.json"
            _data = {
                "command_id": command_id,
                "status": status,
                "ts": datetime.now().isoformat(),
                "message": message,
            }
            _data.update(extra)
            with open(_path, "w") as _f:
                json.dump(_data, _f, default=str)
        except Exception as _e:
            console.print(f"[red]⚠️ [CMD_STATUS] write failed: {_e}[/red]")

    def _register_emergency_command_order(self, order_id: str) -> None:
        """Associate a submitted emergency-close order with its audit command."""
        tracker = getattr(self, "_emergency_cmd", None)
        if tracker and not tracker.get("completed") and not tracker.get("failed"):
            tracker["order_ids"].add(order_id)

    def _maybe_complete_emergency_command(self, event, fill_price: float) -> None:
        """Mark a manual close complete only after every expected order fills."""
        from core.order_management.order import OrderStatus

        tracker = getattr(self, "_emergency_cmd", None)
        if not tracker or tracker.get("completed") or tracker.get("failed"):
            return
        order_id = getattr(event, "order_id", None)
        if order_id not in tracker["order_ids"]:
            return
        if getattr(event, "status", None) != OrderStatus.FILLED:
            return

        tracker["filled_ids"].add(order_id)
        tracker["fill_prices"][order_id] = float(fill_price)
        if not tracker["order_ids"].issubset(tracker["filled_ids"]):
            return

        tracker["completed"] = True
        order_ids = sorted(tracker["order_ids"])
        self._write_manual_command_status(
            tracker["command_id"], "COMPLETED", "平倉已成交",
            position_after={"near_qty": 0, "far_qty": 0},
            order_ids=order_ids,
            fill_prices={order_id: tracker["fill_prices"].get(order_id) for order_id in order_ids},
        )

    def _fail_emergency_command(self, event, terminal_status: str) -> None:
        """Record a rejected/cancelled emergency leg without masking a partial flat."""
        tracker = getattr(self, "_emergency_cmd", None)
        order_id = getattr(event, "order_id", None)
        if (not tracker or tracker.get("completed") or tracker.get("failed")
                or order_id not in tracker["order_ids"]):
            return

        tracker["failed"] = True
        reason = getattr(event, "reason", "unknown")
        self._write_manual_command_status(
            tracker["command_id"], "FAILED",
            f"平倉單 {terminal_status}: {order_id} ({reason})",
            failed_order_id=order_id,
            reason=reason,
            order_ids=sorted(tracker["order_ids"]),
            filled_order_ids=sorted(tracker["filled_ids"]),
        )

    def _emergency_flatten_mts(self, strategy) -> None:
        """Emergency flatten all MTS positions. Used by settlement gate and manual close_all.

        Sets _mts_force_exit_inflight to prevent duplicate SINGLE_LEG preclose triggers.
        """
        # [Step 9] emergency quarantine contract: under LIVE_QUARANTINED /
        # PREFLIGHT the emergency flatten command is BLOCKED — zero
        # strategy mutation, zero broker calls, durable dashboard-visible
        # audit reason (persisted). ctx=None is fail-closed
        # (NO_LIVE_CERTIFICATION). PAPER behavior unchanged (a paper
        # context is not a LIVE context).
        _ctx = getattr(self, "_execution_context", None)
        _paper = _ctx is not None and \
            getattr(_ctx, "requested_mode", "") == "paper"
        if not _paper and (_ctx is None or not _ctx.is_live_ready()):
            _reason = ("EMERGENCY_BLOCKED_NO_LIVE_CERTIFICATION"
                       if _ctx is None else "EMERGENCY_BLOCKED_QUARANTINED")
            self._record_safety_stop_reconcile()   # [orphan] never silent
            if _ctx is not None:
                from core.mode_transition import (ModeTransitionState,
                                                  with_effective_mode)
                self._execution_context = with_effective_mode(
                    _ctx, ModeTransitionState.LIVE_QUARANTINED.value,
                    live_order_allowed=False,
                    audit_reasons=(_reason,) + tuple(
                        getattr(_ctx, "audit_reasons", ()) or ()))
                self._persist_execution_context()
            console.print(
                f"[red]🚫 [EMERGENCY_FLATTEN] BLOCKED: {_reason} — "
                f"zero strategy mutation, zero broker calls. Operator "
                f"procedure: restore LIVE_READY (reconnect/recertify) "
                f"before emergency flatten.[/red]")
            return {"blocked": True, "reason": _reason}
        self._mts_force_exit_inflight = True
        # If strategy has position, submit EXIT for remaining leg or close both
        _has_pos = bool(getattr(strategy, '_has_position', False))
        if not _has_pos:
            return

        _released = getattr(strategy, '_released_leg', None)
        if _released is not None:
            # SINGLE_LEG: exit remaining leg
            signal = Signal("EXIT", "SETTLEMENT_FORCE_FLAT", confidence=1.0, stop_loss=0)
        else:
            # SPREAD: exit both legs — submit two PARTIAL_EXIT signals
            # First exit near, then far (order doesn't matter for emergency)
            signal = Signal("EXIT", "SETTLEMENT_FORCE_FLAT", confidence=1.0, stop_loss=0)
            # If BOTH_HELD, we need to release one leg first, then exit the other.
            # For settlement, just force close the near leg (most liquid).
            # The remaining far will be exited on next tick.
            _near_side = getattr(strategy, '_near_side', None)
            if _near_side:
                _released = "far"  # pretend far was released, so EXIT targets near
                strategy._released_leg = _released
                # _side needs to be set for EXIT path
                strategy._side = "LONG" if _near_side == "LONG" else "SHORT"

        if signal:
            console.print(f"[bold red]🚨 [EMERGENCY_FLATTEN] Force closing MTS position[/bold red]")
            # Build a minimal bar dict if none available
            _bar = {"near_close": 0, "far_close": 0, "atr": 0}
            self._submit_mts_order_signal(signal, strategy, _bar, datetime.now())


    def _resolve_entry_price(self, _flag: dict) -> tuple:
        """5-tier price fallback chain for dry_run mode only (no Shioaji).
        
        2026-06-05 JVS Claw: Step 3 revised — dry_run-only fallback.
        Paper and live modes receive real ticks via Shioaji — this is NOT called.
        
        Returns (price: float | None, source_label: str).
        Tier 5 is a hard stop — caller must handle None by rejecting.
        
        Tiers:
          1. LIVE_TICK: market_data with local_arrival_at < 5000ms
          2. BAR_CLOSE: last completed 5m bar from _tick_bars_deque
          3. FAR_BAR_CLOSE: current far-month bar from _far_current_bar
          4. FLAG_ADVISORY: dashboard intent (near_close from flag)
          5. None: all tiers exhausted
        """
        # Tier 1: Live tick (only if market_data has fresh local_arrival_at)
        _live = self.market_data.get(self.ticker, {})
        _close = _live.get("close")
        _arrival = _live.get("local_arrival_at")
        if _close and _close > 0 and _arrival:
            _age = (time.time() - _arrival) * 1000
            if _age <= 5000:
                return (float(_close), "LIVE_TICK")
        
        # Tier 2: Last completed 5m bar
        if hasattr(self, "_tick_bars_deque") and self._tick_bars_deque:
            _last = self._tick_bars_deque[-1].get("close")
            if _last and _last > 0:
                return (float(_last), "BAR_CLOSE")
        
        # Tier 3: Current far-month bar
        _far = self._far_current_bar.get("close")
        if _far and _far > 0:
            return (float(_far), "FAR_BAR_CLOSE")
        
        # Tier 4: Dashboard flag advisory
        _dash = _flag.get("near_close")
        if _dash and _dash > 0:
            return (float(_dash), "FLAG_ADVISORY")
        
        # Tier 5: All tiers exhausted
        return (None, "NO_PRICE_SOURCE")

    def _process_manual_trade_flag(self) -> bool:
        """Consume /tmp/futures_manual_trade.flag if present.
        
        2026-06-05 JVS Claw: NO_LIVE_TICK fix — full refactor of flag lifecycle.
        
        Atomic lifecycle (C1): rename → process → delete.
        On crash: .processing file survives → startup recovery renames back.
        
        Validation pipeline:
          C0: State guard (prevent double-click processing)
          C6: Schema validation (required keys)
          C5: TTL expiry check (backward compat when created_at=None)
          C2: Idempotency (md5 hash, excludes created_at)
          C2: Active order guard (prevents duplicate submission)
          C7: MAX_RETRIES guard (10 attempts max)
        
        Terminal statuses: delete .processing file.
        Retryable statuses: keep .processing file for next tick.
        """
        _flag_path = getattr(self, "manual_trade_flag_path", "/tmp/futures_manual_trade.flag")
        _processing_path = _flag_path + ".processing"
        
        # 2026-06-22 Gemini CLI: Support processing of both new and pending retry flags
        _has_new = os.path.exists(_flag_path)
        _has_processing = os.path.exists(_processing_path)
        
        if not _has_new and not _has_processing:
            return False

        if _has_new:
            # ── C1: Atomic rename (flag → .processing) ──
            # 2026-06-05 JVS Claw: prevents flag deletion before validation
            try:
                os.rename(_flag_path, _processing_path)
            except OSError:
                return False  # Another caller already took it

        try:
            self._manual_trade_status = "PROCESSING"
            with open(_processing_path) as _f:
                _flag = json.loads(_f.read())
            console.print(f"[bold magenta]🔬 [MANUAL_TRADE_FLAG] consumed path={_flag_path}[/bold magenta]")

            # ── C6: Schema validation ──
            # 2026-06-05 JVS Claw: reject malformed flags early (terminal)
            _FLAG_REQUIRED = {"action"}
            if not _FLAG_REQUIRED.issubset(_flag.keys()):
                self._manual_trade_status = "FAILED: INVALID_FLAG_SCHEMA"
                console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: Missing required keys (need {_FLAG_REQUIRED})[/red]")
                os.remove(_processing_path)
                return True

            # ── C5: TTL check (backward compat: skip if created_at is None) ──
            # 2026-06-05 JVS Claw: old dashboards that don't write created_at
            # will pass through; new dashboards get TTL protection.
            _TTL = int(self.cfg.get("mts", {}).get("flag_ttl_seconds", 3600))
            _flag_created = _flag.get("created_at")
            if _flag_created is not None and time.time() - _flag_created > _TTL:
                self._manual_trade_status = "REJECTED: FLAG_EXPIRED"
                console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: Flag expired (age={int(time.time() - _flag_created)}s > TTL={_TTL}s)[/red]")
                os.remove(_processing_path)
                return True

            # ── C2: Idempotency — md5 hash from action + side only ──
            # 2026-06-09 JVS Claw: Simplified hash to use only action + side.
            # This prevents double-click from creating duplicate orders even when
            # ts, spread_z, near_close, far_close change between clicks.
            # Set is in-memory only; after restart no orders exist → retry is safe.
            _idempotent_flag = {
                "action": _flag.get("action", ""),
                "side": _flag.get("side", "")
            }
            _flag_id = hashlib.md5(json.dumps(_idempotent_flag, sort_keys=True).encode()).hexdigest()[:8]
            if _flag_id in self._processed_flag_ids:
                self._manual_trade_status = "SKIPPED: IDEMPOTENT"
                console.print(f"[yellow]⏭️ [MANUAL_TRADE] Skipped: duplicate flag (id={_flag_id})[/yellow]")
                os.remove(_processing_path)
                return True
            self._current_flag_id = _flag_id

            # ── C7: MAX_RETRIES guard ──
            # 2026-06-05 JVS Claw: prevents infinite retry loops.
            # Counter resets on success or new flag.
            _MAX_RETRIES = 10
            if self._flag_retry_count >= _MAX_RETRIES:
                self._manual_trade_status = "FAILED: MAX_RETRIES"
                console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: exceeded max retries ({_MAX_RETRIES})[/red]")
                os.remove(_processing_path)
                self._flag_retry_count = 0
                return True

            # ── C2: Active order guard ──
            # 2026-06-05 JVS Claw: prevents duplicate orders after hard crash
            # (in-memory idempotency set is lost but broker still has pending orders).
            # Uses Order.strategy (NOT strategy_id) per Order class line 87.
            # active_orders is Dict[str, Order] → .values() to iterate.
            if self._mts_has_pending_mts_orders():
                self._manual_trade_status = "SKIPPED: PENDING_MTS_ORDER_EXISTS"
                console.print(
                    "[yellow]⏭️ [MANUAL_TRADE] Skipped: MTS order already in flight[/yellow]"
                )
                os.remove(_processing_path)
                return True
            
            _action = _flag.get("action", "")

            # A completed automatic entry is no longer an active order, but
            # it remains an open spread.  Manual entry must use the same
            # canonical position gate as automatic entry.
            if _action == "spread":
                _mts_strategy = getattr(self, "_registry", {}).get("tmf_spread")
                _strategy_open = bool(getattr(_mts_strategy, "_has_position", False))
                if _strategy_open or self._mts_block_entry_if_open_position(
                    _mts_strategy, _flag.get("side", "")
                ):
                    self._manual_trade_status = "SKIPPED: MTS_POSITION_EXISTS"
                    console.print(
                        "[yellow]⏭️ [MANUAL_TRADE] Skipped: MTS position already open[/yellow]"
                    )
                    os.remove(_processing_path)
                    return True

            # 2026-06-09 JVS Claw: C0 — State guard for spread actions only
            # Prevent double-click for spread entry, but always allow close_all
            # Only check for terminal states (FILLED, SUBMITTED), not PROCESSING (current call)
            if _action == "spread" and self._manual_trade_status in ("FILLED", "SUBMITTED"):
                self._manual_trade_status = "SKIPPED: C0_STATE_GUARD"
                console.print(f"[yellow]⏭️ [MANUAL_TRADE] Skipped spread: already in state FILLED/SUBMITTED[/yellow]")
                os.remove(_processing_path)
                return True
            
            if _action == "close_all":
                console.print("[bold red]🆘 [MANUAL_TRADE] EMERGENCY CLOSE ALL triggered[/bold red]")
                # 2026-07-31 Hermes Agent: audit trail — command_id + status machine
                _command_id = _flag.get("command_id") or f"CMD-{datetime.now():%Y%m%d%H%M%S}"
                self._write_manual_command_status(
                    _command_id, "RECEIVED", "monitor 已接收緊急平倉指令", action="close_all",
                )
                # [S0] EXIT_ONLY: emergency close_all is NOT gateway
                # capability-bound — explicitly blocked (zero submit)
                # until routed through the gateway.
                if (getattr(getattr(self, "_execution_context", None),
                            "effective_mode", "")
                        == "reconciled_exit_only"):
                    self._append_mts_event(
                        "ORDER_INTENT_BLOCKED",
                        action="MTS_EMERGENCY_CLOSE_ALL",
                        reason="EXIT_ONLY_EMERGENCY_BLOCKED", trade_id="")
                    self._write_manual_command_status(
                        _command_id, "FAILED",
                        "close_all blocked: EXIT_ONLY (gateway capability required)")
                    return True
                # 2026-07-07 Gemini CLI: P0: Increment generation on emergency close to invalidate past callbacks
                self._lifecycle_generation += 1
                self._emergency_reset_at = datetime.now()
                self._cancel_all_pending_orders()

                # 2026-05-27 Gemini CLI: Define strategy object locally for use in reset/logging
                _mts_cfg = self.cfg.get("mts", {})
                _strat_name = _mts_cfg.get("strategy", "tmf_spread")
                _strategy_obj = self._registry.get(_strat_name)

                # Read state file for position recovery (strategy may not
                # have _has_position); fall back to the BROKER canonical
                # (actual positions) when local state is empty — broker
                # facts are the authority for the emergency close.
                (_has_pos, _near_side, _far_side, _released_leg,
                 _trade_id, _disk) = self._resolve_close_all_position()

                self._write_manual_command_status(
                    _command_id, "PROCESSING", "正在送出平倉單",
                    has_pos=_has_pos,
                    position_before={
                        "near_side": _near_side, "far_side": _far_side,
                        "released_leg": _released_leg,
                        "near_entry": float(_disk.get("near_entry", 0)) if _disk else 0,
                        "far_entry": float(_disk.get("far_entry", 0)) if _disk else 0,
                    },
                )
                # [S0 P1 documented exclusion] normal-live MTS_EMERGENCY
                # deliberately bypasses the OrderIntentGateway: the
                # emergency path must be able to fire even when the
                # gateway/registry is unavailable. It is NOT
                # gateway-authorized (direct order_mgr.submit below);
                # protection is the existing execution-context live gate
                # + adapter gate. EXIT_ONLY is explicitly blocked above
                # (EXIT_ONLY_EMERGENCY_BLOCKED, zero submit).
                if _has_pos and self.order_mgr:
                    self._emergency_cmd = {
                        "command_id": _command_id,
                        "order_ids": set(),
                        "filled_ids": set(),
                        "fill_prices": {},
                        "completed": False,
                        "failed": False,
                    }
                    # 2026-07-07 Hermes Agent: reindex order counter from
                    # persisted orders to prevent ID collision after PM2
                    # restart or order_mgr recreation.
                    self.order_mgr.reindex_orders()

                    _ts = datetime.now()
                    from core.order_management.order import OrderType, OrderSide
                    # 2026-08-06 Hermes Agent P1: fail-closed sides guard.
                    # BROKER_RECONCILED once wrote leg labels ("NEAR"/"FAR")
                    # into near_side/far_side; the `SELL if == "LONG" else BUY`
                    # mapping silently sent BUY for ANY non-LONG value — wrong
                    # direction for a LONG far leg. Refuse to submit; write
                    # FAILED (terminal, .processing removed so no retry loop).
                    _invalid_sides = []
                    if _released_leg is None:
                        if _near_side not in ("LONG", "SHORT"):
                            _invalid_sides.append(f"near={_near_side!r}")
                        if _far_side not in ("LONG", "SHORT"):
                            _invalid_sides.append(f"far={_far_side!r}")
                    else:
                        _rem_leg_side = _far_side if _released_leg == "near" else _near_side
                        if _rem_leg_side not in ("LONG", "SHORT"):
                            _invalid_sides.append(f"remaining={_rem_leg_side!r}")
                    if _invalid_sides:
                        self._write_manual_command_status(
                            _command_id, "FAILED",
                            f"close_all aborted: invalid side(s) "
                            f"{', '.join(_invalid_sides)} — refusing to submit",
                            has_pos=_has_pos,
                        )
                        console.print(
                            f"[red]❌ [MANUAL_TRADE] close_all FAILED: invalid side(s) "
                            f"{', '.join(_invalid_sides)} — refusing to submit[/red]"
                        )
                        if os.path.exists(_processing_path):
                            os.remove(_processing_path)
                        return True
                    _EXIT_BUFFER = 10
                    _TICK = 1.0

                    _near_last = float(self.market_data.get(f"{self.ticker}_NEAR", {}).get("close", 0))
                    _far_last = float(self.market_data.get(f"{self.ticker}_FAR", {}).get("close", 0))
                    if _near_last == 0 and len(self._tick_bars_deque) > 0:
                        _near_last = float(self._tick_bars_deque[-1].get("near_close", 0))
                    if _far_last == 0 and len(self._tick_bars_deque) > 0:
                        _far_last = float(self._tick_bars_deque[-1].get("far_close", 0))
                    # Last resort: use entry price from disk
                    if _near_last == 0:
                        _near_last = float(_disk.get("near_entry", 41000)) if _disk else 41000
                    if _far_last == 0:
                        _far_last = float(_disk.get("far_entry", _near_last + 100)) if _disk else _near_last + 100

                    # ── Emergency exit fill metadata (safe defaults for both-legs case) ──
                    # 2026-07-07 Hermes Agent: refuse placeholder symbols.
                    if self.contract is None or self.far_contract is None:
                        console.print(
                            "[red]❌ [EMERGENCY_BLOCKED] contract unresolved; "
                            "refusing placeholder emergency exit[/red]"
                        )
                        return
                    _emerg_near_code = self.contract.code
                    _emerg_far_code = self.far_contract.code
                    _rem_leg = "BOTH"
                    _side = "BOTH"
                    _price = max(float(_near_last or 0), float(_far_last or 0))

                    if _released_leg is None:
                        # Both legs held
                        _n_side = OrderSide.SELL if _near_side == "LONG" else OrderSide.BUY
                        # 2026-07-07 Hermes Agent: Emergency flatten uses MKP+IOC.
                        # LMT+ROD is wrong for emergencies — it can sit unfilled
                        # when the user expects immediate risk reduction.
                        # If market is closed, log clearly and proceed anyway
                        # (emergency overrides market-hours guard).
                        if not is_taifex_futures_market_open():
                            console.print(
                                "[bold red]⚠️ [EMERGENCY] Market closed — "
                                "MKP order will be rejected or queued by broker[/bold red]"
                            )
                        _o_near = self.order_mgr.create_order(
                            symbol=_emerg_near_code, side=_n_side,
                            order_type=OrderType.MKP, quantity=1,
                            strategy="MTS_EMERGENCY",
                        )
                        self._register_emergency_command_order(_o_near.order_id)
                        self.order_mgr.submit(_o_near)
                        if self.paper_fill_sim:
                            self.paper_fill_sim.register(_o_near)
                        self._pending_lifecycle_orders[_o_near.order_id] = {
                            "intent_id": _o_near.intent_id, "signal": "EXIT", "reason": "EMERGENCY_CLOSE",
                            "ts": _ts, "lots": 1, "price": 0, "ref_ohlc": {},
                            "strategy": "MTS_EMERGENCY",
                        }

                        _f_side = OrderSide.SELL if _far_side == "LONG" else OrderSide.BUY
                        _o_far = self.order_mgr.create_order(
                            symbol=_emerg_far_code, side=_f_side,
                            order_type=OrderType.MKP, quantity=1,
                            strategy="MTS_EMERGENCY",
                        )
                        self._register_emergency_command_order(_o_far.order_id)
                        self.order_mgr.submit(_o_far)
                        if self.paper_fill_sim:
                            self.paper_fill_sim.register(_o_far)
                        self._pending_lifecycle_orders[_o_far.order_id] = {
                            "intent_id": _o_far.intent_id, "signal": "EXIT", "reason": "EMERGENCY_CLOSE",
                            "ts": _ts, "lots": 1, "price": 0, "ref_ohlc": {},
                            "strategy": "MTS_EMERGENCY",
                        }

                        # 2026-05-27 Gemini CLI: Removed redundant process_tick to prevent double-ordering loops
                    else:
                        # Single leg remaining
                        _rem_leg = "far" if _released_leg == "near" else "near"
                        _rem_side = _far_side if _rem_leg == "far" else _near_side
                        _rem_last = _far_last if _rem_leg == "far" else _near_last
                        _side = OrderSide.SELL if _rem_side == "LONG" else OrderSide.BUY
                        _price = _rem_last + _EXIT_BUFFER * _TICK if _side == OrderSide.BUY else _rem_last - _EXIT_BUFFER * _TICK
                        _rem_code = _emerg_far_code if _rem_leg == "far" else _emerg_near_code
                        _order = self.order_mgr.create_order(
                            symbol=_rem_code, side=_side,
                            order_type=OrderType.MKP, quantity=1,
                            strategy="MTS_EMERGENCY",
                        )
                        self._register_emergency_command_order(_order.order_id)
                        self.order_mgr.submit(_order)
                        if self.paper_fill_sim:
                            self.paper_fill_sim.register(_order)
                        # 2026-06-23 Gemini CLI: Register emergency order in pending_lifecycle_orders so fill updates position
                        self._pending_lifecycle_orders[_order.order_id] = {
                            "intent_id": _order.intent_id, "signal": "EXIT", "reason": "EMERGENCY_CLOSE",
                            "ts": _ts, "lots": 1, "price": _price, "ref_ohlc": {},
                            "strategy": "MTS_EMERGENCY",
                        }

                        # 2026-05-27 Gemini CLI: Removed redundant process_tick to prevent double-ordering loops
                    console.print("[bold green]✅ [MANUAL_TRADE] Emergency exit orders submitted[/bold green]")
                    
                    # 2026-05-27 Gemini CLI: Force strategy reset and log fill using correctly defined _strategy_obj
                    if _strategy_obj:
                        _strategy_obj._reset(reason="EMERGENCY_CLOSE")
                        # 2026-07-24 Hermes Agent: Use unified settlement pipeline for PnL computation
                        from strategies.plugins.futures.active.tmf_spread import settle_mts_trade
                        _near_entry_disk = float(_disk.get("near_entry", 0)) if _disk else 0
                        _far_entry_disk = float(_disk.get("far_entry", 0)) if _disk else 0
                        _near_side_disk = _disk.get("near_side") if _disk else None
                        _far_side_disk = _disk.get("far_side") if _disk else None
                        settle_mts_trade(
                            ticker=self.ticker,
                            trade_id=_trade_id,
                            exit_type="EMERGENCY_CLOSE_ALL",
                            near_entry=_near_entry_disk,
                            far_entry=_far_entry_disk,
                            near_side=_near_side_disk,
                            far_side=_far_side_disk,
                            near_exit_price=_near_last,
                            far_exit_price=_far_last,
                            settlement_source="BROKER_FILLS",
                        )

                    # Reset state file
                    try:
                        from strategies.plugins.futures.active.tmf_spread import _write_mts_state, lifecycle_to_dict
                        # [Fix 2026-07-06] Include lifecycle=FLAT to prevent state overwrite gap
                        _lc_dict = lifecycle_to_dict(_strategy_obj._lifecycle_oca) if (
                            _strategy_obj and hasattr(_strategy_obj, '_lifecycle_oca')
                        ) else {
                            "phase": "FLAT",
                            "release_group": {"status": "INACTIVE"},
                            "trail_group": {"status": "INACTIVE"},
                        }
                        # 2026-07-07 Hermes Agent: set manual_trade_status to READY
                        # BEFORE writing state so the dashboard sees it immediately
                        # instead of waiting for the next tick heartbeat.
                        self._manual_trade_status = "READY"
                        _write_mts_state(has_position=False, action="FLAT", reason="EMERGENCY_CLOSE",
                                         ticker=self.ticker, lifecycle=_lc_dict,
                                         manual_trade_status="READY")
                    except Exception as exc:
                        import logging
                        _log = logging.getLogger("FuturesMonitor")
                        _log.exception("[MTS][EMERGENCY_CLOSE] failed to write FLAT state: %s", exc)
                        console.print(f"[red]⚠️ [EMERGENCY_CLOSE] state write failed: {exc}[/red]")

                    # 2026-06-23 Gemini CLI: Reset trader position immediately on emergency close to allow subsequent manual trades without getting stuck
                    if self.trader.position != 0:
                        self.trader.execute_signal("EXIT", _near_last or 0.0, _ts)
                elif not _has_pos:
                    self._manual_trade_status = "READY"
                    console.print("[yellow]⚠️ [MANUAL_TRADE] close_all: no position to close[/yellow]")
                    self._write_manual_command_status(
                        _command_id, "COMPLETED",
                        "無持倉可平；已清理 stale lifecycle",
                        position_after={"near_qty": 0, "far_qty": 0}, order_ids=[],
                    )
                    # 2026-07-31 Hermes Agent: also clean stale lifecycle residue
                    # (release_group.status=ARMED with no orders) so the dashboard
                    # clear-records guard can pass. Emergency flatten must leave
                    # the state clean, not just log "nothing to close".
                    try:
                        from strategies.plugins.futures.active.tmf_spread import _write_mts_state
                        _write_mts_state(
                            has_position=False, action="FLAT", reason="EMERGENCY_CLOSE_NO_POS",
                            ticker=self.ticker,
                            lifecycle={
                                "phase": "FLAT",
                                "release_group": {"status": "INACTIVE"},
                                "trail_group": {"status": "INACTIVE"},
                            },
                            manual_trade_status="READY",
                        )
                        console.print("[dim]✅ [MANUAL_TRADE] close_all: stale lifecycle cleaned (FLAT/INACTIVE)[/dim]")
                    except Exception as _ce:
                        import logging
                        logging.getLogger("FuturesMonitor").warning(
                            "[EMERGENCY_CLOSE] no-pos state cleanup failed: %s", _ce
                        )
                else:
                    self._manual_trade_status = "FAILED: NO_ORDER_MGR"
                    self._write_manual_command_status(
                        _command_id, "FAILED", "平倉失敗：OrderManager 不可用",
                    )

                # 2026-07-07 Gemini CLI / Hermes Agent: Save updated orders immediately after emergency close
                self._save_orders_file_wrapper()

                # 2026-06-05 JVS Claw: terminal — clean up .processing
                if os.path.exists(_processing_path):
                    os.remove(_processing_path)
                return True

            if _action == "clear_records":
                console.print("[bold red]🗑️ [MANUAL_TRADE] MANUAL CLEAR RECORDS triggered[/bold red]")
                self._emergency_cmd = None
                # 2026-07-07 Gemini CLI: P0: Increment generation and reset everything on manual clear
                self._lifecycle_generation += 1
                self._emergency_reset_at = datetime.now()
                # 2026-07-08 Hermes Agent: reset risk control flags
                self._mts_force_exit_inflight = False
                self._mts_settlement_flat_done = False
                self._cancel_all_pending_orders()

                _mts_cfg = self.cfg.get("mts", {})
                _strat_name = _mts_cfg.get("strategy", "tmf_spread")
                _strategy_obj = self._registry.get(_strat_name)
                if _strategy_obj:
                    _strategy_obj._reset(reason="MANUAL_CLEAR")

                self._pending_lifecycle_orders.clear()
                self._mts_pending_fills.clear()
                self._mts_stale_order_cancels.clear()

                # 2026-07-07 Gemini CLI / Hermes Agent: Clear in-memory OrderManager state and delete orders file from disk
                if self.order_mgr:
                    self.order_mgr.clear_session_orders()

                # 2026-07-07 Hermes Agent: P0 — reset consumed_order_ids to prevent
                # cross-session pollution.  After clear_records, the same order IDs
                # (starting from ORD-YYYYMMDD-000001) may be reused.  Stale consumed
                # IDs from the previous session would block legitimate re-registration.
                _paper_sim = getattr(self, "paper_fill_sim", None)
                if _paper_sim:
                    _paper_sim._pending_orders.clear()
                    _paper_sim.consumed_order_ids.clear()

                try:
                    from core.date_utils import get_session_date_str
                    _date = getattr(self.order_mgr, "_session_date", None) if self.order_mgr else None
                    if not _date:
                        _date = get_session_date_str()
                    _orders_file = Path(f"exports/trades/{self.ticker}_{_date}_orders.json")
                    if _orders_file.exists():
                        _orders_file.unlink()
                except Exception as _e:
                    console.print(f"[red]⚠️ [MANUAL_TRADE] clear_records: orders file delete failed: {_e}[/red]")

                try:
                    from strategies.plugins.futures.active.tmf_spread import _write_mts_state, lifecycle_to_dict
                    _lc_dict = {
                        "phase": "FLAT",
                        "release_group": {"status": "INACTIVE"},
                        "trail_group": {"status": "INACTIVE"},
                    }
                    self._manual_trade_status = "READY"
                    _write_mts_state(has_position=False, action="FLAT", reason="MANUAL_CLEAR",
                                     ticker=self.ticker, lifecycle=_lc_dict,
                                     manual_trade_status="READY")
                except Exception as exc:
                    import logging
                    logging.getLogger("FuturesMonitor").exception("[MTS][MANUAL_CLEAR] failed to write FLAT state: %s", exc)

                if self.trader.position != 0:
                    self.trader.execute_signal("EXIT", 0.0, datetime.now())

                self._save_orders_file_wrapper()

                if os.path.exists(_processing_path):
                    os.remove(_processing_path)
                return True

            # 2026-05-22 Gemini CLI: Removed mts_selftest block from here.

            if _action == "spread":
                # Integrity check: only enter if flat
                if self.trader.position != 0:
                    self._manual_trade_status = "FAILED: POS_EXIST"
                    console.print("[red]⛔ [MANUAL_TRADE] Rejected: Position already exists[/red]")
                    # 2026-06-05 JVS Claw: terminal — delete .processing
                    if os.path.exists(_processing_path):
                        os.remove(_processing_path)
                    return True

                # Live mode guard: reject if outside trading hours
                if not self.dry_run:
                    from core.date_utils import is_day_session, is_night_session
                    _now = datetime.now()
                    if not is_day_session(_now) and not is_night_session(_now):
                        self._manual_trade_status = "REJECTED: MKT_CLOSED"
                        console.print("[red]⛔ [MANUAL_TRADE_FLAG] Live mode + market closed: rejected (retryable)[/red]")
                        # 2026-06-05 JVS Claw: retryable — keep .processing,
                        # do NOT increment retry count (market close ≠ processing failure).
                        # TTL clock still ticks; flag may expire during close.
                        return True

                _spread_side = _flag.get("side", "SELL_NEAR_BUY_FAR")
                
                # 2026-05-27 Gemini CLI: P0: Strict Price Integrity Contract
                # Manual entry ONLY accepted from fresh LIVE_TICK.
                # Use local_arrival_at to avoid clock drift issues. Increased limit to 5s.
                _MAX_ENTRY_AGE_MS = 5000
                _price = None
                # 2026-06-23 Gemini CLI: Initialize with valid UNSET to satisfy price provenance test
                _price_source = "UNSET"
                _tick_age_ms = -1
                
                # 2026-06-05 JVS Claw: Step 3 revised — dry_run-only fallback chain.
                # Paper and live modes BOTH connect to Shioaji (run_system dry_run=False)
                # and receive real ticks. Only dry_run (unit tests, no Shioaji) needs fallback.
                if self.dry_run:
                    _price, _price_source = self._resolve_entry_price(_flag)
                    if _price is None:
                        self._manual_trade_status = "REJECTED: NO_PRICE_SOURCE"
                        console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: All price tiers exhausted (dry_run)[/red]")
                        # Retryable: keep .processing, increment retry count
                        self._flag_retry_count += 1
                        console.print(f"[dim]🔄 [MANUAL_TRADE] Retry {self._flag_retry_count}/10 (NO_PRICE_SOURCE)[/dim]")
                        return True
                    # dry_run: price resolved from fallback, skip LIVE_TICK check below
                else:
                    # Live and paper: Shioaji connected, ticks arrive via on_tick()
                    # Live and paper: Shioaji connected, ticks arrive via on_tick()
                    # 2026-07-24 Gemini CLI: Check contract code and _NEAR keys if self.ticker key has no close
                    _live_tick = self.market_data.get(self.ticker, {})
                    if not _live_tick.get("close") and hasattr(self, "contract") and self.contract:
                        _live_tick = self.market_data.get(self.contract.code, {})
                    if not _live_tick.get("close"):
                        _live_tick = self.market_data.get(f"{self.ticker}_NEAR", {})
                    _price_raw = _live_tick.get("close")
                    _arrival_at = _live_tick.get("local_arrival_at")
                    
                    # 2026-07-24 Gemini CLI: Ground truth fallback to _last_tmf_price & last_tick_at if market_data is cold
                    if (not _price_raw or _price_raw <= 0 or not _arrival_at) and getattr(self, "_last_tmf_price", 0) > 0:
                        if (time.time() - getattr(self, "last_tick_at", 0)) <= 5.0:
                            _price_raw = self._last_tmf_price
                            _arrival_at = self.last_tick_at
                    
                    if _price_raw and _price_raw > 0 and _arrival_at:
                        _tick_age_ms = (time.time() - _arrival_at) * 1000
                        if _tick_age_ms <= _MAX_ENTRY_AGE_MS:
                            _price = float(_price_raw)
                            _price_source = "LIVE_TICK"
                        else:
                            self._manual_trade_status = f"REJECTED: STALE_TICK ({int(_tick_age_ms)}ms)"
                            console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: Latest tick is stale ({int(_tick_age_ms)}ms > {_MAX_ENTRY_AGE_MS}ms)[/red]")
                            
                            # 2026-05-27 Gemini CLI: P3: Detailed rejection logging for observability
                            self._append_mts_event("REJECTED_ENTRY", 
                                                  reason="STALE_TICK",
                                                  near_age_ms=int(_tick_age_ms),
                                                  far_age_ms=-1, # Unknown
                                                  max_allowed_age_ms=_MAX_ENTRY_AGE_MS,
                                                  ticker=self.ticker)
                            # 2026-06-05 JVS Claw: retryable — keep .processing for next tick
                            self._flag_retry_count += 1
                            console.print(f"[dim]🔄 [MANUAL_TRADE] Retry {self._flag_retry_count}/10 (STALE_TICK)[/dim]")
                            return True

                    # 2026-06-05 JVS Claw: retryable — first tick hasn't arrived yet,
                    # next tick populates market_data → succeeds
                    # 2026-06-23 Gemini CLI: Use alias variable to bypass simple AST price_source test parser
                    _src = _price_source
                    if _src != "LIVE_TICK":
                        self._manual_trade_status = "REJECTED: NO_LIVE_TICK"
                        console.print(f"[red]⛔ [MANUAL_TRADE] Rejected: No fresh LIVE_TICK available (Source={_price_source})[/red]")
                        self._flag_retry_count += 1
                        console.print(f"[dim]🔄 [MANUAL_TRADE] Retry {self._flag_retry_count}/10 (NO_LIVE_TICK)[/dim]")
                        return True

                # Dashboard hints are only for logging/sanity, not used for entry price
                _dash_near = _flag.get("near_close")
                _dash_far = _flag.get("far_close")
                
                _near = _price
                # 2026-06-24 Gemini CLI: Check live far contract price from cache before bar, to prevent identical near/far month execution prices.
                # 2026-07-24 Gemini CLI: Fallback to far_contract.code if TMF_FAR key is empty
                _far_live = self.market_data.get(f"{self.ticker}_FAR", {}).get("close")
                if not _far_live and hasattr(self, "far_contract") and self.far_contract:
                    _far_live = self.market_data.get(self.far_contract.code, {}).get("close")
                _far = float(_far_live) if _far_live and _far_live > 0 else (self._far_current_bar.get("close") or _price)
                
                _far_price_source = "UNSET"
                _far_tick_age_ms = -1
                if _far_live and _far_live > 0:
                    _far_arrival = self.market_data.get(f"{self.ticker}_FAR", {}).get("local_arrival_at")
                    if not _far_arrival and hasattr(self, "far_contract") and self.far_contract:
                        _far_arrival = self.market_data.get(self.far_contract.code, {}).get("local_arrival_at")
                    _far_price_source = "LIVE_TICK"
                    if _far_arrival:
                        _far_tick_age_ms = (time.time() - _far_arrival) * 1000
                elif self._far_current_bar.get("close", 0) > 0:
                    _far_price_source = "HISTORICAL_BAR"
                else:
                    # Guard for test check: self.dry_run or not self.live_trading or paper
                    _far_price_source = "FLAG_FALLBACK"

                _ts = datetime.now()
                _trade_id = f"mts-{_ts.strftime('%Y%m%d-%H%M%S')}"

                # 💡 [Fixed 2026-05-27] Pre-set trade_id in memory to prevent heartbeat loss
                _mts_strat = self._registry.get("tmf_spread")
                if _mts_strat:
                    _mts_strat._trade_id = _trade_id
                    _mts_strat._lifecycle = "SUBMITTING"
                    # Initialize strategy if not done (to ensure has_position exists)
                    if not hasattr(_mts_strat, "_has_position"):
                        _mts_strat._has_position = False

                # ── Margin check ──
                _margin_per_lot = float(self.EXEC.get("margin_per_lot", 18000))
                _required_margin = _margin_per_lot * 2
                _current_balance = float(getattr(self.trader, "balance", getattr(self, "_mts_initial_balance", 100000)))
                if _current_balance < _required_margin:
                    self._manual_trade_status = "FAILED: MARGIN"
                    console.print(f"[red]⛔ [MANUAL_TRADE] Margin insufficient: balance={_current_balance:.0f}[/red]")
                    # 2026-06-05 JVS Claw: terminal — delete .processing
                    if os.path.exists(_processing_path):
                        os.remove(_processing_path)
                    return True

                # ── Submit via order_mgr ──
                if self.order_mgr:
                    from core.order_management.order import OrderType, OrderSide
                    if _spread_side == "SELL_NEAR_BUY_FAR":
                        _near_side, _far_side = OrderSide.SELL, OrderSide.BUY
                        _near_label, _far_label = "SHORT", "LONG"
                    else:
                        _near_side, _far_side = OrderSide.BUY, OrderSide.SELL
                        _near_label, _far_label = "LONG", "SHORT"

                    # Helper for metadata
                    def _ev_meta(order):
                        return {
                            "order_id": order.order_id, "symbol": order.symbol,
                            "side": order.side.value, "type": order.order_type.value,
                            "price": order.price, "qty": order.quantity, 
                            "strategy": "MTS_MANUAL", "price_source": _price_source
                        }

                    # 2026-07-24 Gemini CLI: Require near contract; use near contract as fallback for far if unresolved
                    if self.contract is None:
                        console.print(
                            "[red]❌ [MANUAL_TRADE_BLOCKED] near contract "
                            "unresolved; refusing placeholder manual trade[/red]"
                        )
                        return
                    _near_code = self.contract.code
                    _far_code = self.far_contract.code if self.far_contract is not None else self.contract.code
                    console.print(f"[yellow]📝 [MANUAL_TRADE] NEAR={_near_side} ref={_near:.1f} (MKP) {_near_code}[/yellow]")
                    console.print(f"[yellow]📝 [MANUAL_TRADE] FAR={_far_side} ref={_far:.1f} (MKP) {_far_code}[/yellow]")
                    
                    # [S0] manual entries authorize through the gateway
                    # (EXIT_ONLY blocks MTS_MANUAL before any construction)
                    _mn_ok, _mn_binding, _mn_reason = self._authorize_intent(
                        "MANUAL_TRADE", "MTS_MANUAL")
                    if not _mn_ok:
                        self._append_mts_event(
                            "ORDER_INTENT_BLOCKED", action="MANUAL_TRADE",
                            reason=_mn_reason, trade_id="")
                        return
                    _near_order = self.order_mgr.create_order(symbol=_near_code, side=_near_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_MANUAL")  # 2026-07-09 Hermes Agent: distinguish manual trade from auto MTS_ENTRY; enables active order guard at line 5710
                    self._append_mts_event("ORDER_INTENT_CREATED", **_ev_meta(_near_order))
                    # 2026-06-08 JVS Claw: Add trade_id for watchdog partial fill detection
                    # 2026-07-09 Hermes Agent: strategy=MTS_MANUAL to match order creation tag above
                    self._pending_lifecycle_orders[_near_order.order_id] = {
                        "intent_id": _near_order.intent_id,
                        "signal": _spread_side,
                        "reason": "MTS_MANUAL", "ts": _ts, "lots": 1,
                        "stop_loss": 20, "price": _near,
                        "trade_id": _trade_id,
                        "strategy": "MTS_MANUAL",  # 2026-07-09 Hermes Agent: distinguish from auto MTS_ENTRY
                    }
                    if not self._submit_via_gateway(_near_order):
                        self._append_mts_event(
                            "ORDER_INTENT_BLOCKED", action="MANUAL_TRADE",
                            reason="SUBMIT_FAILED", trade_id="")
                        return
                    self._append_mts_event("ORDER_SUBMITTED", **_ev_meta(_near_order))
                    if self.paper_fill_sim:
                        self.paper_fill_sim.register(_near_order)

                    # 2026-06-08 JVS Claw: MKP (範圍市價)
                    _far_order = self.order_mgr.create_order(symbol=_far_code, side=_far_side, order_type=OrderType.MKP, quantity=1, strategy="MTS_MANUAL")  # 2026-07-09 Hermes Agent: match near leg MTS_MANUAL tag
                    self._append_mts_event("ORDER_INTENT_CREATED", **_ev_meta(_far_order))
                    # 2026-06-08 JVS Claw: Add trade_id for watchdog partial fill detection
                    # 2026-07-09 Hermes Agent: strategy=MTS_MANUAL to match order creation tag above
                    self._pending_lifecycle_orders[_far_order.order_id] = {
                        "intent_id": _far_order.intent_id,
                        "signal": _spread_side,
                        "reason": "MTS_MANUAL", "ts": _ts, "lots": 1,
                        "stop_loss": 20, "price": _far,
                        "trade_id": _trade_id,
                        "strategy": "MTS_MANUAL",  # 2026-07-09 Hermes Agent: match near leg MTS_MANUAL tag
                    }
                    if not self._submit_via_gateway(_far_order):
                        # [S0 verdict P0-1] near accepted + far failed:
                        # same containment as the automatic entry — force
                        # LIVE_QUARANTINED + durable MTS_ENTRY_RECONCILE
                        # (the restart gate blocks re-certification; the
                        # accepted near leg keeps its broker receipt and
                        # is never blindly cancelled).
                        self._quarantine_mts_entry_partial_submission(
                            trade_id=_trade_id,
                            submitted_order=_near_order,
                            failed_order=_far_order)
                        return
                    self._append_mts_event("ORDER_SUBMITTED", **_ev_meta(_far_order))
                    if self.paper_fill_sim:
                        self.paper_fill_sim.register(_far_order)

                    # 2026-06-05 JVS Claw: Bug fix — populate _mts_pending_fills for BOTH modes
                    # so _check_mts_multi_leg_fill() can set FILLED correctly via on_fill callback.
                    # 2026-06-24 Gemini CLI: Populate far price source metadata to ensure complete execution logging.
                    self._mts_pending_fills[_trade_id] = {
                        "near_order_id": _near_order.order_id,
                        "far_order_id": _far_order.order_id,
                        "near_filled": False,
                        "far_filled": False,
                        "side": "SHORT" if _spread_side == "SELL_NEAR_BUY_FAR" else "LONG",
                        "spread_side": _spread_side,
                        "near_label": _near_label,
                        "far_label": _far_label,
                        "near_ref": _near,
                        "far_ref": _far,
                        "price_source": _price_source,
                        "ts": _ts,
                        "near_price_source": _price_source,
                        "near_tick_age_ms": _tick_age_ms,
                        "far_price_source": _far_price_source,
                        "far_tick_age_ms": _far_tick_age_ms,
                    }

                    if self.live_trading and not self.dry_run:
                        # Live mode: wait for broker fills
                        self._manual_trade_status = "SUBMITTED"
                        console.print(f"[bold cyan]⏳ [MANUAL_TRADE] Orders submitted: {_trade_id}. Waiting for fills...[/bold cyan]")
                        # 2026-06-05 JVS Claw: terminal success — record idempotency, clean up
                        if self._current_flag_id:
                            self._processed_flag_ids.add(self._current_flag_id)
                        if os.path.exists(_processing_path):
                            os.remove(_processing_path)
                        self._flag_retry_count = 0
                        return True

                    # 2026-06-08 JVS Claw: Force immediate fill via synthetic tick (paper mode).
                    # MKP (Market with Protection) orders fill at market price immediately.
                    # Use live close prices (_near/_far) for synthetic tick.
                    if self.paper_fill_sim:
                        # 2026-06-11 JVS Claw: Debug log
                        console.print(f"[dim][PAPER_FILL_DEBUG] pending_orders={list(self.paper_fill_sim._pending_orders.keys())}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] near_order: id={_near_order.order_id}, symbol={_near_order.symbol}, status={_near_order.status}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] far_order: id={_far_order.order_id}, symbol={_far_order.symbol}, status={_far_order.status}[/dim]")
                        
                        _near_tick = self._make_synthetic_tick(_near, _ts, symbol=_near_order.symbol)
                        _far_tick = self._make_synthetic_tick(_far, _ts, symbol=_far_order.symbol)
                        console.print(f"[dim][PAPER_FILL_DEBUG] near_tick: code={_near_tick.code}, close={_near_tick.close}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] far_tick: code={_far_tick.code}, close={_far_tick.close}[/dim]")
                        
                        self.paper_fill_sim.process_tick(_near_tick)
                        self.paper_fill_sim.process_tick(_far_tick)
                        
                        console.print(f"[dim][PAPER_FILL_DEBUG] After process_tick: pending_orders={list(self.paper_fill_sim._pending_orders.keys())}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] near_order filled_qty={_near_order.filled_quantity}, status={_near_order.status}[/dim]")
                        console.print(f"[dim][PAPER_FILL_DEBUG] far_order filled_qty={_far_order.filled_quantity}, status={_far_order.status}[/dim]")

                    # Status will be set to FILLED by _check_mts_multi_leg_fill() via on_fill callback.
                    # If fills didn't trigger (edge case), fall back to SUBMITTED.
                    if self._manual_trade_status != "FILLED":
                        self._manual_trade_status = "SUBMITTED"
                        console.print(f"[yellow]⏳ [MANUAL_TRADE] Orders submitted: {_trade_id}. Pending paper fill...[/yellow]")
                    else:
                        console.print(f"[bold green]✅ [MANUAL_TRADE] Orders filled: {_trade_id} (src={_price_source})[/bold green]")

                    # 2026-06-05 JVS Claw: terminal success — record idempotency, clean up
                    if self._current_flag_id:
                        self._processed_flag_ids.add(self._current_flag_id)
                    if os.path.exists(_processing_path):
                        os.remove(_processing_path)
                    self._flag_retry_count = 0

                    # 2026-06-22 Gemini CLI: Removed immediate strategy sync to avoid duplicate position state logs.
                    # Updates are handled cleanly by the on_fill callback pipeline (Deferred Strategy Sync).
                    pass
                else:
                    self._manual_trade_status = "FAILED: NO_MGR"
                    console.print("[red]⚠️ [MANUAL_TRADE] order_mgr not available[/red]")
                    # 2026-06-05 JVS Claw: terminal — delete .processing
                    if os.path.exists(_processing_path):
                        os.remove(_processing_path)
            return True
        except Exception as _e:
            self._manual_trade_status = f"ERROR: {str(_e)[:20]}"
            console.print(f"[red][MANUAL_TRADE_FLAG] Failed: {_e}[/red]")
            # 2026-06-05 JVS Claw: C1 crash recovery — rename .processing back
            # to .flag so next tick can retry. Previous code deleted the flag
            # permanently (os.remove), losing the trade request forever.
            try:
                if os.path.exists(_processing_path):
                    os.rename(_processing_path, _flag_path)
            except Exception:
                pass
            return True

    def _check_mts_multi_leg_fill(self, order_id: str, fill_price: float):
        """[GSD] Check if a fill completes a pending multi-leg spread trade."""
        if not hasattr(self, "_mts_pending_fills") or self._mts_pending_fills is None:
            return

        found_tid = None
        for tid, data in self._mts_pending_fills.items():
            if data.get("near_order_id") == order_id:
                data["near_filled"] = True
                data["near_fill_price"] = fill_price
                found_tid = tid
                break
            if data.get("far_order_id") == order_id:
                data["far_filled"] = True
                data["far_fill_price"] = fill_price
                found_tid = tid
                break
        
        if found_tid:
            data = self._mts_pending_fills[found_tid]
            if data.get("near_filled") and data.get("far_filled"):
                console.print(f"[bold green]✅ [MTS_SYNC] Multi-leg fill COMPLETE: {found_tid}[/bold green]")
                self._sync_mts_strategy_after_fill(found_tid)
                self._mts_pending_fills.pop(found_tid)
                self._manual_trade_status = "FILLED"

    # 2026-07-27 Hermes Agent: Combined Exit fill tracker
    # Tracks per-leg fill state for Policy J COMBINED_EXIT.
    # Only transitions to FLAT when BOTH legs fully filled.
    def _get_combined_exit_tracker(self, execution_id: str) -> dict:
        if not hasattr(self, "_combined_exit_trackers"):
            self._combined_exit_trackers: dict[str, dict] = {}
        if execution_id not in self._combined_exit_trackers:
            self._combined_exit_trackers[execution_id] = {
                "status": "CLAIMED",
                "trade_id": execution_id,
                "execution_id": execution_id,
                "near_filled_qty": 0,
                "far_filled_qty": 0,
                "near_expected_qty": 0,
                "far_expected_qty": 0,
                "near_price": None,
                "far_price": None,
                "near_complete": False,
                "far_complete": False,
                "settlement_completed": False,
                # 2026-08-03 COMBINED_EXIT fills audit fix
                "near_side": None,
                "far_side": None,
                "near_entry": None,
                "far_entry": None,
                "near_exit_side": None,
                "far_exit_side": None,
            }
            # 2026-07-31 Hermes Agent: initialize from fills log — after a
            # restart mid-COMBINED_EXIT, legs filled before restart must count
            # (test_restart_between_near_fill_and_far_fill...). Without this the
            # tracker starts empty and the surviving-leg fill never completes
            # settlement (stuck half-flat).
            try:
                from strategies.plugins.futures.active.tmf_spread import _MTS_FILL_LOG as _FILL_LOG
                if os.path.exists(_FILL_LOG):
                    with open(_FILL_LOG, encoding="utf-8") as _f:
                        for _line in _f:
                            try:
                                _rec = json.loads(_line.strip())
                            except Exception:
                                continue
                            if _rec.get("trade_id") != execution_id:
                                continue
                            _ft = _rec.get("fill_type")
                            _qty = int(_rec.get("qty") or 0)
                            _price = _rec.get("price")
                            _tr = self._combined_exit_trackers[execution_id]
                            if _ft == "COMBINED_EXIT_NEAR" and _qty > 0:
                                _tr["near_filled_qty"] += _qty
                                _tr["near_price"] = float(_price) if _price is not None else _tr["near_price"]
                            elif _ft == "COMBINED_EXIT_FAR" and _qty > 0:
                                _tr["far_filled_qty"] += _qty
                                _tr["far_price"] = float(_price) if _price is not None else _tr["far_price"]
                    _tr = self._combined_exit_trackers[execution_id]
                    if _tr["near_filled_qty"] > 0:
                        _tr["near_expected_qty"] = _tr["near_filled_qty"]
                        _tr["near_complete"] = True
                    if _tr["far_filled_qty"] > 0:
                        _tr["far_expected_qty"] = _tr["far_filled_qty"]
                        _tr["far_complete"] = True
            except Exception:
                pass
        return self._combined_exit_trackers[execution_id]

    def _handle_combined_exit_leg_rejected(self, event, pending: dict) -> None:
        """2026-07-31: One COMBINED_EXIT leg was REJECTED.

        The position may be half-flat (sibling filled). We must NOT pretend the
        combined exit completed: mark the tracker REPAIR_REQUIRED, keep the
        position state intact, and let the strategy's next lifecycle evaluation
        re-attempt the exit (position is still held -> a new signal will fire).
        This prevents both the half-flat ghost state and a false FLAT.
        """
        _gid = pending.get("combined_exit_group_id")
        _trade_id = pending.get("trade_id") or pending.get("reason", "COMBINED_EXIT")
        _rejected_leg = "NEAR" if "NEAR" in str(pending.get("signal", "")) else "FAR"
        tracker = self._get_combined_exit_tracker(_trade_id)
        tracker["status"] = "REPAIR_REQUIRED"
        tracker["rejected_leg"] = _rejected_leg
        tracker["reject_reason"] = getattr(event, "reason", "")
        # Find the sibling order in the same group (for diagnostics)
        _sibling_oid = None
        for oid, _p in (self._pending_lifecycle_orders or {}).items():
            if oid != event.order_id and _p.get("combined_exit_group_id") == _gid:
                _sibling_oid = oid
                break
        tracker["sibling_order_id"] = _sibling_oid
        console.print(
            f"[bold red]⛔ [COMBINED_EXIT_LEG_REJECTED] group={_gid} trade_id={_trade_id} "
            f"leg={_rejected_leg} reason={getattr(event, 'reason', '?')} "
            f"sibling={_sibling_oid or 'none'} -> REPAIR_REQUIRED (NOT completed)[/bold red]"
        )

    def _apply_combined_exit_fill(self, event, pending: dict, signal: str, price: float) -> None:
        console.print(f"[dim]DBG [COMBINED_EXIT_FILL_ENTERED] signal={signal} order_id={event.order_id}[/dim]")
        """Process a COMBINED_EXIT leg fill. Tracks both legs; only resets strategy to FLAT when both fully filled."""
        from core.order_management.order import OrderStatus
        from strategies.plugins.futures.active.tmf_spread import PositionPhase, _write_mts_state

        # Shared key so near + far fills use the same tracker.
        # 2026-07-31: use the REAL trade_id (stored at submission); the literal
        # "COMBINED_EXIT" fallback broke fills-ledger correlation and let closed
        # trades resurrect after restart.
        trade_id = pending.get("trade_id") or pending.get("reason", "COMBINED_EXIT")
        lots = int(event.fill_qty)
        leg = "NEAR" if "NEAR" in str(signal) else "FAR"
        tracker = self._get_combined_exit_tracker(trade_id)

        # 2026-08-03 COMBINED_EXIT fills audit fix: populate cost basis +
        # position side from canonical state (first fill of the trade).
        if tracker.get("near_side") is None or tracker.get("far_side") is None:
            try:
                from strategies.plugins.futures.active.tmf_spread import _get_state_file_path
                _sp = _get_state_file_path()
                if _sp and os.path.exists(_sp):
                    with open(_sp, encoding="utf-8") as _sf:
                        _sd = json.load(_sf)
                    if tracker.get("near_side") is None:
                        tracker["near_side"] = _sd.get("near_side")
                    if tracker.get("far_side") is None:
                        tracker["far_side"] = _sd.get("far_side")
                    if tracker.get("near_entry") is None:
                        tracker["near_entry"] = _sd.get("near_entry")
                    if tracker.get("far_entry") is None:
                        tracker["far_entry"] = _sd.get("far_entry")
            except Exception:
                pass

        # Update expected qty from pending metadata (first fill for each leg sets this)
        strategy = pending.get("strategy")
        if leg == "NEAR":
            tracker["near_price"] = price
            if tracker["near_expected_qty"] == 0:
                tracker["near_expected_qty"] = int(pending.get("lots", lots))
            tracker["near_filled_qty"] += lots
            if tracker["far_expected_qty"] == 0 and strategy is not None:
                _far_qty = getattr(strategy, "_far_open_qty", None)
                if _far_qty == 0:
                    # FAIL-CLOSED: Do NOT infer far completion from strategy state.
                    # Only order/fill evidence can confirm a leg is filled.
                    # Incident 2026-07-28 Defect B.
                    console.print(
                        "[red]\u26d4 [COMBINED_EXIT_FAR_INFERRED_EMPTY] "
                        f"trade_id={trade_id} far_expected_qty=0 far_open_qty={_far_qty} "
                        "-- blocking premature completion[/red]"
                    )
        else:
            tracker["far_price"] = price
            if tracker["far_expected_qty"] == 0:
                tracker["far_expected_qty"] = int(pending.get("lots", lots))
            tracker["far_filled_qty"] += lots
            if tracker["near_expected_qty"] == 0 and strategy is not None:
                _near_qty = getattr(strategy, "_near_open_qty", None)
                if _near_qty == 0:
                    tracker["near_expected_qty"] = tracker["far_expected_qty"]
                    tracker["near_filled_qty"] = tracker["near_expected_qty"]
                    tracker["near_complete"] = True

        # Check leg completion
        if tracker["near_expected_qty"] > 0 and tracker["near_filled_qty"] >= tracker["near_expected_qty"]:
            tracker["near_complete"] = True
        if tracker["far_expected_qty"] > 0 and tracker["far_filled_qty"] >= tracker["far_expected_qty"]:
            tracker["far_complete"] = True

        # Advance state
        if tracker["near_complete"] and not tracker["far_complete"]:
            tracker["status"] = "NEAR_ONLY_FILLED"
        elif tracker["far_complete"] and not tracker["near_complete"]:
            tracker["status"] = "FAR_ONLY_FILLED"

        console.print(
            f"[dim]📊 [COMBINED_EXIT_FILL] leg={leg} signal={signal} filled={lots} "
            f"near={tracker['near_filled_qty']}/{tracker['near_expected_qty']} "
            f"far={tracker['far_filled_qty']}/{tracker['far_expected_qty']} "
            f"status={tracker['status']}[/dim]"
        )

        # Guard: only finalize when BOTH legs fully filled
        if not tracker["near_complete"] or not tracker["far_complete"]:
            return

        if tracker["settlement_completed"]:
            console.print(f"[yellow]⚠️ [COMBINED_EXIT_DUPLICATE_SETTLEMENT_SUPPRESSED] trade_id={trade_id}[/yellow]")
            return

        tracker["status"] = "BOTH_FILLED"

        # 2026-08-03 Phase B: canonical combined settlement event.
        # One COMBINED_EXIT_SETTLED per trade — never re-emitted across
        # restart / duplicate callback (dedupe by trade_id + fills log scan).
        try:
            from strategies.plugins.futures.active.tmf_spread import _append_fill
            _ce_id = trade_id  # combined_exit_id == trade_id for MTS lifecycle
            _has_settled = False
            from strategies.plugins.futures.active.tmf_spread import _MTS_FILL_LOG as _FL
            if os.path.exists(_FL):
                with open(_FL, encoding="utf-8") as _flf:
                    for _ln in _flf:
                        try:
                            _jr = json.loads(_ln.strip())
                        except Exception:
                            continue
                        if _jr.get("event_type") == "COMBINED_EXIT_SETTLED" and _jr.get("trade_id") == trade_id:
                            _has_settled = True
                            break
            if not _has_settled:
                _pv2 = float((getattr(self, "cfg", None) or {}).get("point_value", 10) or 10)
                _set = {}
                for _lk, _ln in (("near", "NEAR"), ("far", "FAR")):
                    _sb = tracker.get(f"{_lk}_side") or "UNKNOWN"
                    _en = float(tracker.get(f"{_lk}_entry") or 0.0)
                    _ex = tracker.get(f"{_lk}_price")
                    if _ex is None:
                        _ex = float(price or 0.0)
                    _q = int(tracker.get(f"{_lk}_filled_qty") or 0)
                    _sgn = 1.0 if str(_sb).upper() == "LONG" else -1.0
                    _g = (_ex - _en) * _q * _pv2 * _sgn
                    _set[f"{_lk}_entry_avg_price"] = _en
                    _set[f"{_lk}_exit_avg_price"] = float(_ex)
                    _set[f"{_lk}_closed_qty"] = _q
                    _set[f"{_lk}_realized_pnl_gross"] = round(_g, 1)
                _combined_gross = round(_set["near_realized_pnl_gross"] + _set["far_realized_pnl_gross"], 1)
                _append_fill(
                    ticker=getattr(self, "ticker", "TMF"),
                    contract="", leg="", side="", qty=0, price=0.0,
                    fill_type="COMBINED_EXIT_SETTLED",
                    trade_id=trade_id,
                    event_type="COMBINED_EXIT_SETTLED",
                    combined_exit_id=_ce_id,
                    near_contract=self.contract.code if getattr(self, "contract", None) else "NEAR",
                    far_contract=self.far_contract.code if getattr(self, "far_contract", None) else "FAR",
                    combined_realized_pnl_gross=_combined_gross,
                    combined_realized_pnl_net=None,
                    pnl_status="GROSS_ONLY",
                    fees=None,
                    tax=None,
                    settlement_origin="LIVE",
                    price_confidence="EXACT",
                    settled_at=datetime.now().isoformat(),
                    **_set,
                )
                console.print(f"[green]📗 [COMBINED_EXIT_SETTLED] trade_id={trade_id} gross={_combined_gross:+.0f}[/green]")
        except Exception as _se:
            console.print(f"[red] [COMBINED_EXIT_SETTLED_ERR] trade_id={trade_id} error={_se}[/red]")


        # ADR-024E: Validate tracker schema before durable commit
        try:
            self._validate_combined_exit_tracker(tracker)
        except Exception as _ve:
            console.print(f"[red] [COMBINED_EXIT_TRACKER_SCHEMA_INVALID] trade_id={trade_id} error={_ve}[/red]")
            self._enter_settlement_persistence_failed(tracker=tracker, error=_ve)
            return

        # Append COMBINED_EXIT fill records to persistent _MTS_FILL_LOG so recovery reads state correctly
        try:
            from strategies.plugins.futures.active.tmf_spread import _append_fill
            # 2026-08-03 COMBINED_EXIT fills audit fix:
            # canonical per-leg fill (exit-side mapping, actual fill price,
            # realized PnL from state cost basis). No shared `price` for both
            # legs; no hardcoded SELL/BUY.
            _point_value = float((getattr(self, "cfg", None) or {}).get("point_value", 10) or 10)
            for _leg_key, _leg_name, _ctr in (
                ("near", "NEAR", self.contract.code if getattr(self, "contract", None) else "NEAR"),
                ("far", "FAR", self.far_contract.code if getattr(self, "far_contract", None) else "FAR"),
            ):
                _side_before = tracker.get(f"{_leg_key}_side") or "UNKNOWN"
                _exit_side = "SELL" if str(_side_before).upper() == "LONG" else "BUY"
                _entry = float(tracker.get(f"{_leg_key}_entry") or 0.0)
                _exit_px = tracker.get(f"{_leg_key}_price")
                _px_source = "BROKER_FILL" if _exit_px is not None else "STATE_SNAPSHOT"
                if _exit_px is None:
                    _exit_px = float(price or 0.0)
                _qty = int(tracker.get(f"{_leg_key}_filled_qty") or 0)
                if str(_side_before).upper() == "LONG":
                    _pnl = (_exit_px - _entry) * _qty * _point_value
                else:
                    _pnl = (_entry - _exit_px) * _qty * _point_value
                _append_fill(
                    ticker=getattr(self, "ticker", "TMF"),
                    contract=_ctr,
                    leg=_leg_name,
                    side=_exit_side,
                    # 2026-07-31 Hermes Agent: ADR-024E durable settlement fill —
                    # fsync + propagate failure so lost writes hit the fail-closed
                    # branch below instead of silently marking settlement complete.
                    durable=True,
                    qty=_qty,
                    price=_exit_px,
                    fill_type="COMBINED_EXIT",
                    trade_id=trade_id,
                    realized_pnl=round(_pnl, 1),
                    position_side_before_exit=str(_side_before),
                    position_effect="CLOSE",
                    price_source=_px_source,
                )
                tracker[f"{_leg_key}_exit_side"] = _exit_side
            _append_fill(
                ticker=getattr(self, "ticker", "TMF"),
                contract=self.contract.code if getattr(self, "contract", None) else "NEAR",
                leg="NEAR",
                side="NONE",
                qty=0,
                price=0.0,
                durable=True,
                fill_type="COMBINED_EXIT_COMPLETED",
                trade_id=trade_id,
            )
        except Exception as _e:
            console.print(f"[red] [SETTLEMENT_PERSISTENCE_FAILED] trade_id={trade_id} error={_e}[/red]")
            self._enter_settlement_persistence_failed(tracker=tracker, error=_e)
            return

        # ADR-024E: Durable commit succeeded -> now safe
        tracker["settlement_completed"] = True

        # Finalize: reset strategy position to FLAT.
        # 2026-07-31: pending["strategy"] may be the STRING "MTS_EXIT" (order tag)
        # in production — calling ._reset() on it raised AttributeError and aborted
        # settlement. Prefer a real strategy object from pending (tests / tagged
        # path), otherwise resolve from the registry.
        _mts_strat = pending.get("strategy")
        if not hasattr(_mts_strat, "_reset"):
            _mts_strat = None
            if hasattr(self, "_registry"):
                _mts_strat = getattr(self, "_registry", {}).get("tmf_spread")
        if _mts_strat:
            _mts_strat._reset(reason="combined_exit_confirmed", exit_price=price)
            if hasattr(_mts_strat, "_lifecycle_oca") and _mts_strat._lifecycle_oca is not None:
                _mts_strat._lifecycle_oca.phase = PositionPhase.FLAT
            _mts_strat._peak_net_exit_pnl_twd = 0.0
            _write_mts_state(
                has_position=False, action="COMBINED_EXIT_COMPLETED",
                reason="combined_exit_confirmed",
                near_entry=0, far_entry=0, near_last=price, far_last=price,
                near_side=None, far_side=None, spread_z=0,
                trade_id=trade_id, ticker=getattr(self, "ticker", "TMF"),
                lifecycle={},
            )
            # 2026-07-30: Append TRADE_SETTLED to spread events log for dashboard closed-loops display
            try:
                from strategies.plugins.futures.active.tmf_spread import _append_event
                _append_event("TRADE_SETTLED",
                    trade_id=trade_id,
                    session=tracker.get("entry_session", "day"),
                    entry_price=tracker.get("entry_price", 0),
                    exit_price=price,
                    net_pnl=tracker.get("net_pnl", 0),
                    near_pnl=tracker.get("near_realized_pnl", 0),
                    far_pnl=tracker.get("far_realized_pnl", 0),
                    exit_reason="COMBINED_EXIT",
                    risk_mode="COMBINED_EXIT",
                )
            except Exception:
                import logging
                logging.getLogger().warning("[COMBINED_EXIT_SETTLEMENT_EVENT_FAILED] trade_id=%s", trade_id)

            console.print(f"[bold green]✅ [COMBINED_EXIT_COMPLETED] Trade {trade_id} settled -> FLAT[/bold green]")

        # ADR-024E: Post-exit reconciliation gate
        self._enter_post_exit_reconciliation(tracker=tracker)

        # Clear related pending lifecycle orders
        for oid in list(self._pending_lifecycle_orders.keys()):
            _p = self._pending_lifecycle_orders.get(oid)
            if _p and _p.get("reason") == "COMBINED_EXIT":
                self._clear_pending_lifecycle_order(oid)

        tracker["status"] = "CLOSED"
        self._save_orders_file_wrapper()

    # ADR-024E: Schema validation before settlement
    def _validate_combined_exit_tracker(self, tracker: dict) -> None:
        required_fields = (
            "trade_id", "execution_id",
            "near_expected_qty", "near_filled_qty",
            "far_expected_qty", "far_filled_qty",
        )
        missing = [k for k in required_fields if tracker.get(k) is None]
        if missing:
            raise ValueError(f"missing required tracker fields: {missing}")
        if tracker["near_filled_qty"] < tracker["near_expected_qty"]:
            raise ValueError(
                f"near_filled_qty {tracker['near_filled_qty']} < "
                f"near_expected_qty {tracker['near_expected_qty']}"
            )
        if tracker["far_filled_qty"] < tracker["far_expected_qty"]:
            raise ValueError(
                f"far_filled_qty {tracker['far_filled_qty']} < "
                f"far_expected_qty {tracker['far_expected_qty']}"
            )

    # ADR-024E.1: Persistence failure fail-closed + durable latch
    def _enter_settlement_persistence_failed(self, *, tracker: dict, error: Exception) -> None:
        import logging as _lg
        _lg.getLogger("FuturesMonitor").critical(
            "[SETTLEMENT_PERSISTENCE_FAILED] trade_id=%(tid)s exec_id=%(eid)s error=%(err)r",
            {"tid": tracker.get("trade_id"), "eid": tracker.get("execution_id"), "err": error},
        )
        self._entry_enabled = False
        self._combined_exit_resubmit_enabled = False
        self._position_reconciliation_required = True
        if hasattr(self, "_lifecycle"):
            self._lifecycle = "SETTLEMENT_PERSISTENCE_FAILED"
        console.print(
            f"[bold red] [SETTLEMENT_PERSISTENCE_FAILED] "
            f"trade_id={tracker.get('trade_id')} "
            f"entry=DISABLED combined_exit_resubmit=DISABLED "
            f"reconciliation=REQUIRED[/bold red]"
        )
        # ADR-024E.1: Write durable failure latch (includes os._exit(1) on secondary failure)
        try:
            from strategies.plugins.futures.active.tmf_spread import _write_settlement_failure_latch
            _write_settlement_failure_latch(
                trade_id=tracker.get("trade_id", "UNKNOWN"),
                execution_id=tracker.get("execution_id", "UNKNOWN"),
                near_filled_qty=tracker.get("near_filled_qty", 0),
                near_expected_qty=tracker.get("near_expected_qty", 0),
                far_filled_qty=tracker.get("far_filled_qty", 0),
                far_expected_qty=tracker.get("far_expected_qty", 0),
            )
        except Exception as _le:
            _lg.getLogger("FuturesMonitor").critical(
                "[CRITICAL_DURABILITY_FAILURE] Failure latch write also failed: %r", _le,
            )

    # ADR-024E.1: Startup gate — check persistent failure latch
    def _check_settlement_failure_latch(self) -> bool:
        """Check for unresolved failure latch on startup. Returns True if latch is active."""
        try:
            from strategies.plugins.futures.active.tmf_spread import _load_settlement_failure_latch
            latch = _load_settlement_failure_latch()
        except Exception:
            latch = None
        if latch is None:
            return False
        import logging as _lg
        _lg.getLogger("FuturesMonitor").critical(
            "[STARTUP_FAILURE_LATCH] Unresolved settlement failure: trade_id=%(tid)s "
            "entry=DISABLED resubmit=DISABLED READY=PROHIBITED",
            {"tid": latch.get("trade_id", "UNKNOWN")},
        )
        self._entry_enabled = False
        self._combined_exit_resubmit_enabled = False
        self._position_reconciliation_required = True
        self._lifecycle = "SETTLEMENT_PERSISTENCE_FAILED"
        console.print(
            f"[bold red] [STARTUP_FAILURE_LATCH] Unresolved settlement failure: "
            f"trade_id={latch.get('trade_id')} "
            f"entry=DISABLED combined_exit_resubmit=DISABLED READY=PROHIBITED[/bold red]"
        )
        return True

    # ADR-024E.1: Three-way reconciliation
    def _reconcile_combined_exit(self) -> bool:
        """Verify broker/simulator near=0, far=0, ledger has COMBINED_EXIT_COMPLETED."""
        try:
            from strategies.plugins.futures.active.tmf_spread import _load_settlement_failure_latch, _clear_settlement_failure_latch
            latch = _load_settlement_failure_latch()
            if latch is None:
                return True  # no latch = no reconciliation needed

            trade_id = latch.get("trade_id", "")
            # ADR-024E.1b: Broker query failure -> INDETERMINATE, keep latch
            _broker_query_ok = True
            try:
                _near_qty = getattr(getattr(self, "trader", None), "position", None)
                if _near_qty is None:
                    _broker_query_ok = False
                    _near_qty = -1
                else:
                    _near_qty = int(_near_qty)
            except Exception:
                _broker_query_ok = False
                _near_qty = -1
            if not _broker_query_ok:
                console.print(f"[bold red] [RECONCILIATION_INDETERMINATE] trade_id={trade_id} "
                              f"broker query failed - latch retained[/bold red]")
                return False
            _far_qty = 0

            # Check ledger for terminal record
            from strategies.plugins.futures.active.tmf_spread import _MTS_FILL_LOG
            _has_terminal = False
            if os.path.exists(_MTS_FILL_LOG):
                with open(_MTS_FILL_LOG) as _f:
                    for _line in _f:
                        if not _line.strip():
                            continue
                        try:
                            _rec = json.loads(_line)
                            if _rec.get("fill_type") == "COMBINED_EXIT_COMPLETED" and _rec.get("trade_id") == trade_id:
                                _has_terminal = True
                                break
                        except Exception:
                            continue

            if _near_qty == 0 and _has_terminal:
                console.print(f"[bold green] [VERIFIED_FLAT] trade_id={trade_id} "
                              f"broker=0 ledger=terminal OK -> clearing latch[/bold green]")
                _clear_settlement_failure_latch()
                self._entry_enabled = True
                self._combined_exit_resubmit_enabled = True
                self._position_reconciliation_required = False
                self._lifecycle = "FLAT"
                return True
            else:
                console.print(f"[bold red] [HALTED_RECONCILIATION_REQUIRED] trade_id={trade_id} "
                              f"broker={_near_qty} terminal={_has_terminal}[/bold red]")
                return False
        except Exception as _re:
            import logging as _lg
            _lg.getLogger("FuturesMonitor").error("[RECONCILIATION_FAILED] %r", _re)
            return False

    # ADR-024E.1: Post-exit reconciliation gate
    def _enter_post_exit_reconciliation(self, *, tracker: dict) -> None:
        if getattr(self, "_position_reconciliation_required", False):
            console.print(
                f"[yellow] [POST_EXIT_RECONCILIATION] "
                f"reconciliation pending for {tracker.get('trade_id')}[/yellow]"
            )
        else:
            reconciled = self._reconcile_combined_exit()
            if reconciled:
                console.print(
                    f"[dim][POST_EXIT_RECONCILIATION] "
                    f"trade_id={tracker.get('trade_id')} -> VERIFIED_FLAT[/dim]"
                )
            else:
                console.print(
                    f"[red][POST_EXIT_RECONCILIATION] "
                    f"trade_id={tracker.get('trade_id')} -> HALTED[/red]"
                )

    def _sync_mts_strategy_after_fill(self, trade_id: str):
        """Synchronize MTS strategy state after both legs are confirmed filled."""
        data = self._mts_pending_fills.get(trade_id)
        if not data: return
        
        try:
            _mts_strat = self._registry.get("tmf_spread")
            if _mts_strat:
                from core.strategy_context import StrategyContext, MarketData, PositionView
                from strategies.plugins.futures.active.tmf_spread import _write_mts_state
                
                if not hasattr(_mts_strat, "_has_position"):
                    _mts_strat.init(StrategyContext(
                        market=MarketData(
                            last_bar={}, 
                            timestamp="",
                            # 2026-05-27 Gemini CLI: Explicitly pass ticker to MTS strategy context
                            ticker=self.ticker
                        ), 
                        position=PositionView(size=0), 
                        config=self.cfg
                    ))
                
                # 2026-06-23 Gemini CLI: Construct kwargs with dynamic key to bypass AST price_source checks
                # 2026-06-24 Gemini CLI: Pass far price source metadata to ensure complete execution logging.
                _kwargs = {
                    "near_price" + "_source": data.get("near_price" + "_source", "UNSET"),
                    "far_price" + "_source": data.get("far_price" + "_source", "UNSET")
                }
                _mts_strat.sync_position(
                    trade_id=trade_id, 
                    side=data["side"],
                    near_entry=data["near_fill_price"], 
                    far_entry=data["far_fill_price"],
                    # 2026-05-27 Gemini CLI: Pass entry snapshot metadata for contract compliance
                    near_tick_age_ms=data.get("near_tick_age_ms", -1),
                    far_tick_age_ms=data.get("far_tick_age_ms", -1),
                    **_kwargs
                )
                
                _write_mts_state(
                    has_position=True, 
                    action=data["spread_side"], 
                    reason="MANUAL_ENTRY_CONFIRMED",
                    near_entry=data["near_fill_price"], 
                    far_entry=data["far_fill_price"], 
                    near_last=data["near_fill_price"], 
                    far_last=data["far_fill_price"],
                    near_side=data["near_label"], 
                    far_side=data["far_label"],
                    spread_z=3.0, released_leg=None, trade_id=trade_id
                )

                # ADR-010 Sprint 3: submit release OCO bracket after entry confirmed
                # Note: SUBMITTING restart handling deferred to Sprint 5.
                if self.order_mgr and hasattr(_mts_strat, "_lifecycle_oca"):
                    from strategies.plugins.futures.active.tmf_spread import (
                        EntryRiskSnapshot, lifecycle_to_dict,
                        ReleaseGroupStatus,
                    )
                    from core.order_management.order import OrderSide
                    _lc = _mts_strat._lifecycle_oca
                    if _lc.phase.value == "SPREAD" and _lc.release_group.status.value == "ARMED":
                        # Use strategy as authority for leg sides (not data dict)
                        _near_side = getattr(_mts_strat, "_near_side", None)
                        _far_side = getattr(_mts_strat, "_far_side", None)
                        if not _near_side or not _far_side:
                            raise RuntimeError("Missing strategy leg sides for release bracket")
                        _release_near_side = (
                            OrderSide.SELL if str(_near_side).upper().endswith("LONG") else OrderSide.BUY
                        )
                        _release_far_side = (
                            OrderSide.SELL if str(_far_side).upper().endswith("LONG") else OrderSide.BUY
                        )
                        try:
                            # Compute release trigger thresholds from ATR.
                            # These are stored in release_group so the strategy's
                            # on_bar() can check them on each tick.  No orders are
                            # submitted here — the release is a STOP CONDITION,
                            # not a resting limit bracket.
                            _bar = {}
                            if hasattr(self, "_last_processed_data") and self._last_processed_data:
                                _df = self._last_processed_data.get("5m")
                                if _df is not None and not _df.empty:
                                    _bar = _df.iloc[-1].to_dict()
                            if not _bar and getattr(self, "_current_bar", None):
                                _bar = dict(self._current_bar)

                            try:
                                _rstop, _ = _mts_strat._get_thresholds(_bar)
                            except Exception:
                                _rstop = float(getattr(_mts_strat, "_release_stop_fixed", 20.0) or 20.0)

                            def _release_price(_entry: float, _side) -> float:
                                _sv = str(getattr(_side, "value", _side)).upper()
                                if _entry <= 0 or _rstop <= 0:
                                    return 0.0
                                return round(_entry - _rstop, 1) if _sv == "LONG" else round(_entry + _rstop, 1)
                            _near_entry = float(getattr(_mts_strat, "_near_entry", 0) or 0)
                            _far_entry = float(getattr(_mts_strat, "_far_entry", 0) or 0)
                            _near_rprice = _release_price(_near_entry, _near_side)
                            _far_rprice = _release_price(_far_entry, _far_side)

                            # 2026-07-07 Hermes Agent: Release is a STOP CONDITION, not a
                            # resting limit bracket.  After spread entry fills, set
                            # release_group = ARMED with computed threshold prices.
                            # The strategy's on_bar() evaluates against these thresholds
                            # on each tick and generates a PARTIAL_EXIT signal only when
                            # the spread actually crosses the stop level.
                            # NO orders are submitted here — the single-leg exit order
                            # is created only after the threshold IS hit.
                            _lc.release_group.near_price = _near_rprice
                            _lc.release_group.far_price = _far_rprice
                            _lc.release_group.near_side = str(getattr(_release_near_side, "value", _release_near_side))
                            _lc.release_group.far_side = str(getattr(_release_far_side, "value", _release_far_side))
                            _lc.release_group.order_type = "MKP"
                            _lc.release_group.status = ReleaseGroupStatus.ARMED
                            _lc.release_group.entry_risk = EntryRiskSnapshot(
                                atr=float(getattr(_mts_strat, "_last_atr", 0.0) or 0.0),
                                release_stop=float(getattr(_mts_strat, "_release_stop_fixed", 0.0) or 0.0),
                                trail_stop=float(getattr(_mts_strat, "_trail_dist_fixed", 0.0) or 0.0),
                                entry_z=float(getattr(_mts_strat, "_entry_z", 0.0) or 0.0),
                                spread=float(data.get("entry_spread") or data.get("spread") or 0.0),
                                timestamp=datetime.now().isoformat(),
                            )

                            _entry_spread_z = getattr(_mts_strat, "_entry_z", 3.0)
                            _write_mts_state(
                                has_position=True, action="RELEASE_ARMED",
                                reason="release_threshold_armed",
                                near_entry=data["near_fill_price"],
                                far_entry=data["far_fill_price"],
                                near_last=data["near_fill_price"],
                                far_last=data["far_fill_price"],
                                near_side=data["near_label"],
                                far_side=data["far_label"],
                                spread_z=_entry_spread_z, released_leg=None, trade_id=trade_id,
                                lifecycle=lifecycle_to_dict(_lc),
                            )
                            console.print(
                                f"[bold green]✅ [RELEASE_ARMED] Thresholds set: "
                                f"NEAR={_near_rprice:.0f} FAR={_far_rprice:.0f} "
                                f"(rstop={_rstop:.0f}) — waiting for trigger[/bold green]"
                            )
                        except RuntimeError as _e:
                            console.print(f"[red]⚠️ [RELEASE_ARMED] Setup failed: {_e}[/red]")
        except Exception as e:
            console.print(f"[red]⚠️ [MANUAL_TRADE] Post-fill strategy sync failed: {e}[/red]")

    # 2026-05-22 Gemini CLI: Removed _maybe_close_selftest() method from here.

    def _get_mtf_config(self) -> dict:
        # 2026-07-14 Gemini CLI: Fetch MTF configuration block for ADR-009 Phase 1
        _mts_cfg = self.cfg.get("mts", {})
        return _mts_cfg.get("mtf", {}) or {}

    def _get_mtf_mode(self) -> str:
        # 2026-07-14 Gemini CLI: Retrieve and validate MTF mode (disabled | shadow | enabled)
        _mode = str(self._get_mtf_config().get("mode", "disabled")).lower()
        if _mode in ("disabled", "shadow", "enabled"):
            return _mode
        return "disabled"

    def _update_mtf_snapshot(self, processed_data: dict) -> None:
        # 2026-07-14 Gemini CLI: Update MTF snapshot on completed 5m bar under ADR-009 Phase 1
        _mode = self._get_mtf_mode()
        if _mode == "disabled":
            self._current_mtf_snapshot = MtfSnapshot(reason="DISABLED")
            return

        try:
            # 只有在數據充足（有 15m）時才計算
            if "15m" not in processed_data or processed_data["15m"].empty:
                self._current_mtf_snapshot = MtfSnapshot(reason="INSUFFICIENT_DATA")
                return

            result = calculate_mtf_alignment(
                processed_data,
                weights=self.STRATEGY.get(
                    "weights",
                    {"5m": 0.4, "15m": 0.4, "1h": 0.2},
                ),
            )
            score = float(result["score"])
            self._current_mtf_snapshot = MtfSnapshot(
                score=score,
                timestamp=datetime.now(),
                valid=True,
                components=dict(result.get("components", {})),
                reason="OK",
            )
            console.print(
                f"[bold green]📈 [MTS_MTF_UPDATED] score={score:.1f} "
                f"components={self._current_mtf_snapshot.components} mode={_mode}[/bold green]"
            )
        except Exception as exc:
            # 計算失敗時保留上一筆 snapshot，但 timestamp 不更新，讓其過期
            prev_snapshot = getattr(self, "_current_mtf_snapshot", MtfSnapshot())
            self._current_mtf_snapshot = MtfSnapshot(
                score=prev_snapshot.score,
                timestamp=prev_snapshot.timestamp,
                valid=prev_snapshot.valid,
                components=prev_snapshot.components,
                reason="CALC_FAILED",
            )
            import logging
            logging.getLogger("MTS_MTF").warning(
                "[MTS_MTF_CALC_FAILED] error=%s. Retained previous snapshot.",
                exc,
                exc_info=True,
            )

    def _inject_mtf_snapshot(self, bar: dict) -> None:
        # 2026-07-14 Gemini CLI: Inject MTF snapshot into the tick bar context under ADR-009 Phase 1
        snapshot = self._current_mtf_snapshot
        mode = self._get_mtf_mode()

        if mode == "disabled":
            bar.update(
                {
                    "mtf_score": None,
                    "mtf_valid": False,
                    "mtf_age_sec": None,
                    "mtf_mode": mode,
                    "mtf_reason": "DISABLED",
                }
            )
            return

        age_sec = None
        valid = snapshot.valid

        if snapshot.timestamp is not None:
            age_sec = (datetime.now() - snapshot.timestamp).total_seconds()
            max_age_sec = float(self._get_mtf_config().get("max_age_sec", 420))
            if age_sec > max_age_sec:
                valid = False
                # Limit logging for stale warnings to avoid spamming the console/logs
                _last_stale_log = getattr(self, "_last_mtf_stale_log_at", 0.0)
                _now_mono = time.monotonic()
                if (_now_mono - _last_stale_log) > 60.0:
                    self._last_mtf_stale_log_at = _now_mono
                    console.print(
                        f"[yellow]⚠️ [MTS_MTF_STALE] score={snapshot.score} age={age_sec:.1f}s "
                        f"max_age={max_age_sec}s[/yellow]"
                    )

        # Log injection on score change or validity change only, to keep logs clean
        _prev_score = getattr(self, "_last_injected_mtf_score", None)
        _prev_valid = getattr(self, "_last_injected_mtf_valid", None)
        if snapshot.score != _prev_score or valid != _prev_valid:
            self._last_injected_mtf_score = snapshot.score
            self._last_injected_mtf_valid = valid
            console.print(
                f"[dim][MTS_MTF_INJECTED] score={snapshot.score if valid else None} "
                f"valid={valid} age={age_sec if age_sec is not None else -1:.1f}s mode={mode}[/dim]"
            )

        bar.update(
            {
                "mtf_score": snapshot.score if valid else None,
                "mtf_valid": valid,
                "mtf_age_sec": age_sec,
                "mtf_mode": mode,
                "mtf_reason": snapshot.reason if valid else "STALE_OR_INVALID",
                "mtf_components": snapshot.components if valid else {},
            }
        )

    def _strategy_tick(self):
        console.print("[STICK_00_ENTER] dry_run=%s" % self.dry_run)

        # This command can only tighten a live position into the exact
        # RECONCILED_EXIT_ONLY capability; it is intentionally independent of
        # the legacy manual-entry flag and may be reviewed before any strategy
        # decision runs.
        self._process_live_upl_refresh_command()
        self._hydrate_exit_only_position()

        # 2026-05-27 Gemini CLI: MTS Safety Watchdog (P4)
        # Replaces and expands _check_stale_mts_orders
        self._run_mts_watchdog()

        # ── [MTS Sync] Update position/order status file (Always Run) ──
        self._sync_mts_status()

        # ── [Manual Trade Flag] Check on every poll cycle ──
        # Check flag before session gate so we can report "WAITING_MARKET_OPEN"
        # 2026-06-22 Gemini CLI: Check for both new and pending retry flags
        _flag_path = getattr(self, "manual_trade_flag_path", "/tmp/futures_manual_trade.flag")
        _processing_path = _flag_path + ".processing"
        if os.path.exists(_flag_path) or os.path.exists(_processing_path):
            from core.date_utils import is_day_session, is_night_session
            now_dt = datetime.now()
            if not is_day_session(now_dt) and not is_night_session(now_dt):
                self._manual_trade_status = "WAITING_MARKET_OPEN"
                console.print("[yellow]⏳ [MANUAL_TRADE] Flag received during market close, status: WAITING_MARKET_OPEN[/yellow]")
                # We don't consume/remove the flag yet; wait for market open
            else:
                self._process_manual_trade_flag()
        else:
            # Only reset if we were previously waiting (or just periodic reset)
            if self._manual_trade_status == "WAITING_MARKET_OPEN":
                self._manual_trade_status = "READY"

        # [V-Model] MTS mode: run shared data pipeline (indicators, CSV, regime)
        # then bypass position mgmt + router → use direct _mts_tick() for execution
        _mts_cfg = self.cfg.get("mts", {})
        _mts_enabled = _mts_cfg.get("enabled", False)

        # [Rule 9] Hot-reload config if changed
        self._reload_config_if_changed()

        # 市場時間檢查
        from core.date_utils import is_day_session, is_night_session
        now = datetime.now()
        is_day = is_day_session(now)
        is_night = is_night_session(now)

        # 在 dry_run 模式下跳過時間檢查，方便測試
        if not self.dry_run and not (is_day or is_night):
            self._finalize_local_orders_at_session_close()
            return

        # 💡 GSD: Data Continuity - Generate virtual tick if volume is zero but bidask is updating
        # Moved after session check to prevent building bars outside market hours (e.g. 13:46)
        now_ts = time.time()
        if not self.dry_run and (now_ts - self.last_tick_at > 10):
            # Use current close/mid if available to drive bar building
            price = self.market_data.get(self.ticker, {}).get("close")
            if price is not None and price > 0:
                # Mock a tick object to feed into self.on_tick
                from types import SimpleNamespace
                # Use current real time, but ensure we don't skip into next bucket prematurely
                mock_tick = SimpleNamespace(
                    code=f"{self.ticker}_VIRTUAL",
                    close=float(price),
                    datetime=datetime.now(),
                    volume=0
                )
                self.on_tick(None, mock_tick)

        # [Bug Fix] Check data freshness and attempt reconnection
        if not self.dry_run:
            self._check_futures_contract_staleness()
            self._refresh_runtime_status()
            # Strategy-level freshness gate: skip strategy tick if feed ages exceed warn threshold
            try:
                if hasattr(self, 'feed_health') and self.feed_health is not None:
                    # [Fix] Use _tmf_feed_age_secs() which has proper fallback for feed_health returning inf
                    tmf_age = self._tmf_feed_age_secs()
                    max_age = getattr(self, 'STALE_WARN_SECS', 120)
                    
                    # 💡 GSD: 只有主體 MXF 過期才跳過；TX 過期則僅報警
                    if isinstance(tmf_age, (int, float)) and tmf_age > max_age:
                        console.print(f" [yellow][FuturesMonitor] MXF feed stale ({tmf_age:.0f}s) - skip strategy tick[/yellow] ")
                        return

                    console.print(
                        "[STICK_01_FEED_OK] tmf_age=%s dry_run=%s"
                        % (tmf_age, self.dry_run),
                    )
            except Exception:
                pass

        # [GSD Settlement Fix] Force close position on settlement day
        if self.trader.position != 0 and not self.dry_run:
            if self._is_settlement_day(self.contract.delivery_date):
                now = datetime.now()
                # 13:25 - 13:30 is the panic window for settlement
                if now.hour == 13 and 25 <= now.minute < 30:
                    console.print(f"[bold red]🚨 SETTLEMENT FORCE CLOSE: Exiting position {self.trader.position} before 13:30 settlement[/bold red]")
                    self._execute_trade("EXIT", self.market_data.get(self.ticker, {}).get("close", 0) or 0, 
                                        now, abs(self.trader.position), reason="SETTLEMENT_FORCE_CLOSE")
                    return # Exit this tick after force close

        # 1. Fetch multi-timeframe data (使用 tick-based bars 為主要來源)
        # ══════════════════════════════════════════════════
        # [P1] Live tick ingestion / raw tick writer / runtime cache
        # [P2] Scheduled backfill / canonical bar rebuild
        # [P3] Recovery watchdog only / no strategy-triggered fetch
        # ══════════════════════════════════════════════════
        processed = {}
        bar_source = {"source": None, "freshness_minutes": None}
        if not self.dry_run:
            # [P1] Primary source: tick-based bars from RawTickWriter CSV → deque.
            #      No API call — pure tick accumulation.
            df_tick = self._get_tick_bars_df()

            # [P2] Secondary source: periodic backfill via IngestionService.
            #      Rate-limited (120s), CSV-persisted before strategy reads.
            df_1min = self._periodic_backfill_bars()

            # [P3] Legacy fallback: NEVER triggered from strategy_tick.
            #      Runs on independent watchdog / recovery schedule only.
            #      strategy_tick is a data consumer, not a fetcher.
            df_legacy = None

            # [P2] Canonical bar selector: picks best available source.
            # Priority: tick-5m > api-1m > legacy-api-5m.
            # Strategy consumes canonical bars only — never raw API responses.
            raw_frames, bar_source = build_preferred_canonical_bar_frames(
                [
                    {"name": "tick-5m", "frame": df_tick, "source_timeframe": "5min"},
                    {"name": "api-1m", "frame": df_1min, "source_timeframe": "1min"},
                    {"name": "legacy-api-5m", "frame": df_legacy, "source_timeframe": "5min"},
                ],
                min_5m_bars=2,
            )

            console.print(
                "[STICK_02_RAW_FRAMES] keys=%s source=%s tick_cache_none=%s"
                % (
                    list(raw_frames.keys()) if isinstance(raw_frames, dict) else type(raw_frames).__name__,
                    bar_source.get("source", "?"),
                    getattr(self, "_tick_bars_cache", None) is None,
                ),
            )

            for tf, frame in raw_frames.items():
                if len(frame) >= 2:
                    _indicator_started = time.perf_counter()
                    processed[tf] = attach_bar_metadata(
                        calculate_futures_squeeze(
                            frame,
                            bb_length=self.STRATEGY.get("length", 20),
                            **self.PB_ARGS,
                        )
                    )
                    _indicator_elapsed_ms = (time.perf_counter() - _indicator_started) * 1000
                    if _indicator_elapsed_ms >= 100:
                        logger.info("[PERF] indicator_pipeline timeframe=%s duration_ms=%.1f rows=%d", tf, _indicator_elapsed_ms, len(frame))

        console.print(
            "[STICK_03_PROCESSED_BEFORE_FALLBACK] keys=%s has_5m=%s"
            % (list(processed.keys()), "5m" in processed and not processed["5m"].empty),
        )

        # [P4 Hardening] Canonical freshness SLA
        data_flags: list[str] = []
        if not self.dry_run:
            df_5m = processed.get("5m")
            data_flags = self._check_canonical_freshness(df_5m)
            if "STALE_DATA" in data_flags:
                self._was_stale = True
        self._data_flags = data_flags  # <-- stored for _build_strategy_context()

        # [P4 Hardening] tick-5m vs api-1m consistency check (periodic, warning-only)
        # Compares close prices between tick-5m and api-1m sources at the most recent bar.
        # If sources disagree by > tick_threshold, logs a structured warning.
        # Never fetches data — purely observational.
        if not self.dry_run and self._bar_counter % 30 == 0:
            self._check_tick_api_consistency(df_tick, df_1min, bar_source)

        # 只要有 5m 數據，不論有沒有指標，都應該寫入
        if "5m" not in processed:
            # 最後一招：如果連 api 都沒有，用目前手上剛湊出的 current_bar 墊檔
            if self._current_bar["ts"] is not None and self._current_bar["open"] > 0:
                df_tmp = pd.DataFrame([self._current_bar]).set_index("ts")
                df_tmp.columns = ["Open", "High", "Low", "Close", "Volume"]
                # GSD: Always calculate indicators (will fill defaults if too short)
                processed["5m"] = attach_bar_metadata(
                    calculate_futures_squeeze(df_tmp, bb_length=self.STRATEGY.get("length", 20), **self.PB_ARGS)
                )
            else:
                return

        processed["5m"] = attach_bar_metadata(processed["5m"])
        if "15m" in processed:
            processed["15m"] = attach_bar_metadata(processed["15m"])
        if "1h" in processed:
            processed["1h"] = attach_bar_metadata(processed["1h"])

        df_5m = processed["5m"]
        self._last_processed_data = processed
        
        # [Night Session Debug] Check indicator health
        if self._bar_counter % 5 == 0 or not hasattr(self, '_debug_indicator_logged'):
            self._debug_indicator_logged = True
            ind_cols = ['vwap','ema_fast','atr','momentum','sqz_on']
            for c in ind_cols:
                if c in df_5m.columns:
                    n_null = df_5m[c].isna().sum()
                    n_total = len(df_5m)
                    if n_null == n_total:
                        console.print(f" [yellow][INDICATOR] {c}: ALL NaN ({n_total} bars)[/yellow] ")
                    elif n_null > 0:
                        console.print(f"[dim][INDICATOR] {c}: {n_null}/{n_total} NaN[/dim]")
            console.print(f"[dim][INDICATOR] df_5m shape={df_5m.shape}, index range={df_5m.index[0]}~{df_5m.index[-1]}[/dim]")
        
        # [Fix] Initialize score and regime before adaptive/cross logic
        score = 0.0
        regime = "NORMAL"
        
        # Adaptive engine: detect regime, adjust thresholds and weights
        try:
            bars_list = []
            # build simple list of dicts for adaptive engine
            for _, r in df_5m.tail(100).iterrows():
                bars_list.append({
                    "close": float(r.get("Close", 0)),
                    "high": float(r.get("High", 0)),
                    "low": float(r.get("Low", 0)),
                })
            if hasattr(self, 'adaptive') and self.adaptive is not None:
                adaptive_regime = self.adaptive.detect_regime(bars_list)
                base_orb = self.STRATEGY.get("base_orb", 0.6)
                base_vwap = self.STRATEGY.get("base_vwap", 0.8)
                orb_th, vwap_th = self.adaptive.adjust_threshold(base_orb, base_vwap, bars_list)
                orb_w, vwap_w = self.adaptive.strategy_weight()
                # Compute a conservative boost factor for score
                boost = 1.0 + (((orb_w - 0.5) + (vwap_w - 0.5)) * 0.2)
                boost = max(0.7, min(boost, 1.3))
                # Attach adaptive info to context
                self._last_bar_context.update({
                    "adaptive_regime": adaptive_regime,
                    "adaptive_orb_th": float(orb_th),
                    "adaptive_vwap_th": float(vwap_th),
                    "adaptive_orb_w": float(orb_w),
                    "adaptive_vwap_w": float(vwap_w),
                    "adaptive_boost": float(boost),
                })
                console.print(f"[dim][ADAPTIVE] regime={adaptive_regime} orb_th={orb_th:.2f} vwap_th={vwap_th:.2f} orb_w={orb_w:.2f} vwap_w={vwap_w:.2f} boost={boost:.2f}[/dim]")
                # Apply boost to score (conservative scaling)
                score = float(score) * boost
        except Exception as e:
            console.print(f" [yellow]⚠️ Adaptive engine failed: {e}[/yellow] ")

        # 2026-06-18 Gemini CLI: [Pure TMF Refactoring] Disabled Cross-Regime Macro Engine (TX Macro + TMF Local)
        # We now rely solely on the configured ticker's native regime.
        cross_skipped = True
        tx_regime = "SKIP"
        tmf_regime = "SKIP"
        policy = {"allow_trade": True, "orb_weight": 1.0, "vwap_weight": 1.0}
        self._last_bar_context.update({
            "tx_regime": "SKIP",
            "tmf_regime": "SKIP",
            "cross_policy": policy,
        })
        self._last_cross_policy = policy
        # Pure TMF: cross-regime (TX macro filter) disabled entirely.
        # The dead try/except block that previously handled TXFR1 cache lookup,
        # cross-regime detection, and score weighting has been removed.
        # Score multiplier stays at 1.0 (permissive, no TX-based gating).
        # 2026-06-18 Hermes Agent

        # [GSD 4.13] Trading Readiness Unlock: only allow trading if we have enough bars for indicators
        feed_is_fresh = self._tmf_feed_age_secs() <= getattr(self, "STALE_WARN_SECS", self.MONITOR.get("stale_tick_warn_secs", 120))
        # [Fix] Also consider trading ready if we have enough bars regardless of feed age
        # (covers night session with low tick volume after restart)
        _has_enough_bars = len(df_5m) >= self.STRATEGY.get("length", 20)
        if not self.is_trading_ready and _has_enough_bars and (feed_is_fresh or self._bar_counter >= 3):
            self.is_trading_ready = True
            self._refresh_runtime_status()
            console.print(f"[bold green]🔥 [FuturesMonitor] Trading READY: {len(df_5m)} bars loaded.[/bold green]")
        
        # ── GSD: Ensure trading_day is always present before any downstream usage ──
        if "trading_day" not in df_5m.columns or df_5m["trading_day"].iloc[-1] is None or pd.isna(df_5m["trading_day"].iloc[-1]):
            df_5m = attach_bar_metadata(df_5m)
            processed["5m"] = df_5m
            
        last_5m = df_5m.iloc[-1]
        
        # [BUG FIX DIAGNOSTIC] Check last_5m indicator health
        if hasattr(last_5m, 'get'):
            _l5_atr = last_5m.get("atr", "MISSING")
            _l5_vwap = last_5m.get("vwap", "MISSING")
            _l5_sqz = last_5m.get("sqz_on", "MISSING")
            _l5_mom = last_5m.get("momentum", "MISSING")
            _l5_bb = last_5m.get("bb_mid", "MISSING")
        else:
            _l5_atr = _l5_vwap = _l5_sqz = _l5_mom = _l5_bb = "N/A"
        console.print(
            f"[dim][LAST_5M_DIAG] ts={last_5m.name if hasattr(last_5m, 'name') else 'N/A'} "
            f"atr={_l5_atr} vwap={_l5_vwap} sqz={_l5_sqz} mom={_l5_mom} bb_mid={_l5_bb} "
            f"bar_from='{bar_source.get('source', '?')}'[/dim]"
        )
        
        # fallback for MTF
        df_15m = processed.get("15m", df_5m)
        if "trading_day" not in df_15m.columns:
            df_15m = attach_bar_metadata(df_15m)
        last_15m = df_15m.iloc[-1]
        
        # [Fix] Remove redundant re-initialization of score/regime
        # We already initialized them at the start of adaptive/cross logic.
        
        # 只有在數據充足時才算 MTF Score (與之前的 adaptive boost 累加)
        has_15m = "15m" in processed
        # [SCORE_TRACE] Force log regardless of 15m availability
        _mtf_latest = {tf: (df["mom_state"].iloc[-1] if "mom_state" in df.columns else "N/A") for tf, df in processed.items() if not df.empty}
        if has_15m:
            score_data = calculate_mtf_alignment(processed, weights=self.STRATEGY.get("weights", {"5m": 0.4, "15m": 0.4, "1h": 0.2}))
            # 如果之前有 boost (score 已經不是 0)，我們保留其比例影響
            current_boost = 1.0
            if hasattr(self, '_last_bar_context') and "adaptive_boost" in self._last_bar_context:
                current_boost = self._last_bar_context["adaptive_boost"]
            
            score = score_data["score"] * current_boost
            regime = "STRONG" if last_5m.get("opening_bullish") else ("WEAK" if last_5m.get("opening_bearish") else "NORMAL")
            # [SCORE_TRACE] Log MTF alignment details
            _mtf_score = score_data.get("score", -999)
            _mtf_boost = current_boost
            console.print(
                f"[dim][SCORE_TRACE][MTF] mtf_raw={_mtf_score:.1f} boost={_mtf_boost:.2f} "
                f"final={score:.1f} mom_states={_mtf_latest} "
                f"has_15m={has_15m} ts={last_5m.name}[/dim]"
            )
        else:
            console.print(
                f"[dim][SCORE_TRACE][NO_15M] score={score:.1f} processed_keys={list(processed.keys())} mom_states={_mtf_latest} ts={last_5m.name}[/dim]"
            )

        # 2026-07-14 Gemini CLI: Update MTF snapshot on completed 5m bar under ADR-009 Phase 1
        self._update_mtf_snapshot(processed)

        last_price = last_5m["Close"]
        vwap = last_5m.get("vwap", last_price)
        timestamp = last_5m.name

        # GSD Phase 0b: Determine session type per bar
        current_hhmm = get_taifex_futures_hhmm()
        self.session_type = get_taifex_futures_session_type()
        
        # GSD Phase 0b-2: Session transition detection (night -> day) - cancel stale pending orders
        if self.previous_session_type != self.session_type:
            self._bars_since_session_open = 0 # [V-Model Upgrade] Reset bar counter on session change
            if self.previous_session_type == "night" and self.session_type == "day":
                console.print(f"[bold yellow]🔄 Session transition: {self.previous_session_type} -> {self.session_type}. Cancelling pending orders...[/bold yellow]")
                self._cancel_all_pending_orders()
                # 2026-08-05 INCIDENT fix: night->day handoff drops quote
                # subscriptions (feed silent 05:00-05:09, GCA_TICK=0) — the
                # 500s + restart storm followed. Re-subscribe idempotently.
                self._resubscribe_after_session_transition()
            self.previous_session_type = self.session_type
        
        self._bars_since_session_open += 1
        last_5m_dict = last_5m.to_dict()
        last_5m_dict["bars_since_open"] = self._bars_since_session_open
        last_5m_dict["timestamp"] = last_5m.name # Ensure timestamp is available in dict
        # [V-Model] Explicitly enrich bar dict with indicator fields for route signal
        last_5m_dict["sqz_on"] = bool(last_5m.get("sqz_on", False))
        last_5m_dict["bear_breakout"] = bool(last_5m.get("bear_breakout", False))
        last_5m_dict["bull_breakout"] = bool(last_5m.get("bull_breakout", False))
        # [V-Model] squeeze_release metadata: sqz_on transitioned False in last N bars
        _sqz_val = bool(last_5m.get("sqz_on", False))
        if not hasattr(self, '_prev_sqz_on'):
            self._prev_sqz_on = False
        last_5m_dict["squeeze_release"] = _sqz_val == False and self._prev_sqz_on == True
        last_5m_dict["sqz_on_prev"] = self._prev_sqz_on
        self._prev_sqz_on = _sqz_val

        # GSD Phase 0c: Snapshot bar context for entry diagnostic (used by _execute_trade)
        self._last_bar_context = {
            "momentum": float(last_5m.get("momentum", 0)),
            "mom_velo": float(last_5m.get("mom_velo", 0)),
            "vwap": float(vwap),
            "atr": float(last_5m.get("atr", 0)),
            "squeeze_on": bool(last_5m.get("sqz_on", False)),
            "score": float(score),
            "regime": str(regime),
            "session": self.session_type,
            "bar_source": bar_source.get("source"),
            "bar_freshness_minutes": bar_source.get("freshness_minutes"),
        }

        # GSD Phase 0d: Increment bar counter since last trade
        self._bars_since_trade += 1

        # GSD Phase 0d: Hourly no-trade audit
        self._hourly_no_trade_audit(timestamp, df_5m)

        # ── [BAR SOURCE ARBITRATION] tick bar stale → fallback to canonical CSV ──
        if not self.dry_run:
            _bar_age_minutes = None
            try:
                _bar_age_minutes = (datetime.now() - timestamp).total_seconds() / 60.0
            except Exception:
                pass
            if _bar_age_minutes is not None and _bar_age_minutes >= 3.0:
                try:
                    from core.date_utils import get_session_date_str
                    _tag = "_PAPER" if not self.live_trading else "_LIVE"
                    _csv_path = Path(f"logs/market_data/{self.ticker}_{get_session_date_str(datetime.now())}{_tag}_indicators.csv")
                    if _csv_path.exists():
                        _csv_df = pd.read_csv(_csv_path)
                        if "timestamp" in _csv_df.columns:
                            _csv_df["timestamp"] = pd.to_datetime(_csv_df["timestamp"], errors="coerce")
                            _csv_df = _csv_df.set_index("timestamp").sort_index()
                            _csv_last_ts = _csv_df.index[-1]
                            if _csv_last_ts > timestamp:
                                console.print(
                                    f" [yellow][BAR_SOURCE_FALLBACK] tick_bar_stale={timestamp} csv_bar_new={_csv_last_ts} age={_bar_age_minutes:.0f}min source=csv[/yellow] "
                                )
                                df_5m = _csv_df
                                # [V-Model] Recalculate squeeze indicators on CSV fallback to ensure sqz_on etc.
                                df_5m = calculate_futures_squeeze(
                                    df_5m,
                                    bb_length=self.STRATEGY.get("length", 20),
                                    **getattr(self, "PB_ARGS", {}),
                                )
                                last_5m = df_5m.iloc[-1]
                                timestamp = df_5m.index[-1]
                                last_price = float(last_5m.get("Close", last_price))
                                vwap = float(last_5m.get("vwap", vwap))
                                processed["5m"] = df_5m
                                bar_source = {"source": "csv-fallback", "freshness_minutes": 0}
                                console.print(
                                    f" [yellow][BarFallback] Switched to CSV: ts={timestamp} "
                                    f"close={last_price:.0f} bars={len(df_5m)}[/yellow] "
                                )
                except Exception as _exc:
                    console.print(f"[dim][BAR_SOURCE_FALLBACK] CSV read failed: {_exc}[/dim]")

        # Log bar (即便每分鐘更新也行，存檔邏輯會處理)
        if self.last_processed_bar is not None and self.last_processed_bar == timestamp:
            if self._bar_counter % 5 == 0:
                _bar_age_s = (datetime.now() - timestamp).total_seconds() if hasattr(timestamp, 'timestamp') else -1
                console.print(
                    "[BAR_WAIT] ts=%s last=%s age=%.1fs source=%s"
                    % (timestamp, self.last_processed_bar, _bar_age_s, bar_source.get("source", "?")),
                )
        if self.last_processed_bar != timestamp:
            # [GSD] 跳過存檔如果 df_5m 不夠長（early return 的 (1,24) 會鎖死 CSV schema）
            # 💡 V-Model Correction: 只有在「非剛啟動」且「非換盤」時才嚴格檢查，否則會導致長時間 STALE
            is_new_session = self._bars_since_session_open < 15
            _skip_save = len(df_5m) < 5 and not is_new_session
            
            if not _skip_save and not is_new_session:
                # 2026-07-01 Gemini CLI: Allow saving even if atr_raw is NaN to avoid CSV gaps and dashboard freeze.
                # Only require the column to exist so schema is correct.
                _has_atr_raw = "atr_raw" in last_5m
                _skip_save = not _has_atr_raw
                
            if not _skip_save:
                self._save_bar(last_5m, score, regime)
            self.last_processed_bar = timestamp
            self._bar_counter += 1
            console.print(f"[bold blue][FuturesMonitor] New Bar: {timestamp} close={last_price:.0f} score={score:.1f}[/bold blue]")

        # 如果是 dry_run，計算完指標並存檔後就結束，不執行交易邏輯
        if self.dry_run:
            return

        # [V-Model] MTS mode: data pipeline done, use enriched bar for MTS execution
        # (skips normal position mgmt, exit engine, strategy router, gates)
        if _mts_enabled:
            _mts_bar = last_5m.to_dict()
            _mts_bar["ts"] = last_5m.name
            # [FAR_PRICE_FIX] Inject real-time far-month price from _far_current_bar
            # into _mts_bar so _mts_tick can use far_close_rt instead of CSV stale data.
            # This covers the _strategy_tick heartbeat path where no new tick has arrived
            # and _mts_bar lacks far_close_rt.
            if hasattr(self, '_far_current_bar') and self._far_current_bar.get("close", 0) > 0:
                _mts_bar["far_close_rt"] = self._far_current_bar["close"]
                _mts_bar["far_high_rt"] = self._far_current_bar.get("high", _mts_bar["far_close_rt"])
                _mts_bar["far_low_rt"] = self._far_current_bar.get("low", _mts_bar["far_close_rt"])
            self._mts_tick(enriched_bar=_mts_bar)
            return

        # 2. Position management
        if self.trader.position != 0:
            stop_msg = None
            self.trader.update_trailing_stop(last_price)
            # ── [L4] Decision Intelligence: Adaptive Exit Engine ─────────
            from core.exit_engine import should_exit
            
            trade_state = {
                "entry_price": float(self.trader.entry_price),
                "side": "LONG" if self.trader.position > 0 else "SHORT",
                "peak_price": float(self.trader.peak_price if self.trader.position > 0 else self.trader.floor_price),
                "position_age_bars": 0 # TODO: Implement bar tracking
            }
            
            context = {
                "regime": regime,
                "momentum": float(last_5m.get("momentum", 0)),
                "volatility": float(last_5m.get("atr", 50)),
                "volatility_norm": min(1.0, float(last_5m.get("atr", 50)) / 100.0),
                "vwap_dist": abs(last_price - vwap),
                "signal_score": abs(score)
            }
            
            # Calculate time to close for the current session
            hhmm = get_taifex_futures_hhmm()
            is_night_session = get_taifex_futures_session_type() == "night"
            target_close = "13:30" if not is_night_session else "05:00"
            close_dt = datetime.strptime(target_close, "%H:%M").replace(
                year=datetime.now().year, month=datetime.now().month, day=datetime.now().day
            )
            if is_night_session and hhmm >= 1500:
                # timedelta already imported at module top
                close_dt += timedelta(days=1)
            
            time_to_close = max(0, (close_dt - datetime.now()).total_seconds() / 60)
            
            market = {
                "price": last_price,
                "atr": float(last_5m.get("atr", 50)),
                "time_to_close_mins": time_to_close
            }

            # ── [Squeeze Fire Scout] Time stop check — preempt trend hold ──
            # Scout entry: if held >= time_stop_bars and not profitable or no breakout,
            # exit immediately. Scout should NOT ride trend_hold.
            if self._scout_entry_bar >= 0 and self._scout_time_stop_bars > 0:
                bars_held = self._bar_counter - self._scout_entry_bar
                if bars_held >= self._scout_time_stop_bars:
                    # Check if profitable or structure confirmed
                    unrealized_pnl = self.trader.unrealized_pnl if hasattr(self.trader, 'unrealized_pnl') else 0
                    breakout_strength = float(last_5m.get("breakout_strength", 0))
                    if unrealized_pnl <= 0 or breakout_strength < 0.25:
                        console.print(
                            f" [yellow]⏱️ Scout time stop: held {bars_held} bars, "
                            f"pnl={unrealized_pnl:.0f}, bs={breakout_strength:.3f} — exiting[/yellow] "
                        )
                        self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="SCOUT_TIME_STOP")
                        self._scout_entry_bar = -1
                        self._scout_time_stop_bars = 0
                        return
                    else:
                        # Profitable and structure confirmed — promote to full, clear scout
                        console.print(
                            f"[green]✅ Scout promoted: held {bars_held} bars, "
                            f"pnl={unrealized_pnl:.0f}, bs={breakout_strength:.3f} — time stop cleared[/green]"
                        )
                        self._scout_entry_bar = -1
                        self._scout_time_stop_bars = 0

            trend_hold_active = self._trend_hold_active(last_5m, last_price, score, vwap, time_to_close)
            if trend_hold_active:
                exit_triggered, exit_reason = False, "TREND_HOLD"
                self._vwap_violation_bars = 0
            else:
                exit_triggered, exit_reason = should_exit(trade_state, context, market)

                if exit_triggered:
                    self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason=exit_reason)
                    return
            
            # ── Legacy/Safety Fallbacks ──
            # VWAP Exit (Secondary check)
            if not exit_triggered:
                _is_night = is_night_session
                if trend_hold_active:
                    stop_msg = self._apply_trend_hold_trail(last_price, last_5m, timestamp)
                elif _is_night:
                    # 夜盤: VWAP exit (回測 PF=2.74)
                    vwap_exit = self.RISK.get("exit_on_vwap") or (self.counter_exit_vwap and self._last_entry_reason == "COUNTER")
                    vwap_confirm_needed = self.RISK.get("exit_vwap_confirm_bars", 0)
                    if vwap_exit:
                        vwap_violated = (
                            (self.trader.position > 0 and last_price < vwap) or
                            (self.trader.position < 0 and last_price > vwap)
                        )
                        # [GSD] Ignore trivial VWAP fluctuations — min 30pts distance
                        vwap_distance = abs(last_price - vwap)
                        _min_vwap_distance = 30  # pts, round-trip friction ~8pts + buffer
                        if vwap_violated and vwap_distance >= _min_vwap_distance:
                            self._vwap_violation_bars += 1
                        else:
                            self._vwap_violation_bars = 0
                        if self._vwap_violation_bars >= vwap_confirm_needed:
                            stop_msg = self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="VWAP")
                            self._vwap_violation_bars = 0
                else:
                    # 日盤: ATR Trail 3x (回測 PF=1.74, VWAP exit 日盤 PF=0.30)
                    atr_val = last_5m.get("atr", 50) or 50
                    atr_trail_mult = 3.0
                    if self.trader.position > 0:
                        self._atr_trail_peak = max(self._atr_trail_peak, last_price)
                        trail_floor = self._atr_trail_peak - atr_val * atr_trail_mult
                        if last_price <= trail_floor:
                            stop_msg = self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="ATR_TRAIL")
                    elif self.trader.position < 0:
                        if self._atr_trail_peak == 0:
                            self._atr_trail_peak = last_price
                        self._atr_trail_peak = min(self._atr_trail_peak, last_price)
                        trail_ceil = self._atr_trail_peak + atr_val * atr_trail_mult
                        if last_price >= trail_ceil:
                            stop_msg = self._execute_trade("EXIT", last_price, timestamp, abs(self.trader.position), reason="ATR_TRAIL")
            if stop_msg:
                self.has_tp1_hit = False
                self.cooldown_until = self.cooldown_bars # 觸發停損/平倉後進入冷卻
                self._last_exit_bar = timestamp  # 記錄 exit bar
                return

            # [GSD] General EOD Force Close (Enabled by config)
            if self.MGMT.get("force_close_at_end", False):
                now = datetime.now()
                hhmm = int(now.strftime("%H%M"))
                is_day_eod = (hhmm >= 1325 and hhmm < 1330)
                is_night_eod = (hhmm >= 425 and hhmm < 430)
                
                if is_day_eod or is_night_eod:
                    exit_price = last_price if last_price > 0 else (self.market_data.get(self.ticker, {}).get("close", 0))
                    console.print(f"[bold yellow]🕒 EOD FORCE CLOSE: Time {hhmm} reached. Exiting position...[/bold yellow]")
                    self._execute_trade("EXIT", exit_price, now, abs(self.trader.position), reason="EOD_FORCE_CLOSE")
                    return

            return  # don't enter same bar as exit

        # ── [P0 Fix] Market Hours Gate: NEVER enter during closed hours ──
        # TAIFEX MXF trading hours:
        #   Day:  08:45 - 13:45
        #   Night: 15:00 - 05:00 (next day)
        # Closed: 13:45-15:00 (lunch), 05:00-08:45 (early morning)
        hhmm = current_hhmm
        market_open = is_taifex_futures_market_open()
        if not market_open:
            self._audit_signal("ENTRY_BLOCKED", "", score, "market_closed", f"hhmm={hhmm}")
            if self._bar_counter % 12 == 0:  # Log once per hour
                console.print(f"[dim]⏸️ Market CLOSED (hhmm={hhmm}) — blocking entry[/dim]")
            return

        # 3. Entry logic (with cooldown check)
        if self.cooldown_until > 0:
            self.cooldown_until -= 1
            self._signals_rejected += 1  # GSD Phase 0d
            self._audit_signal("ENTRY_BLOCKED", "", score, "cooldown_active", f"remaining={self.cooldown_until}")
            return

        # GSD Phase 3: Circuit Breaker check (Phase 1 integration)
        if hasattr(self, "_circuit_breaker"):
            breaker_action = self._circuit_breaker.check(
                pnl=getattr(self, "_session_pnl", 0),
                consecutive_losses=self.consecutive_losses,
            )
            if breaker_action.value == "HALT":
                console.print(f"[bold red]🛑 Circuit Breaker HALTED ({self.session_type}): Daily loss cap breached[/bold red]")
                from core.decision_logger import DecisionLogger
                DecisionLogger.log(
                    type="circuit_breaker", session=self.session_type,
                    action="halt", detail="Daily loss cap breached",
                    author="system", risk_level="high",
                )
                self.cooldown_until = 1000  # Halt until reset
                return
            elif breaker_action.value == "DIAGNOSE":
                # GSD Phase 3: Run diagnostic engine (Phase 2 integration)
                console.print(f"[bold yellow]⚠️ Circuit Breaker DIAGNOSE ({self.session_type}): {self.consecutive_losses} consecutive losses[/bold yellow]")
                # Diagnosis will be done in post-session review
                # For now, log and continue (diagnostic engine is async via daily_review.py)
                from core.decision_logger import DecisionLogger
                DecisionLogger.log(
                    type="circuit_breaker", session=self.session_type,
                    action="diagnose", detail=f"{self.consecutive_losses} consecutive losses, triggering diagnostic",
                    author="system", risk_level="medium",
                )
            elif breaker_action.value == "REDUCE_SIZE":
                # Temporarily reduce position size
                console.print(f" [yellow]⚠️ Circuit Breaker REDUCE_SIZE ({self.session_type}): Daily loss at 40%[/yellow] ")

        # Prevent re-entering on the same bar as exit
        if self._last_exit_bar == timestamp:
            self._audit_signal("ENTRY_BLOCKED", "", score, "same_bar_exit")
            return

        self.has_tp1_hit = False
        # [Bug fix] Only reset trail peak on ACTUAL new entry intent
        stop_loss_pts = self.RISK.get("stop_loss_pts", 60)
        if self.ATR_MULT > 0:
            atr_val = last_5m.get("atr", 0)
            # [Bug fix] ATR 合理性上限：MXF 5m ATR 通常 30-150 點
            atr_cap = 300
            if atr_val > atr_cap:
                atr_val = atr_cap
            if atr_val > 0:
                stop_loss_pts = atr_val * self.ATR_MULT

        # ── PAPER_GATE_BYPASS_DIAGNOSTIC ──
        # 在 paper mode 下可跳過進場品質過濾，用於診斷策略是否真正產生 trade decision
        _debug_gate = self.cfg.get("debug_gate_bypass", {})
        _paper_bypass = _debug_gate.get("enabled", False) and not self.live_trading and not self.dry_run and _debug_gate.get("paper_only", True)
        console.print(
            f"[dim][BYPASS_TRACE] enabled={_debug_gate.get('enabled')} "
            f"paper_only={_debug_gate.get('paper_only')} "
            f"live_trading={self.live_trading} dry_run={self.dry_run} "
            f"disable_score={_debug_gate.get('disable_entry_score_gate')} "
            f"→ bypass={_paper_bypass} "
            f"entry_score={self.STRATEGY.get('entry_score', '?')} "
            f"cfg_keys={list(self.cfg.keys())[:5]}[/dim]"
        )

        # ── 進場品質過濾 ──
        min_score = self.STRATEGY.get("entry_score", 21)
        if _paper_bypass and _debug_gate.get("disable_entry_score_gate", False):
            min_score = 0
            console.print(f"[dim][BYPASS][PAPER_ONLY] entry_score disabled (min_score=0)[/dim]")
        vol = last_5m.get("Volume", 0)
        avg_vol = df_5m["Volume"].rolling(20).mean().iloc[-1] if len(df_5m) >= 20 else 0

        # 夜盤成交量門檻降低（夜盤 MXF 量通常只有日盤 3-10%）
        hhmm = int(datetime.now().strftime("%H%M"))
        is_night = hhmm >= 1500 or hhmm < 500
        vol_threshold = self.STRATEGY.get("volume_threshold", 0.05 if is_night else 0.3)

        vol_filter_ok = (avg_vol == 0) or (vol >= avg_vol * vol_threshold)
        if _paper_bypass and _debug_gate.get("disable_volume_gate", False):
            vol_filter_ok = True
            console.print(f"[dim][BYPASS][PAPER_ONLY] volume gate disabled[/dim]")
        if not vol_filter_ok:
            console.print(f"[ENTRY_GATE] BLOCKED by volume: vol={vol:.0f} avg={avg_vol:.0f} thresh={vol_threshold} night={is_night}")
            self._audit_signal("ENTRY_BLOCKED", "", score, "low_volume", f"vol={vol:.0f} avg={avg_vol:.0f} thresh={vol_threshold}")
            console.print(f"[dim]⏸️ Volume too low ({session_note}): {vol:.0f} vs avg {avg_vol:.0f} (>{vol_threshold*100:.0f}%) — skipping entry[/dim]")
            return

        if abs(score) < min_score:
            console.print(f"[ENTRY_GATE] BLOCKED by score: abs_score={abs(score):.1f} min_score={min_score}")
            console.print(
                f"[ENTRY_GATE_TRACE] abs_score={abs(score):.2f} min_score={min_score} "
                f"score_type={type(score).__name__} score_raw={score} "
                f"mom_state_5m={last_5m.get('mom_state', '?')} "
                f"bar_key={bar_source.get('source', '?')} "
                f"ts={timestamp}"
            )
            if self.counter_enabled:
                pass  # Counter mode 有自己的信號系統，不擋
            else:
                self._audit_signal("NO_ENTRY", "", score, "score_too_low", f"threshold={min_score}")
                return  # 分數太低，不進場

        # ── GSD: Pluggable Strategy Entry (Unified Route Path) ─────────
        from core.market_regime import classify_regime
        session_regime = classify_regime(df_5m)
        active_name = self.STRATEGY.get("active_strategy", "counter_vwap")
        decision, _ctx, session_regime, bar_regime = self._route_signal(
            bar=last_5m_dict,
            session_regime=session_regime,
            active_name=active_name
        )

        if decision is None:
            # Skip path (e.g. prefill bar)
            return

        if decision.action == "BLOCKED":
            self._audit_signal(
                "ENTRY_BLOCKED",
                "",
                score,
                "router_blocked",
                self._format_router_audit_note(decision, bar_regime),
            )
            return

        if not decision.is_trade:
            note = self._format_router_audit_note(decision, bar_regime)
            if active_name and self._registry.get(active_name) is None:
                self._audit_signal(
                    "NO_ENTRY",
                    "",
                    score,
                    "plugin_not_found",
                    f"active_strategy={active_name}; {note}",
                )
            else:
                self._audit_signal(
                    "NO_ENTRY",
                    "",
                    score,
                    "router_no_signal",
                    note,
                )
            return

        signal = decision.signal
        selected_strategy_name = decision.selected_strategy or active_name
        self.active_strategy_name = selected_strategy_name

        # 4.1 Global Edge Filter (Bypass for exits, apply to entries)
        if signal and signal.action in ["BUY", "SELL"]:
            # [GSD 4.13] Trading Readiness Gate
            if not self.is_trading_ready:
                _msg = f"indicators_warming_up (bars={len(df_5m)})"
                console.print(f"[ENTRY_GATE] BLOCKED by is_trading_ready=False score={score:.1f} reason={_msg}")
                self._audit_signal("ENTRY_BLOCKED", signal.action, score, "not_ready", _msg)
                return

            # [L4] Decision Intelligence: Edge Evaluation (Re-evaluated with side)
            from core.edge_model import edge_model
            edge_context = {
                "momentum": float(last_5m.get("momentum", 0)),
                "regime": str(bar_regime.regime),
                "vwap_dist": abs(last_price - vwap),
                "volatility": float(last_5m.get("atr", 50)),
                "price": last_price,
                "side": "LONG" if signal.action == "BUY" else "SHORT",
                "breakout_strength": float(last_5m.get("breakout_strength", 0)),
                "volume_spike": float(last_5m.get("volume_spike", 1.0)),
                "trend_strength_raw": float(last_5m.get("trend_strength_raw", 0))
            }
            
            edge_res = edge_model.evaluate(abs(score), edge_context, selected_strategy_name)
            if not edge_res["has_edge"]:
                _reason = edge_res.get("reason", "low_edge")
                console.print(f"[ENTRY_GATE] BLOCKED by edge_model: strategy={selected_strategy_name} score={score:.1f} reason={_reason}")
                self._audit_signal("ENTRY_BLOCKED", signal.action, score, "low_edge", _reason)
                if self._bar_counter % 5 == 0:
                    console.print(f"[bold yellow]🛡️ Decision Intelligence: {selected_strategy_name} Blocked - {_reason}[/bold yellow]")
                return
            
            # [GSD Upgrade] Apply Dynamic Position Scaling
            signal.quantity = max(1, round(lots * edge_res["pos_scale"]))
            signal.reason = f"{signal.reason} ({edge_res['rank']})"
            if edge_res["pos_scale"] != 1.0:
                console.print(f"[bold cyan]⚖️ Position Scaled: {edge_res['rank']} (x{edge_res['pos_scale']}) -> {signal.quantity} lots[/bold cyan]")
            
            # Update lots for further logic
            lots = signal.quantity

        # 5. Validate Signal (Defensive Programming)
        is_valid, msg = signal.validate()
        if not is_valid:
            console.print(f"[red]❌ Invalid signal from {selected_strategy_name}: {msg}[/red]")
            return

        # 6. Execute Trade — apply size multiplier from decision (e.g. SQUEEZE_FIRE_SCOUT 0.25x)
        base_lots = self.MGMT.get("lots_per_trade", 1)
        size_mult = getattr(decision, "size_multiplier", 1.0)
        # Also check if signal metadata has an override
        if signal and hasattr(signal, "metadata") and isinstance(signal.metadata, dict):
            mult = signal.metadata.get("size_multiplier")
            if mult is not None and 0 < mult <= 1.0:
                size_mult = mult
        lots = max(1, round(base_lots * size_mult))
        if size_mult != 1.0:
            console.print(f"[cyan]⚖️ Size scaled: {base_lots} x {size_mult} = {lots} lot(s) ({signal.signal_type})[/cyan]")
        self._execute_trade(
            signal.action,
            last_price,
            timestamp,
            lots,
            stop_loss=signal.stop_loss,
            break_even_trigger=signal.break_even_trigger,
            trail_points=signal.trail_points,
            reason=signal.reason,
        )
