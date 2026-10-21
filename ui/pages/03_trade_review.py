# 2026-10-21 Hermes Agent: add a read-only daily MTS Trade Review report.
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import streamlit as st

from core.performance_provenance import _record_trading_day
from core.runtime_paths import mts_runtime_path


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _trading_day(timestamp, session):
    if not timestamp:
        return None
    return _record_trading_day({"timestamp": timestamp, "session": session})


def _today_trading_day():
    return _trading_day(datetime.now().isoformat(), "UNKNOWN")


def _session_name(rows, entry_time, exit_time):
    for row in rows:
        label = str(row.get("session") or row.get("session_label") or "").lower()
        if "night" in label:
            return "night"
        if "day" in label:
            return "day"
    timestamp = entry_time or exit_time
    if timestamp:
        try:
            hour = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).hour
            return "night" if hour >= 15 or hour < 8 else "day"
        except (TypeError, ValueError):
            pass
    return "day"


def _duration_seconds(start, end):
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0.0, (end_dt - start_dt).total_seconds())
    except (TypeError, ValueError):
        return None


def _quantile(sorted_values, probability):
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight


def _fill_costs(rows):
    friction = 0.0
    unitemized = 0
    gross_values = []
    for row in rows:
        gross = _number(row.get("gross_pnl"))
        fee = _number(row.get("fee"))
        tax = _number(row.get("tax"))
        if gross is not None:
            gross_values.append(gross)
        if gross is None or fee is None or tax is None:
            unitemized += 1
        else:
            friction += fee + tax
    gross_total = sum(gross_values) if len(gross_values) == len(rows) and rows else None
    return gross_total, friction, unitemized


