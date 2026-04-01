import sys
import os
from pathlib import Path

# 確保能匯入同目錄下的 greeks
sys.path.append(os.path.dirname(__file__))
try:
    from greeks import black_scholes
except ImportError:
    # 備援路徑
    from options_engine.engine.greeks import black_scholes

POINT_VALUE = 50
DEFAULT_PRICING_CFG = {
    "pricing_model": "linear",
    "entry_premium_mode": "profile",
    "default_iv": 0.25,
    "min_iv": 0.18,
    "max_iv": 0.32,
    "bull_call_iv_mult": 0.95,
    "bear_put_iv_mult": 1.05,
    "neutral_iv_mult": 1.0,
    "risk_free_rate": 0.02,
    "near_dte_days": 3.0,
    "expiry_dte_floor_days": 0.35,
    "strike_rounding": 100,
}


def get_pricing_cfg(cfg=None):
    pricing_cfg = dict(DEFAULT_PRICING_CFG)
    if cfg:
        pricing_cfg.update(cfg.get("pricing", {}))
    return pricing_cfg


def resolve_option_strike(underlying_price, strike_rounding=100):
    if strike_rounding <= 0:
        return underlying_price
    return int(round(underlying_price / strike_rounding) * strike_rounding)


def resolve_dte_years(pricing_cfg, elapsed_days=0.0):
    near_dte_days = pricing_cfg.get("near_dte_days", DEFAULT_PRICING_CFG["near_dte_days"])
    floor_days = pricing_cfg.get("expiry_dte_floor_days", DEFAULT_PRICING_CFG["expiry_dte_floor_days"])
    remaining_days = max(floor_days, near_dte_days - elapsed_days)
    return remaining_days / 365.0


def resolve_effective_iv(pricing_cfg, side, mid_trend=None):
    base_iv = pricing_cfg.get("default_iv", DEFAULT_PRICING_CFG["default_iv"])
    iv_mult = pricing_cfg.get("neutral_iv_mult", DEFAULT_PRICING_CFG["neutral_iv_mult"])
    if side == "C" and mid_trend == "BULL":
        iv_mult = pricing_cfg.get("bull_call_iv_mult", DEFAULT_PRICING_CFG["bull_call_iv_mult"])
    elif side == "P" and mid_trend == "BEAR":
        iv_mult = pricing_cfg.get("bear_put_iv_mult", DEFAULT_PRICING_CFG["bear_put_iv_mult"])
    effective_iv = base_iv * iv_mult
    min_iv = pricing_cfg.get("min_iv", DEFAULT_PRICING_CFG["min_iv"])
    max_iv = pricing_cfg.get("max_iv", DEFAULT_PRICING_CFG["max_iv"])
    return min(max(effective_iv, min_iv), max_iv)


def initial_option_premium(underlying_price, side, pricing_cfg=None, strike=None, mode_profile=None, mid_trend=None):
    pricing_cfg = pricing_cfg or dict(DEFAULT_PRICING_CFG)
    pricing_model = pricing_cfg.get("pricing_model", "linear")
    premium_mode = pricing_cfg.get("entry_premium_mode", "profile")
    strike = strike or resolve_option_strike(underlying_price, pricing_cfg.get("strike_rounding", 100))

    if pricing_model == "black_scholes" and premium_mode == "model":
        res = black_scholes(
            underlying_price,
            strike,
            resolve_dte_years(pricing_cfg),
            pricing_cfg.get("risk_free_rate", DEFAULT_PRICING_CFG["risk_free_rate"]),
            resolve_effective_iv(pricing_cfg, side, mid_trend=mid_trend),
            option_type=side,
        )
        return res["price"]

    fallback_profile = mode_profile or {}
    return fallback_profile.get("initial_premium", 100)

