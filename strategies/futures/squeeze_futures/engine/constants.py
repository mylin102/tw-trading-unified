POINT_VALUE_BY_TICKER = {
    "TMF": 10,
    "MXF": 50,  # 2026-05-26 Gemini CLI: Added MXF point value
    "MXFR1": 50,
    "MTX": 50,
    "TXFR1": 200,
}


def get_point_value(ticker: str, default: int = 10) -> int:
    return POINT_VALUE_BY_TICKER.get(ticker, default)
