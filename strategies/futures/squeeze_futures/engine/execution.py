from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionModel:
    order_type: str = "market"
    market_slippage_pts: float = 0.0
    limit_offset_pts: float = 0.0
    range_protection_pts: float = 2.0
    tick_size: float = 1.0


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(price / tick_size) * tick_size


def simulate_order_fill(signal: str, reference_price: float, bar, model: ExecutionModel) -> float | None:
    side = "buy" if signal == "BUY" else "sell"
    order_type = model.order_type.lower()
    high = float(bar["High"])
    low = float(bar["Low"])
    close = float(bar["Close"])
    tick = model.tick_size

    if order_type == "market":
        fill = reference_price + model.market_slippage_pts if side == "buy" else reference_price - model.market_slippage_pts
        return _round_to_tick(fill, tick)

    if order_type == "limit":
        limit_price = reference_price - model.limit_offset_pts if side == "buy" else reference_price + model.limit_offset_pts
        if low <= limit_price <= high:
            return _round_to_tick(limit_price, tick)
        return None

    if order_type == "range_market":
        market_fill = reference_price + model.market_slippage_pts if side == "buy" else reference_price - model.market_slippage_pts
        protection = reference_price + model.range_protection_pts if side == "buy" else reference_price - model.range_protection_pts
        if side == "buy":
            if market_fill > protection:
                return None
            fill = min(max(market_fill, low), max(close, low))
        else:
            if market_fill < protection:
                return None
            fill = max(min(market_fill, high), min(close, high))
        return _round_to_tick(fill, tick)

    raise ValueError(f"Unsupported order_type: {model.order_type}")


def build_execution_model(config: dict | None) -> ExecutionModel:
    config = config or {}
    return ExecutionModel(
        order_type=config.get("order_type", "market"),
        market_slippage_pts=float(config.get("market_slippage_pts", 0.0)),
        limit_offset_pts=float(config.get("limit_offset_pts", 0.0)),
        range_protection_pts=float(config.get("range_protection_pts", 2.0)),
        tick_size=float(config.get("tick_size", 1.0)),
    )
