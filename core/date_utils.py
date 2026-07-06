import pandas as pd
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Union

# ── Calendar Library ──
try:
    import pandas_market_calendars as mcal
    _TW_CAL = mcal.get_calendar('XTAI')
except ImportError:
    _TW_CAL = None

# ── Holiday Cache (Legacy/Fallback) ──
HOLIDAYS_PATH = Path(__file__).parent.parent / "config" / "holidays.json"
CUSTOM_HOLIDAYS_PATH = Path(__file__).parent.parent / "config" / "holidays_custom.json"

def fetch_holidays(api=None):
    """
    Fetch holidays from Shioaji API and cache locally.
    Also merges with config/holidays_custom.json if it exists.
    """
    h_set = set()
    if api:
        try:
            holidays = []
            if hasattr(api, 'get_holidays'):
                holidays = api.get_holidays()
            
            h_list = [h if isinstance(h, str) else h.strftime("%Y-%m-%d") for h in holidays]
            if h_list:
                HOLIDAYS_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(HOLIDAYS_PATH, "w", encoding="utf-8") as f:
                    json.dump(h_list, f)
                h_set.update(h_list)
        except Exception as e:
            print(f"⚠️ Failed to fetch holidays from API: {e}")
    
    if HOLIDAYS_PATH.exists():
        try:
            with open(HOLIDAYS_PATH, "r", encoding="utf-8") as f:
                h_set.update(json.load(f))
        except:
            pass
            
    # 加載使用者手動維護的假日清單 (GSD enhancement)
    if CUSTOM_HOLIDAYS_PATH.exists():
        try:
            with open(CUSTOM_HOLIDAYS_PATH, "r", encoding="utf-8") as f:
                custom = json.load(f)
                if isinstance(custom, list):
                    h_set.update(custom)
        except Exception as e:
            print(f"⚠️ Error loading custom holidays: {e}")
            
    return h_set

def get_trading_day(dt: Union[datetime, pd.Timestamp, pd.DatetimeIndex, pd.Series], holidays=None) -> Union[date, pd.Series]:
    """
    獲取台灣期貨的「交易日 (Trading Day)」。
    台指期規則 (Taifex Standard)：
    - 一般交易時段 (日盤): 08:45 ~ 13:45 -> 歸屬當日
    - 盤後交易時段 (夜盤): 15:00 ~ 05:00 (次日) -> 歸屬「下一個交易日」
    
    GSD Rationale: Using .apply() for Series/Index to guarantee scalar handling and index alignment.
    """
    if dt is None:
        dt = datetime.now()

    # ── Handle Vectorized Inputs (Series/Index) ──
    if isinstance(dt, pd.Series):
        return pd.Series(
            [get_trading_day(x, holidays) for x in dt],
            index=dt.index,
            name=dt.name,
        )
    if isinstance(dt, pd.DatetimeIndex):
        return pd.Series(
            [get_trading_day(x, holidays) for x in dt],
            index=dt,
        )

    # ── Handle Scalar Input ──
    h_set = holidays or fetch_holidays()

    # 1. Convert to standardized datetime object
    if isinstance(dt, pd.Timestamp):
        d = dt.to_pydatetime()
    elif isinstance(dt, datetime):
        d = dt
    else:
        # Fallback for strings or other types (e.g. NaT)
        try:
            d = pd.to_datetime(dt).to_pydatetime()
        except:
            return dt

    # 2. V2: Professional Calendar Logic ──
    if _TW_CAL is not None:
        # 15:00 之後屬於下一個可能的交易日
        target = d + timedelta(days=1) if d.hour >= 15 else d
        
        while True:
            # GSD: schedule check
            schedule = _TW_CAL.schedule(start_date=target, end_date=target + timedelta(days=14))
            if schedule.empty:
                # V-Model fix: Explicitly return scalar date
                return pd.Timestamp(target).to_pydatetime().date()
            
            # V-Model fix: Explicitly convert to python date to avoid Cython method access issues
            candidate = schedule.index[0].to_pydatetime().date()
            if candidate.strftime("%Y-%m-%d") not in h_set:
                return candidate
            # 如果剛好是自定義假日，往後推一天再查
            target = datetime.combine(candidate + timedelta(days=1), datetime.min.time())

    # ── V1: Fallback Manual Logic ──
    if d.hour >= 15:
        target = d + timedelta(days=1)
    else:
        target = d
    while True:
        is_weekend = target.weekday() >= 5
        is_holiday = target.strftime("%Y-%m-%d") in h_set
        if not is_weekend and not is_holiday:
            break
        target += timedelta(days=1)
    # V-Model fix: Safe conversion to date
    return pd.Timestamp(target).to_pydatetime().date()

