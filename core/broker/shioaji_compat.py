"""
Shioaji Version Compatibility Layer (v1.3.3 <-> v1.5.10)
Handles breaking changes in attribute naming and error imports.
"""
from __future__ import annotations

import logging
import time
import re
from typing import Any, Optional, List, Callable
import pandas as pd
import shioaji as sj

logger = logging.getLogger(__name__)

def get_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Robustly fetch attributes from an object by trying multiple names.
    Supports both attribute access and dict-style key access.
    """
    for name in names:
        # Attribute access
        if hasattr(obj, name):
            return getattr(obj, name)
        # Dict access
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        # Dict access (case-insensitive fallback)
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() == name.lower():
                    return v
    return default

def kbars_to_dataframe(kbars: Any) -> pd.DataFrame:
    """
    Robustly converts Shioaji KBars object to a standardized DataFrame.
    Supports both 1.3.3 (lowercase) and 1.5.10 (Uppercase) attribute names.
    """
    if kbars is None:
        return pd.DataFrame()
        
    if isinstance(kbars, pd.DataFrame):
        return kbars

    data = {
        "ts": get_attr(kbars, "Timestamp", "ts"),
        "Open": get_attr(kbars, "Open", "open"),
        "High": get_attr(kbars, "High", "high"),
        "Low": get_attr(kbars, "Low", "low"),
        "Close": get_attr(kbars, "Close", "close"),
        "Volume": get_attr(kbars, "Volume", "volume"),
        "Amount": get_attr(kbars, "Amount", "amount")
    }
    
    # Filter out None values and check if we have any data
    data = {k: v for k, v in data.items() if v is not None}
    if not data:
        return pd.DataFrame()
        
    try:
        df = pd.DataFrame(data)
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"])
            df.set_index("ts", inplace=True)
            df.sort_index(inplace=True)
            
        # [Rule 11 Fix] Explicitly cast price columns to float to avoid Decimal vs float math errors
        for col in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
                
        return df
    except Exception as e:
        logger.error(f"[Compat] Failed to convert kbars to DataFrame: {e}")
        return pd.DataFrame()

# ── Error Type Resolution ──

try:
    # 1. Try modern rshioaji (1.5.10) top-level re-export
    SjTimeoutError = getattr(sj, "ShioajiTimeoutError", None)
    if SjTimeoutError is None:
        # 2. Try legacy shioaji.error location
        from shioaji.error import TimeoutError as _SjTimeoutError
        SjTimeoutError = _SjTimeoutError
except (ImportError, AttributeError):
    # 3. Fallback to built-in
    SjTimeoutError = TimeoutError

def safe_login(api: sj.Shioaji, api_key: str, secret_key: str, **kwargs) -> Any:
    """
    Safely logins using parameters compatible with the installed version.
    Uses fallback for versions that don't support contracts_timeout.
    """
    try:
        # [rshioaji 1.5.9+] login with contracts_timeout returns True if contracts loaded
        return api.login(
            api_key=api_key,
            secret_key=secret_key,
            contracts_timeout=kwargs.get("contracts_timeout", 10000),
            **kwargs
        )
    except TypeError:
        # Fallback for 1.3.3
        kwargs.pop("contracts_timeout", None)
        return api.login(api_key=api_key, secret_key=secret_key, **kwargs)

def wait_for_contracts(api: sj.Shioaji, category: str = "Futures", symbol: str = "MXF", timeout: int = 30):
    """Wait for specific contracts to be available in the local cache."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            cat_obj = getattr(api.Contracts, category, None)
            if cat_obj:
                # [rshioaji 1.5.10+] Avoid iteration/list/int-indexing due to C++ binding bug.
                if symbol in repr(cat_obj):
                    return True
                # legacy check
                if hasattr(cat_obj, symbol):
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False

