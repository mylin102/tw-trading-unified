from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from core.date_utils import get_trade_day
from strategies.options.options_engine.engine.greeks import black_scholes


ENTRY_ACTIONS_FUTURES = {"BUY", "SELL", "SHORT"}
EXIT_ACTIONS_FUTURES = {"EXIT", "COVER", "PARTIAL_EXIT"}
OPTIONS_EXIT_KEYWORDS = ("EXIT", "THETA_EXIT", "TP1", "TRAIL", "TIME", "REVERSAL", "TRAP", "EOD", "FILL")


@dataclass
class FuturesOpenPosition:
    direction: str
    entry_price: float
    lots: int
    timestamp: str

    @property
    def cost_basis(self) -> float:
        return self.entry_price * 50 * self.lots


@dataclass
class OptionsOpenPosition:
    action: str
    side: str
    entry_price: float
    quantity: int
    timestamp: str
    note: str

    @property
    def cost_basis(self) -> float:
        return self.entry_price * 50 * self.quantity


def count_futures_entries(trades_df: pd.DataFrame | None) -> int:
    if trades_df is None or trades_df.empty:
        return 0
    action_col = "type" if "type" in trades_df.columns else "action" if "action" in trades_df.columns else None
    if not action_col:
        return 0
    actions = trades_df[action_col].fillna("").astype(str).str.upper()
    return int(actions.isin(ENTRY_ACTIONS_FUTURES).sum())


def find_latest_open_futures_position(trades_df: pd.DataFrame | None) -> FuturesOpenPosition | None:
    if trades_df is None or trades_df.empty:
        return None
    action_col = "type" if "type" in trades_df.columns else "action" if "action" in trades_df.columns else None
    if not action_col:
        return None

    open_pos: FuturesOpenPosition | None = None
    for _, row in trades_df.iterrows():
        action = str(row.get(action_col, "")).upper()
        if action in ENTRY_ACTIONS_FUTURES:
            direction = "BUY" if action == "BUY" else "SHORT"
            open_pos = FuturesOpenPosition(
                direction=direction,
                entry_price=float(row.get("entry_price", row.get("price", 0)) or 0),
                lots=int(row.get("lots", row.get("qty", 1)) or 1),
                timestamp=str(row.get("timestamp", row.get("Timestamp", ""))),
            )
        elif open_pos and action in EXIT_ACTIONS_FUTURES:
            exit_qty = int(row.get("lots", row.get("qty", open_pos.lots)) or open_pos.lots)
            remaining = max(0, open_pos.lots - exit_qty)
            if action == "PARTIAL_EXIT" and remaining > 0:
                open_pos = FuturesOpenPosition(
                    direction=open_pos.direction,
                    entry_price=open_pos.entry_price,
                    lots=remaining,
                    timestamp=open_pos.timestamp,
                )
            else:
                open_pos = None
    return open_pos


def count_options_entries(ledger_df: pd.DataFrame | None, trading_day_str: str) -> int:
    if ledger_df is None or ledger_df.empty or "Timestamp" not in ledger_df.columns or "Action" not in ledger_df.columns:
        return 0
    df = ledger_df.copy()
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df = df.dropna(subset=["Timestamp"])
    if df.empty:
        return 0
    df["TradingDay"] = df["Timestamp"].apply(lambda x: get_trade_day(x).strftime("%Y%m%d"))
    actions = df["Action"].fillna("").astype(str)
    is_entry = actions.str.contains("ENTRY", na=False) & ~actions.str.contains("EXIT", na=False)
    return int((df["TradingDay"].eq(trading_day_str) & is_entry).sum())


def find_latest_open_options_position(ledger_df: pd.DataFrame | None) -> OptionsOpenPosition | None:
    if ledger_df is None or ledger_df.empty or "Action" not in ledger_df.columns:
        return None

    open_pos: OptionsOpenPosition | None = None
    for _, row in ledger_df.iterrows():
        action = str(row.get("Action", "")).upper()
        if "ENTRY" in action and "EXIT" not in action and "RETRY" not in action and "SUBMITTED" not in action and "CLEARED" not in action:
            open_pos = OptionsOpenPosition(
                action=action,
                side=str(row.get("Side", "")),
                entry_price=float(row.get("Price", 0) or 0),
                quantity=int(row.get("Quantity", 1) or 1),
                timestamp=str(row.get("Timestamp", "")),
                note=str(row.get("Note", "")),
            )
        elif open_pos and any(keyword in action for keyword in OPTIONS_EXIT_KEYWORDS):
            open_pos = None
    return open_pos