def get_trade_day(dt):
    """
    獲取交易記錄所屬的交易日。
    基於參考資料的 get_trading_date 邏輯改進：
    
    dt: datetime 物件
    回傳值: yyyymmdd 格式字串
    
    規則：
    - 情況 A：15:00 之後 (夜盤開始)
        - 週五晚跳週一 (+3天)
        - 其他日跳隔日 (+1天)
    - 情況 B：00:00 - 08:00 (夜盤後半段，包含5點收盤前後的緩衝)
        - 週六凌晨跳週一 (+2天)
        - 週一凌晨保持週一 (不調整)
        - 其他日保持當日 (日期已自動跨日)
    - 情況 C：一般日盤 (08:00-14:59)
        - 保持當日
    """
    if hasattr(dt, 'hour'):
        hour = dt.hour
        weekday = dt.weekday()  # 0=Mon, 4=Fri, 5=Sat
    else:
        try:
            dt = pd.to_datetime(dt)
            hour = dt.hour
            weekday = dt.weekday()
        except:
            return dt
    
    # 情況 A：15:00 之後 (夜盤開始)
    if hour >= 15:
        if weekday == 4:  # 週五晚跳週一
            target_date = dt + pd.Timedelta(days=3)
        else:
            target_date = dt + pd.Timedelta(days=1)
    
    # 情況 B：00:00 - 08:00 (夜盤後半段，包含5點收盤前後的緩衝)
    elif hour < 8:
        if weekday == 5:  # 週六凌晨跳週一
            target_date = dt + pd.Timedelta(days=2)
        elif weekday == 0 and hour < 8:  # 週一凌晨(極少見但預留)
            target_date = dt
        else:
            # 平日凌晨，日期已自動跨日，但交易記錄屬於前一日
            # 例如：週四凌晨的交易屬於週三的夜盤
            target_date = dt - pd.Timedelta(days=1)
    
    # 情況 C：一般日盤 (08:00-14:59)
    else:
        target_date = dt
    
    return target_date.date()

def get_session(dt: Union[datetime, pd.Timestamp]):
    # GSD: Absolute type safety check
    if hasattr(dt, "dt"): # Series
        # Vectorized check via apply if it's a Series to guarantee scalar logic parity
        return dt.apply(get_session)
    
    if not hasattr(dt, "hour"):
        try:
            dt = pd.to_datetime(dt)
        except:
            return 1
            
    hour = dt.hour
    
    # 依據測試期待：08:00 ~ 14:59 為日盤
    if 8 <= hour < 15:
        return 1
    # 其餘為夜盤
    return 2

def is_night_session(dt: Union[datetime, pd.Timestamp, pd.DatetimeIndex, pd.Series]) -> Union[bool, pd.Series]:
    if isinstance(dt, pd.Series):
        return dt.apply(lambda x: get_session(x) == 2)
    if isinstance(dt, pd.DatetimeIndex):
        return pd.Series([get_session(x) == 2 for x in dt], index=dt)
    return get_session(dt) == 2

def is_day_session(dt: Union[datetime, pd.Timestamp, pd.DatetimeIndex, pd.Series]) -> Union[bool, pd.Series]:
    if isinstance(dt, pd.Series):
        return dt.apply(lambda x: get_session(x) == 1)
    if isinstance(dt, pd.DatetimeIndex):
        return pd.Series([get_session(x) == 1 for x in dt], index=dt)
    return get_session(dt) == 1


def get_taifex_futures_hhmm(dt: Union[datetime, pd.Timestamp, str, None] = None) -> int:
    """Return HHMM trading clock for TAIFEX session gates.

    This helper intentionally represents the *current trading clock* rather than the
    timestamp of the latest completed bar. Session-boundary entry gates should use
    wall-clock semantics so a stale 14:55 bar does not keep 15:00 night session closed.
    """
    if dt is None:
        dt = datetime.now()
    if not hasattr(dt, "strftime"):
        dt = pd.to_datetime(dt)
    return int(dt.strftime("%H%M"))


