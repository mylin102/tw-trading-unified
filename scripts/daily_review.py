#!/usr/bin/env python3
"""
Post-Session Review — independent day/night strategy assessment.

Usage:
    python3 scripts/daily_review.py --session day    # Run day review
    python3 scripts/daily_review.py --session night  # Run night review
    python3 scripts/daily_review.py --session day --dry-run  # Report only, no config write

Runs at 13:50 (day close) and 05:05 (night close).
Reads trades + entry diagnostics → generates review → writes config update.

日盤檢討 → 下次日盤套用
夜盤檢討 → 下次夜盤套用
互不影響。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import os
from pathlib import Path
from datetime import datetime
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.decision_logger import DecisionLogger
from core.diagnostic_engine import diagnose_losing_streak, TradeDiagnosis
from core.strategy_registry import get_strategy_ranking, select_best_strategy

console = Console()

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
TRADES_PATH = Path(__file__).resolve().parent.parent / "exports" / "trades"
REVIEWS_DIR = Path(__file__).resolve().parent.parent / "logs" / "session_reviews"


def load_session_trades(session: str) -> list[dict]:
    """Load trades for a specific session type."""
    if not TRADES_PATH.exists():
        return []

    trades = []
    for csv_file in TRADES_PATH.glob("*.csv"):
        try:
            with open(csv_file) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Filter by session type if available
                    if row.get("session", "").lower() == session.lower() or not row.get("session"):
                        trades.append(row)
        except Exception:
            pass

    return trades


def compute_session_metrics(trades: list[dict]) -> dict:
    """Compute PnL, WinRate, PF for the session."""
    if not trades:
        return {"pnl_pts": 0, "pnl_cash": 0, "trade_count": 0, "win_rate": 0, "pf": 0, "wins": 0, "losses": 0}

    pnl_pts_list = []
    pnl_cash_list = []
    wins = 0
    losses = 0

    for t in trades:
        try:
            pts = float(t.get("pnl_pts", 0))
            cash = float(t.get("pnl_cash", 0))
            pnl_pts_list.append(pts)
            pnl_cash_list.append(cash)
            if pts > 0:
                wins += 1
            elif pts < 0:
                losses += 1
        except (ValueError, TypeError):
            pass

    total_trades = wins + losses
    gross_profit = sum(p for p in pnl_pts_list if p > 0) or 1
    gross_loss = abs(sum(p for p in pnl_pts_list if p < 0)) or 1
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "pnl_pts": sum(pnl_pts_list),
        "pnl_cash": sum(pnl_cash_list),
        "trade_count": total_trades,
        "win_rate": wins / total_trades * 100 if total_trades > 0 else 0,
        "pf": pf,
        "wins": wins,
        "losses": losses,
    }


def run_session_review(session: str, dry_run: bool = False) -> dict:
    """
    Run post-session review for day or night.

    Returns:
        Review report dict.
    """
    # 1. Load trades
    trades = load_session_trades(session)
    metrics = compute_session_metrics(trades)

    # 2. Build trade diagnoses (from entry_diag records + exit trades)
    entry_trades = [t for t in trades if t.get("type") == "ENTRY_DIAG"]
    exit_trades = [t for t in trades if t.get("type") in ("EXIT", "PARTIAL_EXIT")]

    losing_diagnoses = []
    for et in exit_trades:
        try:
            pnl_pts = float(et.get("pnl_pts", 0))
            if pnl_pts < 0:
                # Find matching entry diagnostic
                entry_diag = {}
                for ed in entry_trades:
                    if ed.get("timestamp") == et.get("timestamp"):
                        import ast
                        try:
                            entry_diag = ast.literal_eval(ed.get("entry_diag", "{}"))
                        except Exception:
                            pass
                        break

                losing_diagnoses.append(TradeDiagnosis(
                    exit_reason=et.get("reason", "UNKNOWN"),
                    pnl_pts=pnl_pts,
                    entry_diag=entry_diag,
                    session=session,
                ))
        except (ValueError, TypeError):
            pass

    # 3. Run diagnostic if there are losing trades
    diagnostic_action = None
    if losing_diagnoses:
        from core.strategy_registry import STRATEGY_PERF
        diagnostic_action = diagnose_losing_streak(
            trades=losing_diagnoses,
            current_strategy="counter_vwap",  # TODO: read from config
            regime="trending",  # TODO: detect live regime
        )

    # 4. Get strategy rankings for this session
    ranking = get_strategy_ranking(session)

    # 5. Generate recommendation
    recommendation = _generate_recommendation(metrics, diagnostic_action, ranking)

    # 6. Build report
    report = {
        "session": session,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "losing_trades": len(losing_diagnoses),
        "diagnostic_action": {
            "action_type": diagnostic_action.action_type if diagnostic_action else "CONTINUE",
            "reason": diagnostic_action.reason if diagnostic_action else "",
            "param": diagnostic_action.param if diagnostic_action else "",
            "delta": diagnostic_action.delta if diagnostic_action else 0,
        },
        "ranking": ranking,
        "recommendation": recommendation,
    }

    # 7. Log decision
    if diagnostic_action and diagnostic_action.action_type != "CONTINUE":
        DecisionLogger.log(
            type="post_session",
            session=session,
            action=diagnostic_action.action_type.lower(),
            detail=diagnostic_action.reason,
            author="system",
            risk_level="medium" if diagnostic_action.action_type != "HALT" else "high",
        )

    # 7.5. Apply config changes (Phase 4.2 integration)
    if not dry_run and diagnostic_action and diagnostic_action.action_type == "TIGHTEN_ENTRY":
        try:
            from core.session_config import SessionConfig
            cfg = SessionConfig.load(session)
            param = diagnostic_action.param
            delta = diagnostic_action.delta

            # Map diagnostic params to config keys
            param_map = {
                "confirm_bars": "strategy.counter_mode.confirm_bars",
                "min_momentum": "strategy.counter_mode.min_momentum",
            }
            config_key = param_map.get(param)
            if config_key:
                current_val = cfg.get(config_key)
                if current_val is not None:
                    new_val = current_val + delta
                    cfg.set(config_key, new_val)
                    cfg.save(backup=True)
                    console.print(f"[green]🔧 Config updated: {config_key} {current_val} → {new_val}[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Config update failed: {e}[/yellow]")

    # 8. Write report
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REVIEWS_DIR / f"review_{ts}_{session}.json"

    if not dry_run:
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        console.print(f"[dim]💾 Report saved: {report_path}[/dim]")

    return report


def _generate_recommendation(metrics: dict, diagnostic_action, ranking: list) -> str:
    """Generate human-readable recommendation."""
    lines = []

    # Metric-based recommendations
    if metrics["trade_count"] == 0:
        lines.append("⚠️  No trades this session. Strategy may be too strict or data issue.")
    elif metrics["pf"] < 1.0:
        lines.append(f"🔴 Session PF={metrics['pf']:.2f} < 1.0. Consider switching strategy.")
    elif metrics["pf"] < 1.3:
        lines.append(f"⚠️ Session PF={metrics['pf']:.2f} < 1.3. Monitor closely.")

    # Diagnostic-based recommendations
    if diagnostic_action:
        action = diagnostic_action.action_type
        if action == "TIGHTEN_ENTRY":
            lines.append(
                f"🔧 Diagnostic: {diagnostic_action.param} "
                f"should be adjusted by {diagnostic_action.delta}"
            )
            lines.append(f"   Reason: {diagnostic_action.reason}")
        elif action == "COOLDOWN":
            lines.append(f"⏸️  Cool down for {diagnostic_action.cooldown_mins} minutes")
        elif action == "SWITCH_STRATEGY":
            lines.append(f"🔄 Switch to {diagnostic_action.new_strategy}: {diagnostic_action.reason}")
        elif action == "HALT":
            lines.append(f"🛑 HALT: {diagnostic_action.reason}")

    # Strategy ranking note
    if ranking and len(ranking) > 1:
        best_name = ranking[0][0]
        lines.append(f"📊 Current session ranking: {', '.join(f'{n} ({p:.1f})' for n, p in ranking[:3])}")

    return "\n".join(lines) if lines else "✅ No issues detected. Maintain current settings."


def main():
    parser = argparse.ArgumentParser(description="Post-Session Review")
    parser.add_argument(
        "--session",
        choices=["day", "night"],
        default="day",
        help="Session type to review (default: day)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report only, don't save",
    )

    args = parser.parse_args()

    console.print(f"[bold]🔬 Post-Session Review ({args.session})[/bold]")
    console.print(f"[dim]Timestamp: {datetime.now().isoformat()}[/dim]\n")

    report = run_session_review(args.session, dry_run=args.dry_run)

    # Print summary
    m = report["metrics"]
    console.print(Panel(
        f"**Session**: {report['session']}\n"
        f"**PnL**: {m['pnl_cash']:,.0f} TWD ({m['pnl_pts']:.1f} pts)\n"
        f"**Trades**: {m['trade_count']} ({m['wins']}W/{m['losses']}L)\n"
        f"**Win Rate**: {m['win_rate']:.1f}%\n"
        f"**PF**: {m['pf']:.2f}\n"
        f"**Losing Trades Diagnosed**: {report['losing_trades']}\n"
        f"**Action**: {report['diagnostic_action']['action_type']}\n"
        f"**Recommendation**: {report['recommendation'][:100]}",
        title=f"Session Review — {args.session}",
        border_style="cyan",
    ))


if __name__ == "__main__":
    main()


def run_auto_review():
    """
    Auto-schedule entry point. Called by cron/systemd at 13:50 and 05:05.
    Determines session type based on current time and runs review.
    """
    from datetime import datetime
    now = datetime.now()
    hhmm = now.hour * 100 + now.minute

    # 13:40-14:00 → day review
    # 05:00-05:15 → night review
    if 1340 <= hhmm <= 1400:
        session = "day"
    elif 500 <= hhmm <= 515 or hhmm >= 2300 or hhmm < 600:
        session = "night"
    else:
        # Default to day
        session = "day"

    return run_session_review(session, dry_run=False)
