"""
router_attribution.py

Utilities for router-aware strategy attribution in a futures trading system.

This module is designed for a regime-aware, priority-driven, short-circuit router.

Main goals:
1. Record per-bar strategy evaluation flow
2. Summarize exposure / evaluation / winner / shadow statistics
3. Summarize trade attribution
4. Compute starvation metrics
5. Provide pandas-friendly reporting helpers

Authoring assumptions:
- One bar can produce multiple router evaluation rows
- Exactly zero or one winner per bar in normal execution mode
- Shadow replay, if enabled later, can be added on top of these schemas
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import time
from typing import Any, Iterable, Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RouterEvaluationRow:
    """One row per strategy considered (or potentially considered) on one bar."""
    timestamp: str
    symbol: str
    regime: str
    strategy_name: str
    candidate_order: int
    status: str
    evaluated: bool
    winner: bool
    signal_side: Optional[str] = None
    signal_type: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StrategySignalRow:
    """One row per generated strategy signal."""
    timestamp: str
    symbol: str
    regime: str
    strategy_name: str
    candidate_order: int
    side: str
    signal_type: str
    selected: bool
    score: Optional[float] = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TradeAttributionRow:
    """One row per executed trade."""
    trade_id: str
    symbol: str
    strategy_name: str
    regime_at_entry: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    pnl: float
    mae: Optional[float] = None
    mfe: Optional[float] = None
    hold_bars: Optional[int] = None
    exit_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

@dataclass
class AttributionRecorder:
    """
    In-memory recorder for router attribution with auto-flush.

    Typical usage:
        recorder = AttributionRecorder(output_dir="logs/attribution/")

        # per bar
        recorder.log_router_row(...)
        recorder.log_signal(...)
        recorder.log_trade(...)

        # auto-flush when buffer full, time elapsed, or shutdown
        # later
        router_df = recorder.router_df()
        summary = summarize_router(router_df)
    """
    router_rows: list[RouterEvaluationRow] = field(default_factory=list)
    signal_rows: list[StrategySignalRow] = field(default_factory=list)
    trade_rows: list[TradeAttributionRow] = field(default_factory=list)
    output_dir: str | Path | None = None
    buffer_size: int = 500
    flush_interval_seconds: int = 10
    last_flush_time: float = field(default_factory=lambda: time.time())
    flush_on_exit: bool = True

    def __post_init__(self):
        if self.flush_on_exit:
            import atexit
            atexit.register(self.export_csv_if_needed, force=True)

    def log_router_row(
        self,
        timestamp: str,
        symbol: str,
        regime: str,
        strategy_name: str,
        candidate_order: int,
        status: str,
        evaluated: bool,
        winner: bool,
        signal_side: Optional[str] = None,
        signal_type: Optional[str] = None,
        notes: str = "",
    ) -> None:
        self.router_rows.append(
            RouterEvaluationRow(
                timestamp=timestamp,
                symbol=symbol,
                regime=regime,
                strategy_name=strategy_name,
                candidate_order=candidate_order,
                status=status,
                evaluated=evaluated,
                winner=winner,
                signal_side=signal_side,
                signal_type=signal_type,
                notes=notes,
            )
        )
        self._check_and_flush()

    def log_signal(
        self,
        timestamp: str,
        symbol: str,
        regime: str,
        strategy_name: str,
        candidate_order: int,
        side: str,
        signal_type: str,
        selected: bool,
        score: Optional[float] = None,
        notes: str = "",
    ) -> None:
        self.signal_rows.append(
            StrategySignalRow(
                timestamp=timestamp,
                symbol=symbol,
                regime=regime,
                strategy_name=strategy_name,
                candidate_order=candidate_order,
                side=side,
                signal_type=signal_type,
                selected=selected,
                score=score,
                notes=notes,
            )
        )
        self._check_and_flush()

    def log_trade(
        self,
        trade_id: str,
        symbol: str,
        strategy_name: str,
        regime_at_entry: str,
        side: str,
        entry_time: str,
        exit_time: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        mae: Optional[float] = None,
        mfe: Optional[float] = None,
        hold_bars: Optional[int] = None,
        exit_reason: str = "",
    ) -> None:
        self.trade_rows.append(
            TradeAttributionRow(
                trade_id=trade_id,
                symbol=symbol,
                strategy_name=strategy_name,
                regime_at_entry=regime_at_entry,
                side=side,
                entry_time=entry_time,
                exit_time=exit_time,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=pnl,
                mae=mae,
                mfe=mfe,
                hold_bars=hold_bars,
                exit_reason=exit_reason,
            )
        )
        self._check_and_flush()

    def router_df(self) -> pd.DataFrame:
        return pd.DataFrame([row.to_dict() for row in self.router_rows])

    def signal_df(self) -> pd.DataFrame:
        return pd.DataFrame([row.to_dict() for row in self.signal_rows])

    def trade_df(self) -> pd.DataFrame:
        return pd.DataFrame([row.to_dict() for row in self.trade_rows])

    def _check_and_flush(self):
        """Check buffer size and time interval, flush if needed."""
        if self.output_dir is None:
            return
        
        now = time.time()
        should_flush = (
            len(self.router_rows) >= self.buffer_size or
            len(self.signal_rows) >= self.buffer_size or
            len(self.trade_rows) >= self.buffer_size or
            (now - self.last_flush_time) >= self.flush_interval_seconds
        )
        
        if should_flush:
            self.export_csv_if_needed(force=False)
    
    def export_csv_if_needed(self, force: bool = False):
        """Export CSV if there is data and output_dir is set."""
        if self.output_dir is None:
            return
        
        has_data = (len(self.router_rows) > 0 or 
                   len(self.signal_rows) > 0 or 
                   len(self.trade_rows) > 0)
        
        if not has_data and not force:
            return
        
        # Check if we should flush based on buffer size
        should_flush = force or (
            (len(self.router_rows) >= self.buffer_size) or
            (len(self.signal_rows) >= self.buffer_size) or
            (len(self.trade_rows) >= self.buffer_size)
        )
        
        if not should_flush:
            return
        
        # Actually export
        self.export_csv(self.output_dir, force_write_empty=force)
        
    def export_csv(self, output_dir: str | Path, force_write_empty: bool = False) -> None:
        """Export all data to CSV files.
        
        Args:
            output_dir: Directory to write CSV files
            force_write_empty: If True, write empty files with headers even if no data
        """
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)

        # Write all data
        if self.router_rows or force_write_empty:
            df = self.router_df() if self.router_rows else pd.DataFrame(columns=RouterEvaluationRow.__annotations__.keys())
            df.to_csv(output / "router_evaluation_log.csv", mode='a', 
                     index=False, header=not (output / "router_evaluation_log.csv").exists())
        if self.signal_rows or force_write_empty:
            df = self.signal_df() if self.signal_rows else pd.DataFrame(columns=StrategySignalRow.__annotations__.keys())
            df.to_csv(output / "strategy_signal_log.csv", mode='a',
                     index=False, header=not (output / "strategy_signal_log.csv").exists())
        if self.trade_rows or force_write_empty:
            df = self.trade_df() if self.trade_rows else pd.DataFrame(columns=TradeAttributionRow.__annotations__.keys())
            df.to_csv(output / "trade_attribution_log.csv", mode='a',
                     index=False, header=not (output / "trade_attribution_log.csv").exists())
        
        # Clear buffers after successful write
        self.router_rows.clear()
        self.signal_rows.clear()
        self.trade_rows.clear()


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def _safe_div(n: float, d: float) -> float:
    return float(n) / float(d) if d else 0.0


def summarize_router(router_df: pd.DataFrame) -> pd.DataFrame:
    """
    Summarize strategy exposure / evaluation / winner / shadow stats.

    Expected status values include:
    - candidate
    - no_signal
    - winner
    - shadowed
    - regime_mismatch
    - missing

    candidate_count is defined as the number of times a strategy appears in the candidate list,
    which must be explicitly logged, including shadowed strategies.
    """
    if router_df.empty:
        return pd.DataFrame()

    df = router_df.copy()

    grouped = []
    for strategy_name, g in df.groupby("strategy_name", dropna=False):
        candidate_count = len(g)
        eval_count = int(g["evaluated"].fillna(False).sum())
        winner_count = int(g["winner"].fillna(False).sum())
        shadowed_count = int((g["status"] == "shadowed").sum())
        regime_mismatch_count = int((g["status"] == "regime_mismatch").sum())
        no_signal_count = int((g["status"] == "no_signal").sum())
        missing_count = int((g["status"] == "missing").sum())

        # Priority Impact Score: shadowed_count / winner_count (higher = more suppressed)
        priority_impact = _safe_div(shadowed_count, winner_count) if winner_count > 0 else 0.0
        
        grouped.append(
            {
                "strategy_name": strategy_name,
                "candidate_count": candidate_count,
                "eval_count": eval_count,
                "winner_count": winner_count,
                "shadowed_count": shadowed_count,
                "regime_mismatch_count": regime_mismatch_count,
                "no_signal_count": no_signal_count,
                "missing_count": missing_count,
                "candidate_rate": None,  # filled later if total bars available
                "evaluation_rate": _safe_div(eval_count, candidate_count),
                "shadow_rate": _safe_div(shadowed_count, candidate_count),
                "win_conversion": _safe_div(winner_count, eval_count),
                "starvation_index": 1.0 - _safe_div(eval_count, candidate_count),
                "priority_impact": priority_impact,
            }
        )

    out = pd.DataFrame(grouped).sort_values(
        ["winner_count", "eval_count", "candidate_count"],
        ascending=[False, False, False],
    )
    out.reset_index(drop=True, inplace=True)
    return out


def summarize_router_by_regime(router_df: pd.DataFrame) -> pd.DataFrame:
    """Summarize router stats by strategy x regime."""
    if router_df.empty:
        return pd.DataFrame()

    df = router_df.copy()

    grouped = []
    for (strategy_name, regime), g in df.groupby(["strategy_name", "regime"], dropna=False):
        candidate_count = len(g)
        eval_count = int(g["evaluated"].fillna(False).sum())
        winner_count = int(g["winner"].fillna(False).sum())
        shadowed_count = int((g["status"] == "shadowed").sum())
        regime_mismatch_count = int((g["status"] == "regime_mismatch").sum())
        no_signal_count = int((g["status"] == "no_signal").sum())

        grouped.append(
            {
                "strategy_name": strategy_name,
                "regime": regime,
                "candidate_count": candidate_count,
                "eval_count": eval_count,
                "winner_count": winner_count,
                "shadowed_count": shadowed_count,
                "regime_mismatch_count": regime_mismatch_count,
                "no_signal_count": no_signal_count,
                "evaluation_rate": _safe_div(eval_count, candidate_count),
                "shadow_rate": _safe_div(shadowed_count, candidate_count),
                "win_conversion": _safe_div(winner_count, eval_count),
                "starvation_index": 1.0 - _safe_div(eval_count, candidate_count),
            }
        )

    out = pd.DataFrame(grouped).sort_values(
        ["strategy_name", "regime"]
    ).reset_index(drop=True)
    return out


def summarize_signals(signal_df: pd.DataFrame) -> pd.DataFrame:
    """Summarize signal generation and selection."""
    if signal_df.empty:
        return pd.DataFrame()

    grouped = []
    for strategy_name, g in signal_df.groupby("strategy_name", dropna=False):
        signal_count = len(g)
        selected_count = int(g["selected"].fillna(False).sum())

        grouped.append(
            {
                "strategy_name": strategy_name,
                "signal_count": signal_count,
                "selected_count": selected_count,
                "selection_rate": _safe_div(selected_count, signal_count),
            }
        )

    return pd.DataFrame(grouped).sort_values(
        ["selected_count", "signal_count"], ascending=[False, False]
    ).reset_index(drop=True)


def summarize_trades(trade_df: pd.DataFrame) -> pd.DataFrame:
    """Summarize realized trade performance by strategy."""
    if trade_df.empty:
        return pd.DataFrame()

    df = trade_df.copy()

    grouped = []
    for strategy_name, g in df.groupby("strategy_name", dropna=False):
        pnl = g["pnl"].fillna(0.0)
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]

        trade_count = len(g)
        win_count = int((pnl > 0).sum())
        loss_count = int((pnl < 0).sum())
        total_pnl = float(pnl.sum())
        avg_pnl = float(pnl.mean()) if trade_count else 0.0
        avg_mae = float(g["mae"].dropna().mean()) if "mae" in g else 0.0
        avg_mfe = float(g["mfe"].dropna().mean()) if "mfe" in g else 0.0
        avg_hold_bars = float(g["hold_bars"].dropna().mean()) if "hold_bars" in g else 0.0

        gross_profit = float(wins.sum()) if not wins.empty else 0.0
        gross_loss_abs = float(abs(losses.sum())) if not losses.empty else 0.0
        avg_win = float(wins.mean()) if not wins.empty else 0.0
        avg_loss_abs = float(abs(losses.mean())) if not losses.empty else 0.0
        win_rate = _safe_div(win_count, trade_count)
        loss_rate = _safe_div(loss_count, trade_count)
        expectancy = (avg_win * win_rate) - (avg_loss_abs * loss_rate)
        profit_factor = _safe_div(gross_profit, gross_loss_abs)

        grouped.append(
            {
                "strategy_name": strategy_name,
                "trade_count": trade_count,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "avg_pnl": avg_pnl,
                "avg_mae": avg_mae,
                "avg_mfe": avg_mfe,
                "avg_hold_bars": avg_hold_bars,
                "profit_factor": profit_factor,
                "expectancy": expectancy,
            }
        )

    return pd.DataFrame(grouped).sort_values(
        ["total_pnl", "trade_count"], ascending=[False, False]
    ).reset_index(drop=True)


def summarize_trades_by_regime(trade_df: pd.DataFrame) -> pd.DataFrame:
    """Summarize realized trade performance by strategy x regime_at_entry."""
    if trade_df.empty:
        return pd.DataFrame()

    df = trade_df.copy()

    grouped = []
    for (strategy_name, regime), g in df.groupby(["strategy_name", "regime_at_entry"], dropna=False):
        pnl = g["pnl"].fillna(0.0)
        trade_count = len(g)
        win_count = int((pnl > 0).sum())
        loss_count = int((pnl < 0).sum())

        grouped.append(
            {
                "strategy_name": strategy_name,
                "regime_at_entry": regime,
                "trade_count": trade_count,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": _safe_div(win_count, trade_count),
                "total_pnl": float(pnl.sum()),
                "avg_pnl": float(pnl.mean()) if trade_count else 0.0,
            }
        )

    return pd.DataFrame(grouped).sort_values(
        ["strategy_name", "regime_at_entry"]
    ).reset_index(drop=True)


def build_starvation_report(router_df: pd.DataFrame) -> pd.DataFrame:
    """
    Convenience report focused on starvation / shadowing.

    Interpretation:
    - starvation_index > 0.70 : severe starvation
    - 0.40 ~ 0.70            : moderate
    - < 0.40                 : acceptable
    """
    summary = summarize_router(router_df)
    if summary.empty:
        return summary

    def classify_starvation(x: float) -> str:
        if x > 0.70:
            return "severe"
        if x >= 0.40:
            return "moderate"
        return "acceptable"

    out = summary[[
        "strategy_name",
        "candidate_count",
        "eval_count",
        "shadowed_count",
        "evaluation_rate",
        "shadow_rate",
        "starvation_index",
    ]].copy()

    out["starvation_level"] = out["starvation_index"].map(classify_starvation)
    return out.sort_values(
        ["starvation_index", "shadowed_count"], ascending=[False, False]
    ).reset_index(drop=True)


def merge_router_and_trade_summary(
    router_df: pd.DataFrame,
    trade_df: pd.DataFrame,
) -> pd.DataFrame:
    """Merge router exposure stats with realized trade stats."""
    router_summary = summarize_router(router_df)
    trade_summary = summarize_trades(trade_df)

    if router_summary.empty and trade_summary.empty:
        return pd.DataFrame()
    if router_summary.empty:
        return trade_summary
    if trade_summary.empty:
        return router_summary

    out = router_summary.merge(
        trade_summary,
        how="outer",
        on="strategy_name",
    )

    ordered_cols = [
        "strategy_name",
        "candidate_count",
        "eval_count",
        "winner_count",
        "shadowed_count",
        "evaluation_rate",
        "shadow_rate",
        "win_conversion",
        "starvation_index",
        "trade_count",
        "win_count",
        "loss_count",
        "win_rate",
        "total_pnl",
        "avg_pnl",
        "profit_factor",
        "expectancy",
        "avg_mae",
        "avg_mfe",
        "avg_hold_bars",
    ]
    keep = [c for c in ordered_cols if c in out.columns] + [
        c for c in out.columns if c not in ordered_cols
    ]
    return out[keep]


# ---------------------------------------------------------------------------
# Helper hooks for live router integration
# ---------------------------------------------------------------------------

def log_router_event(
    recorder: AttributionRecorder | None,
    timestamp: str,
    symbol: str,
    regime: str,
    strategy_name: str,
    candidate_order: int,
    status: str,
    evaluated: bool,
    winner: bool = False,
    signal: Any = None,
    note: str = "",
) -> None:
    """
    Convenience helper for router integration.
    """
    if recorder is None:
        return

    recorder.log_router_row(
        timestamp=timestamp,
        symbol=symbol,
        regime=regime,
        strategy_name=strategy_name,
        candidate_order=candidate_order,
        status=status,
        evaluated=evaluated,
        winner=winner,
        signal_side=getattr(signal, "side", None),
        signal_type=getattr(signal, "type", None),
        notes=note,
    )


def log_candidates_for_bar(
    recorder: AttributionRecorder,
    timestamp: str,
    symbol: str,
    regime: str,
    candidates: Iterable[str],
) -> None:
    """
    Optional helper: pre-log candidate presence before evaluation.
    Usually not necessary if you log final status rows directly.
    """
    for i, name in enumerate(candidates):
        recorder.log_router_row(
            timestamp=timestamp,
            symbol=symbol,
            regime=regime,
            strategy_name=name,
            candidate_order=i,
            status="candidate",
            evaluated=False,
            winner=False,
            notes="pre-evaluation candidate",
        )


def mark_shadowed_remaining(
    recorder: AttributionRecorder,
    timestamp: str,
    symbol: str,
    regime: str,
    remaining_candidates: Iterable[str],
    start_order: int,
    winner_name: str,
) -> None:
    """
    Convenience helper after a winner is found.
    Marks later candidates as shadowed.
    """
    for offset, name in enumerate(remaining_candidates):
        recorder.log_router_row(
            timestamp=timestamp,
            symbol=symbol,
            regime=regime,
            strategy_name=name,
            candidate_order=start_order + offset,
            status="shadowed",
            evaluated=False,
            winner=False,
            notes=f"short-circuited by winner={winner_name}",
        )


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    recorder = AttributionRecorder()

    # Example bar
    ts = "2026-04-22 09:15:00"
    symbol = "TX"
    regime = "WEAK"

    # counter_vwap evaluated, no signal
    recorder.log_router_row(
        timestamp=ts,
        symbol=symbol,
        regime=regime,
        strategy_name="counter_vwap",
        candidate_order=0,
        status="no_signal",
        evaluated=True,
        winner=False,
        notes="squeeze not fired",
    )

    # spring_upthrust evaluated, no signal
    recorder.log_router_row(
        timestamp=ts,
        symbol=symbol,
        regime=regime,
        strategy_name="spring_upthrust",
        candidate_order=1,
        status="no_signal",
        evaluated=True,
        winner=False,
        notes="structure not confirmed",
    )

    # kbar_feature winner
    recorder.log_router_row(
        timestamp=ts,
        symbol=symbol,
        regime=regime,
        strategy_name="kbar_feature",
        candidate_order=2,
        status="winner",
        evaluated=True,
        winner=True,
        signal_side="SELL",
        signal_type="KBAR_FEATURE_SHORT",
        notes="multi-factor confirmation",
    )

    recorder.log_signal(
        timestamp=ts,
        symbol=symbol,
        regime=regime,
        strategy_name="kbar_feature",
        candidate_order=2,
        side="SELL",
        signal_type="KBAR_FEATURE_SHORT",
        selected=True,
        score=-30.0,
    )

    recorder.log_trade(
        trade_id="T1",
        symbol=symbol,
        strategy_name="kbar_feature",
        regime_at_entry=regime,
        side="SELL",
        entry_time=ts,
        exit_time="2026-04-22 09:18:00",
        entry_price=20100.0,
        exit_price=20070.0,
        pnl=30.0,
        mae=-8.0,
        mfe=36.0,
        hold_bars=3,
        exit_reason="target",
    )

    print("=== Router Summary ===")
    print(summarize_router(recorder.router_df()).to_string(index=False))

    print("\n=== Starvation Report ===")
    print(build_starvation_report(recorder.router_df()).to_string(index=False))

    print("\n=== Trade Summary ===")
    print(summarize_trades(recorder.trade_df()).to_string(index=False))