def is_taifex_futures_market_open(dt: Union[datetime, pd.Timestamp, str, None] = None) -> bool:
    """Return whether TAIFEX futures market is open for entry decisions.
    
    Rules:
    - Day session (T 08:45-13:45): Open if T is a business day.
    - Night session (T 15:00 - T+1 05:00): Open if T is a business day AND the next trading day is a business day.
    """
    if dt is None:
        dt = datetime.now()
    if isinstance(dt, str):
        dt = pd.to_datetime(dt)
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
        
    hhmm = int(dt.strftime("%H%M"))
    weekday = dt.weekday() # 0=Mon, 6=Sun
    date_str = dt.strftime("%Y-%m-%d")
    h_set = fetch_holidays()
    _weekday_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][weekday]
    
    # 1. Time-only check
    is_day_time = (845 <= hhmm <= 1345)
    is_night_time = (hhmm >= 1500) or (hhmm < 500)
    
    if not (is_day_time or is_night_time):
        import logging
        logging.getLogger("regime").warning(
            "[MARKET_OPEN_TRACE] now=%s weekday=%s(%s) hhmm=%s "
            "date_str=%s is_open=False reason=NOT_TRADING_HOURS",
            dt, weekday, _weekday_name, hhmm, date_str,
        )
        return False
        
    # 2. Weekend/Holiday check
    if is_day_time:
        if weekday >= 5 or date_str in h_set:
            import logging
            logging.getLogger("regime").warning(
                "[MARKET_OPEN_TRACE] now=%s weekday=%s(%s) hhmm=%s "
                "date_str=%s is_open=False reason=DAY_WEEKEND_OR_HOLIDAY "
                "weekday_ge5=%s in_h_set=%s",
                dt, weekday, _weekday_name, hhmm, date_str,
                weekday >= 5, date_str in h_set,
            )
            return False
        return True
        
    if is_night_time:
        # Night session starting TODAY at 15:00
        if hhmm >= 1500:
            if weekday >= 5 or date_str in h_set:
                import logging
                logging.getLogger("regime").warning(
                    "[MARKET_OPEN_TRACE] now=%s weekday=%s(%s) hhmm=%s "
                    "date_str=%s is_open=False reason=NIGHT_WEEKEND_OR_HOLIDAY",
                    dt, weekday, _weekday_name, hhmm, date_str,
                )
                return False
            # Also check if NEXT trading day is a business day
            # If today is a business day but tomorrow is a holiday, there is NO night session
            t_day = get_trading_day(dt)
            if t_day.strftime("%Y-%m-%d") in h_set or t_day.weekday() >= 5:
                import logging
                logging.getLogger("regime").warning(
                    "[MARKET_OPEN_TRACE] now=%s weekday=%s(%s) hhmm=%s "
                    "date_str=%s is_open=False reason=NIGHT_TRADING_DAY_WEEKEND_OR_HOLIDAY "
                    "t_day=%s in_h_set=%s t_day_wday_ge5=%s",
                    dt, weekday, _weekday_name, hhmm, date_str,
                    t_day.strftime("%Y-%m-%d"), t_day.strftime("%Y-%m-%d") in h_set, t_day.weekday() >= 5,
                )
                return False
            return True
        else:
            # Night session that started YESTERDAY (00:00-05:00)
            # Trading day is yesterday, NOT today
            session_date = dt - timedelta(days=1)
            if session_date.weekday() >= 5 or session_date.strftime("%Y-%m-%d") in h_set:
                import logging
                logging.getLogger("regime").warning(
                    "[MARKET_OPEN_TRACE] now=%s weekday=%s(%s) hhmm=%s "
                    "date_str=%s is_open=False reason=MIDNIGHT_WEEKEND_OR_HOLIDAY "
                    "session_date=%s",
                    dt, weekday, _weekday_name, hhmm, date_str,
                    session_date.strftime("%Y-%m-%d"),
                )
                return False
            return True

    import logging
    logging.getLogger("regime").warning(
        "[MARKET_OPEN_TRACE] now=%s weekday=%s(%s) hhmm=%s "
        "date_str=%s is_open=False reason=FALLTHROUGH",
        dt, weekday, _weekday_name, hhmm, date_str,
    )
    return False


def get_taifex_futures_session_type(dt: Union[datetime, pd.Timestamp, str, None] = None) -> str:
    """Return TAIFEX futures session type using wall-clock semantics."""
    hhmm = get_taifex_futures_hhmm(dt)
    return "night" if (hhmm >= 1500 or hhmm < 500) else "day"

def get_session_date_str(dt=None):
    """
    獲取交易會話日期字串 (YYYYMMDD)。
    """
    if dt is None:
        dt = datetime.now()
    t_day = get_trading_day(dt)
    
    # Handle both scalar and Series returns
    if isinstance(t_day, pd.Series):
        # date objects in Series don't have .dt accessor, use apply
        return t_day.apply(lambda x: x.strftime("%Y%m%d") if hasattr(x, "strftime") else str(x))
    
    return t_day.strftime("%Y%m%d")


def parse_csv_last_timestamp(csv_path) -> pd.Timestamp:
    """Read the last valid timestamp from an indicator CSV file.

    Uses pandas to correctly identify the timestamp column regardless
    of column position, unlike subprocess tail+split(',')[0] which
    assumes timestamp is always the first column.

    Returns pd.NaT if the CSV has no valid timestamp.
    """
    try:
        df = pd.read_csv(csv_path)
        if "timestamp" not in df.columns:
            return pd.NaT
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"])
        if df.empty:
            return pd.NaT
        return df["timestamp"].iloc[-1]
    except Exception:
        return pd.NaT
