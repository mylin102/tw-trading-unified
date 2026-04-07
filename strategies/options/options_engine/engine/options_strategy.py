DEFAULT_WEIGHTS = {"1h": 0.2, "15m": 0.4, "5m": 0.4}

MODE_PROFILES = {
    "V1": {
        "theta_per_5m": 0.02 / 54,
        "initial_premium": 100,
        "default_tp1_pct": 0.5,
        "default_force_close": True,
        "default_bear_boost_pct": 0.5,
    },
    "V2": {
        "theta_per_5m": 0.005 / 54,
        "initial_premium": 250,
        "default_tp1_pct": 0.5,
        "default_force_close": False,
        "default_bear_boost_pct": 0.8,
    },
}


def get_strategy_cfg(cfg):
    return cfg.get("strategy", {})


def get_risk_cfg(cfg):
    return cfg.get("risk_mgmt", cfg.get("exit_strategy", {}))


def get_strategy_weights(cfg):
    return get_strategy_cfg(cfg).get("weights", DEFAULT_WEIGHTS)


def get_stop_loss_pct(cfg, default=0.3):
    return get_risk_cfg(cfg).get("stop_loss_pct", default)


def get_score_floor(cfg, default=20):
    return get_strategy_cfg(cfg).get("score_floor", default)


def get_max_holding_days(cfg, default=None):
    return get_risk_cfg(cfg).get("max_holding_days", default)


def get_min_dte_to_exit(cfg, default=None):
    return get_risk_cfg(cfg).get("min_dte_to_exit", default)


def get_mode_cfg(cfg, mode=None):
    selected_mode = mode or cfg.get("active_mode", "V1")
    return cfg.get("modes", {}).get(selected_mode, {})


def get_mode_profile(cfg=None, mode=None):
    selected_mode = mode or (cfg.get("active_mode", "V1") if cfg else "V1")
    profile = dict(MODE_PROFILES.get(selected_mode, MODE_PROFILES["V1"]))
    mode_cfg = get_mode_cfg(cfg, selected_mode) if cfg else {}
    profile["tp1_pct"] = mode_cfg.get("tp1_pct", profile["default_tp1_pct"])
    profile["force_close_at_end"] = mode_cfg.get("force_close_at_end", profile["default_force_close"])
    profile["bear_boost_pct"] = mode_cfg.get("bear_boost_pct", profile["default_bear_boost_pct"])
    profile["delivery_pref"] = mode_cfg.get("delivery_pref")
    profile["holding_mode"] = mode_cfg.get("holding_mode")
    return profile


def infer_mid_trend(m15):
    if m15 is None or m15.empty or "ema_filter" not in m15.columns:
        return None
    return "BULL" if m15.iloc[-1]["Close"] > m15.iloc[-1]["ema_filter"] else "BEAR"


def resolve_entry_side(row, score, price_mtx, score_thresh, mid_trend=None, require_mid_trend=False):
    if row.get("sqz_on", True):
        return None
    if score >= score_thresh and price_mtx >= row["vwap"]:
        if require_mid_trend and mid_trend != "BULL":
            return None
        return "C"
    if score <= -score_thresh and price_mtx <= row["vwap"]:
        if require_mid_trend and mid_trend != "BEAR":
            return None
        return "P"
    return None