def option_order_matches_open_position(order_row, open_pos: OptionsOpenPosition | None) -> bool:
    if open_pos is None:
        return False

    if str(order_row.get("strategy", "")).upper() == "RECOVERED":
        return False

    entry_price = float(order_row.get("avg_fill_price", order_row.get("price", 0)) or 0)
    if abs(entry_price - open_pos.entry_price) > 1e-6:
        return False

    quantity = int(order_row.get("filled_quantity", order_row.get("quantity", 0)) or 0)
    if quantity != open_pos.quantity:
        return False

    order_ts = pd.to_datetime(
        order_row.get("filled_at")
        or order_row.get("created_at")
        or order_row.get("timestamp")
        or order_row.get("Timestamp"),
        errors="coerce",
    )
    open_ts = pd.to_datetime(open_pos.timestamp, errors="coerce")
    if not pd.isna(order_ts) and not pd.isna(open_ts):
        return order_ts.strftime("%Y-%m-%d %H:%M:%S") == open_ts.strftime("%Y-%m-%d %H:%M:%S")

    return True


def summarize_combo_legs(combo_legs) -> str:
    if not isinstance(combo_legs, list) or not combo_legs:
        return ""

    parts = []
    for leg in combo_legs:
        if not isinstance(leg, dict):
            continue
        action = str(leg.get("action", "")).upper()
        side = str(leg.get("side", "")).upper()
        strike = leg.get("strike")
        if strike is None or side not in {"C", "P"} or action not in {"BUY", "SELL"}:
            continue
        strike_val = float(strike or 0)
        strike_text = f"{int(strike_val)}" if strike_val.is_integer() else f"{strike_val:g}"
        parts.append(f"{action} {side}{strike_text}")
    return " | ".join(parts)


def describe_options_order_truth(order_row, *, orders_rebuilt_from_ledger: bool = False) -> dict:
    raw_truth = str(order_row.get("truth_source", "") or "").strip().lower()
    if orders_rebuilt_from_ledger:
        truth_source = "ledger_rebuilt"
    elif raw_truth in {"broker_combo", "paper_strategy", "ledger_rebuilt"}:
        truth_source = raw_truth
    else:
        truth_source = "paper_strategy"

    if truth_source == "broker_combo":
        return {
            "truth_source": truth_source,
            "badge": "✅ broker_combo",
            "show_paper_disclaimer": False,
            "degraded_caption": "",
        }
    if truth_source == "ledger_rebuilt":
        return {
            "truth_source": truth_source,
            "badge": "⚠️ ledger_rebuilt",
            "show_paper_disclaimer": True,
            "degraded_caption": "⚠️ broker truth unavailable; rebuilt from ledger fallback.",
        }
    return {
        "truth_source": "paper_strategy",
        "badge": "📝 paper_strategy",
        "show_paper_disclaimer": True,
        "degraded_caption": "📝 紙上策略估值／非券商複式單逐腿成交回報。",
    }


def _estimate_combo_unrealized(
    combo_legs,
    *,
    entry_value: float,
    quantity: int,
    current_spot: float,
    current_iv: float,
    dte_years: float,
    strategy: str = "theta",
    max_loss: float = 0.0,
) -> dict | None:
    if not isinstance(combo_legs, list) or not combo_legs:
        return None
    if entry_value <= 0 or current_spot <= 0 or current_iv <= 0 or dte_years <= 0:
        return None

    current_value = 0.0
    parsed_legs = []
    for leg in combo_legs:
        if not isinstance(leg, dict):
            continue
        action = str(leg.get("action", "")).upper()
        side = str(leg.get("side", "")).upper()
        strike = float(leg.get("strike", 0) or 0)
        if action not in {"SELL", "BUY"} or side not in {"C", "P"} or strike <= 0:
            continue
        res = black_scholes(current_spot, strike, dte_years, 0.02, current_iv, side)
        leg_value = float(res.get("price", 0) or 0)
        current_value += leg_value if action == "SELL" else -leg_value
        parsed_legs.append({"action": action, "side": side, "strike": strike, "premium": leg_value})

    if not parsed_legs:
        return None

    gross_pnl = entry_value - current_value
    broker_fee = 20 * 2 * quantity
    exchange_fee = 5 * 2 * quantity
    tax = (entry_value + current_value) * 50 * 0.001 * quantity
    total_cost = broker_fee + exchange_fee + tax
    unrealized_pnl = gross_pnl * 50 * quantity - total_cost

    return {
        "strategy": strategy,
        "entry_credit": entry_value,
        "current_value": current_value,
        "max_loss": max_loss,
        "quantity": quantity,
        "cost_basis": entry_value * 50 * quantity,
        "unrealized_pnl": unrealized_pnl,
        "fees_estimate": total_cost,
        "legs": parsed_legs,
    }


def _extract_theta_note_details(note: str) -> dict | None:
    if not note:
        return None

    credit_match = re.search(r"credit=([0-9.]+)", note)
    strategy_match = re.search(r"strategy=([a-z_]+)", note)
    max_loss_match = re.search(r"max_loss=([0-9.]+)", note)
    legs_match = re.search(r"\[(.+)\]", note)
    if not credit_match or not legs_match:
        return None

    combo_legs = []
    for part in [segment.strip() for segment in legs_match.group(1).split("|")]:
        leg_match = re.match(r"(SELL|BUY)\s+([CP])([0-9.]+)", part)
        if not leg_match:
            continue
        action, side, strike_str = leg_match.groups()
        combo_legs.append({"action": action, "side": side, "strike": float(strike_str)})

    if not combo_legs:
        return None

    return {
        "entry_credit": float(credit_match.group(1)),
        "strategy": strategy_match.group(1) if strategy_match else "theta",
        "max_loss": float(max_loss_match.group(1)) if max_loss_match else 0.0,
        "combo_legs": combo_legs,
    }


