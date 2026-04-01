DEFAULT_EXECUTION_CFG = {
    "aggressive_ticks": 0,
    "tick_size": 1.0,
    "max_spread_pct": 0.05,
    "simulated_spread_pct": 0.02,
}


def get_execution_cfg(cfg=None):
    execution_cfg = dict(DEFAULT_EXECUTION_CFG)
    if cfg:
        execution_cfg.update(cfg.get("execution", {}))
    return execution_cfg


def synthetic_bid_ask(mid_price, execution_cfg=None):
    execution_cfg = execution_cfg or dict(DEFAULT_EXECUTION_CFG)
    spread_pct = execution_cfg.get("simulated_spread_pct", DEFAULT_EXECUTION_CFG["simulated_spread_pct"])
    half_spread = max(mid_price * spread_pct / 2.0, execution_cfg.get("tick_size", 1.0) / 2.0)
    bid = max(0.0, mid_price - half_spread)
    ask = mid_price + half_spread
    return bid, ask


def should_reject_for_spread(mid_price, execution_cfg=None):
    execution_cfg = execution_cfg or dict(DEFAULT_EXECUTION_CFG)
    bid, ask = synthetic_bid_ask(mid_price, execution_cfg)
    spread_pct = ((ask - bid) / mid_price) if mid_price > 0 else float("inf")
    return spread_pct > execution_cfg.get("max_spread_pct", DEFAULT_EXECUTION_CFG["max_spread_pct"]), spread_pct


def apply_slippage(mid_price, action, execution_cfg=None):
    execution_cfg = execution_cfg or dict(DEFAULT_EXECUTION_CFG)
    reject_trade, spread_pct = should_reject_for_spread(mid_price, execution_cfg)
    if reject_trade:
        return None, {"rejected": True, "spread_pct": spread_pct}

    bid, ask = synthetic_bid_ask(mid_price, execution_cfg)
    tick_size = execution_cfg.get("tick_size", DEFAULT_EXECUTION_CFG["tick_size"])
    aggressive_ticks = execution_cfg.get("aggressive_ticks", DEFAULT_EXECUTION_CFG["aggressive_ticks"])
    tick_adjustment = aggressive_ticks * tick_size

    if action == "buy":
        fill_price = ask + tick_adjustment
    elif action == "sell":
        fill_price = max(tick_size, bid - tick_adjustment)
    else:
        raise ValueError(f"Unsupported action: {action}")

    return fill_price, {"rejected": False, "spread_pct": spread_pct, "bid": bid, "ask": ask}
