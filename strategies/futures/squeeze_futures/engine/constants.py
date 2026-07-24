POINT_VALUE_BY_TICKER = {
    "TMF": 10,
    "MTX": 50,
}


def get_point_value(ticker: str, default: int = 10) -> int:
    return POINT_VALUE_BY_TICKER.get(ticker, default)


def get_product_spec(ticker: str):
    """Get full product spec including fees and margin.

    Tries core.product_spec first, falls back to builtin constants.
    """
    try:
        from core.product_spec import get_builtin_product
        return get_builtin_product(ticker)
    except Exception:
        return None


def get_margin_per_lot(ticker: str, default: int = 46000) -> int:
    """Get margin per lot for a ticker."""
    spec = get_product_spec(ticker)
    if spec is not None:
        return int(spec.margin_per_lot)
    return default


def get_broker_fee(ticker: str, default: int = 22) -> int:
    """Get broker fee per side for a ticker."""
    spec = get_product_spec(ticker)
    if spec is not None:
        return int(spec.broker_fee_per_side)
    return default
