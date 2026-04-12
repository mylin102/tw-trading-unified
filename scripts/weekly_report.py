#!/usr/bin/env python3
"""
Weekly Strategic Report (L1) — runs every Monday morning.

Usage:
    python3 scripts/weekly_report.py              # Full report
    python3 scripts/weekly_report.py --dry-run    # Report only, no save

Generates:
    logs/weekly_reports/report_YYYY-MM-DD.json
    Decision log entry
    Dashboard-ready summary
"""
from __future__ import annotations

import argparse
import json
import sys
import csv
from pathlib import Path
from datetime import datetime, timedelta
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.decision_logger import DecisionLogger
from core.strategy_registry import get_strategy_ranking, STRATEGY_PERF

console = Console()
REPORTS_DIR = Path(__file__).resolve().parent.parent / "logs" / "weekly_reports"
TRADES_DIR = Path(__file__).resolve().parent.parent / "exports" / "trades"


def load_weekly_trades() -> list[dict]:
    """Load all trades from the past 7 days."""
    if not TRADES_DIR.exists():
        return []

    trades = []
    cutoff = datetime.now() - timedelta(days=7)

    for csv_file in TRADES_DIR.glob("*.csv"):
        try:
            with open(csv_file) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts_str = row.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_str) if ts_str else None
                    except ValueError:
                        ts = None
                    if ts and ts >= cutoff:
                        trades.append(row)
        except Exception:
            pass

    return trades


def compute_weekly_metrics(trades: list[dict]) -> dict:
    """Compute weekly PnL, WinRate, PF, per session."""
    day_trades = []
    night_trades = []

    for t in trades:
        if t.get("type") not in ("EXIT", "PARTIAL_EXIT"):
            continue
        try:
            pnl_pts = float(t.get("pnl_pts", 0))
        except (ValueError, TypeError):
            continue

        sess = t.get("session", "day").lower()
        if sess == "night":
            night_trades.append(pnl_pts)
        else:
            day_trades.append(pnl_pts)

    def calc_metrics(pts_list: list[float]) -> dict:
        wins = [p for p in pts_list if p > 0]
        losses = [p for p in pts_list if p < 0]
        total_trades = len(wins) + len(losses)
        gross_profit = sum(wins) or 1
        gross_loss = abs(sum(losses)) or 1
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        return {
            "pnl_pts": sum(pts_list),
            "trade_count": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / total_trades * 100 if total_trades > 0 else 0,
            "pf": round(pf, 2) if pf != float("inf") else "inf",
        }

    return {
        "day": calc_metrics(day_trades),
        "night": calc_metrics(night_trades),
        "total": calc_metrics(day_trades + night_trades),
    }


def generate_weekly_report(dry_run: bool = False) -> dict:
    """Generate the full weekly strategic report."""
    # 1. Load trades
    trades = load_weekly_trades()
    metrics = compute_weekly_metrics(trades)

    # 2. Strategy rankings (day/night)
    day_ranking = get_strategy_ranking("day")
    night_ranking = get_strategy_ranking("night")

    # 3. Recent decisions
    recent_decisions = DecisionLogger.read(limit=20)

    # 4. Strategy pipeline status
    pipeline = []
    for name, perf in STRATEGY_PERF.items():
        day_pf = perf.get("day_pf", 0)
        night_pf = perf.get("night_pf", 0)
        status = "✅ Active" if day_pf >= 1.3 else "⚠️ Watch" if day_pf >= 1.0 else "🔴 Retired"
        pipeline.append({
            "name": name,
            "day_pf": day_pf,
            "night_pf": night_pf,
            "status": status,
        })

    # 5. Build report
    report = {
        "period_start": (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
        "period_end": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "rankings": {
            "day": day_ranking,
            "night": night_ranking,
        },
        "pipeline": pipeline,
        "recent_decisions": [
            {"timestamp": d.timestamp, "type": d.type, "session": d.session, "action": d.action, "detail": d.detail}
            for d in recent_decisions[:10]
        ],
    }

    # 6. Generate recommendations
    recommendations = _generate_recommendations(metrics, pipeline)
    report["recommendations"] = recommendations

    # 7. Save report
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"report_{ts}.json"

    if not dry_run:
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        console.print(f"[dim]💾 Report saved: {report_path}[/dim]")

    return report


def _generate_recommendations(metrics: dict, pipeline: list) -> list[str]:
    """Generate strategic recommendations based on weekly metrics."""
    recs = []

    total = metrics.get("total", {})
    day = metrics.get("day", {})
    night = metrics.get("night", {})

    # Check overall performance
    pf_total = total.get("pf", 0)
    if pf_total == "inf":
        recs.append("✅ 整體 PF 極高，無虧損交易。")
    elif isinstance(pf_total, (int, float)) and pf_total >= 1.5:
        recs.append(f"✅ 整體 PF={pf_total:.2f} 表現優異。")
    elif isinstance(pf_total, (int, float)) and pf_total < 1.0:
        recs.append(f"🔴 整體 PF={pf_total:.2f} < 1.0。考慮全面檢視策略。")
    elif isinstance(pf_total, (int, float)) and pf_total < 1.3:
        recs.append(f"⚠️ 整體 PF={pf_total:.2f} < 1.3。持續觀察。")

    # Day vs Night comparison
    day_pf = day.get("pf", 0)
    night_pf = night.get("pf", 0)
    if isinstance(day_pf, (int, float)) and isinstance(night_pf, (int, float)):
        if day_pf >= 1.5 and night_pf < 1.0:
            recs.append(f"⚠️ 日盤 PF={day_pf:.2f} 但夜盤 PF={night_pf:.2f}。考慮夜盤降口數或換策略。")

    # Pipeline health
    retired = [p for p in pipeline if "Retired" in p["status"]]
    if retired:
        names = [p["name"] for p in retired]
        recs.append(f"📊 退役策略: {', '.join(names)}")

    if not recs:
        recs.append("✅ 本週無重大問題。")

    return recs


def main():
    parser = argparse.ArgumentParser(description="Weekly Strategic Report")
    parser.add_argument("--dry-run", action="store_true", help="Print only, don't save")
    args = parser.parse_args()

    console.print("[bold]📊 Weekly Strategic Report[/bold]")
    console.print(f"[dim]Generated: {datetime.now().isoformat()}[/dim]\n")

    report = generate_weekly_report(dry_run=args.dry_run)

    # Print summary
    m = report["metrics"]
    total = m.get("total", {})
    day = m.get("day", {})
    night = m.get("night", {})

    md = f"""
## 本週表現
| | 日盤 | 夜盤 | 總計 |
|--|--|--|--|
| PnL (pts) | {day.get('pnl_pts', 0):.0f} | {night.get('pnl_pts', 0):.0f} | {total.get('pnl_pts', 0):.0f} |
| 交易數 | {day.get('trade_count', 0)} | {night.get('trade_count', 0)} | {total.get('trade_count', 0)} |
| 勝率 | {day.get('win_rate', 0):.1f}% | {night.get('win_rate', 0):.1f}% | {total.get('win_rate', 0):.1f}% |
| PF | {day.get('pf', 0)} | {night.get('pf', 0)} | {total.get('pf', 0)} |

## 策略管道
{''.join(f'- {p["name"]}: 日盤 PF={p["day_pf"]:.1f}, 夜盤 PF={p["night_pf"]:.1f} {p["status"]}\n' for p in report["pipeline"])}

## 建議
{''.join(f'- {r}\n' for r in report["recommendations"])}
"""
    console.print(Panel(Markdown(md), title="Weekly Report", border_style="cyan"))


if __name__ == "__main__":
    main()
