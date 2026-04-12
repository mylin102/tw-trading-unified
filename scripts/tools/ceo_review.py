#!/usr/bin/env python3
"""
GStack CEO Review — Scope & Strategy Validation

Purpose:
  Executive-level review of the trading system's scope, strategy alignment,
  risk/reward ratios, and capital efficiency. Used before go/no-go decisions
  for deploying strategies to live trading.

Scope:
  - Strategy portfolio alignment with business goals
  - Risk-adjusted return validation
  - Capital efficiency & exposure analysis
  - Live trading readiness assessment

Usage:
  python3 scripts/tools/ceo_review.py                    # Full review
  python3 scripts/tools/ceo_review.py --scope futures    # Futures only
  python3 scripts/tools/ceo_review.py --scope options    # Options only
  python3 scripts/tools/ceo_review.py --min-pf 1.5       # Custom PF threshold
  python3 scripts/tools/ceo_review.py --history          # Show review history
"""
import os
import sys
import json
import yaml
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markdown import Markdown

console = Console()

# ─── Paths ───────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FUTURES = REPO_ROOT / "config" / "futures.yaml"
CONFIG_OPTIONS = REPO_ROOT / "config" / "options_strategy.yaml"
EXPORTS_DIR = REPO_ROOT / "exports"
REVIEW_LOG_DIR = REPO_ROOT / "logs" / "ceo_reviews"

# ─── CEO Review Thresholds (business-level KPIs) ────────────────────────
DEFAULT_MIN_PF = 1.3
DEFAULT_MAX_DD = -15.0
DEFAULT_MIN_WR = 30.0
DEFAULT_MIN_TRADES = 10
DEFAULT_MIN_SHARPE = 1.0
DEFAULT_MAX_CAPITAL_AT_RISK = 0.20  # 20% of capital


@dataclass
class ReviewFinding:
    category: str
    status: str  # PASS, WARN, FAIL
    metric: str
    value: str
    threshold: str
    recommendation: str = ""


@dataclass
class ReviewReport:
    review_id: str = ""
    timestamp: str = ""
    scope: str = "all"
    findings: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    accepted: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    verdict: str = ""
    summary: str = ""


# ─── Data Loading ────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    """Load YAML config safely."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_backtest_results() -> pd.DataFrame:
    """Load the latest backtest sweep results."""
    candidates = [
        EXPORTS_DIR / "vbt_futures_sweep.csv",
        EXPORTS_DIR / "vbt_options_sweep.csv",
        EXPORTS_DIR / "vbt_counter_sweep.csv",
    ]
    frames = []
    for p in candidates:
        if p.exists():
            try:
                df = pd.read_csv(p)
                df["_source"] = p.stem
                frames.append(df)
            except Exception:
                pass
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_trade_history() -> pd.DataFrame:
    """Load trade history from exports/trades/."""
    trades_dir = EXPORTS_DIR / "trades"
    if not trades_dir.exists():
        return pd.DataFrame()
    frames = []
    for p in trades_dir.glob("*.csv"):
        try:
            df = pd.read_csv(p)
            frames.append(df)
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ─── Review Checks ───────────────────────────────────────────────────────
def check_strategy_scope(futures_cfg: dict, options_cfg: dict) -> list[ReviewFinding]:
    """Validate strategy portfolio alignment."""
    findings = []
    active = futures_cfg.get("strategy", {}).get("active_strategy", "unknown")
    auto_select = futures_cfg.get("strategy", {}).get("auto_select", False)
    counter_mode = futures_cfg.get("strategy", {}).get("counter_mode", {})
    counter_enabled = counter_mode.get("enabled", False)

    findings.append(ReviewFinding(
        category="SCOPE",
        status="PASS" if active else "FAIL",
        metric="Active Strategy",
        value=active,
        threshold="must be set",
        recommendation="Set active_strategy in config/futures.yaml" if not active else "",
    ))

    findings.append(ReviewFinding(
        category="SCOPE",
        status="PASS" if auto_select else "WARN",
        metric="Auto Select",
        value=str(auto_select),
        threshold="recommended for regime adaptation",
        recommendation="Enable auto_select to adapt to market conditions" if not auto_select else "",
    ))

    findings.append(ReviewFinding(
        category="SCOPE",
        status="PASS" if counter_enabled else "WARN",
        metric="Counter-VWAP Mode",
        value=str(counter_enabled),
        threshold="enabled (highest PF strategy)",
        recommendation="Enable counter_mode for Counter-VWAP strategy" if not counter_enabled else "",
    ))

    options_mode = options_cfg.get("active_mode", options_cfg.get("mode", "unknown"))
    findings.append(ReviewFinding(
        category="SCOPE",
        status="PASS" if options_mode in ("V2", "V3") else "FAIL",
        metric="Options Mode",
        value=options_mode,
        threshold="V2 or V3",
        recommendation="Use V2 (swing) or V3 (night) mode for options",
    ))

    return findings


