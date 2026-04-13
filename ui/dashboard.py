#!/usr/bin/env python3
"""
tw-trading-unified — 整合儀表板 v2
4 tabs: 總覽 / 期貨 / 選擇權 / 設定
"""
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.parent))

import streamlit as st
import pandas as pd
import yaml
import datetime
import os
from core.date_utils import get_session_date_str
import subprocess
import time
from pathlib import Path
from dotenv import load_dotenv
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from streamlit_autorefresh import st_autorefresh

# V-Model fix: Clear cache on startup to avoid stale duplicate data
if "_cache_cleared" not in st.session_state:
    st.session_state._cache_cleared = True
    st.cache_data.clear()

# Ensure project root is in sys.path for strategy imports
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load environment variables early
load_dotenv(Path(__file__).parent.parent / ".env")

st.set_page_config(page_title="Trading Unified", page_icon="📊", layout="wide")

# ── Custom CSS ──
st.markdown("""
    <style>
    /* Force modern sans-serif font stack */
    html, body, [class*="css"]  {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    }
    /* Ensure numbers are easy to compare vertically */
    [data-testid="stMetricValue"], .stMarkdown code, .stTable {
        font-variant-numeric: tabular-nums;
        font-family: 'Inter', 'Roboto Mono', monospace;
    }
    /* Muted divider line style */
    hr {
        margin-top: 1rem;
        margin-bottom: 1rem;
        border: 0;
        border-top: 1px solid rgba(49, 51, 63, 0.1);
    }
    </style>
    """, unsafe_allow_html=True)

# ── 密碼保護 ──
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False
    if st.session_state["authenticated"]:
        return True
    
    # GSD: Large, focused password field for better UX
    st.markdown("""
        <style>
        div[data-baseweb="input"] { height: 60px !important; }
        input[type="password"] { font-size: 24px !important; }
        </style>
        <script>
        // High-reliability Autofocus: Poll until input renders
        var focusAttempts = 0;
        var focusInterval = setInterval(function() {
            var inputs = window.parent.document.querySelectorAll('input[type="password"]');
            if (inputs.length > 0) {
                inputs[0].focus();
                if (focusAttempts++ > 10) clearInterval(focusInterval);
            }
        }, 100);
        </script>
    """, unsafe_allow_html=True)
    
    pwd = st.text_input("🔒 請輸入密碼", type="password", placeholder="點擊此處或直接輸入...", key="password_input")
    
    # JavaScript to auto-focus the password field
    st.markdown("""
    <script>
    // Wait for the page to load, then focus the password input
    document.addEventListener('DOMContentLoaded', function() {
        // Find the password input by its data-testid attribute
        const passwordInput = document.querySelector('input[type="password"]');
        if (passwordInput) {
            passwordInput.focus();
            // Also select all text if there's any
            passwordInput.select();
        }
    });
    
    // Also try after a short delay in case the page loads dynamically
    setTimeout(function() {
        const passwordInput = document.querySelector('input[type="password"]');
        if (passwordInput) {
            passwordInput.focus();
            passwordInput.select();
        }
    }, 100);
    </script>
    """, unsafe_allow_html=True)
    if pwd == os.environ.get("DASHBOARD_PASSWORD", "5888"):
        st.session_state["authenticated"] = True
        st.rerun()
    elif pwd:
        st.error("密碼錯誤")
    return False

if not check_password():
    st.stop()

# 每 30 秒自動刷新（登入後才啟用）
st_autorefresh(interval=30_000, key="data_refresh")

BASE = Path(__file__).parent.parent
DATE_STR = datetime.datetime.now().strftime("%Y%m%d")  # Calendar date for display (not trading day)
TODAY = datetime.datetime.now().strftime("%Y-%m-%d")

# ── Sidebar Info ──
with st.sidebar:
    st.title("Trading Unified")
    st.markdown(f"🗓️ **交易日 (Trading Day)**")
    # GSD: Always use the latest date string from session helper
    st.code(f"{DATE_STR[:4]}-{DATE_STR[4:6]}-{DATE_STR[6:]}")
    
    # 💡 GSD: Continuous Chart Mode toggle
    cont_mode = st.toggle("🕒 連續圖表模式", value=True, help="顯示最近 24 小時資料，而非僅今日交易日。")
    
    st.markdown(f"🕒 **最後更新**: {datetime.datetime.now().strftime('%H:%M:%S')}")
    st.divider()
    if st.button("🔄 強制刷新頁面"):
        st.rerun()

# ── YAML helpers ──
def load_yaml(path):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

def save_yaml(path, data):
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# ── Configs ──
FUTURES_CFG_PATH = BASE / "config" / "futures.yaml"
OPTIONS_CFG_PATH = BASE / "config" / "options_strategy.yaml"
RISK_CFG_PATH = BASE / "config" / "risk_global.yaml"
STOCK_CFG_PATH = BASE / "config" / "stocks.yaml"
futures_cfg = load_yaml(FUTURES_CFG_PATH)
options_cfg = load_yaml(OPTIONS_CFG_PATH)
risk_cfg = load_yaml(RISK_CFG_PATH)
stock_cfg = load_yaml(STOCK_CFG_PATH)
f_live = futures_cfg.get("live_trading", False)
o_live = options_cfg.get("live_trading", False)
s_live = stock_cfg.get("live_trading", False)
alloc = risk_cfg.get("allocation", {})
reserve_pct = risk_cfg.get("account", {}).get("margin_reserve_pct", 0.20)

# ── Paths ──
RESTART_FLAG = BASE / ".restart"

def trigger_restart():
    RESTART_FLAG.touch()
    st.toast("🔄 正在重啟 monitor（約 30 秒）...")
OPTIONS_REPO = BASE / "strategies" / "options"
FUTURES_MKT = BASE / "logs" / "market_data"
FUTURES_TRADES = BASE / "exports" / "trades"
OPTIONS_DATA = OPTIONS_REPO / "logs" / ("live_trading" if o_live else "paper_trading")

# ── Filter by latest Trading Day ──
def filter_today(df, ts_col="timestamp"):
    if df is None or df.empty:
        return df
    try:
        from core.date_utils import get_trading_day
        
        # V-Model fix 1: Deduplicate columns first
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated()].copy()

        if ts_col not in df.columns:
            return df

        # Standardize timestamp
        df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce").dt.tz_localize(None)
        df = df.dropna(subset=[ts_col])

        if df.empty:
            return df

        # GSD Rationale: Group by Trading Day and pick the latest one.
        # Using .apply ensures each timestamp is handled correctly as a scalar.
        df["_tday"] = df[ts_col].apply(lambda x: get_trading_day(x))
        latest_tday = df["_tday"].max()
        filtered_df = df[df["_tday"] == latest_tday].copy()
        filtered_df = filtered_df.drop(columns=["_tday"])

        # 確保排序
        filtered_df = filtered_df.sort_values(ts_col)

        # V-Model fix 3: 過濾 fallback 假資料
        for col in ["close", "price_mtx"]:
            if col in filtered_df.columns and len(filtered_df) > 0:
                # 排除 0 或負數 (GSD: Data Integrity)
                filtered_df = filtered_df[filtered_df[col] > 0].copy()
                
                if not filtered_df.empty:
                    latest_val = filtered_df[col].iat[-1]
                    try:
                        latest_val_float = float(latest_val)
                        if latest_val_float > 0:
                            # 異常過濾：若價格低於最新價的 30%（寬鬆一點），判定為系統初始化假數據
                            mask = filtered_df[col].gt(latest_val_float * 0.3)
                            filtered_df = filtered_df[mask].copy()
                    except (TypeError, ValueError):
                        continue
        return filtered_df
    except Exception as e:
        st.error(f"資料過濾錯誤: {e}")
        return df

# ── Chart builder (unified style) ──
def make_price_score_chart(df, price_col, title, ts_col="timestamp", signals=None):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.05)
    # V-Model fix: Convert to numpy arrays to avoid narwhals duplicate check
    p_col = price_col.lower() if price_col.lower() in df.columns else price_col
    
    # 1. Price Line
    fig.add_trace(go.Scatter(
        x=df[ts_col].to_numpy(),
        y=df[p_col].to_numpy(),
        name=price_col,
        line=dict(width=1.5, color="#1f77b4")
    ), row=1, col=1)

    # 2. Add Signal Markers if available
    if signals is not None and not signals.empty and "action" in signals.columns:
        # Buy signals (Green Triangles Up)
        buys = signals[signals["action"].str.contains("BUY", case=False, na=False)]
        if not buys.empty:
            fig.add_trace(go.Scatter(
                x=buys[ts_col],
                y=buys["price"],
                mode="markers",
                marker=dict(symbol="triangle-up", size=12, color="#00cc66", line=dict(width=1, color="white")),
                name="BUY"
            ), row=1, col=1)

        # Sell signals (Red Triangles Down)
        sells = signals[signals["action"].str.contains("SELL", case=False, na=False)]
        if not sells.empty:
            fig.add_trace(go.Scatter(
                x=sells[ts_col],
                y=sells["price"],
                mode="markers",
                marker=dict(symbol="triangle-down", size=12, color="#ff4444", line=dict(width=1, color="white")),
                name="SELL"
            ), row=1, col=1)

        # Exit signals (Orange Diamonds)
        exits = signals[signals["action"].str.contains("EXIT|COVER", case=False, na=False)]
        if not exits.empty:
            fig.add_trace(go.Scatter(
                x=exits[ts_col],
                y=exits["price"],
                mode="markers",
                marker=dict(symbol="diamond", size=10, color="#ffa500", line=dict(width=1, color="white")),
                name="EXIT"
            ), row=1, col=1)

    # 3. Score Bar Chart
    if "score" in df.columns:
        scores = df["score"].to_numpy()
        colors = ["#00cc66" if s >= 0 else "#ff4444" for s in scores]
        fig.add_trace(go.Bar(
            x=df[ts_col].to_numpy(),
            y=scores,
            name="Score",
            marker_color=colors
        ), row=2, col=1)
    
    # GSD: Enhanced visuals for continuous trading day
    # 1. Add vertical line at 15:00 (Start of Trading Day)
    from core.date_utils import is_night_session
    ts_series = pd.to_datetime(df[ts_col], errors="coerce")
    for ts in ts_series:
        if pd.notna(ts) and hasattr(ts, "hour") and ts.hour == 15 and ts.minute == 0:
            fig.add_vline(x=ts, line_width=1, line_dash="dash", line_color="gray", row="all", col=1)

    # 2. Shaded background for night session
    night_mask = is_night_session(df[ts_col])
    if night_mask.any():
        # Find continuous night blocks
        night_starts = df.loc[night_mask & ~night_mask.shift(1).fillna(False), ts_col]
        night_ends = df.loc[night_mask & ~night_mask.shift(-1).fillna(False), ts_col]
        for start, end in zip(night_starts, night_ends):
            fig.add_vrect(
                x0=start, x1=end,
                fillcolor="gray", opacity=0.1,
                layer="below", line_width=0,
                row="all", col=1
            )

    fig.update_layout(height=400, margin=dict(t=10, b=10, l=40, r=20), showlegend=False)
    
    # ── GSD Enhancement: Remove non-trading gaps from time axis ──
    # This prevents the chart from showing long flat lines during gaps
    fig.update_xaxes(
        rangebreaks=[
            dict(bounds=["sat", "mon"]), # Remove weekends
            dict(bounds=[5, 8.75], pattern="hour"),  # 05:00 - 08:45
            dict(bounds=[13.75, 15], pattern="hour"), # 13:45 - 15:00
        ],
        row=1, col=1
    )
    fig.update_xaxes(
        rangebreaks=[
            dict(bounds=["sat", "mon"]),
            dict(bounds=[5, 8.75], pattern="hour"),
            dict(bounds=[13.75, 15], pattern="hour"),
        ],
        row=2, col=1
    )
    
    # 💡 GSD: Explicitly set Y-axis range to follow data closely
    if len(df) > 0:
        p_vals = df[p_col].dropna().to_numpy()
        if len(p_vals) > 0:
            p_min, p_max = p_vals.min(), p_vals.max()
            if p_max > p_min:
                # Add 5% padding instead of 10% for tighter zoom
                padding = (p_max - p_min) * 0.05
                fig.update_yaxes(range=[p_min - padding, p_max + padding], row=1, col=1, autorange=False)
            else:
                # Flat line: use normal range mode (don't force zero)
                fig.update_yaxes(rangemode="normal", row=1, col=1)
    
    # Apply format to all Y axes, but don't force autorange here
    fig.update_yaxes(tickformat=",.0f", fixedrange=False)
    
    # 3. Remove non-trading gaps and improve labels
    fig.update_xaxes(
        tickformat="%m/%d\n%H:%M",
        hoverformat="%Y/%m/%d %H:%M",
        rangebreaks=[
            dict(bounds=[13.75, 15], pattern="hour"),  # 13:45 - 15:00
            dict(bounds=[5, 8.75], pattern="hour"),    # 05:00 - 08:45
            dict(bounds=["sat", "mon"]),               # Weekend
        ]
    )
    return fig

