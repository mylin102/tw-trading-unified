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

def get_session(dt: Union[datetime, pd.Timestamp]):
    """
    判斷盤別：1=日盤 (08:00-14:59), 2=夜盤 (15:00-07:59)
    GSD: Only supports scalar input.
    """
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