def check_risk_reward(
    backtest_df: pd.DataFrame,
    min_pf: float,
    max_dd: float,
    min_wr: float,
    min_trades: int,
) -> list[ReviewFinding]:
    """Validate risk/reward ratios from backtest data."""
    findings = []

    if backtest_df.empty:
        findings.append(ReviewFinding(
            category="RISK_REWARD",
            status="FAIL",
            metric="Backtest Data",
            value="missing",
            threshold="exists in exports/",
            recommendation="Run backtest: python3 scripts/backtest/backtest_elite_strategies.py",
        ))
        return findings

    # Evaluate each source separately to handle different schemas
    sources = backtest_df["_source"].unique() if "_source" in backtest_df.columns else ["combined"]

    best_overall = None
    best_pnl = float("-inf")
    best_with_pf = None
    best_pf_pnl = float("-inf")

    for source in sources:
        src_df = backtest_df[backtest_df["_source"] == source] if "_source" in backtest_df.columns else backtest_df
        result = _evaluate_single_source(src_df, source)
        if result:
            # Track best overall (by PnL)
            if result["pnl"] > best_pnl:
                best_pnl = result["pnl"]
                best_overall = result
            # Track best with PF data (for comprehensive evaluation)
            if "pf" in result and result["pnl"] > best_pf_pnl:
                best_pf_pnl = result["pnl"]
                best_with_pf = result

    # Prefer source with PF data if available and PnL is positive
    preferred = best_with_pf if best_with_pf else best_overall

    if not best_overall:
        findings.append(ReviewFinding(
            category="RISK_REWARD",
            status="WARN",
            metric="Backtest Data",
            value="no evaluable results",
            threshold="valid backtest data",
            recommendation="Run backtest with compatible data format",
        ))
        return findings

    # Generate findings from preferred result (best with PF if available)
    result = preferred
    if "pf" in result:
        pf_val = result["pf"]
        findings.append(ReviewFinding(
            category="RISK_REWARD",
            status="PASS" if pf_val >= min_pf else "FAIL",
            metric="Profit Factor",
            value=f"{pf_val:.2f}",
            threshold=f">= {min_pf:.2f}",
            recommendation="Optimize parameters or switch strategy" if pf_val < min_pf else "",
        ))

    if "dd" in result:
        dd_val = result["dd"]
        findings.append(ReviewFinding(
            category="RISK_REWARD",
            status="PASS" if dd_val >= max_dd else "FAIL",
            metric="Max Drawdown %",
            value=f"{dd_val:.1f}%",
            threshold=f">= {max_dd:.1f}%",
            recommendation="Reduce position size or tighten stops" if dd_val < max_dd else "",
        ))

    if "wr" in result:
        wr_val = result["wr"]
        findings.append(ReviewFinding(
            category="RISK_REWARD",
            status="PASS" if wr_val >= min_wr else "WARN",
            metric="Win Rate %",
            value=f"{wr_val:.1f}%",
            threshold=f">= {min_wr:.1f}%",
            recommendation="Acceptable if PF compensates" if wr_val < min_wr else "",
        ))

    if "trades" in result:
        t_val = result["trades"]
        findings.append(ReviewFinding(
            category="RISK_REWARD",
            status="PASS" if t_val >= min_trades else "WARN",
            metric="Trade Count",
            value=f"{t_val:.0f}",
            threshold=f">= {min_trades:.0f}",
            recommendation="Extend backtest period for statistical significance" if t_val < min_trades else "",
        ))

    if "sharpe" in result:
        sharpe_val = result["sharpe"]
        findings.append(ReviewFinding(
            category="RISK_REWARD",
            status="PASS" if sharpe_val >= DEFAULT_MIN_SHARPE else "WARN",
            metric="Sharpe Ratio",
            value=f"{sharpe_val:.2f}",
            threshold=f">= {DEFAULT_MIN_SHARPE:.2f}",
            recommendation="" if sharpe_val >= DEFAULT_MIN_SHARPE else "Acceptable for high-frequency strategies",
        ))

    pnl_val = result["pnl"]
    findings.append(ReviewFinding(
        category="RISK_REWARD",
        status="PASS" if pnl_val > 0 else "FAIL",
        metric="Net PnL (TWD)",
        value=f"{pnl_val:,.0f}",
        threshold="> 0",
        recommendation="Strategy is unprofitable — do not deploy" if pnl_val <= 0 else "",
    ))

    findings.append(ReviewFinding(
        category="RISK_REWARD",
        status="INFO",
        metric="Best Source",
        value=result.get("source", "unknown"),
        threshold="—",
        recommendation="",
    ))

    # Also report best overall PnL if different from preferred
    if best_overall and best_overall["source"] != result["source"]:
        findings.append(ReviewFinding(
            category="RISK_REWARD",
            status="INFO",
            metric="Best PnL Source (no PF)",
            value=f"{best_overall['source']} (PnL={best_overall['pnl']:,.0f})",
            threshold="—",
            recommendation="",
        ))

    return findings


