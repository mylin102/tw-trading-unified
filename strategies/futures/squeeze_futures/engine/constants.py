POINT_VALUE_BY_TICKER = {
    "TMF": 10,
    "MTX": 50,
}


def get_point_value(ticker: str, default: int = 10) -> int:
    return POINT_VALUE_BY_TICKER.get(ticker, default)