def estimate_options_order_unrealized(
    order_row,
    open_pos: OptionsOpenPosition | None,
    *,
    live_premium: float = 0.0,
    current_spot: float = 0.0,
    current_iv: float = 0.0,
    dte_years: float = 0.0,
    strike: float = 0.0,
) -> dict | None:
    if str(order_row.get("status", "")).lower() not in {"filled", "partial_filled"}:
        return None

    open_quantity = open_pos.quantity if open_pos is not None else 1
    quantity = int(order_row.get("filled_quantity", order_row.get("quantity", open_quantity)) or open_quantity or 1)

    truth_source = str(order_row.get("truth_source", "") or "").lower()
    combo_legs = order_row.get("combo_legs")
    if truth_source == "broker_combo" or (isinstance(combo_legs, list) and combo_legs):
        combo_estimate = _estimate_combo_unrealized(
            combo_legs,
            entry_value=float(order_row.get("avg_fill_price", order_row.get("price", 0)) or 0),
            quantity=quantity,
            current_spot=current_spot,
            current_iv=current_iv,
            dte_years=dte_years,
            strategy=str(order_row.get("combo_strategy", order_row.get("strategy", "theta")) or "theta"),
        )
        if combo_estimate is not None:
            return {
                "unrealized_pnl": float(combo_estimate["unrealized_pnl"]),
                "current_price": float(combo_estimate["current_value"]),
                "pricing_label": "spread_value",
            }

    if open_pos is None:
        return None
    if not option_order_matches_open_position(order_row, open_pos):
        return None

    if "THETA" in str(open_pos.action).upper():
        theta_estimate = estimate_theta_unrealized(
            open_pos.note,
            current_spot=current_spot,
            current_iv=current_iv,
            dte_years=dte_years,
            quantity=open_pos.quantity or quantity,
        )
        if theta_estimate is None:
            return None
        return {
            "unrealized_pnl": float(theta_estimate["unrealized_pnl"]),
            "current_price": float(theta_estimate["current_value"]),
            "pricing_label": "spread_value",
        }

    # ── Single-leg (buy Call or buy Put) ──
    # 2026-05-25 Hermes Agent: BS theoretical premium with hard fallback chain
    entry = float(order_row.get("avg_fill_price", order_row.get("price", 0)) or 0)
    if entry <= 0:
        return None

    side = str(order_row.get("side", "")).lower()
    option_type = "C" if side == "buy" else "P"  # buy call → C, buy put → P

    premium_source = "ENTRY_FALLBACK"
    current_premium = entry  # default: entry price fallback

    # Priority 1: live premium from bid/ask mid
    if live_premium > 0:
        current_premium = live_premium
        premium_source = "LIVE_QUOTE"

    # Priority 2: BS theoretical (only if live premium unavailable)
    elif current_spot > 0 and current_iv > 0 and dte_years > 0 and strike > 0:
        try:
            rf_rate = 0.02  # Taiwan risk-free rate ~2%
            bs_result = black_scholes(current_spot, strike, dte_years, rf_rate, current_iv, option_type)
            bs_price = float(bs_result.get("price", 0) or 0)
            if bs_price > 0:
                current_premium = bs_price
                premium_source = "BS_THEO"
        except Exception:
            pass  # fall through to entry fallback

    pnl_pts = entry - current_premium if side == "sell" else current_premium - entry
    return {
        "unrealized_pnl": float(pnl_pts * 50 * quantity),
        "current_price": float(current_premium),
        "pricing_label": "option_premium",
        "premium_source": premium_source,
        "dte_days": round(dte_years * 365.0, 2),
    }


def latest_indicator_close(indicator_df: pd.DataFrame | None) -> float:
    if indicator_df is None or indicator_df.empty:
        return 0.0

    for col in ("close", "Close"):
        if col in indicator_df.columns:
            value = pd.to_numeric(indicator_df[col], errors="coerce").iloc[-1]
            if pd.notna(value):
                return float(value)
    return 0.0


def estimate_theta_unrealized(note: str, current_spot: float, current_iv: float, dte_years: float, quantity: int = 1) -> dict | None:
    if not note or current_spot <= 0 or current_iv <= 0 or dte_years <= 0:
        return None

    details = _extract_theta_note_details(note)
    if details is None:
        return None

    return _estimate_combo_unrealized(
        details["combo_legs"],
        entry_value=details["entry_credit"],
        quantity=quantity,
        current_spot=current_spot,
        current_iv=current_iv,
        dte_years=dte_years,
        strategy=details["strategy"],
        max_loss=details["max_loss"],
    )