def _evaluate_single_source(df: pd.DataFrame, source: str) -> dict | None:
    """Evaluate a single backtest source and return normalized metrics."""
    cols_lower = {c: c.lower() for c in df.columns}

    # Find PnL column — prefer one that has non-NaN values
    pnl_candidates = [c for c, cl in cols_lower.items() if cl == "pnl"]
    pnl_col = None
    for c in pnl_candidates:
        if df[c].notna().any():
            pnl_col = c
            break

    # Find PF column — prefer one that has non-NaN values
    pf_candidates = [c for c, cl in cols_lower.items() if cl == "pf"]
    pf_col = None
    for c in pf_candidates:
        if df[c].notna().any():
            pf_col = c
            break

    dd_candidates = [c for c, cl in cols_lower.items() if "max_dd" in cl or "maxdd" in cl]
    dd_col = next((c for c in dd_candidates if df[c].notna().any()), None)
    wr_candidates = [c for c, cl in cols_lower.items() if "win" in cl]
    wr_col = next((c for c in wr_candidates if df[c].notna().any()), None)
    trades_candidates = [c for c, cl in cols_lower.items() if "trade" in cl]
    trades_col = next((c for c in trades_candidates if df[c].notna().any()), None)
    sharpe_col = next((c for c, cl in cols_lower.items() if cl == "sharpe" and df[c].notna().any()), None)

    # Sort by PnL or PF
    sort_col = pnl_col or pf_col
    if not sort_col or sort_col not in df.columns:
        return None

    valid_df = df.dropna(subset=[sort_col])
    if valid_df.empty:
        return None

    best = valid_df.sort_values(sort_col, ascending=False).iloc[0]

    result = {
        "source": source,
        "pnl": float(best[pnl_col]) if pnl_col and pnl_col in best.index and pd.notna(best[pnl_col]) else 0.0,
    }
    if pf_col and pf_col in best.index and pd.notna(best[pf_col]):
        result["pf"] = float(best[pf_col])
    if dd_col and dd_col in best.index:
        result["dd"] = float(best[dd_col])
    if wr_col and wr_col in best.index:
        result["wr"] = float(best[wr_col])
    if trades_col and trades_col in best.index:
        result["trades"] = float(best[trades_col])
    if sharpe_col and sharpe_col in best.index:
        result["sharpe"] = float(best[sharpe_col])

    return result