# ── PnL helpers ──
def calc_futures_pnl(trades_df):
    """從期貨 trades CSV 的 pnl_cash 欄位累計"""
    if trades_df is None or trades_df.empty:
        return None
    if "pnl_cash" not in trades_df.columns:
        return None
    trades_df["pnl_cash"] = pd.to_numeric(trades_df["pnl_cash"], errors="coerce").fillna(0)
    exits = trades_df[trades_df["pnl_cash"] != 0].copy()
    if exits.empty:
        return None
    exits["cum_pnl"] = exits["pnl_cash"].cumsum()
    return exits[["timestamp", "cum_pnl"]].rename(columns={"cum_pnl": "pnl"})

def format_options_trades(ledger_df):
    """
    將 flat ledger 轉換成 round-trip 格式，方便理解每筆交易。
    回傳 DataFrame: 序號 | 進場時間 | 出場時間 | Side | 進場價 | 出場價 | 口數 | 出場原因 | 毛利 | 手續費稅 | 淨利
    """
    if ledger_df is None or ledger_df.empty or "Action" not in ledger_df.columns:
        return ledger_df

    trades = []
    pending_entry = None
    trade_num = 0

    for _, row in ledger_df.iterrows():
        action = str(row.get("Action", ""))
        if "ENTRY" in action and "RETRY" not in action and "SUBMITTED" not in action and "CLEARED" not in action:
            # 記錄進場
            pending_entry = {
                "entry_time": str(row.get("Timestamp", "")),
                "side": str(row.get("Side", "")),
                "entry_price": row.get("Price", 0),
                "quantity": row.get("Quantity", 1),
                "entry_note": str(row.get("Note", "")),
            }
        elif pending_entry and any(kw in action for kw in ["EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "FILL"]):
            # 出場 → 結算 round-trip
            trade_num += 1
            entry_price = float(pending_entry["entry_price"] or 0)
            exit_price = float(row.get("Price", 0))
            qty = int(pending_entry["quantity"] or 1)
            point_value = 50

            gross_pnl = (exit_price - entry_price) * point_value * qty
            # 摩擦成本
            broker_fee = 20 * 2 * qty
            exchange_fee = 5 * 2 * qty
            tax = (entry_price + exit_price) * point_value * 0.001 * qty
            total_cost = broker_fee + exchange_fee + tax
            net_pnl = gross_pnl - total_cost

            trades.append({
                "#": trade_num,
                "進場時間": pending_entry["entry_time"],
                "出場時間": str(row.get("Timestamp", "")),
                "方向": pending_entry["side"],
                "進場價": round(entry_price, 1),
                "出場價": round(exit_price, 1),
                "口數": qty,
                "出場原因": action,
                "毛利": round(gross_pnl, 0),
                "摩擦成本": round(total_cost, 0),
                "淨利": round(net_pnl, 0),
            })
            pending_entry = None

    # 如果還有未出場的持倉
    if pending_entry:
        trade_num += 1
        trades.append({
            "#": trade_num,
            "進場時間": pending_entry["entry_time"],
            "出場時間": "⏳ 持倉中",
            "方向": pending_entry["side"],
            "進場價": round(pending_entry["entry_price"], 1),
            "出場價": "-",
            "口數": int(pending_entry["quantity"] or 1),
            "出場原因": "-",
            "毛利": "-",
            "摩擦成本": "-",
            "淨利": "-",
        })

    return _format_coerce_floats(pd.DataFrame(trades)) if trades else ledger_df


def format_futures_trades(ledger_df):
    """Round-trip formatter for futures trades. Matches BUY→EXIT pairs."""
    if ledger_df is None or ledger_df.empty or "type" not in ledger_df.columns and "action" not in ledger_df.columns:
        return ledger_df

    action_col = "action" if "action" in ledger_df.columns else "type"
    trades = []
    pending_entry = None
    trade_num = 0

    for _, row in ledger_df.iterrows():
        action = str(row.get(action_col, "")).upper()
        if action in ("BUY", "SELL", "SHORT"):
            pending_entry = {
                "entry_time": str(row.get("timestamp", row.get("Timestamp", ""))),
                "direction": action,
                "entry_price": float(row.get("entry_price", row.get("price", 0)) or 0),
                "lots": int(row.get("lots", row.get("qty", row.get("Quantity", 1)) or 1)),
            }
        elif pending_entry and action in ("EXIT", "COVER", "PARTIAL_EXIT"):
            trade_num += 1
            entry = pending_entry["entry_price"]
            exit_p = float(row.get("price", row.get("exit_price", 0)) or 0)
            lots = pending_entry["lots"]
            direction = pending_entry["direction"]

            # Try to get PnL from CSV first
            gross = row.get("gross_pnl", row.get("pnl_cash", row.get("pnl", None)))
            if gross is not None:
                try:
                    gross = float(gross or 0)
                except (ValueError, TypeError):
                    gross = None
            # Fallback: calculate from price difference
            if gross is None or gross == 0:
                mult = 1 if direction == "BUY" else -1
                gross = (exit_p - entry) * 50 * lots * mult

            cost = row.get("total_cost", row.get("fees", 0))
            try:
                cost = float(cost or 0)
            except (ValueError, TypeError):
                cost = 0
            net = gross - cost if cost > 0 else gross

            trades.append({
                "#": trade_num,
                "進場時間": pending_entry["entry_time"],
                "出場時間": str(row.get("timestamp", row.get("Timestamp", ""))),
                "方向": pending_entry["direction"],
                "進場價": round(entry, 0),
                "出場價": round(exit_p, 0),
                "口數": lots,
                "出場原因": action,
                "毛利": round(gross, 0),
                "摩擦成本": round(cost, 0),
                "淨利": round(net, 0),
            })
            pending_entry = None

    if pending_entry:
        trade_num += 1
        trades.append({
            "#": trade_num,
            "進場時間": pending_entry["entry_time"],
            "出場時間": "⏳ 持倉中",
            "方向": pending_entry["direction"],
            "進場價": round(pending_entry["entry_price"], 0),
            "出場價": "-",
            "口數": pending_entry["lots"],
            "出場原因": "-",
            "毛利": "-",
            "摩擦成本": "-",
            "淨利": "-",
        })

    return _format_coerce_floats(pd.DataFrame(trades)) if trades else ledger_df


def _format_coerce_floats(df):
    """Format price and PnL columns to 2 decimal places."""
    if df is None or df.empty or "#" not in df.columns:
        return df
    for col in ["進場價", "出場價"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").apply(
                lambda x: f"{x:.2f}" if pd.notna(x) else "-")
    for col in ["毛利", "摩擦成本", "淨利"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").apply(
                lambda x: f"{x:,.0f}" if pd.notna(x) else "-")
    return df


def format_stock_trades(ledger_df):
    """Round-trip formatter for stock trades. Matches BUY→SELL pairs per ticker."""
    if ledger_df is None or ledger_df.empty or "action" not in ledger_df.columns:
        return ledger_df

    # GSD: Build ticker→name lookup from indicator CSVs
    ticker_names = {}
    for f in FUTURES_MKT.glob("STOCK_*_indicators.csv"):
        try:
            tmp = pd.read_csv(f, nrows=1)
            if "name" in tmp.columns:
                t = f.stem.split("_")[1]
                ticker_names[t] = str(tmp["name"].iloc[0])
        except Exception:
            pass

    trades = []
    # GSD fix: Track pending entries by ticker to support multiple concurrent positions
    pending_entries = {}
    trade_num = 0

    for _, row in ledger_df.iterrows():
        action = str(row.get("action", "")).upper()
        ticker = str(row.get("ticker", ""))
        if action == "BUY":
            pending_entries[ticker] = {
                "entry_time": str(row.get("timestamp", "")),
                "ticker": ticker,
                "name": ticker_names.get(ticker, ""),
                "entry_price": float(row.get("entry_price", row.get("price", 0)) or 0),
                "qty": int(row.get("qty", row.get("Quantity", 0)) or 0),
                "reason": str(row.get("reason", "")),
            }
        elif action == "SELL" and ticker in pending_entries:
            trade_num += 1
            pending = pending_entries.pop(ticker)
            entry = pending["entry_price"]
            exit_p = float(row.get("price", 0) or 0)
            qty = pending["qty"]
            gross = float(row.get("pnl_gross", row.get("pnl_cash", 0)) or 0)
            fees = float(row.get("fees", 0) or 0)
            net = float(row.get("pnl_cash", 0) or 0)
            if net == 0 and gross == 0:
                net = (exit_p - entry) * qty - fees
                gross = (exit_p - entry) * qty

            trades.append({
                "#": trade_num,
                "進場時間": pending["entry_time"],
                "出場時間": str(row.get("timestamp", "")),
                "代號": f"{ticker} {pending['name']}".strip(),
                "進場價": round(entry, 0),
                "出場價": round(exit_p, 0),
                "股數": qty,
                "出場原因": str(row.get("reason", "")),
                "毛利": round(gross, 0),
                "手續費+稅": round(fees, 0),
                "淨利": round(net, 0),
            })

    # Remaining open positions
    for ticker, pending in pending_entries.items():
        trade_num += 1
        trades.append({
            "#": trade_num,
            "進場時間": pending["entry_time"],
            "出場時間": "⏳ 持倉中",
            "代號": f"{ticker} {pending['name']}".strip(),
            "進場價": round(pending["entry_price"], 0),
            "出場價": None,
            "股數": pending["qty"],
            "出場原因": "⏳",
            "毛利": None,
            "手續費+稅": None,
            "淨利": None,
        })

    return _format_coerce_floats(pd.DataFrame(trades)) if trades else ledger_df



def calc_options_pnl(ledger_df):
    """從選擇權 ledger 的 PnL 欄位累計"""
    if ledger_df is None or ledger_df.empty:
        return None
    if "PnL" not in ledger_df.columns:
        return None
    ledger_df["PnL"] = pd.to_numeric(ledger_df["PnL"], errors="coerce").fillna(0)
    exits = ledger_df[ledger_df["PnL"] != 0].copy()
    if exits.empty:
        return None
    exits["cum_pnl"] = exits["PnL"].cumsum()
    return exits[["Timestamp", "cum_pnl"]].rename(columns={"Timestamp": "timestamp", "cum_pnl": "pnl"})

def calc_stock_pnl(trades_df):
    """從台股 trades 的 pnl_cash 欄位累計"""
    if trades_df is None or trades_df.empty:
        return None
    if "pnl_cash" not in trades_df.columns:
        return None
    trades_df["pnl_cash"] = pd.to_numeric(trades_df["pnl_cash"], errors="coerce").fillna(0)
    exits = trades_df[trades_df["pnl_cash"] != 0].copy()
    if exits.empty:
        return None
    exits["cum_pnl"] = exits["pnl_cash"].cumsum()
    return exits[["timestamp", "cum_pnl"]].rename(columns={"cum_pnl": "pnl"})


def make_pnl_chart(pnl_df, title):
    if pnl_df is None or pnl_df.empty:
        return None
    pnl_df["timestamp"] = pd.to_datetime(pnl_df["timestamp"], format="mixed")
    color = "#00cc66" if pnl_df["pnl"].iloc[-1] >= 0 else "#ff4444"
    fig = go.Figure()
    # V-Model fix: Convert to numpy arrays to avoid narwhals duplicate check
    fig.add_trace(go.Scatter(
        x=pnl_df["timestamp"].to_numpy(),
        y=pnl_df["pnl"].to_numpy(),
        fill="tozeroy",
        line=dict(color=color, width=1.5),
        name="PnL"
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="gray", line_width=0.5)
    fig.update_layout(height=250, margin=dict(t=10, b=10, l=40, r=20), title_text=title, title_font_size=14, yaxis_tickformat=",.0f")
    return fig

@st.cache_data(ttl=5)
def load_futures_indicators(full_history=False):
    def _read_and_standardize(path):
        try:
            df = pd.read_csv(path)
            # V-Model fix: Immediate deduplication after reading
            if df.columns.duplicated().any():
                df = df.loc[:, ~df.columns.duplicated()].copy()
            
            # 1. 處理 timestamp
            if "timestamp" not in df.columns:
                if df.index.name == "timestamp" or df.index.name == "ts":
                    df = df.reset_index()
                elif "ts" in df.columns:
                    df = df.rename(columns={"ts": "timestamp"})
                else:
                    df = df.rename(columns={df.columns[0]: "timestamp"})

            # 2. V-Model fix: Remove case-insensitive duplicate columns BEFORE renaming
            col_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume", "Amount": "amount"}
            for upper, lower in col_map.items():
                if upper in df.columns and lower in df.columns:
                    df = df.drop(columns=[upper])

            # 3. 統一 OHLC 欄位為小寫
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            return df
        except Exception:
            return None

    # 1. 優先找最近 3 天所有可能的檔案並合併 (確保跨交易日資料完整)
    import datetime as dt
    now = dt.datetime.now()
    # GSD: Include tomorrow to cover the active trading session after 15:00 rollover
    search_days = [
        (now - dt.timedelta(days=1)).strftime("%Y%m%d"),
        now.strftime("%Y%m%d"),
        (now + dt.timedelta(days=1)).strftime("%Y%m%d"),
        DATE_STR,  # GSD Fix: Always include the active trading session date
    ]
    search_days = list(dict.fromkeys(search_days))  # dedupe, preserve order

    all_dfs = []
    for date_part in search_days:
        for tag in ["", "_LIVE", "_PAPER", "_DRY"]:
            f = FUTURES_MKT / f"TMF_{date_part}{tag}_indicators.csv"
            if f.exists():
                df = _read_and_standardize(f)
                if df is not None and "timestamp" in df.columns:
                    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                    if not df.empty:
                        all_dfs.append(df)

    # GSD: Parquet Fallback (Wave 18.3)
    is_fallback = False
    if not all_dfs:
        try:
            from core.data_manager import data_manager
            df_hist = data_manager.load_historical("TXFR1")
            if not df_hist.empty:
                is_fallback = True
                df_hist = df_hist.tail(100).copy()
                
                # V-Model fix: Deduplicate before rename to prevent clashes
                df_hist = df_hist.loc[:, ~df_hist.columns.duplicated()].copy()
                
                # Standardize columns carefully
                if df_hist.index.name == "timestamp" or df_hist.index.name == "ts":
                    df_hist = df_hist.reset_index()
                elif pd.api.types.is_datetime64_any_dtype(df_hist.index):
                    # If index is datetime but not named 'timestamp', reset and rename
                    df_hist = df_hist.reset_index()
                    df_hist = df_hist.rename(columns={"index": "timestamp"})
                else:
                    # If index is not datetime, create a timestamp column from index
                    df_hist = df_hist.reset_index()
                    if "index" in df_hist.columns:
                        df_hist = df_hist.rename(columns={"index": "timestamp"})
                    elif "timestamp" not in df_hist.columns:
                        # Create a dummy timestamp column if none exists
                        df_hist["timestamp"] = pd.to_datetime("2023-01-01")
                
                rename_map = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
                # Only rename if source exists AND target doesn't already exist
                actual_renames = {k: v for k, v in rename_map.items() if k in df_hist.columns and v not in df_hist.columns}
                df_hist = df_hist.rename(columns=actual_renames)
                
                # Ensure timestamp column exists and is datetime
                if "timestamp" not in df_hist.columns:
                    df_hist["timestamp"] = pd.to_datetime("2023-01-01")
                elif not pd.api.types.is_datetime64_any_dtype(df_hist["timestamp"]):
                    df_hist["timestamp"] = pd.to_datetime(df_hist["timestamp"], errors="coerce")
                
                all_dfs.append(df_hist)
        except Exception:
            pass

    result = None
    if all_dfs:
        common_cols = set(all_dfs[0].columns)
        for df in all_dfs[1:]:
            common_cols &= set(df.columns)
        if "timestamp" not in common_cols:
            common_cols.add("timestamp")
        cleaned_dfs = [df[list(common_cols)] for df in all_dfs]
        merged = pd.concat(cleaned_dfs).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        
        # FINAL GUARD: Ensure no duplicate columns for PyArrow
        if merged.columns.duplicated().any():
            merged = merged.loc[:, ~merged.columns.duplicated()].copy()
        
        if is_fallback:
            result = merged # Skip date filtering if loading from history
        elif full_history:
            cutoff = now - dt.timedelta(hours=24)
            result = merged[merged["timestamp"] >= cutoff].copy()
        else:
            result = filter_today(merged)
    else:
        # 2. 備案：找目錄下最新的 CSV
        try:
            all_files = list(FUTURES_MKT.glob("TMF_*_indicators.csv"))
            if all_files:
                latest_file = max(all_files, key=os.path.getmtime)
                df = _read_and_standardize(latest_file)
                if df is not None and "timestamp" in df.columns:
                    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                    if full_history:
                        cutoff = now - dt.timedelta(hours=24)
                        result = df[df["timestamp"] >= cutoff].copy()
                    else:
                        result = filter_today(df) if not df.empty else None
        except Exception:
            result = None

    # Stale data detection
    if result is not None and not result.empty and "timestamp" in result.columns:
        try:
            result_ts = result["timestamp"].copy()
            if not pd.api.types.is_datetime64_any_dtype(result_ts):
                result_ts = pd.to_datetime(result_ts, errors="coerce")
            result_ts = result_ts.dropna()
            if not result_ts.empty:
                age_secs = (pd.Timestamp.now() - result_ts.max()).total_seconds()
                if age_secs > 600:
                    st.warning(f"⚠️ 期貨資料停滯 {age_secs/60:.0f} 分鐘")
        except Exception:
            pass
    return result

@st.cache_data(ttl=5)
def load_futures_trades():
    for d in [FUTURES_TRADES]:
        f = d / f"TMF_{DATE_STR}_trades.csv"
        if f.exists():
            try:
                return pd.read_csv(f)
            except Exception:
                pass
    return None

OPTIONS_SUB = "live_trading" if o_live else "paper_trading"

@st.cache_data(ttl=5)
def load_options_indicators(full_history=False):
    # GSD: Load multiple days to cover full trading session
    import datetime as dt
    now = dt.datetime.now()
    # 交易日邏輯：15:00 之後歸屬明天
    t_day_str = get_session_date_str(now)
    
    days = [
        (now - dt.timedelta(days=1)).strftime("%Y%m%d"),
        now.strftime("%Y%m%d"),
        t_day_str
    ]
    # Deduplicate days
    days = sorted(list(set(days)))
    
    all_dfs = []
    for d_str in days:
        for sub in ["live_trading", "paper_trading"]:
            f = OPTIONS_REPO / "logs" / sub / f"OPTIONS_{d_str}_indicators.csv"
            if f.exists():
                try:
                    df = pd.read_csv(f)
                    if not df.empty:
                        # 確保 timestamp 是 datetime
                        if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                        all_dfs.append(df)
                except Exception:
                    continue

    # GSD: Parquet Fallback (Wave 18.3)
    if not all_dfs:
        try:
            from core.data_manager import data_manager
            df_hist = data_manager.load_historical("OPTIONS")
            if not df_hist.empty:
                df_hist = df_hist.tail(100).copy()
                if df_hist.index.name != "timestamp":
                    df_hist = df_hist.reset_index().rename(columns={"index": "timestamp"})
                all_dfs.append(df_hist)
        except Exception:
            pass

    result = None
    if all_dfs:
        try:
            merged = pd.concat(all_dfs).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
            if merged.columns.duplicated().any():
                merged = merged.loc[:, ~merged.columns.duplicated()].copy()
            
            if full_history:
                cutoff = now - dt.timedelta(hours=24)
                result = merged[merged["timestamp"] >= cutoff].copy()
            else:
                # 💡 GSD: We want to see data belonging to the CURRENT trading session
                # If it's 09:00 AM, we want to see data from 15:00 (yesterday) onwards.
                result = filter_today(merged, ts_col="timestamp")
        except Exception:
            pass
            
    if result is None or result.empty:
        # 2. 備案：找目錄下最新的任何指標檔案（防斷鍊）
        try:
            all_opt_files = list((OPTIONS_REPO / "logs").rglob("OPTIONS_*_indicators.csv"))
            if all_opt_files:
                latest_f = max(all_opt_files, key=os.path.getmtime)
                df = pd.read_csv(latest_f)
                if not df.empty:
                    if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                    if full_history:
                        cutoff = now - dt.timedelta(hours=24)
                        result = df[df["timestamp"] >= cutoff].copy()
                    else:
                        result = filter_today(df, ts_col="timestamp")
        except Exception:
            pass
            
    return result

@st.cache_data(ttl=5)
def load_options_ledger():
    f = OPTIONS_REPO / "logs" / OPTIONS_SUB / "options_trade_ledger.csv"
    if f.exists():
        try:
            return pd.read_csv(f, parse_dates=["Timestamp"])
        except Exception:
            pass
    return None

@st.cache_data(ttl=5)
def load_options_equity():
    f = OPTIONS_REPO / "logs" / OPTIONS_SUB / "equity_curve.csv"
    if f.exists():
        try:
            return pd.read_csv(f, parse_dates=["timestamp"])
        except Exception:
            pass
    return None

@st.cache_data(ttl=5)
def load_stock_trades(mode="PAPER"):
    f = FUTURES_TRADES / f"STOCK_{DATE_STR}_{mode}_trades.csv"
    if f.exists():
        try:
            return pd.read_csv(f)
        except Exception:
            pass
    return None

@st.cache_data(ttl=5)
def load_stock_indicators(ticker):
    f = FUTURES_MKT / f"STOCK_{ticker}_{DATE_STR}_indicators.csv"
    if f.exists():
        try:
            df = pd.read_csv(f)
            if df.columns.duplicated().any():
                df = df.loc[:, ~df.columns.duplicated()].copy()
            if "ts" in df.columns:
                df = df.rename(columns={"ts": "timestamp"})
            elif "Date" in df.columns:
                df = df.rename(columns={"Date": "timestamp"})
            return df
        except Exception:
            pass
    return None

# ── Header ──
def mode_badge(live):
    return "🔴 LIVE" if live else "📝 PAPER"

hc = st.columns([1.5, 1, 1, 1, 1.5])
hc[0].title("Trading Unified")
hc[1].metric("期貨 TMF", mode_badge(f_live))
hc[2].metric("選擇權 TXO", mode_badge(o_live))
hc[3].metric("台股 Stocks", mode_badge(s_live))
hc[4].caption(f"📅 {TODAY}")

if f_live or o_live or s_live:
    st.markdown('<div style="background:#ff4444;color:white;padding:8px;text-align:center;border-radius:4px;font-weight:bold;">⚠️ LIVE TRADING ACTIVE</div>', unsafe_allow_html=True)

def _monitor_status():
    try:
        r = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True)
        return "🟢 Running" if r.returncode == 0 else "🔴 Stopped"
    except Exception:
        return "⚪ Unknown"

st.caption(f"更新: {datetime.datetime.now().strftime('%H:%M:%S')} | Monitor: {_monitor_status()}")

# ── Tabs ──
tab_overview, tab_futures, tab_options, tab_stocks, tab_pipeline, tab_settings = st.tabs([
    "總覽", "期貨 TMF", "選擇權 TXO", "台股 Stocks", "策略管道", "設定"
])

# ════════════════════════════════════════
# Tab 1: 總覽
# ════════════════════════════════════════
with tab_overview:
    col1, col2 = st.columns(2)
    f_df = load_futures_indicators(full_history=cont_mode)
    o_df = load_options_indicators(full_history=cont_mode)

    with col1:
        st.header(f"期貨 TMF ({mode_badge(f_live)})")
        if f_df is not None and not f_df.empty:
            last = f_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            # V-Model fix: Handle duplicate columns by taking first match
            cl_val = last.get('close') if 'close' in last else last.get('Close', 0)
            if hasattr(cl_val, 'iloc'):
                cl_val = float(cl_val.iloc[0]) if len(cl_val) > 0 else 0.0
            else:
                cl_val = float(cl_val or 0)
            sc_val = last.get('score', 0)
            if hasattr(sc_val, 'iloc'):
                sc_val = float(sc_val.iloc[0]) if len(sc_val) > 0 else 0.0
            else:
                sc_val = float(sc_val or 0)
            c1.metric("Close", f"{cl_val:.0f}")
            c2.metric("Score", f"{sc_val:.1f}")
            c3.metric("Bars", len(f_df))
        else:
            st.info("無期貨指標數據")
        ft = load_futures_trades()
        st.write(f"今日交易: {len(ft) if ft is not None else 0} 筆")

    with col2:
        st.header(f"選擇權 TXO ({mode_badge(o_live)})")
        if o_df is not None and not o_df.empty:
            last = o_df.iloc[-1]
            c1, c2, c3 = st.columns(3)
            # V-Model fix: Handle duplicate columns by taking first match
            mtx_val = last.get('price_mtx', 0)
            if hasattr(mtx_val, 'iloc'):
                mtx_val = float(mtx_val.iloc[0]) if len(mtx_val) > 0 else 0.0
            else:
                mtx_val = float(mtx_val or 0)
            sc_val = last.get('score', 0)
            if hasattr(sc_val, 'iloc'):
                sc_val = float(sc_val.iloc[0]) if len(sc_val) > 0 else 0.0
            else:
                sc_val = float(sc_val or 0)
            c1.metric("MTX", f"{mtx_val:.0f}")
            c2.metric("Score", f"{sc_val:.1f}")
            c3.metric("Bars", len(o_df))
        else:
            st.info("無選擇權指標數據")
        ol = load_options_ledger()
        if ol is not None and not ol.empty and "Timestamp" in ol.columns:
            ol["Timestamp"] = pd.to_datetime(ol["Timestamp"], errors="coerce")
            ol = ol.dropna(subset=["Timestamp"])
            
            # 使用交易日邏輯，而不是日曆日
            from core.date_utils import get_trade_day
            ol["TradingDay"] = ol["Timestamp"].apply(lambda x: get_trade_day(x).strftime("%Y%m%d"))
            
            # 比較交易日，而不是日曆日
            today_l = ol[ol["TradingDay"] == DATE_STR]
            entries = today_l[today_l["Action"].str.contains("ENTRY", na=False)]
            st.write(f"今日進場: {len(entries)} 筆")
        else:
            st.write("今日交易: 0 筆")

    # ── 總覽圖：指數走勢（雙軸：期貨 + 選擇權 MTX）──
    st.header("今日指數走勢")
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    has_data = False
    if f_df is not None and not f_df.empty:
        # V-Model fix: Convert to numpy arrays to avoid narwhals duplicate check
        f_close = f_df["close"] if "close" in f_df.columns else f_df["Close"]
        fig.add_trace(go.Scatter(
            x=f_df["timestamp"].to_numpy(),
            y=f_close.to_numpy(),
            name="TMF",
            line=dict(color="#1f77b4", width=1.5)
        ), secondary_y=False)
        has_data = True
    if o_df is not None and not o_df.empty and "price_mtx" in o_df.columns:
        fig.add_trace(go.Scatter(
            x=o_df["timestamp"].to_numpy(),
            y=o_df["price_mtx"].to_numpy(),
            name="MTX (Options)",
            line=dict(color="#ff7f0e", width=1.5)
        ), secondary_y=True)
        has_data = True
    if has_data:
        fig.update_layout(height=350, margin=dict(t=10, b=10, l=40, r=20), legend=dict(orientation="h", y=1.02))
        fig.update_yaxes(title_text="TMF", tickformat=",.0f", secondary_y=False)
        fig.update_yaxes(title_text="MTX", tickformat=",.0f", secondary_y=True)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("等待數據...")

    # ── 總覽 PnL ──
    st.header("今日累計 PnL")
    pc1, pc2, pc3 = st.columns(3)
    ft = load_futures_trades()
    fpnl = calc_futures_pnl(ft)
    ol = load_options_ledger()
    opnl = calc_options_pnl(ol)
    sl = load_stock_trades()
    spnl = calc_stock_pnl(sl)
    with pc1:
        if fpnl is not None and not fpnl.empty:
            val = fpnl["pnl"].iloc[-1]
            st.metric("期貨 PnL", f"{val:+,.0f} TWD")
        else:
            st.metric("期貨 PnL", "0 TWD")
    with pc2:
        if opnl is not None and not opnl.empty:
            val = opnl["pnl"].iloc[-1]
            st.metric("選擇權 PnL", f"{val:+,.0f} TWD")
        else:
            st.metric("選擇權 PnL", "0 TWD")
    with pc3:
        if spnl is not None and not spnl.empty:
            val = spnl["pnl"].iloc[-1]
            st.metric("台股 PnL", f"{val:+,.0f} TWD")
        else:
            st.metric("台股 PnL", "0 TWD")

    # ── 總覽：台股快訊 ──
    st.header("台股快訊 (Watchlist Quick View)")
    watchlist = stock_cfg.get("stocks", {}).get("watchlist", [])
    if watchlist:
        ov_data = []
        for ticker in watchlist:
            s_df = load_stock_indicators(ticker)
            if s_df is not None and not s_df.empty:
                last = s_df.iloc[-1]
                # 計算趨勢與噴發偏向 (Bias) - 整合趨勢感
                bull = last.get("bullish_align", False)
                bear = last.get("bearish_align", False)
                mom = last.get("momentum", 0)
                mom_prev = s_df["momentum"].iloc[-2] if len(s_df) > 1 else 0
                
                bias = "⚪中性"
                if mom > 0:
                    if bull:
                        bias = "🚀強勢多" if mom >= mom_prev else "↗️多轉弱"
                    else:
                        bias = "⚠️空反彈" if mom >= mom_prev else "↗️弱反彈"
                elif mom < 0:
                    if bear:
                        bias = "💀強勢空" if mom <= mom_prev else "↘️空轉弱"
                    else:
                        bias = "⚠️多拉回" if mom <= mom_prev else "↘️弱拉回"

                ov_data.append({
                    "代號": ticker,
                    "名稱": last.get("name", "Unknown"),
                    "股價": last.get('close', last.get('Close', 0)),
                    "量": f"{int(last.get('volume', last.get('Volume', 0))//1000)}k",
                    "Score": round(last.get('score', 0), 1),
                    "Sqz": "🔒壓" if last.get("sqz_on", False) else "🔓釋",
                    "偏向": bias,
                    "投信": "🔥連買" if last.get("it_buy_rolling_count", 0) >= 2 else "⚪ —",
                    "200MA": "🟢上" if last.get("ema_200_up", False) else "⚪ —"
                })
        
        if ov_data:
            ov_df = pd.DataFrame(ov_data)
            def style_overview(row):
                styles = [''] * len(row)
                if "🔒" in str(row["Sqz"]):
                    styles[5] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                
                # 噴發偏向著色
                if "🚀" in str(row["偏向"]) or "↗️" in str(row["偏向"]):
                    styles[6] = 'color: #059669; font-weight: bold'
                elif "💀" in str(row["偏向"]) or "↘️" in str(row["偏向"]):
                    styles[6] = 'color: #dc2626; font-weight: bold'

                if "🔥" in str(row["投信"]):
                    styles[7] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                if "🟢" in str(row["200MA"]):
                    styles[8] = 'color: #059669; font-weight: bold'
                return styles
            
            # CSS 縮小字體
            st.markdown("""
                <style>
                [data-testid="stMetricValue"] { font-size: 1.4rem !important; }
                [data-testid="stMetricLabel"] { font-size: 0.8rem !important; }
                .styled-table { font-size: 0.8rem !important; }
                </style>
            """, unsafe_allow_html=True)
            
            st.dataframe(ov_df.style.apply(style_overview, axis=1), use_container_width=True, hide_index=True)
        else:
            st.info("等待個股指標數據...")
    else:
        st.info("尚未設定監控名單")

# ════════════════════════════════════════
# Tab 2: 期貨
# ════════════════════════════════════════
with tab_futures:
    st.header(f"期貨 TMF ({mode_badge(f_live)})")

    f_df = load_futures_indicators(full_history=cont_mode)
    if f_df is not None and not f_df.empty:
        last = f_df.iloc[-1]
        fc1, fc2, fc3, fc4, fc5 = st.columns(5)
        
        # 噴發偏向 (Bias) 計算 - 整合趨勢感
        bull = last.get("bullish_align", False)
        bear = last.get("bearish_align", False)
        mom = last.get("momentum", 0)
        mom_prev = f_df["momentum"].iloc[-2] if len(f_df) > 1 else 0
        
        bias = "⚪中性"
        if mom > 0:
            if bull:
                bias = "🚀強勢多" if mom >= mom_prev else "↗️多轉弱"
            else:
                bias = "⚠️空反彈" if mom >= mom_prev else "↗️弱反彈"
        elif mom < 0:
            if bear:
                bias = "💀強勢空" if mom <= mom_prev else "↘️空轉弱"
            else:
                bias = "⚠️多拉回" if mom <= mom_prev else "↘️弱拉回"

        # V-Model fix: Handle duplicate columns
        cl_val = last.get('close') if 'close' in last else last.get('Close', 0)
        if hasattr(cl_val, 'iloc'):
            cl_val = float(cl_val.iloc[0]) if len(cl_val) > 0 else 0.0
        else:
            cl_val = float(cl_val or 0)
        
        sc_val = last.get('score', 0)
        if hasattr(sc_val, 'iloc'):
            sc_val = float(sc_val.iloc[0]) if len(sc_val) > 0 else 0.0
        else:
            sc_val = float(sc_val or 0)

        fc1.metric("Close", f"{cl_val:.0f}")
        fc2.metric("Score", f"{sc_val:.1f}")
        bull = last.get("bull_align", last.get("bullish_align", False))
        bear = last.get("bear_align", last.get("bearish_align", False))
        trend = "🟢多頭" if bull else ("🔴空頭" if bear else "⚪中性")
        fc3.metric("趨勢", trend)
        fc4.metric("Sqz狀態", "🔒壓縮" if last.get("sqz_on", False) is True else "🔓釋放")
        fc5.metric("噴發向", bias)

        if "fired" in last and last.get("fired", False) is True:
            st.success("🔥 **FIRE — 壓縮釋放！**")
        
        ft = load_futures_trades()
        st.plotly_chart(make_price_score_chart(f_df, "close", "TMF 價格 & Score", signals=ft), use_container_width=True)
        st.dataframe(f_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")
    ft = load_futures_trades()
    if ft is not None and not ft.empty:
        # --- Unrealized PnL ---
        round_trips = format_futures_trades(ft)
        open_pos = None
        if round_trips is not None and "#" in round_trips.columns:
            open_rows = round_trips[round_trips["出場時間"] == "⏳ 持倉中"]
            if not open_rows.empty:
                open_pos = open_rows.iloc[-1]

        if open_pos is not None:
            cur_price = float(f_df["close"].iloc[-1]) if len(f_df) > 0 else 0
            entry = float(open_pos.get("進場價", 0))
            lots = int(open_pos.get("口數", 1))
            direction = str(open_pos.get("方向", ""))
            if cur_price > 0 and entry > 0:
                mult = 1 if direction == "BUY" else -1
                unrealized = (cur_price - entry) * 50 * lots * mult
                color = "green" if unrealized >= 0 else "red"
                st.metric(f"📊 未實現損益 (持倉中)", f"{unrealized:+,.0f} TWD",
                          delta=f"{unrealized:+,.0f} ({(unrealized/(entry*50*lots)*100):+.1f}%)")
                st.caption(f"進場價: {entry:.0f} | 目前: {cur_price:.0f} | {direction} {lots}口")

        st.header("交易記錄 (Round-Trip)")
        round_trips = format_futures_trades(ft)
        if round_trips is not None and not round_trips.empty and "#" in round_trips.columns:
            def style_trades(row):
                pnl = row.get("淨利", "-")
                if pnl != "-" and isinstance(pnl, (int, float)):
                    color = '#dcfce7' if pnl > 0 else ('#fef2f2' if pnl < 0 else '')
                    return [f'background-color: {color}; font-weight: bold'] * len(row)
                return [''] * len(row)
            st.dataframe(round_trips.style.apply(style_trades, axis=1), use_container_width=True, hide_index=True)
        else:
            st.dataframe(ft, use_container_width=True)
        fpnl = calc_futures_pnl(ft)
        fig = make_pnl_chart(fpnl, "期貨累計 PnL (TWD)")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 原始 Ledger (進階)"):
            st.dataframe(ft, use_container_width=True)

# ════════════════════════════════════════
# Tab 3: 選擇權
# ════════════════════════════════════════
with tab_options:
    st.header(f"選擇權 TXO ({mode_badge(o_live)})")
    o_df = load_options_indicators(full_history=cont_mode)
    if o_df is not None and not o_df.empty:
        # Debug info (expander)
        with st.expander("🛠️ 數據狀態 (Debug)"):
            st.write(f"資料筆數: {len(o_df)}")
            st.write(f"時間範圍: {o_df['timestamp'].min()} ~ {o_df['timestamp'].max()}")
            if "price_mtx" in o_df.columns:
                st.write(f"MTX 範圍: {o_df['price_mtx'].min():.0f} ~ {o_df['price_mtx'].max():.0f}")
            
            # GSD: Show which files were loaded
            import glob
            st.write("**載入檔案列表:**")
            for sub in ["live_trading", "paper_trading"]:
                pattern = str(OPTIONS_REPO / "logs" / sub / "OPTIONS_*_indicators.csv")
                files = sorted(glob.glob(pattern), reverse=True)[:3]
                for f in files:
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M:%S')
                    st.text(f"{os.path.basename(f)} (修改: {mtime})")
        
        if "price_mtx" in o_df.columns:
            last = o_df.iloc[-1]
        
        # 噴發偏向 (Bias) 計算 - 整合選擇權趨勢感
        trend_val = last.get("mid_trend", "")
        bull = (trend_val == "BULL")
        bear = (trend_val == "BEAR")
        mom = last.get("momentum", last.get("mom_mtx", 0))
        mom_prev = o_df["momentum"].iloc[-2] if "momentum" in o_df.columns and len(o_df) > 1 else 0
        
        bias = "⚪中性"
        if mom > 0:
            if bull:
                bias = "🚀強勢多" if mom >= mom_prev else "↗️多轉弱"
            else:
                bias = "⚠️空反彈" if mom >= mom_prev else "↗️弱反彈"
        elif mom < 0:
            if bear:
                bias = "💀強勢空" if mom <= mom_prev else "↘️空轉弱"
            else:
                bias = "⚠️多拉回" if mom <= mom_prev else "↘️弱拉回"

        oc1, oc2, oc3, oc4, oc5, oc6 = st.columns(6)
        # V-Model fix: Handle duplicate columns by taking first match
        mtx_val = last.get('price_mtx', 0)
        if hasattr(mtx_val, 'iloc'):
            mtx_val = float(mtx_val.iloc[0]) if len(mtx_val) > 0 else 0.0
        else:
            mtx_val = float(mtx_val or 0)
        sc_val = last.get('score', 0)
        if hasattr(sc_val, 'iloc'):
            sc_val = float(sc_val.iloc[0]) if len(sc_val) > 0 else 0.0
        else:
            sc_val = float(sc_val or 0)
        
        oc1.metric("MTX", f"{mtx_val:.0f}")
        oc2.metric("Score", f"{sc_val:.1f}")
        trend_label = "🟢BULL" if trend_val == "BULL" else ("🔴BEAR" if trend_val == "BEAR" else "⚪ —")
        oc3.metric("趨勢", trend_label)
        iv = last.get("iv", 0)
        oc4.metric("IV", f"{iv*100:.1f}%" if iv and iv < 1 else f"{iv:.1f}%")
        oc5.metric("Sqz狀態", "🔒壓縮" if last.get("sqz_on", False) is True else "🔓釋放")
        oc6.metric("噴發向", bias)

        if "fired" in last and last.get("fired", False) is True:
            st.success("🔥 **FIRE — 壓縮釋放！**")

        # 顯示當前選擇權持倉
        ol = load_options_ledger()
        if ol is not None and not ol.empty:
            # 找最後一筆非 EXIT 的交易
            active = ol[~ol["Action"].str.contains("EXIT|TP1", na=False)]
            if not active.empty:
                last_pos = active.iloc[-1]
                side = str(last_pos.get("Side", ""))
                action = str(last_pos.get("Action", ""))
                if "iron_condor" in side.lower():
                    pos_label = "🦅 Iron Condor"
                    # 從 Note 取腿資訊
                    note = str(last_pos.get("Note", ""))
                    if "[" in note:
                        pos_label += " " + note.split("[")[1].split("]")[0]
                elif side.upper() == "C":
                    pos_label = "📞 Call"
                elif side.upper() == "P":
                    pos_label = "📉 Put"
                else:
                    pos_label = side
                st.caption(f"當前持倉: **{pos_label}** | 進場: {action} @ {last_pos.get('Price', '')}")

                # --- Unrealized PnL (proxy via MTX price change) ---
                entry_price = float(last_pos.get("Price", 0))
                qty = int(last_pos.get("Quantity", 1))
                cur_mtx = float(o_df["price_mtx"].iloc[-1]) if len(o_df) > 0 and "price_mtx" in o_df.columns else 0
                if entry_price > 0 and cur_mtx > 0:
                    # 選項權利金變化無法直接取得，用 MTX 價格變化估算方向
                    st.caption(f"目前 MTX: {cur_mtx:.0f}")
            else:
                st.caption("目前無持倉")
        else:
            st.caption("目前無持倉")
        
        ol = load_options_ledger()
        # Pre-process ledger to match signal expected format if not empty
        sig_df = None
        if ol is not None and not ol.empty:
            sig_df = ol.rename(columns={"Timestamp": "timestamp", "Action": "action", "Price": "price"})
            
        st.plotly_chart(make_price_score_chart(o_df, "price_mtx", "MTX 價格 & Score", signals=sig_df), use_container_width=True)
        st.dataframe(o_df.tail(20), use_container_width=True)
    else:
        st.info("無數據")
    ol = load_options_ledger()
    if ol is not None and not ol.empty:
        st.header("交易記錄 (Round-Trip)")
        round_trips = format_options_trades(ol)
        if round_trips is not None and not round_trips.empty and "#" in round_trips.columns:
            # Style with color for profit/loss
            def style_trades(row):
                styles = [''] * len(row)
                pnl = row.get("淨利", "-")
                if pnl != "-" and isinstance(pnl, (int, float)):
                    color = '#dcfce7' if pnl > 0 else ('#fef2f2' if pnl < 0 else '')
                    styles = [f'background-color: {color}; font-weight: bold'] * len(row)
                return styles
            st.dataframe(round_trips.style.apply(style_trades, axis=1), use_container_width=True, hide_index=True)
        else:
            st.dataframe(ol.tail(30), use_container_width=True)
        opnl = calc_options_pnl(ol)
        fig = make_pnl_chart(opnl, "選擇權累計 PnL (TWD)")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        # 展開原始 Ledger (進階)
        with st.expander("📋 原始 Ledger (進階)"):
            st.dataframe(ol, use_container_width=True)

# ════════════════════════════════════════
# Tab 4: 台股 Stocks
# ════════════════════════════════════════
with tab_stocks:
    st.header(f"🍎 台股 Stocks ({mode_badge(s_live)})")
    
    st.subheader("Watchlist 實時監控牆")
    watchlist = stock_cfg.get("stocks", {}).get("watchlist", [])
    
    if not watchlist:
        st.info("Watchlist 為空")
    else:
        monitor_data = []
        for ticker in watchlist:
            s_df = load_stock_indicators(ticker)
            if s_df is not None and not s_df.empty:
                last = s_df.iloc[-1]
                close = float(last.get('close', last.get('Close', 0)))
                vol = int(last.get('volume', last.get('Volume', 0)))
                score = round(last.get('score', 0), 1)
                bb_lower = float(last.get('bb_lower', 0))
                sqz = "🔒 壓縮" if last.get("sqz_on", False) else "🔓 釋放"
                
                # 計算距布林帶下軌距離 (%)
                if bb_lower > 0 and close > 0:
                    dist_bb = ((close - bb_lower) / bb_lower) * 100
                    if dist_bb < 0:
                        dist_label = f"🔥 已跌破 {dist_bb:.1f}%"
                    else:
                        dist_label = f"{dist_bb:.1f}%"
                else:
                    dist_label = "—%"

                monitor_data.append({
                    "代號": ticker,
                    "名稱": last.get("name", "Unknown"),
                    "股價": close,
                    "成交量": f"{vol:,}",
                    "Score": score,
                    "距BB下軌": dist_label,
                    "壓縮": sqz,
                })
        
        if monitor_data:
            m_df = pd.DataFrame(monitor_data)
            
            def style_monitor(row):
                styles = [''] * len(row)
                # 壓縮 (紅底白字)
                if "🔒" in str(row.get("壓縮", "")):
                    styles[6] = 'background-color: #fee2e2; color: #b91c1c; font-weight: bold'
                # 距BB下軌 (綠底白字代表已跌破，進場訊號)
                if "🔥" in str(row.get("距BB下軌", "")):
                    styles[5] = 'background-color: #dcfce7; color: #065f46; font-weight: bold'
                return styles

            st.dataframe(m_df.style.apply(style_monitor, axis=1), use_container_width=True, hide_index=True)
        else:
            st.info("等待 Monitor 寫入指標數據...")
    
    st.divider()
    # 讀取目前運行的模式
    current_mode = "LIVE" if s_live else "PAPER"
    sl = load_stock_trades(current_mode)
    
    st.header(f"交易記錄 (Round-Trip, {current_mode} 模式)")
    if sl is not None and not sl.empty:
        # 摘要指標
        sells = sl[sl["action"] == "SELL"].copy()
        if not sells.empty:
            sells["pnl_cash"] = pd.to_numeric(sells["pnl_cash"], errors="coerce").fillna(0)
            m1, m2, m3, m4 = st.columns(4)
            total_pnl = sells["pnl_cash"].sum()
            total_fees = pd.to_numeric(sells.get("fees", 0), errors="coerce").fillna(0).sum()
            wins = (sells["pnl_cash"] > 0).sum()
            total = len(sells)
            m1.metric("淨損益", f"{total_pnl:+,.0f} TWD")
            m2.metric("勝率", f"{wins}/{total} ({wins/total*100:.0f}%)" if total > 0 else "-")
            m3.metric("摩擦成本", f"{total_fees:,.0f} TWD")
            m4.metric("平均每筆", f"{total_pnl/total:+,.0f} TWD" if total > 0 else "-")

        # Round-trip 明細
        round_trips = format_stock_trades(sl)
        if round_trips is not None and not round_trips.empty and "#" in round_trips.columns:
            # --- Unrealized PnL for open stock positions ---
            open_rows = round_trips[round_trips["出場時間"] == "⏳ 持倉中"]
            if not open_rows.empty:
                st.subheader("📊 未實現損益 (持倉中)")
                cols = st.columns(min(len(open_rows), 4))
                for idx, (_, row) in enumerate(open_rows.iterrows()):
                    ticker = str(row.get("代號", "")).split()[0]  # Extract ticker from "1525 綠電"
                    entry = float(row.get("進場價", 0))
                    qty = int(row.get("股數", 0))
                    # Get current price from indicator CSV
                    cur_price = 0
                    ind_path = FUTURES_MKT / f"STOCK_{ticker}_{DATE_STR}_indicators.csv"
                    if ind_path.exists():
                        try:
                            ind_df = pd.read_csv(ind_path, nrows=1)
                            close_col = [c for c in ind_df.columns if c.lower() in ("close", "close")]
                            if close_col:
                                cur_price = float(ind_df[close_col[0]].iloc[0])
                        except Exception:
                            pass
                    if cur_price > 0 and entry > 0 and qty > 0:
                        unrealized = (cur_price - entry) * qty
                        color = "green" if unrealized >= 0 else "red"
                        with cols[idx % 4]:
                            st.metric(f"{row.get('代號', ticker)}", f"{unrealized:+,.0f} TWD",
                                      delta=f"{unrealized:+,.0f} ({(unrealized/(entry*qty)*100):+.1f}%)")
                            st.caption(f"進場: {entry:.0f} | 目前: {cur_price:.0f} | {qty}股")

            def style_stock_trades(row):
                pnl = row.get("淨利", "-")
                if pnl != "-" and isinstance(pnl, (int, float)):
                    color = '#dcfce7' if pnl > 0 else ('#fef2f2' if pnl < 0 else '')
                    return [f'background-color: {color}; font-weight: bold'] * len(row)
                return [''] * len(row)
            st.dataframe(round_trips.style.apply(style_stock_trades, axis=1), use_container_width=True, hide_index=True)
        else:
            display_cols = [c for c in ["timestamp", "ticker", "action", "entry_price", "price", "qty", "reason", "strategy", "pnl_gross", "fees", "pnl_cash"] if c in sl.columns]
            col_rename = {"timestamp": "時間", "ticker": "代號", "action": "動作", "entry_price": "進場價",
                          "price": "出場價", "qty": "股數", "reason": "原因", "strategy": "策略",
                          "pnl_gross": "毛利", "fees": "手續費+稅", "pnl_cash": "淨損益"}
            display = sl[display_cols].sort_values("timestamp", ascending=False).head(30)
            display = display.rename(columns={c: col_rename.get(c, c) for c in display.columns})
            st.dataframe(display, use_container_width=True, hide_index=True)

        spnl = calc_stock_pnl(sl)
        fig = make_pnl_chart(spnl, f"台股累計 PnL ({current_mode})")
        if fig:
            st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 原始 Ledger (進階)"):
            st.dataframe(sl, use_container_width=True)
    else:
        st.info(f"今日尚無 {current_mode} 交易紀錄")

# ════════════════════════════════════════
# Tab 5: 策略管道 (Pipeline)
# ════════════════════════════════════════
with tab_pipeline:
    st.header("📊 策略管道 (Strategy Pipeline)")

    # Strategy Rankings
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("☀️ 日盤排行榜")
        try:
            from core.strategy_registry import get_strategy_ranking
            day_ranking = get_strategy_ranking("day")
            for i, (name, pf) in enumerate(day_ranking, 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
                st.write(f"{emoji} {i}. {name} (PF={pf:.1f})")
        except Exception as e:
            st.error(f"Error: {e}")

    with col2:
        st.subheader("🌙 夜盤排行榜")
        try:
            night_ranking = get_strategy_ranking("night")
            for i, (name, pf) in enumerate(night_ranking, 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
                st.write(f"{emoji} {i}. {name} (PF={pf:.1f})")
        except Exception as e:
            st.error(f"Error: {e}")

    # Pipeline Status
    st.subheader("🔄 管道狀態")
    try:
        from core.strategy_registry import STRATEGY_PERF
        pipeline_data = [
            {"策略": "Counter-VWAP", "日盤 PF": STRATEGY_PERF["counter_vwap"]["day_pf"], "夜盤 PF": STRATEGY_PERF["counter_vwap"]["night_pf"], "狀態": "✅ Paper"},
            {"策略": "Spring-Upthrust", "日盤 PF": STRATEGY_PERF["spring_upthrust"]["day_pf"], "夜盤 PF": STRATEGY_PERF["spring_upthrust"]["night_pf"], "狀態": "⏳ 回測驗證"},
            {"策略": "Vol-Squeeze", "日盤 PF": STRATEGY_PERF["vol_squeeze"]["day_pf"], "夜盤 PF": STRATEGY_PERF["vol_squeeze"]["night_pf"], "狀態": "⏳ 觀察中"},
            {"策略": "PSAR", "日盤 PF": STRATEGY_PERF["psar"]["day_pf"], "夜盤 PF": STRATEGY_PERF["psar"]["night_pf"], "狀態": "🔴 夜盤 PF<1.0"},
        ]
        st.table(pd.DataFrame(pipeline_data))
    except Exception as e:
        st.error(f"Error loading pipeline: {e}")

    # Circuit Breaker Status
    st.subheader("🛡️ Circuit Breaker 狀態")
    try:
        from core.circuit_breaker import CircuitBreaker
        day_cb = CircuitBreaker(session="day")
        night_cb = CircuitBreaker(session="night")
        c1, c2 = st.columns(2)
        with c1:
            st.write(f"**日盤**: {day_cb.state.session_pnl:.0f} pts, {day_cb.state.consecutive_losses} 連虧, {'🛑 HALTED' if day_cb.is_halted else '✅ OK'}")
        with c2:
            st.write(f"**夜盤**: {night_cb.state.session_pnl:.0f} pts, {night_cb.state.consecutive_losses} 連虧, {'🛑 HALTED' if night_cb.is_halted else '✅ OK'}")
    except Exception as e:
        st.error(f"Error: {e}")

    # Recent Decisions
    st.subheader("📝 最近決策日誌")
    try:
        from core.decision_logger import DecisionLogger
        recent = DecisionLogger.read(limit=10)
        if recent:
            df_dec = pd.DataFrame([{
                "時間": d.timestamp[:19],
                "類型": d.type,
                "Session": d.session,
                "動作": d.action,
                "細節": d.detail[:50],
            } for d in recent])
            st.dataframe(df_dec, use_container_width=True)
        else:
            st.info("尚無決策記錄")
    except Exception as e:
        st.error(f"Error: {e}")

    # ── Hourly Audit Timeline ──
    st.subheader("🕐 每小時審計時間軸")
    try:
        from pathlib import Path
        audit_dir = Path("logs/market_data")
        today_str = datetime.datetime.now().strftime("%Y%m%d")
        audit_file = audit_dir / f"TMF_{today_str}_signals_audit.csv"

        if audit_file.exists():
            try:
                df_audit = pd.read_csv(audit_file, on_bad_lines='skip')
            except Exception:
                df_audit = pd.DataFrame()

            if df_audit is not None and not df_audit.empty and "signal" in df_audit.columns:
                df_hourly = df_audit[df_audit["signal"] == "HOURLY_AUDIT"]

                if not df_hourly.empty:
                    verdict_map = {
                        "NORMAL": "✅",
                        "COOLDOWN": "🔵",
                        "NO_VALID_SIGNALS": "⚠️",
                        "DATA_FAILURE": "🚨",
                    }
                    df_display = pd.DataFrame({
                        "時間": df_hourly["timestamp"].apply(lambda x: str(x)[-8:]),
                        "Verdict": df_hourly["reason"].map(lambda r: verdict_map.get(r, "❓")),
                        "細節": df_hourly["rejection"].apply(lambda x: str(x)[:60] if pd.notna(x) else ""),
                    })
                    st.dataframe(df_display, use_container_width=True, hide_index=True)

                    verdict_counts = df_hourly["reason"].value_counts()
                    cols = st.columns(min(4, len(verdict_counts)))
                    for i, (verdict, count) in enumerate(verdict_counts.items()):
                        with cols[i % 4]:
                            emoji = verdict_map.get(verdict, "❓")
                            st.metric(f"{emoji} {verdict}", f"{count} 次")
                else:
                    st.info("今日尚無審計記錄（monitor 尚未運行或未到整點）")
        else:
            st.info(f"今日審計檔不存在：{audit_file.name}")
    except Exception as e:
        st.error(f"審計讀取錯誤: {e}")

# ════════════════════════════════════════
# Tab 6: 設定
# ════════════════════════════════════════
with tab_settings:
    st.header("⚙️ 系統設定")

    # ── 0. 實盤就緒度檢查 ──
    with st.expander("🚀 實盤就緒度檢查", expanded=True):
        from core.live_readiness import check_all, get_readiness_summary
        results = check_all()
        status, passed, total = get_readiness_summary(results)

        st.markdown(f"### {status} ({passed}/{total} 項通過)")

        # Progress bar
        pct = passed / total if total > 0 else 0
        st.progress(pct)

        # Detail table
        for r in results:
            icon = "✅" if r.passed else "❌"
            st.caption(f"{icon} **{r.name}**: {r.value} — {r.detail}")

        # Action recommendation
        if passed == total:
            st.success("🎉 所有檢查通過！可以考慮進入 Phase 2 小額實盤測試")
            st.info("建議: 先用 1 口 TMF 測試 5 個交易日，設定每日最大虧損 2%")
        elif passed >= total * 0.6:
            remaining = total - passed
            st.warning(f"⚠️ 還有 {remaining} 項未通過，建議繼續 Paper 觀察")
            for r in results:
                if not r.passed:
                    st.caption(f"❌ 待解決: {r.name} (目前: {r.value})")
        else:
            st.error("❌ 多數檢查未通過，不建議開啟實盤交易")

        st.divider()
        st.caption("參考文件: `docs/LIVE_TRADING_GUIDE.md`")

    # ── 1. 期貨 TMF 設定 ──
    with st.expander("📈 期貨 TMF 設定", expanded=True):
        from core.strategy_registry import StrategyRegistry
        _reg = StrategyRegistry()
        _reg.discover()
        fut_strats = {item["name"]: item for item in _reg.list_all() if item.get("asset_class") == "futures" and item.get("available", False)}
        current_fut_strat = futures_cfg.get("strategy", {}).get("active_strategy", futures_cfg.get("active_strategy", "counter_vwap"))

        with st.form("futures_settings_form"):
            f_live_new = st.checkbox("啟用期貨實盤交易 (LIVE)", value=futures_cfg.get("live_trading", False))

            # Strategy selector from Registry
            strat_options = list(fut_strats.keys())
            try:
                strat_idx = strat_options.index(current_fut_strat)
            except ValueError:
                strat_idx = 0

            f_strat_new = st.selectbox("核心進場策略", strat_options, index=strat_idx)

            # Show strategy metadata
            meta = fut_strats.get(f_strat_new, {})
            desc = meta.get("description", "無說明")
            pf = meta.get("backtest_pf", 0)
            wr = meta.get("backtest_wr", 0)
            maxdd = meta.get("backtest_maxdd", 0)
            regime = meta.get("market_regime", "")
            st.info(f"💡 **{f_strat_new}**: {desc}  \n"
                    f"📊 PF={pf:.2f} | WR={wr:.1f}% | MaxDD={maxdd:.1f}% | 適用: {regime}")

            st.divider()

            # ── 口數與持倉限制 ──
            st.markdown("##### 📦 口數與持倉限制")
            c1, c2 = st.columns(2)
            f_lots = c1.number_input("每筆交易口數", min_value=1, max_value=10,
                                     value=futures_cfg.get("trade_mgmt", {}).get("lots_per_trade", 2),
                                     help="每次進場的口數。實盤建議從 1 口開始。")
            f_max_pos = c2.number_input("最大持倉口數", min_value=1, max_value=10,
                                        value=futures_cfg.get("trade_mgmt", {}).get("max_positions", 2),
                                        help="同時最大持倉口數。建議 1-2 口控制風險。")

            st.divider()
            f_regime = st.selectbox("市場濾網 (Regime)", ["low", "mid", "high"], index=["low", "mid", "high"].index(futures_cfg.get("strategy", {}).get("regime_filter", "mid")))
            
            fc1, fc2 = st.columns(2)
            f_score = fc1.slider("進場門檻 (Score)", 10, 100, value=futures_cfg.get("strategy", {}).get("entry_score", 20))
            f_atr = fc2.slider("ATR 止損倍數", 1.0, 5.0, value=float(futures_cfg.get("risk_mgmt", {}).get("atr_multiplier", 2.0)), step=0.1)

            st.divider()
            st.markdown("##### 🛡️ 進階安全與成本設定")
            fc3, fc4 = st.columns(2)
            f_stop_fixed = fc3.number_input("固定停損點數 (pts)", min_value=10, max_value=200,
                                           value=int(futures_cfg.get("risk_mgmt", {}).get("stop_loss_pts", 60)),
                                           help="ATR 失效或為 0 時使用的保險底線停損點數。")
            f_fee = fc4.number_input("單邊手續費 (TWD)", min_value=0.0, max_value=100.0,
                                    value=float(futures_cfg.get("execution", {}).get("broker_fee_per_side", 20.0)),
                                    help="用於 PnL 計算。請輸入你的真實券商折扣後手續費。")

            if st.form_submit_button("💾 儲存並重啟期貨模組"):
                futures_cfg["live_trading"] = f_live_new
                futures_cfg["strategy"]["active_strategy"] = f_strat_new
                futures_cfg["strategy"]["regime_filter"] = f_regime
                futures_cfg["strategy"]["entry_score"] = f_score
                futures_cfg["risk_mgmt"]["atr_multiplier"] = f_atr
                futures_cfg["risk_mgmt"]["stop_loss_pts"] = f_stop_fixed
                futures_cfg["execution"]["broker_fee_per_side"] = f_fee
                futures_cfg["trade_mgmt"]["lots_per_trade"] = f_lots
                futures_cfg["trade_mgmt"]["max_positions"] = f_max_pos
                save_yaml(FUTURES_CFG_PATH, futures_cfg)
                trigger_restart()
                st.success(f"期貨設定已更新！策略: {f_strat_new} | 口數: {f_lots} | 最大持倉: {f_max_pos}")
                st.rerun()

    # ── 2. 選擇權 TXO 設定 ──
    with st.expander("🔮 選擇權 TXO 設定", expanded=False):
        # 假設選擇權也有類似的策略結構，目前從 config 讀取
        current_opt_mode = options_cfg.get("mode", "V2")
        
        with st.form("options_settings_form"):
            o_live_new = st.checkbox("啟用選擇權實盤交易 (LIVE)", value=options_cfg.get("live_trading", False))
            
            o_mode_new = st.selectbox("交易模式", ["V1", "V2", "V3"], index=["V1", "V2", "V3"].index(current_opt_mode),
                                      help="V1: 當沖, V2: 波段(月選), V3: 夜盤當沖")
            
            # 模式說明
            mode_desc = {
                "V1": "近月合約當日沖銷，適合高波動行情。",
                "V2": "月選合約波段持有 (≥14天 DTE)，降低時間價值衰減。",
                "V3": "專攻夜盤波動，開盤進場收盤前出場。"
            }
            st.info(f"💡 **模式說明**: {mode_desc.get(o_mode_new)}")

            o_score = st.slider("進場門檻 (Score)", 10, 100, value=options_cfg.get("entry_score", 80))

            o_fire_thresh = st.slider("Fire 門檻 (強趨勢 score)", 10, 100,
                                       value=int(options_cfg.get("strategy", {}).get("fire_score_threshold", 80)),
                                       help="fired=False 但 score 超過此值也允許進場。降低此值可在趨勢行情中進場。")

            # ── 口數與持倉限制 ──
            st.markdown("##### 📦 口數與持倉限制")
            oc1, oc2 = st.columns(2)
            o_lots = oc1.number_input("每筆交易口數", min_value=1, max_value=10,
                                     value=options_cfg.get("risk_mgmt", {}).get("lots_per_trade", 2),
                                     help="每次進場的口數。實盤建議從 1 口開始。")
            o_max_pos = oc2.number_input("最大持倉口數", min_value=1, max_value=10,
                                        value=options_cfg.get("risk_mgmt", {}).get("max_positions", 2),
                                        help="同時最大持倉口數。建議 1-2 口控制風險。")

            st.divider()
            oc3, oc4 = st.columns(2)
            o_min_iv = oc3.slider("最低 IV 限制", 0.1, 0.5, value=float(options_cfg.get("min_iv", 0.15)), step=0.01)
            o_max_iv = oc4.slider("最高 IV 限制", 0.3, 1.0, value=float(options_cfg.get("max_iv", 0.60)), step=0.01)

            st.divider()
            st.markdown("##### 🛡️ 進階安全與成本設定")
            oc5, oc6 = st.columns(2)
            o_fee = oc5.number_input("單邊手續費 (TWD)", min_value=0.0, max_value=100.0,
                                    value=float(options_cfg.get("execution", {}).get("broker_fee_per_side", 20.0)),
                                    help="券商收取的單口單邊手續費。")
            o_exch = oc6.number_input("單邊交易所費 (TWD)", min_value=0.0, max_value=100.0,
                                     value=float(options_cfg.get("execution", {}).get("exchange_fee_per_side", 5.0)),
                                     help="期交所收取的單口單邊費用。")

            if st.form_submit_button("💾 儲存並重啟選擇權模組"):
                options_cfg["live_trading"] = o_live_new
                options_cfg["active_mode"] = o_mode_new
                options_cfg["entry_score"] = o_score
                options_cfg["strategy"]["fire_score_threshold"] = o_fire_thresh
                options_cfg["min_iv"] = o_min_iv
                options_cfg["max_iv"] = o_max_iv
                options_cfg["execution"]["broker_fee_per_side"] = o_fee
                options_cfg["execution"]["exchange_fee_per_side"] = o_exch
                options_cfg["risk_mgmt"]["lots_per_trade"] = o_lots
                options_cfg["risk_mgmt"]["max_positions"] = o_max_pos
                save_yaml(OPTIONS_CFG_PATH, options_cfg)
                trigger_restart()
                st.success(f"選擇權設定已更新！模式: {o_mode_new} | 口數: {o_lots} | 最大持倉: {o_max_pos} | Fire閾值: {o_fire_thresh}")
                st.rerun()

    # ── 3. 台股 Stocks 設定 ──
    with st.expander("🍎 台股 Stocks 設定", expanded=True):
        stk_inner = stock_cfg.get("stocks", {})
        
        # 將同步按鈕與顯示邏輯整合
        if st.button("🔄 同步 Squeeze Screener 推薦名單"):
            try:
                import subprocess
                subprocess.run(["python3", "scripts/sync/sync_watchlist.py"], check=True, timeout=30)
                st.success("同步成功！")
                st.rerun()
            except Exception as e:
                st.error(f"同步失敗: {e}")

        with st.form("stock_settings_form"):
            s_live_new = st.checkbox("啟用台股實盤交易 (LIVE)", value=stock_cfg.get("live_trading", False))
            
            # --- 改為直式多行輸入 ---
            current_watchlist = stk_inner.get("watchlist", ["2330", "2454"])
            watchlist_area = st.text_area("監控名單 (每行一個代號)", 
                                         value="\n".join(current_watchlist),
                                         height=200,
                                         help="請輸入股票代號，例如 2330。支援一行一個標的。")
            
            c1, c2, c2b = st.columns(3)
            from strategies.stocks.entry_strategies import STOCK_STRATEGIES
            strat_options = list(STOCK_STRATEGIES.keys())
            current_strat = stk_inner.get("strategy", "momentum_breakout")
            strat_idx = strat_options.index(current_strat) if current_strat in strat_options else 0
            strat_new = c1.selectbox("策略", strat_options, index=strat_idx,
                                     help=STOCK_STRATEGIES.get(current_strat, {}).get("desc", ""))
            budget_new = c2.number_input("總分配資金 (TWD)", value=stk_inner.get("total_portfolio_budget", 100000), step=10000)
            capital_new = c2b.number_input("單筆預算 (TWD)", value=stk_inner.get("capital_per_trade", 20000), step=1000)
            
            c3, c4, c5 = st.columns(3)
            sl_new = c3.slider("停損 (%)", 1.0, 10.0, value=float(stk_inner.get("stop_loss_pct", 0.02)*100), step=0.5) / 100.0
            tp_new = c4.slider("停利 (%)", 2.0, 20.0, value=float(stk_inner.get("take_profit_pct", 0.10)*100), step=0.5) / 100.0
            ts_new = c5.slider("移動停損 (%)", 0.5, 5.0, value=float(stk_inner.get("trailing_stop_pct", 0.01)*100), step=0.1) / 100.0
            
            if st.form_submit_button("💾 儲存並重啟台股模組"):
                # 處理多行輸入，轉回 list
                new_tickers = [t.strip() for t in watchlist_area.split("\n") if t.strip()]
                new_stocks = {
                    "watchlist": new_tickers,
                    "strategy": strat_new,
                    "total_portfolio_budget": budget_new,
                    "capital_per_trade": capital_new,
                    "entry_score": stk_inner.get("entry_score", 20),
                    "atr_mult": stk_inner.get("atr_mult", 2.0),
                    "stop_loss_pct": sl_new,
                    "take_profit_pct": tp_new,
                    "trailing_stop_pct": ts_new,
                }
                # 保留 bear_defense 等既有區塊
                if "bear_defense" in stk_inner:
                    new_stocks["bear_defense"] = stk_inner["bear_defense"]
                if "fallback_strategy" in stk_inner:
                    new_stocks["fallback_strategy"] = stk_inner["fallback_strategy"]
                new_stock_cfg = {
                    "live_trading": s_live_new,
                    "stocks": new_stocks,
                }
                save_yaml(STOCK_CFG_PATH, new_stock_cfg)
                trigger_restart()
                # GSD Fix: stock_runner is independent process, must be killed separately
                import subprocess
                subprocess.run(["pkill", "-f", "stock_runner.py"], capture_output=True)
                st.success("台股設定已更新，正在重啟系統...")

    # ── 4. 危險區域 ──
    st.divider()
    with st.expander("🚨 危險區域 (Danger Zone)", expanded=False):
        st.warning("以下操作將永久刪除數據，請謹慎執行。")
        col_d1, col_d2 = st.columns([2, 1])
        with col_d1:
            st.markdown("##### 🗑️ 清空模擬交易數據")
            st.caption("這將歸零所有 Paper Trading 的持倉與歷史紀錄 CSV，方便切換實盤前清空數據。")
        with col_d2:
            if st.button("執行清空", type="primary", use_container_width=True):
                try:
                    import subprocess
                    result = subprocess.run(["python3", "scripts/maintenance/clear_simulation_data.py", "--force"], 
                                                         capture_output=True, text=True)
                    st.success("✅ 數據已清空！")
                    st.toast("Simulation data cleared successfully.")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"清空失敗: {e}")

    st.info("💡 提示: 部分進階設定可直接編輯 `config/*.yaml` 檔案。")

# ── Footer and Refresh ──
refresh = 30
time.sleep(refresh)
st.rerun()