def build_daily_review(fills, events, selected_day=None, today_trading_day=None):
    """Aggregate completed MTS trade_ids using the dashboard's ledger day rule."""
    today = today_trading_day or _today_trading_day()
    available_days = sorted(
        {
            day
            for fill in fills
            if (day := _trading_day(
                _timestamp(fill, "timestamp", "ts", "fill_timestamp"),
                fill.get("session") or fill.get("session_label") or "UNKNOWN",
            ))
        },
        reverse=True,
    )
    chosen_day = selected_day if selected_day in available_days else None
    if chosen_day is None:
        chosen_day = today if today in available_days else (available_days[0] if available_days else today)

    grouped = defaultdict(list)
    for fill in fills:
        trade_id = fill.get("trade_id")
        if trade_id:
            grouped[str(trade_id)].append(fill)

    selected_trades = []
    for trade_id, rows in grouped.items():
        entries = [row for row in rows if str(row.get("fill_type", "")).upper() == "ENTRY"]
        terminal = [
            row for row in rows
            if str(row.get("fill_type", "")).upper() in ("EXIT", "COMBINED_EXIT")
        ]
        releases = [row for row in rows if str(row.get("fill_type", "")).upper() == "RELEASE"]
        if not terminal:
            continue

        terminal.sort(key=lambda row: _timestamp(row, "timestamp", "ts") or "")
        entry_time = next(
            (_timestamp(row, "entry_ts", "timestamp", "ts") for row in entries
             if _timestamp(row, "entry_ts", "timestamp", "ts")),
            next((_timestamp(row, "entry_ts") for row in rows if _timestamp(row, "entry_ts")), None),
        )
        exit_time = _timestamp(terminal[-1], "exit_ts", "timestamp", "ts")
        close_rows = releases + terminal
        session = _session_name(rows, entry_time, exit_time)
        trade_day = _trading_day(exit_time, session)
        if trade_day != chosen_day:
            continue

        net_values = [_number(row.get("realized_pnl")) for row in close_rows]
        net_values = [value for value in net_values if value is not None]
        net_pnl = sum(net_values) if net_values else None
        gross_pnl, friction_cost, unitemized_rows = _fill_costs(close_rows)
        hold_seconds = _duration_seconds(entry_time, exit_time)
        selected_trades.append({
            "trade_id": trade_id,
            "entry_time": entry_time or "進場時間缺失",
            "exit_time": exit_time or "N/A",
            "holding_seconds": hold_seconds,
            "session": session,
            "gross_pnl": gross_pnl,
            "friction_cost": friction_cost if close_rows and not unitemized_rows else None,
            "_itemized_friction_cost": friction_cost,
            "unitemized_cost_rows": unitemized_rows,
            "net_pnl": net_pnl,
            "close_rows": close_rows,
        })

    selected_ids = {row["trade_id"] for row in selected_trades}
    combined_reasons = defaultdict(set)
    for event in events:
        trade_id = str(event.get("trade_id") or "")
        if trade_id not in selected_ids or event.get("event") != "COMBINED_EXIT_SUBMITTED":
            continue
        event_day = _trading_day(
            _timestamp(event, "ts", "timestamp"),
            event.get("session") or event.get("session_label") or "UNKNOWN",
        )
        if event_day != chosen_day:
            continue
        reason = str(event.get("policy_j_trigger_path") or "UNKNOWN")
        combined_reasons[trade_id].add(reason)

    fill_reasons = defaultdict(set)
    for trade in selected_trades:
        for row in trade["close_rows"]:
            fill_type = str(row.get("fill_type", "")).upper()
            if fill_type in ("EXIT", "RELEASE"):
                fill_reasons[fill_type].add(trade["trade_id"])
        reasons = combined_reasons.get(trade["trade_id"])
        if reasons:
            trade["exit_reason"] = " / ".join(sorted(reasons))
        else:
            trade["exit_reason"] = " / ".join(sorted({
                str(row.get("fill_type", "UNKNOWN")).upper() for row in trade["close_rows"]
            })) or "UNKNOWN"

    def _session_summary(session_key):
        session_trades = [row for row in selected_trades if row["session"] == session_key]
        return {
            "trade_count": len(session_trades),
            "net_pnl": sum(row["net_pnl"] or 0.0 for row in session_trades),
            "friction_cost": sum(row["_itemized_friction_cost"] for row in session_trades),
            "unitemized_cost_rows": sum(row["unitemized_cost_rows"] for row in session_trades),
        }

    close_rows = [fill for trade in selected_trades for fill in trade["close_rows"]]
    gross_pnl, friction_cost, unitemized_cost_rows = _fill_costs(close_rows)
    net_values = [trade["net_pnl"] for trade in selected_trades if trade["net_pnl"] is not None]
    holding_values = sorted(
        trade["holding_seconds"] for trade in selected_trades
        if trade["holding_seconds"] is not None
    )
    trade_count = len(selected_trades)
    return {
        "available_days": available_days,
        "selected_day": chosen_day,
        "summary": {
            "net_pnl": sum(net_values) if net_values else 0.0,
            "trade_count": trade_count,
            "win_rate": sum(value > 0 for value in net_values) / trade_count if trade_count else None,
            "friction_cost": friction_cost,
            "unitemized_cost_rows": unitemized_cost_rows,
            "gross_pnl": gross_pnl,
        },
        "sessions": {"day": _session_summary("day"), "night": _session_summary("night")},
        "exit_reasons": {
            "policy_j_trigger_path": {
                reason: len(ids)
                for reason, ids in _count_trade_reasons(combined_reasons, selected_ids).items()
            },
            "fill_type": {reason: len(ids) for reason, ids in sorted(fill_reasons.items())},
        },
        "holding_time": {
            "count": len(holding_values),
            "median_seconds": _quantile(holding_values, 0.5),
            "p25_seconds": _quantile(holding_values, 0.25),
            "p75_seconds": _quantile(holding_values, 0.75),
            "under_60_seconds": sum(value < 60 for value in holding_values),
        },
        "trades": selected_trades,
    }


def _count_trade_reasons(reasons_by_trade, selected_ids):
    counts = defaultdict(set)
    for trade_id, reasons in reasons_by_trade.items():
        if trade_id in selected_ids:
            for reason in reasons:
                counts[reason].add(trade_id)
    return counts


def _read_jsonl(path):
    records = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _money(value):
    return "N/A" if value is None else f"{value:+,.0f}"


fills_path = Path(mts_runtime_path("logs", "mts_trade_fills.jsonl"))
events_path = Path(mts_runtime_path("logs", "mts_spread_events.jsonl"))