def check_capital_efficiency(
    futures_cfg: dict,
    options_cfg: dict,
    trade_df: pd.DataFrame,
) -> list[ReviewFinding]:
    """Validate capital allocation and exposure."""
    findings = []

    # Futures position limits
    futures_lots = futures_cfg.get("trade_mgmt", {}).get("lots_per_trade", 1)
    futures_max_pos = futures_cfg.get("trade_mgmt", {}).get("max_positions", 1)
    initial_capital = futures_cfg.get("execution", {}).get("initial_balance", 100000)

    findings.append(ReviewFinding(
        category="CAPITAL",
        status="PASS" if futures_lots <= 2 else "WARN",
        metric="Futures Lots",
        value=str(futures_lots),
        threshold="<= 2 (conservative)",
        recommendation="Reduce lots_per_trade to manage risk" if futures_lots > 2 else "",
    ))

    findings.append(ReviewFinding(
        category="CAPITAL",
        status="PASS" if futures_max_pos <= 2 else "FAIL",
        metric="Futures Max Positions",
        value=str(futures_max_pos),
        threshold="<= 2",
        recommendation="Reduce max_positions to limit exposure",
    ))

    # Options position sizing
    options_lots = options_cfg.get("risk_mgmt", {}).get("lots_per_trade", 1)
    options_max_pos = options_cfg.get("risk_mgmt", {}).get("max_positions", 1)
    options_capital = options_cfg.get("risk_mgmt", {}).get("initial_capital", 40000)
    max_daily_loss = options_cfg.get("risk_mgmt", {}).get("max_daily_loss", 0.02)

    findings.append(ReviewFinding(
        category="CAPITAL",
        status="PASS" if options_max_pos <= 2 else "FAIL",
        metric="Options Max Positions",
        value=str(options_max_pos),
        threshold="<= 2",
        recommendation="Reduce max_positions to limit exposure",
    ))

    findings.append(ReviewFinding(
        category="CAPITAL",
        status="PASS" if max_daily_loss <= 0.05 else "FAIL",
        metric="Max Daily Loss %",
        value=f"{max_daily_loss*100:.1f}%",
        threshold="<= 5%",
        recommendation="Tighten max_daily_loss to protect capital",
    ))

    # ThetaGang risk
    tg = options_cfg.get("theta_gang", {})
    tg_enabled = tg.get("enabled", False)
    tg_max_loss = tg.get("max_loss_pct", 1.0)

    if tg_enabled:
        findings.append(ReviewFinding(
            category="CAPITAL",
            status="PASS" if tg_max_loss <= 1.0 else "FAIL",
            metric="ThetaGang Max Loss %",
            value=f"{tg_max_loss*100:.1f}%",
            threshold="<= 100%",
            recommendation="Reduce ThetaGang max_loss_pct",
        ))

    return findings


