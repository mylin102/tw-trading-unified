import pandas as pd
from datetime import datetime, date, timedelta
from typing import Union

def get_trading_day(dt: Union[datetime, pd.Timestamp, pd.DatetimeIndex]) -> Union[date, pd.Series]:
    """
    獲取台灣期貨的「交易日 (Trading Day)」。
    台指期規則：
    - 一般交易時段 (日盤): 08:45 ~ 13:45
    - 盤後交易時段 (夜盤): 15:00 ~ 05:00 (次日)
    
    演算法：將時間加上 9 小時，其所在的日曆日即為交易日。
    - 15:00 + 9h = 00:00 (次日) -> 歸屬次日
    - 05:00 + 9h = 14:00 (當日) -> 歸屬當日
    - 08:45 + 9h = 17:45 (當日) -> 歸屬當日
    - 13:45 + 9h = 22:45 (當日) -> 歸屬當日
    
    支援單一 datetime 或 Pandas DatetimeIndex 的向量化運算。
    """
    if isinstance(dt, pd.DatetimeIndex) or isinstance(dt, pd.Series):
        return (dt + pd.Timedelta(hours=9)).date
    else:
        # 單一 datetime 或 Timestamp
        if isinstance(dt, pd.Timestamp):
            dt = dt.to_pydatetime()
        return (dt + timedelta(hours=9)).date()

def is_night_session(dt: Union[datetime, pd.Timestamp, pd.DatetimeIndex]) -> Union[bool, pd.Series]:
    """
    判斷是否為夜盤時段 (15:00 ~ 05:00)。
    注意：此處判斷是以日曆時間的「小時」為準。
    """
    if isinstance(dt, pd.DatetimeIndex) or isinstance(dt, pd.Series):
        hour = dt.hour
        # 15:00 (含) 到 23:59，或 00:00 到 05:00 (含，因為可能會有 05:00 的 K 棒收盤)
        return (hour >= 15) | (hour <= 5)
    else:
        hour = dt.hour
        return (hour >= 15) or (hour <= 5)

def is_day_session(dt: Union[datetime, pd.Timestamp, pd.DatetimeIndex]) -> Union[bool, pd.Series]:
    """
    判斷是否為日盤時段 (08:45 ~ 13:45)。
    """
    if isinstance(dt, pd.DatetimeIndex) or isinstance(dt, pd.Series):
        hour = dt.hour
        return (hour >= 8) & (hour <= 14)
    else:
        hour = dt.hour
        return (hour >= 8) and (hour <= 14)


def get_session_date_str(dt=None):
    """
    Get the session date string in YYYYMMDD format.

    Taiwan futures cross-day rule:
    - 00:00-05:00 belongs to previous calendar day's session
    - 05:00+ belongs to current calendar day

    Both main.py (writer) and dashboard.py (reader) MUST call this
    to guarantee filename alignment.

    Returns:
        str: YYYYMMDD date string for the current session.
    """
    if dt is None:
        from datetime import datetime as _dt
        dt = _dt.now()
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if isinstance(dt, datetime):
        if dt.hour < 5:
            dt = dt - timedelta(days=1)
        return dt.strftime("%Y%m%d")
    # Fallback: use current time
    from datetime import datetime as _dt
    now = _dt.now()
    if now.hour < 5:
        now = now - timedelta(days=1)
    return now.strftime("%Y%m%d")
