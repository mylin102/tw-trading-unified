import os
import logging
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import Callable, Optional, Dict, Any
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
        if sj is None: return
        self.api = sj.Shioaji()

    def login(self, retries: int = 3, retry_delay: int = 10):
        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        cert_path = os.getenv("SHIOAJI_CERT_PATH")
        cert_password = os.getenv("SHIOAJI_CERT_PASSWORD")
        if not all([api_key, secret_key]): return False
        for attempt in range(1, retries + 1):
            try:
                self.api.login(api_key=api_key, secret_key=secret_key, fetch_contract=True)
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
        """
        訂閱市場數據（使用 callback 模式）
        
        Args:
            contract: Shioaji 合約物件
            callback: 回呼函數，接收 (contract, tick) 參數
        """
        if not self.is_logged_in: return False
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
        """取消訂閱市場數據"""
        if not self.is_logged_in: return False
        try:
            self.api.quote.unsubscribe(contract)
            return True
        except Exception as e:
            logger.error(f"Unsubscribe failed: {e}")
            return False

    def get_kline(self, ticker: str, interval: str = "5m"):
        """
        獲取 K 棒數據（polling 模式，向後相容）
        
        Args:
            ticker: 商品代號
            interval: 週期 (5m, 15m, 1h)
            
        Returns:
            DataFrame with OHLCV data
        """
        if not self.is_logged_in: return pd.DataFrame()
        try:
            contract = self.get_futures_contract(ticker)
            if not contract: return pd.DataFrame()
            start_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            kbars = self.api.kbars(contract, start=start_date)
            df = pd.DataFrame({**kbars})
            if df.empty: return df
            df.ts = pd.to_datetime(df.ts)
            df.set_index('ts', inplace=True)
            rule = INTERVAL_MAP.get(interval, interval)
            if rule != "1min":
                df = df.resample(rule, label="right", closed="left").agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum",
                })
            df = df.rename(columns={'Open':'Open','High':'High','Low':'Low','Close':'Close','Volume':'Volume'})
            return df.dropna(subset=["Open", "High", "Low", "Close"])
        except Exception: return pd.DataFrame()

    def start_kbar_callback(self, contract, interval: str, callback: Callable):
        """
        啟動 K 棒回呼（非同步接收 K 棒更新）
        
        Args:
            contract: Shioaji 合約物件
            interval: K 棒週期 (1min, 5min, etc.)
            callback: 回呼函數，接收 (contract, kbar) 參數
            
        Kbar 物件屬性:
            - ts: timestamp
            - Open, High, Low, Close: 價格
            - Volume: 成交量
            - amount: 成交金額
        """
        if not self.is_logged_in: return False
        try:
            # 訂閱 K 棒數據
            self.api.quote.subscribe(
                contract,
                quote_type=sj.constant.QuoteType.Quote,
                callback=callback
            )
            logger.info(f"Subscribed to {contract.code} kbar ({interval})")
            return True
        except Exception as e:
            logger.error(f"Kbar callback subscription failed: {e}")
            return False

    def get_available_margin(self):
        """查詢期貨帳戶可用保證金 (TWD)"""
        if not self.is_logged_in: return 0
        try:
            margins = self.api.get_account_margin()
            if margins:
                return float(margins[0].available_margin)
            return 0
        except Exception as e:
            logger.error(f"Failed to fetch margin: {e}")
            return 0

    def get_futures_contract(self, ticker: str):
        if not self.is_logged_in: return None
        try:
            if ticker == 'TXFR1': return self.api.Contracts.Futures.TXF.TXFR1
            if ticker == 'MXFR1': return self.api.Contracts.Futures.MXF.MXFR1
            if ticker == 'TMF':
                contracts = [c for c in self.api.Contracts.Futures.TMF if c.delivery_month]
                return sorted(contracts, key=lambda x: x.delivery_month)[0]
            return None
        except Exception: return None

    def place_order(self, contract, action: str, quantity: int, price: float = 0):
        if not self.is_logged_in: return None
        try:
            action_value = action
            if sj is not None and isinstance(action, str):
                normalized = action.strip().lower()
                if normalized == "buy":
                    action_value = sj.constant.Action.Buy
                elif normalized == "sell":
                    action_value = sj.constant.Action.Sell
            order = self.api.Order(
                action=action_value, price=price, quantity=quantity,
                order_type=sj.constant.OrderType.MTL,
                price_type=sj.constant.FuturesPriceType.MKT if price == 0 else sj.constant.FuturesPriceType.LMT,
                market_type=sj.constant.FuturesMarketType.Night if datetime.now().hour >= 15 or datetime.now().hour < 5 else sj.constant.FuturesMarketType.Common,
                account=self.api.futopt_account,
            )
            trade = self.api.place_order(contract, order)
            return trade
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None

    def update_order(self, trade, price: float, quantity: int = 1):
        """改單（移動停損用，不刪單重下以保留排隊順位）"""
        if not self.is_logged_in: return False
        try:
            self.api.update_order(trade, price=price, qty=quantity)
            return True
        except Exception as e:
            logger.error(f"Update order failed: {e}")
            return False

    def cancel_order(self, trade):
        """撤單（停利成交後撤銷場上停損單）"""
        if not self.is_logged_in: return False
        try:
            self.api.cancel_order(trade)
            return True
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            return False

    def logout(self):
        """登出並取消所有訂閱"""
        # 取消所有訂閱
        for contract in list(self._kbar_callbacks.keys()):
            self.unsubscribe_market_data(contract)
        self._kbar_callbacks.clear()
        self._tick_callbacks.clear()
        
        if self.api:
            self.api.logout()
            self.is_logged_in = False