def get_contracts_list(api: sj.Shioaji, category: str, symbol: str) -> List[Any]:
    """
    Robustly retrieves a list of contract objects for a category and symbol.
    Workaround for rshioaji 1.5.10 C++ binding bug where list() and iteration crash.
    """
    try:
        # Get category container (e.g., api.Contracts.Futures)
        cat_container = getattr(api.Contracts, category, None)
        if not cat_container:
            return []
            
        # Get symbol group (e.g., api.Contracts.Futures.MXF)
        group = getattr(cat_container, symbol, None)
        if not group:
            return []
            
        # 1. Try direct list conversion (Preferred for 1.3.3, but crashes on 1.5.10)
        # We try it ONLY if we are sure it's not the bugged version, or inside a try block
        try:
            # We filter for objects that have 'code' to ensure they are real contracts
            res = [c for c in list(group) if hasattr(c, "code")]
            if res:
                return res
        except Exception:
            pass
            
        # 2. [rshioaji 1.5.10+ Workaround] Parse repr() to extract codes
        r = repr(group)
        # Match all patterns that look like contract codes inside parentheses or commas
        # Example: MXF(MXFE6, MXFG6) or TXO(TXO20260541400C, ...)
        codes = re.findall(r"([A-Z0-9/]{3,})", r)
        
        contracts = []
        for code in codes:
            # Skip the group name itself
            if code == symbol or len(code) < 5:
                continue
            try:
                # Bracket access with string code WORKS in 1.5.10
                c = group[code]
                if c is not None and hasattr(c, "code"):
                    contracts.append(c)
            except Exception:
                continue
        return contracts
    except Exception as e:
        logger.error(f"[Compat] Failed to get contracts list for {category}/{symbol}: {e}")
        return []

def fetch_all_contracts(api: sj.Shioaji, timeout: int = 300):
    """Aggressively ensure contracts are available in cache (5 min timeout)."""
    from core.shioaji_session import fetch_contracts
    
    # 2026-06-26 Gemini CLI: Load the configured ticker dynamically to avoid hardcoded 'MXF' check (Rule 11)
    from pathlib import Path
    import yaml
    
    ticker = "MXF"
    try:
        # 2026-07-01 Gemini CLI: Corrected resolution path to point to root config/ instead of core/config/
        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "futures.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                if cfg and "ticker" in cfg:
                    ticker = cfg["ticker"]
    except Exception:
        pass
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Check if Futures and Options categories are at least visible in dir()
            # and contain actual contract data
            cats = dir(api.Contracts)
            if "Futures" in cats and "Options" in cats:
                # 2026-07-01 Gemini CLI: Swig wrappers do not dynamically expose contract keys in dir(), use hasattr/repr check
                if hasattr(api.Contracts.Futures, ticker) or ticker in repr(api.Contracts.Futures):
                    return True
        except Exception:
            pass
            
        # Try/Retry fetching
        fetch_contracts(api)
        
        # If not successful yet, wait a bit before next check/retry
        time.sleep(10)
    return False

def is_rust_version() -> bool:
    """Returns True if the current shioaji version is the Rust-based 1.5+ version."""
    try:
        ver = sj.__version__
        major, minor = map(int, ver.split(".")[:2])
        return major > 1 or (major == 1 and minor >= 5)
    except Exception:
        return False
def set_tick_callback(api: sj.Shioaji, callback: Callable):
    """Register tick callback for the installed shioaji version.
    v1.3.3: use decorator pattern
    rshioaji: api.set_on_tick_fop_v1_callback(callback)
    """
    if is_rust_version():
        api.set_on_tick_fop_v1_callback(callback)
    else:
        # v1.3.3: decorator pattern with bind=False
        api.on_tick_fop_v1(bind=False)(callback)


def set_bidask_callback(api: sj.Shioaji, callback: Callable):
    """Register bidask callback for the installed shioaji version."""
    if is_rust_version():
        api.set_on_bidask_fop_v1_callback(callback)
    else:
        api.on_bidask_fop_v1(bind=False)(callback)


def clear_tick_callback(api: sj.Shioaji):
    """Clear tick callback."""
    if is_rust_version():
        api.set_on_tick_fop_v1_callback(lambda *args: None)
    else:
        api.set_on_tick_fop_v1_callback(None)


def clear_bidask_callback(api: sj.Shioaji):
    """Clear bidask callback."""
    if is_rust_version():
        api.set_on_bidask_fop_v1_callback(lambda *args: None)
    else:
        api.set_on_bidask_fop_v1_callback(None)

def safe_subscribe(api: sj.Shioaji, contract: Any, quote_type: str = 'tick'):
    """Subscribe to market data using version-appropriate method."""
    if is_rust_version():
        # rshioaji 1.5.x
        if quote_type.lower() == 'tick':
            # 💡 Gemini CLI: Use sj.QuoteType.Tick enum instead of string 'tick' for rshioaji 1.5+ compatibility
            api.subscribe(contract, quote_type=sj.QuoteType.Tick, version=sj.QuoteVersion.v1)
        else:
            api.subscribe(contract, quote_type=sj.QuoteType.BidAsk, version=sj.QuoteVersion.v1)
    else:
        # legacy shioaji 1.3.3
        if quote_type.lower() == 'tick':
            api.quote.subscribe(contract, quote_type='tick')
        else:
            api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)