st.set_page_config(page_title="Trade Review | Trading Unified", page_icon="🔍", layout="wide")
st.title("🔍 交易檢討 (Trade Review)")
st.caption("唯讀每日交易報告；帳本資料依交易日彙整。")

try:
    all_fills = _read_jsonl(fills_path)
    all_events = _read_jsonl(events_path)
except OSError as exc:
    all_fills = []
    all_events = []
    st.error(f"讀取交易帳本失敗: {exc}")

report = build_daily_review(all_fills, all_events)
if not report["available_days"]:
    st.info("尚無帳本交易日可供檢討。")
else:
    selected_day = st.selectbox(
        "交易日",
        options=report["available_days"],
        index=report["available_days"].index(report["selected_day"]),
    )
    report = build_daily_review(
        all_fills,
        all_events,
        selected_day=selected_day,
        today_trading_day=report["selected_day"] if report["selected_day"] in report["available_days"] else None,
    )
    summary = report["summary"]
    st.subheader(f"每日摘要：{report['selected_day']}")
    summary_cols = st.columns(5)
    summary_cols[0].metric("淨損益", f"{summary['net_pnl']:+,.0f} TWD")
    summary_cols[1].metric("交易筆數", summary["trade_count"])
    summary_cols[2].metric(
        "勝率", "N/A" if summary["win_rate"] is None else f"{summary['win_rate']:.1%}"
    )
    summary_cols[3].metric("摩擦成本合計", f"{summary['friction_cost']:,.0f} TWD")
    summary_cols[4].metric("毛利", f"{_money(summary['gross_pnl'])} TWD")
    if summary["unitemized_cost_rows"]:
        st.caption(f"{summary['unitemized_cost_rows']} 列未分列費用；未估算缺失成本。")

    st.subheader("日盤 / 夜盤")
    session_cols = st.columns(2)
    for column, session_key, label in zip(session_cols, ("day", "night"), ("日盤", "夜盤")):
        data = report["sessions"][session_key]
        column.metric(f"{label}筆數", data["trade_count"])
        column.metric(f"{label}淨損益", f"{data['net_pnl']:+,.0f} TWD")
        column.metric(f"{label}摩擦成本", f"{data['friction_cost']:,.0f} TWD")
        if data["unitemized_cost_rows"]:
            column.caption(f"{data['unitemized_cost_rows']} 列未分列費用")

    st.subheader("出場原因分布（來源分列）")
    policy_rows = [
        {"來源": "policy_j_trigger_path", "原因": reason, "筆數": count}
        for reason, count in sorted(report["exit_reasons"]["policy_j_trigger_path"].items())
    ]
    fill_rows = [
        {"來源": "fill_type", "原因": reason, "筆數": count}
        for reason, count in sorted(report["exit_reasons"]["fill_type"].items())
    ]
    reason_cols = st.columns(2)
    reason_cols[0].caption("COMBINED_EXIT_SUBMITTED · policy_j_trigger_path")
    reason_cols[0].dataframe(policy_rows, hide_index=True, use_container_width=True)
    reason_cols[1].caption("EXIT / RELEASE · fill_type")
    reason_cols[1].dataframe(fill_rows, hide_index=True, use_container_width=True)

    holding = report["holding_time"]
    st.subheader("持有時間")
    holding_cols = st.columns(4)
    for column, label, key in zip(
        holding_cols,
        ("中位數（秒）", "P25（秒）", "P75（秒）", "少於 60 秒"),
        ("median_seconds", "p25_seconds", "p75_seconds", "under_60_seconds"),
    ):
        value = holding[key]
        column.metric(label, "N/A" if value is None else f"{value:g}")

    st.subheader("逐筆明細")
    detail_rows = [
        {
            "trade_id": trade["trade_id"],
            "進場時間": trade["entry_time"],
            "出場時間": trade["exit_time"],
            "持有秒數": "N/A" if trade["holding_seconds"] is None else trade["holding_seconds"],
            "出場原因": trade["exit_reason"],
            "毛利": _money(trade["gross_pnl"]),
            "摩擦成本": _money(trade["friction_cost"]),
            "淨損益": _money(trade["net_pnl"]),
        }
        for trade in report["trades"]
    ]
    st.dataframe(detail_rows, hide_index=True, use_container_width=True)