def check_live_readiness(
    futures_cfg: dict,
    options_cfg: dict,
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Assess live trading readiness."""
    readiness = []

    fail_count = sum(1 for f in findings if f.status == "FAIL")
    warn_count = sum(1 for f in findings if f.status == "WARN")
    pass_count = sum(1 for f in findings if f.status == "PASS")
    total = len(findings)

    readiness.append(ReviewFinding(
        category="READINESS",
        status="PASS" if fail_count == 0 else "FAIL",
        metric="Critical Failures",
        value=str(fail_count),
        threshold="0",
        recommendation="Resolve all FAIL items before live deployment",
    ))

    readiness.append(ReviewFinding(
        category="READINESS",
        status="PASS" if warn_count <= 3 else "WARN",
        metric="Warnings",
        value=str(warn_count),
        threshold="<= 3",
        recommendation="Review warnings before proceeding",
    ))

    pass_rate = pass_count / total * 100 if total > 0 else 0
    readiness.append(ReviewFinding(
        category="READINESS",
        status="PASS" if pass_rate >= 70 else "FAIL",
        metric="Pass Rate",
        value=f"{pass_rate:.0f}%",
        threshold=">= 70%",
        recommendation="Improve strategy performance or reduce risk exposure",
    ))

    live_trading_futures = futures_cfg.get("live_trading", False)
    live_trading_options = options_cfg.get("live_trading", False)

    readiness.append(ReviewFinding(
        category="READINESS",
        status="PASS" if not live_trading_futures else "WARN",
        metric="Futures Live Mode",
        value=str(live_trading_futures),
        threshold="false (paper trading)",
        recommendation="Keep in paper mode until all checks pass",
    ))

    readiness.append(ReviewFinding(
        category="READINESS",
        status="PASS" if not live_trading_options else "WARN",
        metric="Options Live Mode",
        value=str(live_trading_options),
        threshold="false (paper trading)",
        recommendation="Keep in paper mode until all checks pass",
    ))

    return readiness


# ─── Report Generation ──────────────────────────────────────────────────
def generate_verdict(findings: list[ReviewFinding]) -> tuple[str, str]:
    """Generate executive verdict."""
    fail_count = sum(1 for f in findings if f.status == "FAIL")
    warn_count = sum(1 for f in findings if f.status == "WARN")
    pass_count = sum(1 for f in findings if f.status == "PASS")

    if fail_count > 0:
        verdict = "❌ REJECTED"
        summary = f"{fail_count} critical failure(s) detected. Do NOT deploy to live trading."
    elif warn_count > 3:
        verdict = "⚠️ CONDITIONAL"
        summary = f"{warn_count} warning(s). Review before deployment."
    else:
        verdict = "✅ CLEARED"
        summary = f"All checks passed ({pass_count} PASS, {warn_count} WARN, {sum(1 for f in findings if f.status == 'INFO')} INFO). Ready for live deployment."

    return verdict, summary


def save_review_report(report: ReviewReport) -> Path:
    """Save review report to logs."""
    REVIEW_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = REVIEW_LOG_DIR / f"ceo_review_{ts}.json"
    with open(filepath, "w") as f:
        json.dump(asdict(report), f, indent=2, default=str)
    return filepath


# ─── CLI Output ─────────────────────────────────────────────────────────
def render_findings_table(findings: list[ReviewFinding]) -> Table:
    """Render findings as Rich table."""
    table = Table(title="CEO Review Findings")
    table.add_column("Category", style="cyan", width=14)
    table.add_column("Status", width=8)
    table.add_column("Metric", style="bold", width=22)
    table.add_column("Value", justify="right", width=14)
    table.add_column("Threshold", justify="center", width=24)
    table.add_column("Recommendation", style="yellow", width=30)

    status_colors = {"PASS": "green", "WARN": "yellow", "FAIL": "red", "INFO": "cyan"}
    for f in findings:
        color = status_colors.get(f.status, "white")
        table.add_row(
            f.category,
            f"[{color}]{f.status}[/{color}]",
            f.metric,
            f.value,
            f.threshold,
            f.recommendation or "—",
        )
    return table


def render_summary_panel(report: ReviewReport):
    """Render executive summary panel."""
    md = f"""
## Executive Summary

**Verdict**: {report.verdict}
**Scope**: {report.scope}
**Timestamp**: {report.timestamp}

{report.summary}

---

### Proposals
- **Proposed**: {len(report.proposals)}
- **Accepted**: {len(report.accepted)}
- **Deferred**: {len(report.deferred)}
"""
    console.print(Panel(Markdown(md), title="GStack CEO Review Report", border_style="bold"))


def show_review_history():
    """Display past review reports."""
    if not REVIEW_LOG_DIR.exists():
        console.print("[yellow]No review history found.[/yellow]")
        return

    reports = sorted(REVIEW_LOG_DIR.glob("ceo_review_*.json"), reverse=True)
    if not reports:
        console.print("[yellow]No review history found.[/yellow]")
        return

    table = Table(title="CEO Review History")
    table.add_column("#", justify="right")
    table.add_column("Timestamp", style="cyan")
    table.add_column("Scope")
    table.add_column("Verdict", style="bold")
    table.add_column("Findings", justify="right")
    table.add_column("Summary", max_width=50)

    for i, filepath in enumerate(reports[:10], 1):
        with open(filepath) as f:
            data = json.load(f)
        findings = data.get("findings", [])
        pass_c = sum(1 for f in findings if f.get("status") == "PASS")
        fail_c = sum(1 for f in findings if f.get("status") == "FAIL")
        warn_c = sum(1 for f in findings if f.get("status") == "WARN")

        table.add_row(
            str(i),
            data.get("timestamp", "")[:19],
            data.get("scope", ""),
            data.get("verdict", ""),
            f"{pass_c}P/{warn_c}W/{fail_c}F",
            data.get("summary", "")[:60],
        )

    console.print(table)


# ─── Main Entry ──────────────────────────────────────────────────────────
def run_ceo_review(
    scope: str = "all",
    min_pf: float = DEFAULT_MIN_PF,
    max_dd: float = DEFAULT_MAX_DD,
    min_wr: float = DEFAULT_MIN_WR,
    min_trades: int = DEFAULT_MIN_TRADES,
) -> ReviewReport:
    """Run the full CEO review pipeline."""
    console.print("[bold]🔬 GStack CEO Review — Scope & Strategy Validation[/bold]")
    console.print(f"[dim]Timestamp: {datetime.now().isoformat()}[/dim]\n")

    # Load configs
    futures_cfg = load_config(CONFIG_FUTURES)
    options_cfg = load_config(CONFIG_OPTIONS)

    if not futures_cfg and not options_cfg:
        console.print("[red]❌ No configuration files found.[/red]")
        console.print("    Ensure config/futures.yaml and config/options_strategy.yaml exist.")
        sys.exit(1)

    # Load data
    backtest_df = load_backtest_results()
    trade_df = load_trade_history()

    all_findings: list[ReviewFinding] = []

    # 1. Strategy Scope
    if scope in ("all", "futures"):
        all_findings.extend(check_strategy_scope(futures_cfg, options_cfg))

    # 2. Risk/Reward
    all_findings.extend(check_risk_reward(backtest_df, min_pf, max_dd, min_wr, min_trades))

    # 3. Capital Efficiency
    all_findings.extend(check_capital_efficiency(futures_cfg, options_cfg, trade_df))

    # 4. Live Readiness
    all_findings.extend(check_live_readiness(futures_cfg, options_cfg, all_findings))

    # Render findings
    console.print(render_findings_table(all_findings))

    # Generate verdict
    verdict, summary = generate_verdict(all_findings)

    # Collect proposals from recommendations
    proposals = []
    accepted = []
    deferred = []
    for f in all_findings:
        if f.recommendation:
            proposals.append(f"{f.metric}: {f.recommendation}")
            if f.status == "PASS":
                accepted.append(f.metric)
            elif f.status == "WARN":
                deferred.append(f.metric)

    # Build report
    report = ReviewReport(
        review_id=f"ceo_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        timestamp=datetime.now().isoformat(),
        scope=scope,
        findings=[asdict(f) for f in all_findings],
        proposals=proposals,
        accepted=accepted,
        deferred=deferred,
        verdict=verdict,
        summary=summary,
    )

    # Save report
    filepath = save_review_report(report)

    # Render summary
    render_summary_panel(report)

    console.print(f"\n[dim]💾 Report saved: {filepath}[/dim]")

    # Verdict color
    if "REJECTED" in verdict:
        console.print(f"\n[bold red]{verdict}[/bold red]")
    elif "CONDITIONAL" in verdict:
        console.print(f"\n[bold yellow]{verdict}[/bold yellow]")
    else:
        console.print(f"\n[bold green]{verdict}[/bold green]")

    return report


def main():
    import argparse

    parser = argparse.ArgumentParser(description="GStack CEO Review — Scope & Strategy Validation")
    parser.add_argument(
        "--scope",
        choices=["all", "futures", "options"],
        default="all",
        help="Review scope (default: all)",
    )
    parser.add_argument(
        "--min-pf",
        type=float,
        default=DEFAULT_MIN_PF,
        help=f"Minimum profit factor threshold (default: {DEFAULT_MIN_PF})",
    )
    parser.add_argument(
        "--max-dd",
        type=float,
        default=DEFAULT_MAX_DD,
        help=f"Maximum drawdown threshold % (default: {DEFAULT_MAX_DD})",
    )
    parser.add_argument(
        "--min-wr",
        type=float,
        default=DEFAULT_MIN_WR,
        help=f"Minimum win rate % (default: {DEFAULT_MIN_WR})",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=DEFAULT_MIN_TRADES,
        help=f"Minimum trade count (default: {DEFAULT_MIN_TRADES})",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Show review history",
    )

    args = parser.parse_args()

    if args.history:
        show_review_history()
        return

    run_ceo_review(
        scope=args.scope,
        min_pf=args.min_pf,
        max_dd=args.max_dd,
        min_wr=args.min_wr,
        min_trades=args.min_trades,
    )


if __name__ == "__main__":
    main()
