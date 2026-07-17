#!/usr/bin/env python3
# 2026-07-08 Gemini CLI: Automated Daily Review and HTML Report Generator for MTS Spread Trading.

import os
import sys
import json
from datetime import datetime, timedelta
from collections import defaultdict

def get_trading_day(timestamp_str: str, session: str) -> str:
    """Resolve the trading day based on calendar timestamp and trading session."""
    try:
        dt = datetime.fromisoformat(timestamp_str)
        if session == "night" and dt.hour >= 15:
            return (dt + timedelta(days=1)).strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return timestamp_str.split("T")[0]

def format_triplet(val1, val2, val3, decimals=0) -> str:
    """Format indicators triplet as val1/val2/val3 with specified decimals."""
    parts = []
    for val in (val1, val2, val3):
        if val is None or val == "":
            parts.append("-")
        else:
            try:
                f_val = float(val)
                parts.append(f"{f_val:.{decimals}f}")
            except (ValueError, TypeError):
                parts.append(str(val))
    return "/".join(parts)

def parse_logs(fills_path: str, events_path: str, target_trading_day: str) -> dict:
    if not os.path.exists(fills_path):
        print(f"Error: Fills log file not found at {fills_path}", file=sys.stderr)
        return {}

    trades = defaultdict(lambda: {
        "entries": [],
        "release": None,
        "exit": None,
        "exit_reason": "UNKNOWN",
        "entry_ts": None,
        "exit_ts": None,
        "risk_mode": "UNKNOWN",
        "session": "UNKNOWN"
    })
    
    # 1. Parse fills log (primary data source for PnL and execution)
    with open(fills_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                fill = json.loads(line)
                trade_id = fill.get("trade_id")
                if not trade_id:
                    continue
                
                fill_type = fill.get("fill_type")
                ts_str = fill.get("timestamp", "")
                session = fill.get("session", "UNKNOWN")
                
                if fill_type == "ENTRY":
                    trades[trade_id]["entries"].append(fill)
                    if not trades[trade_id]["entry_ts"]:
                        trades[trade_id]["entry_ts"] = ts_str
                    trades[trade_id]["session"] = session
                elif fill_type == "RELEASE":
                    trades[trade_id]["release"] = fill
                elif fill_type == "EXIT":
                    trades[trade_id]["exit"] = fill
                    trades[trade_id]["exit_ts"] = ts_str
            except Exception as e:
                print(f"Warning: Failed to parse fill line: {e}", file=sys.stderr)
                continue

    # 2. Parse events log to map timestamps/orders to trade_ids, and extract details
    order_to_trade = {}
    time_to_trade = {}
    trade_mtf_scores = {}  # trade_id -> mtf_score (from RELEASE_SUBMITTED or EXIT_LOG event)
    
    if os.path.exists(events_path):
        # First pass: Build mappings
        with open(events_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    trade_id = event.get("trade_id")
                    ts = event.get("ts", "")
                    if ts and trade_id:
                        time_to_trade[ts[:19]] = trade_id
                    
                    order_id = event.get("order_id")
                    if order_id and trade_id:
                        order_to_trade[order_id] = trade_id
                except Exception:
                    continue
                    
        # Second pass: Associate events with trades
        with open(events_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    ev_type = event.get("event")
                    ts = event.get("ts", "")
                    trade_id = event.get("trade_id")
                    
                    if not trade_id and ts:
                        trade_id = time_to_trade.get(ts[:19])
                    
                    if not trade_id:
                        continue
                        
                    if ev_type == "ENTRY_SUBMITTED":
                        for key in ["mtf_score", "vwap", "near_vwap", "far_vwap"]:
                            val = event.get(key)
                            if val is not None:
                                trades[trade_id][f"entry_{key}"] = val
                    elif ev_type in ("RELEASE_NEAR_SUBMITTED", "RELEASE_FAR_SUBMITTED"):
                        trades[trade_id]["risk_mode"] = event.get("risk_mode", "UNKNOWN")
                        for key in ["mtf_score", "vwap", "near_vwap", "far_vwap"]:
                            val = event.get(key)
                            if val is not None:
                                trades[trade_id][f"release_{key}"] = val
                                if key == "mtf_score":
                                    trades[trade_id]["mtf_score"] = val
                    elif ev_type in ("EXIT_REMAINING", "EXIT_LOG"):
                        if ev_type == "EXIT_REMAINING":
                            trades[trade_id]["exit_reason"] = event.get("reason", "UNKNOWN")
                            trades[trade_id]["risk_mode"] = event.get("risk_mode", "UNKNOWN")
                        if ev_type == "EXIT_LOG":
                            trades[trade_id]["mfe"] = event.get("mfe")
                        for key in ["mtf_score", "vwap", "near_vwap", "far_vwap"]:
                            val = event.get(key)
                            if val is not None:
                                trades[trade_id][f"exit_{key}"] = val
                                if key == "mtf_score":
                                    trades[trade_id]["mtf_score"] = val
                except Exception:
                    continue

    # Classify trades based on trading day
    report_data = {
        "completed": [],
        "active": []
    }
    
    for trade_id, data in trades.items():
        # Check if the trade is active (has entries but no exit fill)
        if len(data["entries"]) > 0 and not data["exit"]:
            near_entry = next((e for e in data["entries"] if e["leg"] == "NEAR"), None)
            far_entry = next((e for e in data["entries"] if e["leg"] == "FAR"), None)
            
            entry_ts = data["entry_ts"]
            session = data["session"]
            trading_day = get_trading_day(entry_ts, session)
            
            if trading_day == target_trading_day:
                report_data["active"].append({
                    "trade_id": trade_id,
                    "entry_time": entry_ts,
                    "session": session,
                    "near_entry": near_entry["price"] if near_entry else 0.0,
                    "far_entry": far_entry["price"] if far_entry else 0.0,
                    "spread_z": near_entry.get("spread_z") if near_entry else None,
                    "atr": near_entry.get("atr") if near_entry else None,
                    "action": f"SELL Near / BUY Far" if near_entry and near_entry.get("side") == "SHORT" else "BUY Near / SELL Far",
                    # 2026-07-16 Gemini CLI: MTF and VWAP indicators at entry
                    "entry_mtf": data.get("entry_mtf_score"),
                    "entry_vwap": data.get("entry_vwap"),
                })
        elif len(data["entries"]) > 0 and data["exit"]:
            exit_ts = data["exit_ts"]
            session = data["session"]
            trading_day = get_trading_day(exit_ts, session)
            
            if trading_day == target_trading_day:
                near_entry = next((e for e in data["entries"] if e["leg"] == "NEAR"), None)
                far_entry = next((e for e in data["entries"] if e["leg"] == "FAR"), None)
                
                release_fill = data["release"]
                exit_fill = data["exit"]
                
                release_pnl = release_fill.get("realized_pnl", 0.0) if release_fill else 0.0
                exit_pnl = exit_fill.get("realized_pnl", 0.0) if exit_fill else 0.0
                net_pnl = release_pnl + exit_pnl
                
                # 2026-07-17 Gemini CLI: Calculate durations for release and trail phases
                import pandas as pd
                release_ts = release_fill.get("timestamp") if release_fill else None
                entry_ts = data["entry_ts"]
                exit_ts = data["exit_ts"]
                
                release_duration_str = "—"
                trail_duration_str = "—"
                
                if entry_ts and release_ts:
                    try:
                        t_entry = pd.to_datetime(entry_ts)
                        t_release = pd.to_datetime(release_ts)
                        diff = t_release - t_entry
                        tot_sec = int(diff.total_seconds())
                        if tot_sec < 0: tot_sec = 0
                        h = tot_sec // 3600
                        m = (tot_sec % 3600) // 60
                        s = tot_sec % 60
                        if h > 0:
                            release_duration_str = f"{h}h {m}m"
                        else:
                            release_duration_str = f"{m}m {s}s"
                    except Exception:
                        pass
                
                if release_ts and exit_ts:
                    try:
                        t_release = pd.to_datetime(release_ts)
                        t_exit = pd.to_datetime(exit_ts)
                        diff = t_exit - t_release
                        tot_sec = int(diff.total_seconds())
                        if tot_sec < 0: tot_sec = 0
                        h = tot_sec // 3600
                        m = (tot_sec % 3600) // 60
                        s = tot_sec % 60
                        if h > 0:
                            trail_duration_str = f"{h}h {m}m"
                        else:
                            trail_duration_str = f"{m}m {s}s"
                    except Exception:
                        pass
                
                # 2026-07-17 Gemini CLI: Calculate Release Efficiency (Post-Release capture ratio)
                # Gating Invariant: ε = 100 TWD
                mfe_pts = data.get("mfe")
                release_efficiency_str = "—"
                if mfe_pts is not None:
                    peak_pnl = float(mfe_pts) * 10.0
                    if peak_pnl > 100.0:
                        release_efficiency_str = f"{(net_pnl / peak_pnl):.1%}"
                
                report_data["completed"].append({
                    "trade_id": trade_id,
                    "entry_time": data["entry_ts"],
                    "exit_time": data["exit_ts"],
                    "session": session,
                    "action": f"SELL Near / BUY Far" if near_entry and near_entry.get("side") == "SHORT" else "BUY Near / SELL Far",
                    "near_entry": near_entry["price"] if near_entry else 0.0,
                    "far_entry": far_entry["price"] if far_entry else 0.0,
                    "release_leg": release_fill.get("leg") if release_fill else "UNKNOWN",
                    "release_price": release_fill.get("price") if release_fill else 0.0,
                    "release_pnl": release_pnl,
                    "exit_price": exit_fill.get("price") if exit_fill else 0.0,
                    "exit_pnl": exit_pnl,
                    "exit_reason": data["exit_reason"],
                    "net_pnl": net_pnl,
                    "risk_mode": data["risk_mode"],
                    "mtf_score": data.get("mtf_score"),
                    # 2026-07-16 Gemini CLI: MTF and VWAP indicators at key points
                    "entry_mtf": data.get("entry_mtf_score"),
                    "entry_vwap": data.get("entry_vwap"),
                    "release_mtf": data.get("release_mtf_score"),
                    "release_vwap": data.get("release_vwap"),
                    "exit_mtf": data.get("exit_mtf_score"),
                    "exit_vwap": data.get("exit_vwap"),
                    "release_duration": release_duration_str,
                    "trail_duration": trail_duration_str,
                    "release_efficiency": release_efficiency_str
                })
                
    return report_data


def generate_html(report_data: dict, target_trading_day: str) -> str:
    completed = report_data["completed"]
    active = report_data["active"]
    
    # Calculate global metrics
    total_trades = len(completed)
    win_trades = sum(1 for t in completed if t["net_pnl"] > 0)
    loss_trades = sum(1 for t in completed if t["net_pnl"] <= 0)
    win_rate = (win_trades / total_trades) if total_trades > 0 else 0.0
    
    total_net_pnl = sum(t["net_pnl"] for t in completed)
    gross_wins = sum(t["net_pnl"] for t in completed if t["net_pnl"] > 0)
    gross_losses = sum(abs(t["net_pnl"]) for t in completed if t["net_pnl"] <= 0)
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (99.9 if gross_wins > 0 else 0.0)
    avg_net_pnl = (total_net_pnl / total_trades) if total_trades > 0 else 0.0

    # Calculate metrics by Day vs. Night Session
    sessions = {"day": {"trades": [], "wins": 0, "losses": 0, "net_pnl": 0.0},
                "night": {"trades": [], "wins": 0, "losses": 0, "net_pnl": 0.0}}
                
    for t in completed:
        sess = t["session"].lower()
        if sess in sessions:
            sessions[sess]["trades"].append(t)
            sessions[sess]["net_pnl"] += t["net_pnl"]
            if t["net_pnl"] > 0:
                sessions[sess]["wins"] += 1
            else:
                sessions[sess]["losses"] += 1

    # Analysis & Optimization suggestions
    suggestions = []
    if total_trades > 0:
        day_pnl = sessions["day"]["net_pnl"]
        night_pnl = sessions["night"]["net_pnl"]
        if night_pnl < day_pnl and len(sessions["night"]["trades"]) > 0:
            suggestions.append("⚠️ <b>夜盤表現落後日盤</b>：考慮收緊夜盤的 `max_spread_width` (滑價保護)，或微調夜盤移動停利比例以防流動性差時利潤回吐。")
        if any(t["exit_reason"] == "RELEASE_STOP" for t in completed):
            suggestions.append("💡 <b>觸及單腿釋放停損</b>：可考慮評估布林濾網（BB Filter）是否在該時間點正常發揮延遲平倉效果，或適度擴大 ATR 停損倍數。")
        if avg_net_pnl < 100.0:
            suggestions.append("⚠️ <b>平均每筆淨利偏低</b>：目前的移動停利或釋放停損可能太近，容易在小幅震盪中被震出場。可嘗試採用 Sweep 中勝率較佳的 `Stop 2.5x / Trail 2.0x` 參數。")
    else:
        suggestions.append("💡 今日無已完成交易。請繼續觀察進行中的持倉部位。")

    # Styling helper classes
    pnl_class = "text-green" if total_net_pnl >= 0 else "text-red"
    pnl_symbol = "+" if total_net_pnl >= 0 else ""
    
    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <title>MTS 價差交易日報表 - {target_trading_day}</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-dark: #0f172a;
            --bg-card: #1e293b;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #3b82f6;
            --success: #10b981;
            --danger: #ef4444;
            --border: #334155;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            background-color: var(--bg-dark);
            color: var(--text-main);
            font-family: 'Inter', sans-serif;
            line-height: 1.6;
            padding: 2rem 1.5rem;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        header {{
            margin-bottom: 2rem;
            border-bottom: 1px solid var(--border);
            padding-bottom: 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        h1 {{
            font-family: 'Outfit', sans-serif;
            font-weight: 700;
            font-size: 2.2rem;
            background: linear-gradient(to right, #3b82f6, #60a5fa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .header-meta {{
            text-align: right;
            color: var(--text-muted);
            font-size: 0.9rem;
        }}
        .header-meta code {{
            background-color: var(--bg-card);
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            color: var(--primary);
        }}
        
        /* KPI Cards Grid */
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1.2rem;
            margin-bottom: 2rem;
        }}
        .kpi-card {{
            background-color: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1);
        }}
        .kpi-title {{
            font-size: 0.85rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.5rem;
            font-weight: 600;
        }}
        .kpi-value {{
            font-size: 1.8rem;
            font-weight: 700;
            font-family: 'Outfit', sans-serif;
        }}
        .text-green {{ color: var(--success); }}
        .text-red {{ color: var(--danger); }}
        
        /* Sections Layout */
        .section-title {{
            font-family: 'Outfit', sans-serif;
            font-size: 1.4rem;
            font-weight: 600;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        .card {{
            background-color: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1);
        }}
        
        /* Tables styling */
        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.9rem;
        }}
        th {{
            color: var(--text-muted);
            font-weight: 600;
            padding: 0.75rem 1rem;
            border-bottom: 2px solid var(--border);
            text-transform: uppercase;
            font-size: 0.8rem;
            letter-spacing: 0.03em;
        }}
        td {{
            padding: 1rem;
            border-bottom: 1px solid var(--border);
        }}
        tr:hover td {{
            background-color: rgba(255, 255, 255, 0.02);
        }}
        
        /* Badges */
        .badge {{
            display: inline-block;
            padding: 0.2rem 0.5rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-day {{ background-color: rgba(59, 130, 246, 0.15); color: #60a5fa; }}
        .badge-night {{ background-color: rgba(139, 92, 246, 0.15); color: #a78bfa; }}
        .badge-reason {{ background-color: rgba(245, 158, 11, 0.15); color: #fbbf24; }}
        
        /* Optimization Tips */
        .opt-list {{
            list-style-type: none;
        }}
        .opt-item {{
            background-color: rgba(59, 130, 246, 0.05);
            border-left: 4px solid var(--primary);
            padding: 1rem;
            border-radius: 0 8px 8px 0;
            margin-bottom: 0.8rem;
            font-size: 0.95rem;
        }}
        .opt-item.warning {{
            background-color: rgba(239, 68, 68, 0.05);
            border-left-color: var(--danger);
      }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>MTS 價差交易日績效檢討</h1>
                <p style="color: var(--text-muted); font-size: 0.95rem; margin-top: 0.3rem;">策略版本：<code>tmf_spread</code> (ATR 動態防護 & BB 濾網載入)</p>
            </div>
            <div class="header-meta">
                <p>交易日：<code>{target_trading_day}</code></p>
                <p style="margin-top: 0.2rem; font-size: 0.8rem;">生成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
        </header>

        <!-- KPI Grid -->
        <div class="kpi-grid">
            <div class="kpi-card">
                <span class="kpi-title">本日淨損益 (Net PnL)</span>
                <span class="kpi-value {pnl_class}">{pnl_symbol}${total_net_pnl:,.1f} TWD</span>
            </div>
            <div class="kpi-card">
                <span class="kpi-title">已完成交易圈數</span>
                <span class="kpi-value">{total_trades}</span>
            </div>
            <div class="kpi-card">
                <span class="kpi-title">勝率 (Win Rate)</span>
                <span class="kpi-value { 'text-green' if win_rate >= 0.5 else 'text-red' }">{win_rate:.1%}</span>
            </div>
            <div class="kpi-card">
                <span class="kpi-title">獲利因子 (Profit Factor)</span>
                <span class="kpi-value">{profit_factor:.2f}</span>
            </div>
            <div class="kpi-card">
                <span class="kpi-title">每筆平均淨損益 (PnL)</span>
                <span class="kpi-value {pnl_class}">{pnl_symbol}${avg_net_pnl:,.1f} TWD</span>          
            </div>
        </div>

        <!-- Session Comparison Card -->
        <div class="section-title">📊 日盤 vs 夜盤 績效對比</div>
        <div class="card" style="padding: 0;">
            <table>
                <thead>
                    <tr>
                        <th>盤別</th>
                        <th>已完結筆數</th>
                        <th>勝 / 敗</th>
                        <th>勝率</th>
                        <th>盤別淨損益</th>
                        <th>平均單筆損益</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td><span class="badge badge-day">☀️ 日盤 (Day)</span></td>
                        <td>{len(sessions["day"]["trades"])}</td>
                        <td>{sessions["day"]["wins"]} 勝 / {sessions["day"]["losses"]} 敗</td>
                        <td>{ (sessions["day"]["wins"] / len(sessions["day"]["trades"]) if len(sessions["day"]["trades"]) > 0 else 0.0):.1%}</td>
                        <td class="{ 'text-green' if sessions['day']['net_pnl'] >= 0 else 'text-red' }">
                            { '+' if sessions['day']['net_pnl'] >= 0 else '' }${sessions['day']['net_pnl']:,.1f} TWD
                        </td>
                        <td class="{ 'text-green' if sessions['day']['net_pnl'] >= 0 else 'text-red' }">
                            { '+' if sessions['day']['net_pnl'] >= 0 else '' }${(sessions['day']['net_pnl'] / len(sessions["day"]["trades"]) if len(sessions["day"]["trades"]) > 0 else 0.0):,.1f} TWD
                        </td>
                    </tr>
                    <tr>
                        <td><span class="badge badge-night">🌙 夜盤 (Night)</span></td>
                        <td>{len(sessions["night"]["trades"])}</td>
                        <td>{sessions["night"]["wins"]} 勝 / {sessions["night"]["losses"]} 敗</td>
                        <td>{ (sessions["night"]["wins"] / len(sessions["night"]["trades"]) if len(sessions["night"]["trades"]) > 0 else 0.0):.1%}</td>
                        <td class="{ 'text-green' if sessions['night']['net_pnl'] >= 0 else 'text-red' }">
                            { '+' if sessions['night']['net_pnl'] >= 0 else '' }${sessions['night']['net_pnl']:,.1f} TWD
                        </td>
                        <td class="{ 'text-green' if sessions['night']['net_pnl'] >= 0 else 'text-red' }">
                            { '+' if sessions['night']['net_pnl'] >= 0 else '' }${(sessions['night']['net_pnl'] / len(sessions["night"]["trades"]) if len(sessions["night"]["trades"]) > 0 else 0.0):,.1f} TWD
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>

        <!-- Closed Trades Card -->
        <div class="section-title">📝 本日完結交易清單 (Closed Loops)</div>
        <div class="card" style="padding: 0; overflow-x: auto;">
            <table>
                <thead>
                    <tr>
                        <th>交易 ID</th>
                        <th>時段</th>
                        <th>方向</th>
                        <th>進場時間</th>
                        <th>第一腿 PnL</th>
                        <th>第一腿時間</th>
                        <th>第二腿 PnL</th>
                        <th>第二腿時間</th>
                        <th>釋放原因</th>
                        <th>釋放效率</th>
                        <th>淨損益</th>
                        <th>風控模式</th>
                        <th>MTF (Entry/Rel/Exit)</th>
                        <th>VWAP (Entry/Rel/Exit)</th>
                    </tr>
                </thead>
                <tbody>"""
                
    if completed:
        for t in completed:
            net_class = "text-green" if t["net_pnl"] >= 0 else "text-red"
            net_symbol = "+" if t["net_pnl"] >= 0 else ""
            badge_class = "badge-day" if t["session"].lower() == "day" else "badge-night"
            sess_label = "☀️ 日盤" if t["session"].lower() == "day" else "🌙 夜盤"
            
            mtf_str = format_triplet(t.get('entry_mtf'), t.get('release_mtf'), t.get('exit_mtf'), decimals=1)
            vwap_str = format_triplet(t.get('entry_vwap'), t.get('release_vwap'), t.get('exit_vwap'), decimals=0)
            
            # 2026-07-17 Gemini CLI: Render first/second leg duration and release efficiency columns in closed loops table
            html += f"""
                    <tr>
                        <td><code>{t['trade_id']}</code></td>
                        <td><span class="badge {badge_class}">{sess_label}</span></td>
                        <td style="font-size: 0.85rem; color: var(--text-muted);">{t['action']}</td>
                        <td>{t['entry_time'].split('T')[1][:8]}</td>
                        <td class="{ 'text-green' if t['release_pnl'] >= 0 else 'text-red' }">${t['release_pnl']:+,.1f}</td>
                        <td style="font-size: 0.85rem;">{t.get('release_duration', '—')}</td>
                        <td class="{ 'text-green' if t['exit_pnl'] >= 0 else 'text-red' }">${t['exit_pnl']:+,.1f}</td>
                        <td style="font-size: 0.85rem;">{t.get('trail_duration', '—')}</td>
                        <td><span class="badge badge-reason">{t['exit_reason']}</span></td>
                        <td style="font-size: 0.85rem; font-weight: 600;">{t.get('release_efficiency', '—')}</td>
                        <td class="{net_class}" style="font-weight: 600;">{net_symbol}${t['net_pnl']:,.1f}</td>
                        <td style="font-size: 0.85rem; color: var(--text-muted);"><code>{t['risk_mode']}</code></td>
                        <td style="font-size: 0.85rem;"><code>{mtf_str}</code></td>
                        <td style="font-size: 0.85rem;"><code>{vwap_str}</code></td>
                    </tr>"""
    else:
        html += """
                    <tr>
                        <td colspan="14" style="text-align: center; padding: 2rem; color: var(--text-muted);">今日無已完結交易。</td>
                    </tr>"""

                    
    html += """
                </tbody>
            </table>
        </div>

        <!-- Open Positions Card -->
        <div class="section-title">⏳ 進行中 / 未平倉部位 (Open Positions)</div>
        <div class="card" style="padding: 0;">
            <table>
                <thead>
                    <tr>
                        <th>交易 ID</th>
                        <th>時段</th>
                        <th>方向</th>
                        <th>進場時間</th>
                        <th>近月進場價</th>
                        <th>遠月進場價</th>
                        <th>Spread Z</th>
                        <th>ATR</th>
                        <th>Entry MTF</th>
                        <th>Entry VWAP</th>
                    </tr>
                </thead>
                <tbody>"""
                
    if active:
        for a in active:
            badge_class = "badge-day" if a["session"].lower() == "day" else "badge-night"
            sess_label = "☀️ 日盤" if a["session"].lower() == "day" else "🌙 夜盤"
            z_str = f"{a['spread_z']:.2f}" if a["spread_z"] is not None else "N/A"
            atr_str = f"{a['atr']:.1f}" if a["atr"] is not None else "N/A"
            
            entry_mtf_str = f"{a['entry_mtf']:.1f}" if a.get('entry_mtf') is not None else "-"
            entry_vwap_str = f"{a['entry_vwap']:.0f}" if a.get('entry_vwap') is not None else "-"
            
            html += f"""
                    <tr>
                        <td><code>{a['trade_id']}</code></td>
                        <td><span class="badge {badge_class}">{sess_label}</span></td>
                        <td>{a['action']}</td>
                        <td>{a['entry_time'].split('T')[1][:8]}</td>
                        <td>{a['near_entry']:.0f}</td>
                        <td>{a['far_entry']:.0f}</td>
                        <td style="color: var(--primary); font-weight: 600;">{z_str}</td>
                        <td>{atr_str}</td>
                        <td><code>{entry_mtf_str}</code></td>
                        <td><code>{entry_vwap_str}</code></td>
                    </tr>"""
    else:
        html += """
                    <tr>
                        <td colspan="10" style="text-align: center; padding: 2rem; color: var(--text-muted);">目前無未平倉部位。</td>
                    </tr>"""
                    
    html += """
                </tbody>
            </table>
        </div>

        <!-- Optimization Suggestions Card -->
        <div class="section-title">💡 策略與參數優化建議 (Optimization Notes)</div>
        <div class="card">
            <ul class="opt-list">"""
            
    for s in suggestions:
        item_class = "warning" if "落後" in s or "偏低" in s else ""
        html += f"""
                <li class="opt-item {item_class}">{s}</li>"""
                
    html += """
            </ul>
        </div>
    </div>
</body>
</html>"""
    return html

def main():
    # Trading Day defaults to today, unless an override argument is passed
    target_date = datetime.now().strftime("%Y-%m-%d")
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
        
    fills_path = "logs/mts_trade_fills.jsonl"
    events_path = "logs/mts_spread_events.jsonl"
    report_dir = "reports"
    os.makedirs(report_dir, exist_ok=True)
    
    print(f"Analyzing MTS events for Trading Day: {target_date}...")
    report_data = parse_logs(fills_path, events_path, target_date)
    
    if not report_data or (not report_data["completed"] and not report_data["active"]):
        print(f"No trade data found for Trading Day: {target_date}. Skip report generation.")
        return
        
    html_content = generate_html(report_data, target_date)
    
    # Save HTML report file
    report_file = os.path.join(report_dir, f"daily_report_{target_date.replace('-', '')}.html")
    with open(report_file, "w") as f:
        f.write(html_content)
        
    print(f"Daily performance report successfully generated at: {report_file}")
    print("\n--- Daily Summary ---")
    print(f"Total Completed Trades: {len(report_data['completed'])}")
    total_net = sum(t["net_pnl"] for t in report_data["completed"])
    print(f"Net Realized PnL: ${total_net:+,.1f} TWD")
    print(f"Active Positions remaining: {len(report_data['active'])}")
    print("---------------------\n")

if __name__ == "__main__":
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
