import os
import logging
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import Callable, Dict
from collections import deque

try:
    import shioaji as sj
except ImportError:
    sj = None

load_dotenv()
logger = logging.getLogger(__name__)

INTERVAL_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "1h",
    "1h": "1h",
}

class ShioajiClient:
    def __init__(self):
        self.api = None
        self.is_logged_in = False
        self._tick_callbacks = {}  # 儲存 tick 回呼函數
        self._kbar_callbacks = {}  # 儲存 K 棒回呼函數
        self._latest_kbars: Dict[str, deque] = {}  # 儲存最新 K 棒數據
        if sj is None:
            return
        self.api = sj.Shioaji()

    def login(self, retries: int = 3, retry_delay: int = 10):
        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        cert_path = os.getenv("SHIOAJI_CERT_PATH")
        cert_password = os.getenv("SHIOAJI_CERT_PASSWORD")
        if not all([api_key, secret_key]):
            return False
        
        from core.broker.shioaji_compat import safe_login
        for attempt in range(1, retries + 1):
            try:
                safe_login(self.api, api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
                if cert_path and os.path.exists(cert_path):
                    self.api.activate_ca(ca_path=cert_path, ca_passwd=cert_password, person_id=api_key)
                self.is_logged_in = True
                return True
            except Exception as e:
                logger.error(f"Shioaji login failed (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    import time
                    time.sleep(retry_delay)
        return False

    def subscribe_market_data(self, contract, callback: Callable):
        if not self.is_logged_in:
            return False
        try:
            self.api.quote.subscribe(
                contract,
                quote_type=sj.constant.QuoteType.Tick,
                callback=callback
            )
            return True
        except Exception as e:
            logger.error(f"Subscribe failed: {e}")
            return False

    def unsubscribe_market_data(self, contract):
        if not self.is_logged_in:
            return False
        try:
            self.api.quote.unsubscribe(contract)
            return True
        except Exception as e:
            logger.error(f"Unsubscribe failed: {e}")
            return False

    def get_kline(self, ticker: str, interval: str = "5m"):
        if not self.is_logged_in:
            return pd.DataFrame()
        try:
            contract = self.get_futures_contract(ticker)
            if not contract:
                return pd.DataFrame()
            
            # [gstack] 延長追溯至 7 天
            start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            kbars = self.api.kbars(contract, start=start_date)
            
            from core.broker.shioaji_compat import kbars_to_dataframe
            df = kbars_to_dataframe(kbars)
            
            if df.empty:
                return df
                
            rule = INTERVAL_MAP.get(interval, interval)
            if rule != "1min":
                df = df.resample(rule, label="right", closed="left").agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum",
                })
            return df.dropna(subset=["Open", "High", "Low", "Close"])
        except Exception as e:
            logger.error(f"[kbars] Error: {e}")
            return pd.DataFrame()

    def start_kbar_callback(self, contract, interval: str, callback: Callable):
        if not self.is_logged_in:
            return False
        try:
            self.api.quote.subscribe(
                contract,
                quote_type=sj.constant.QuoteType.Quote,
                callback=callback
            )
            return True
        except Exception as e:
            logger.error(f"Kbar callback subscription failed: {e}")
            return False

    def get_available_margin(self):
        if not self.is_logged_in:
            return 0
        try:
            margins = self.api.get_account_margin()
            if margins:
                return float(margins[0].available_margin)
            return 0
        except Exception as e:
            logger.error(f"Failed to fetch margin: {e}")
            return 0

    def _resolve_front_month_futures_contract(self, market_keys: tuple[str, ...], code_prefix: str):
        if self.api is None: return None
        futures = getattr(self.api.Contracts, "Futures", None)
        if futures is None: return None

        for key in market_keys:
            node = getattr(futures, key, None)
            if node is None: continue
            for attr in ("near_month", "current", "front"):
                contract = getattr(node, attr, None)
                if contract is not None and hasattr(contract, "code") and str(contract.code).startswith(code_prefix):
                    return contract
        return None

    def get_futures_contract(self, ticker: str):
        if not self.is_logged_in: return None
        try:
            # 2026-06-18 Gemini CLI: [Pure TMF Refactoring] Disabled TXF/MXF hardcoded fallbacks
            # if ticker in {'TX', 'TXF'}:
            #     return self._resolve_front_month_futures_contract(("TXF", "TX"), "TXF")
            # if ticker == 'TXFR1':
            #     return self.api.Contracts.Futures["TXF"]["TXFR1"]
            # if ticker == 'MXFR1':
            #     return self.api.Contracts.Futures["MXF"]["MXFR1"]
            
            if ticker in {'MXF', 'MX', 'TMF'}:
                # [rshioaji 1.5.10 Workaround] Use robust list helper to avoid C++ binding crash
                from core.broker.shioaji_compat import get_contracts_list
                mxf_list = get_contracts_list(self.api, "Futures", "MXF")

                if not mxf_list: return None
                now_str = datetime.now().strftime("%Y/%m/%d")
                valid = [c for c in mxf_list if c.delivery_date >= now_str]
                if valid:
                    return sorted(valid, key=lambda c: c.delivery_date)[0]
                return mxf_list[0]
                
            category = ticker[:3] if len(ticker) > 3 else ticker
            return self.api.Contracts.Futures[category][ticker]
        except Exception as e:
            logger.error(f"[shioaji_client] Get contract {ticker} error: {e}")
            return None

    def place_order(self, contract, action: str, quantity: int, price: float = 0):
        if not self.is_logged_in: return None
        try:
            action_value = sj.constant.Action.Buy if action.upper() in ("BUY", "LONG") else sj.constant.Action.Sell
            order = self.api.Order(
                action=action_value, price=price, quantity=quantity,
                order_type=sj.constant.OrderType.MTL,
                price_type=sj.constant.FuturesPriceType.MKP if price == 0 else sj.constant.FuturesPriceType.LMT,
                market_type=sj.constant.FuturesMarketType.Night if datetime.now().hour >= 15 or datetime.now().hour < 5 else sj.constant.FuturesMarketType.Common,
                account=self.api.futopt_account,
            )
            return self.api.place_order(contract, order)
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None

    def update_order(self, trade, price: float, quantity: int = 1):
        if not self.is_logged_in: return False
        try:
            self.api.update_order(trade, price=price, qty=quantity)
            return True
        except Exception as e:
            logger.error(f"Update order failed: {e}")
            return False

    def cancel_order(self, trade):
        if not self.is_logged_in: return False
        try:
            self.api.cancel_order(trade)
            return True
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            return False

    def refresh_status(self, account=None, trade=None):
        if not self.is_logged_in: return None
        try:
            if account: return self.api.update_status(account=account)
            if trade: return self.api.update_status(trade=trade)
            return self.api.update_status()
        except Exception: return None

    def list_trades(self, account=None):
        if not self.is_logged_in: return []
        try:
            return list(self.api.list_trades(account=account)) if account else list(self.api.list_trades())
        except Exception: return []

    def list_positions(self, account=None):
        if not self.is_logged_in: return []
        try:
            return list(self.api.list_positions(account=account)) if account else list(self.api.list_positions())
        except Exception: return []

    def logout(self):
        if self.api:
            try: self.api.logout()
            except Exception: pass
            self.is_logged_in = False