def mark_option_premium(entry_price_mtx, current_price_mtx, side, entry_opt_premium, 
                        theta_per_bar=0.0, delta=0.5, use_greeks=False, 
                        dte=0.01, iv=0.2, r=0.02, strike=None):
    """
    更新選擇權權利金。
    if use_greeks=True: 使用 BS 模型根據期貨價格變動計算 delta 與價格。
    else: 使用傳入的固定 delta 計算。
    """
    if use_greeks and strike is not None:
        # 使用 BS 模型估算新的價格
        # 注意：這裡是一個簡化模型，假設 IV 不變
        res = black_scholes(current_price_mtx, strike, dte, r, iv, option_type=side)
        current_premium = res["price"]
        # 為了保持回測引擎的一致性，我們仍返回 decayed_entry (雖然在 BS 模型中 theta 已包含在價格變化中)
        decayed_entry = entry_opt_premium 
    else:
        # 傳統線性模型
        decayed_entry = entry_opt_premium * (1 - theta_per_bar)
        pts_diff = (current_price_mtx - entry_price_mtx) * (1 if side == "C" else -1)
        current_premium = decayed_entry + (pts_diff * delta)
        
    return decayed_entry, current_premium


def mark_option_premium_from_cfg(
    entry_price_mtx,
    current_price_mtx,
    side,
    entry_opt_premium,
    cfg=None,
    mode_profile=None,
    elapsed_days=0.0,
    mid_trend=None,
):
    pricing_cfg = get_pricing_cfg(cfg)
    pricing_model = pricing_cfg.get("pricing_model", "linear")
    theta_per_bar = (mode_profile or {}).get("theta_per_5m", 0.0)
    strike = resolve_option_strike(entry_price_mtx, pricing_cfg.get("strike_rounding", 100))
    return mark_option_premium(
        entry_price_mtx,
        current_price_mtx,
        side,
        entry_opt_premium,
        theta_per_bar=theta_per_bar,
        use_greeks=pricing_model == "black_scholes",
        dte=resolve_dte_years(pricing_cfg, elapsed_days=elapsed_days),
        iv=resolve_effective_iv(pricing_cfg, side, mid_trend=mid_trend),
        r=pricing_cfg.get("risk_free_rate", DEFAULT_PRICING_CFG["risk_free_rate"]),
        strike=strike,
    )


def should_take_partial_profit(position, has_tp1, entry_opt_premium, current_premium, tp1_pct):
    if has_tp1 or position != 2:
        return False
    return (current_premium - entry_opt_premium) / entry_opt_premium >= tp1_pct


def stop_threshold(entry_opt_premium, stop_loss_pct, has_tp1):
    return entry_opt_premium if has_tp1 else entry_opt_premium * (1 - stop_loss_pct)


def should_exit_position(current_premium, entry_opt_premium, stop_loss_pct, score, has_tp1, score_floor=20):
    return current_premium <= stop_threshold(entry_opt_premium, stop_loss_pct, has_tp1) or abs(score) < score_floor


def should_force_close_by_session(current_time, mode_profile=None, panic_hour=13, panic_minute=25):
    if not (mode_profile or {}).get("force_close_at_end", False):
        return False
    return current_time.hour > panic_hour or (current_time.hour == panic_hour and current_time.minute >= panic_minute)


def classify_exit_reason(
    current_premium,
    entry_opt_premium,
    stop_loss_pct,
    score,
    has_tp1,
    current_time=None,
    mode_profile=None,
    score_floor=20,
):
    if current_premium <= stop_threshold(entry_opt_premium, stop_loss_pct, has_tp1):
        return "stop_loss"
    if abs(score) < score_floor:
        return "score_decay"
    if current_time is not None and should_force_close_by_session(current_time, mode_profile=mode_profile):
        return "force_close"
    return None


def should_exit_by_time_constraints(entry_time, current_time, dte_days, max_days=None, min_dte=None):
    """
    檢查是否因持有天數過長或太接近到期日而需要出場。
    entry_time: 進場 datetime
    current_time: 當前 datetime
    dte_days: 距離到期剩餘天數 (實數天)
    max_days: 最大持有天數
    min_dte: 強制出場的最小剩餘天數
    """
    if max_days is not None:
        holding_days = (current_time - entry_time).total_seconds() / (24 * 3600)
        if holding_days >= max_days:
            return True, f"Max holding days ({max_days}) reached"
            
    if min_dte is not None:
        if dte_days <= min_dte:
            return True, f"Min DTE ({min_dte}) reached"
            
    return False, ""


def realized_pnl(current_premium, entry_opt_premium, position, point_value=POINT_VALUE):
    return (current_premium - entry_opt_premium) * point_value * position


def unrealized_equity(balance, current_premium, entry_opt_premium, position, point_value=POINT_VALUE):
    if position <= 0:
        return balance
    return balance + realized_pnl(current_premium, entry_opt_premium, position, point_value=point_value)
