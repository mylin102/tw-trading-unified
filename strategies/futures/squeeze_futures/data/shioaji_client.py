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


class AdapterOrderError(Exception):
    """Structured, stable-code adapter failure (P0 2026-08-08).

    Raised on order build/API failures so callers can write a durable,
    order-manager-visible failure reason — never an ambiguous None.
    """

    def __init__(self, code: str, context: dict):
        super().__init__(f"{code}: {context}")
        self.code = code
        self.context = context


class ShioajiClient:
    def __init__(self):
        self.api = None
        self.is_logged_in = False
        # [Step 8] execution-context gate reference — set by the monitor
        # on every mode transition (None = no certification -> fail-closed)
        self._execution_context = None
        self._tick_callbacks = {}  # 儲存 tick 回呼函數
        self._kbar_callbacks = {}  # 儲存 K 棒回呼函數
        self._latest_kbars: Dict[str, deque] = {}  # 儲存最新 K 棒數據
        if sj is None:
            return
        self.api = sj.Shioaji()

    def _gate_or_raise(self, method: str, order=None):
        # [S0] gateway authorization: direct adapter calls (no live
        # submission authorization) are rejected.
        _gw_registry = getattr(self, "_gateway_registry", None)
        if (_gw_registry is not None
                and method in ("place_order", "place_order_object")
                and not _gw_registry.verify_pending_submission(order)):
            raise AdapterOrderError(
                code="ADAPTER_GATEWAY_AUTHORIZATION_MISSING",
                context={"method": method})
        """[Step 8] execution-context gate: non-LIVE_READY or ctx=None
        makes ZERO broker calls and raises the canonical structured gate
        exception LiveOrderBlocked (typed reason); LIVE_READY permits the
        intended call. Fail-closed by default (None context = no live
        certification)."""
        from core.mode_transition import LiveOrderBlocked
        ctx = getattr(self, "_execution_context", None)
        if ctx is None:
            raise LiveOrderBlocked(
                f"{method}: NO_LIVE_CERTIFICATION")
        checker = getattr(ctx, "assert_order_allowed", None)
        if callable(checker):
            try:
                checker(order, method=method)
                return
            except LiveOrderBlocked as exc:
                raise LiveOrderBlocked(f"{method}: {exc.reason}") from exc
        if not ctx.is_live_ready():
            raise LiveOrderBlocked(
                f"{method}: LIVE_QUARANTINED "
                f"audit_reasons="
                f"{list(getattr(ctx, 'audit_reasons', ()) or ())}")

    def login(self, retries: int = 3, retry_delay: int = 10):
        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        cert_path = (os.getenv("SHIOAJI_CA_PATH")
                     or os.getenv("SHIOAJI_CERT_PATH"))
        cert_password = (os.getenv("SHIOAJI_CA_PASSWD")
                         or os.getenv("SHIOAJI_CERT_PASSWORD"))
        if not all([api_key, secret_key]):
            return False
        
        from core.broker.shioaji_compat import safe_login
        for attempt in range(1, retries + 1):
            try:
                safe_login(self.api, api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
                if cert_path and os.path.exists(cert_path):
                    from core.shioaji_session import _activate_futopt_ca
                    _activate_futopt_ca(self.api, cert_path, cert_password)
                self.is_logged_in = True
                return True
            except Exception:
                # SDK failures can contain certificate/account details.  The
                # caller receives only the fail-closed False result.
                logger.error(
                    "Shioaji login failed (attempt %s/%s): "
                    "SHIOAJI_LOGIN_FAILED", attempt, retries)
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
            # 2026-06-23 Gemini CLI: Restore TXF/MXF contract resolution fallbacks for alias support
            if ticker in {'TX', 'TXF'}:
                return self._resolve_front_month_futures_contract(("TXF", "TX"), "TXF")
            if ticker == 'TXFR1':
                return self.api.Contracts.Futures["TXF"]["TXFR1"]
            if ticker == 'MXFR1':
                return self.api.Contracts.Futures["MXF"]["MXFR1"]
            
            if ticker in {'MXF', 'MX', 'TMF'}:
                # [rshioaji 1.5.10 Workaround] Use robust list helper to avoid C++ binding crash
                from core.broker.shioaji_compat import get_contracts_list
                mxf_list = get_contracts_list(self.api, "Futures", "MXF")

                if not mxf_list: return None
                # 2026-07-24 Gemini CLI: Normalize delivery_date to datetime.date to prevent str vs datetime.date TypeError
                now_date = datetime.now().date()
                def _to_date(c):
                    d = getattr(c, "delivery_date", None)
                    if d is None: return now_date
                    if isinstance(d, str):
                        try:
                            return datetime.strptime(d.replace("-", "/"), "%Y/%m/%d").date()
                        except Exception:
                            return now_date
                    return d if hasattr(d, "year") else now_date

                valid = [c for c in mxf_list if _to_date(c) >= now_date]
                if valid:
                    return sorted(valid, key=_to_date)[0]
                return mxf_list[0]
                
            category = ticker[:3] if len(ticker) > 3 else ticker
            return self.api.Contracts.Futures[category][ticker]
        except Exception as e:
            logger.error(f"[shioaji_client] Get contract {ticker} error: {e}")
            return None

    def place_order(self, contract, action: str, quantity: int, price: float = 0):
        # P0 fix 2026-08-08 (verified on Mini shioaji 1.7.0):
        # OrderType=[FOK,IOC,ROD] — MTL does not exist; FuturesPriceType=
        # [LMT,MKP,MKT]; Order has NO market_type field.
        # TAIFEX order rules: Market-With-Protection (MKP) requires IOC or
        # FOK — ROD is for LIMIT orders only. MTS intent for a market order
        # is immediate execution with protection → MKP + IOC (default;
        # FOK is the explicit all-or-none alternative). price>0 (limit) →
        # LMT + ROD. octype is set EXPLICITLY to Auto (broker determines
        # New/Cover from position) — never rely on SDK defaults.
        # Non-deprecated sj.OrderType/sj.FuturesPriceType/sj.Action used.
        # Terminal failure/rejection semantics (fix-forward): a None API
        # return is ADAPTER_ORDER_NO_TRADE; a trade with terminal status
        # Failed/Rejected is ADAPTER_ORDER_REJECTED — zero silent failures.
        self._gate_or_raise("place_order")          # [Step 8] fail-closed
        return self._place_order_unchecked(contract, action, quantity, price)

    def _place_order_unchecked(self, contract, action: str, quantity: int, price: float = 0):
        """Broker call after an already-authorized route-specific gate."""
        if not self.is_logged_in:
            return None
        try:
            action_value = sj.Action.Buy if action.upper() in ("BUY", "LONG") \
                else sj.Action.Sell
            order = self.api.Order(
                action=action_value, price=price, quantity=quantity,
                order_type=sj.OrderType.IOC if price == 0 else sj.OrderType.ROD,
                price_type=sj.FuturesPriceType.MKP if price == 0
                else sj.FuturesPriceType.LMT,
                octype=sj.FuturesOCType.Auto,
                account=self.api.futopt_account,
            )
            trade = self.api.place_order(contract, order)
            if trade is None:
                raise AdapterOrderError(
                    code="ADAPTER_ORDER_NO_TRADE",
                    context={"method": "place_order",
                             "contract": getattr(contract, "code", None),
                             "action": action, "quantity": quantity,
                             "price": price,
                             "reason": "api returned no trade"})
            # never return a rejected/failed trade as success
            _status = getattr(getattr(trade, "status", None), "status", None)
            if _status is not None and str(_status) in ("Failed", "Rejected"):
                raise AdapterOrderError(
                    code="ADAPTER_ORDER_REJECTED",
                    context={"method": "place_order",
                             "contract": getattr(contract, "code", None),
                             "action": action, "quantity": quantity,
                             "price": price, "status": str(_status)})
            return trade
        except AdapterOrderError:
            raise                    # typed failures pass through unwrapped
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            raise AdapterOrderError(
                code="ADAPTER_ORDER_PLACE_FAILED",
                context={"method": "place_order",
                         "contract": getattr(contract, "code", None),
                         "action": action, "quantity": quantity,
                         "price": price, "error": str(e)}) from e

    def place_order_object(self, order):
        """Bridge the canonical OrderManager order object to Shioaji.

        OrderManager submits domain ``Order`` objects, while this adapter's
        broker boundary deliberately accepts contract/action/quantity.  Keep
        that conversion here so a live MTS path cannot accidentally call
        ``place_order(order)`` and fail after local intent creation.
        """
        self._gate_or_raise("place_order", order)
        symbol = getattr(order, "symbol", None)
        contract = self.get_contract(symbol) if isinstance(symbol, str) else None
        side = getattr(getattr(order, "side", None), "value",
                       getattr(order, "side", None))
        quantity = getattr(order, "quantity", None)
        if contract is None or not isinstance(side, str) or not isinstance(quantity, int) \
                or isinstance(quantity, bool) or quantity <= 0:
            raise AdapterOrderError(
                code="ADAPTER_ORDER_OBJECT_INVALID",
                context={"method": "place_order_object", "symbol": symbol,
                         "side": side, "quantity": quantity})
        return self._place_order_unchecked(
            contract, action=side, quantity=quantity,
            price=getattr(order, "price", 0) or 0,
        )

    def get_contract(self, symbol: str):
        """Resolve one exact futures code without guessing a near/far alias.

        ``get_futures_contract`` intentionally accepts product aliases such as
        ``TMF``.  The live order bridge instead receives broker codes (for
        example ``TMFH6``), so it must fail closed on a missing or ambiguous
        exact match, especially around a contract roll.
        """
        if self.api is None or not self.is_logged_in or not isinstance(symbol, str) or not symbol:
            return None
        try:
            from core.broker.shioaji_compat import get_contracts_list

            matches = [
                contract for contract in get_contracts_list(self.api, "Futures", symbol[:3])
                if str(getattr(contract, "code", "")) == symbol
            ]
            return matches[0] if len(matches) == 1 else None
        except Exception as exc:
            logger.error("[shioaji_client] exact contract resolve %s failed: %s", symbol, exc)
            return None

    def update_order(self, trade, price: float, quantity: int = 1):
        self._gate_or_raise("update_order")         # [Step 8] fail-closed
        if not self.is_logged_in:
            return False
        try:
            result = self.api.update_order(trade, price=price, qty=quantity)
            if result is None:
                raise AdapterOrderError(
                    code="ADAPTER_ORDER_NO_TRADE",
                    context={"method": "update_order", "price": price,
                             "quantity": quantity,
                             "reason": "api returned no result"})
            if result is False:
                # official API returns Trade — False is a silent failure
                raise AdapterOrderError(
                    code="ADAPTER_ORDER_UPDATE_FAILED",
                    context={"method": "update_order", "price": price,
                             "quantity": quantity,
                             "reason": "api returned False (not a Trade)"})
            return result
        except AdapterOrderError:
            raise
        except Exception as e:
            logger.error(f"Update order failed: {e}")
            raise AdapterOrderError(
                code="ADAPTER_ORDER_UPDATE_FAILED",
                context={"method": "update_order", "price": price,
                         "quantity": quantity, "error": str(e)}) from e

    def cancel_order(self, trade):
        self._gate_or_raise("cancel_order")        # [Step 8] fail-closed
        if not self.is_logged_in:
            return False
        try:
            result = self.api.cancel_order(trade)
            if result is None:
                raise AdapterOrderError(
                    code="ADAPTER_ORDER_NO_TRADE",
                    context={"method": "cancel_order",
                             "reason": "api returned no result"})
            if result is False:
                raise AdapterOrderError(
                    code="ADAPTER_ORDER_CANCEL_FAILED",
                    context={"method": "cancel_order",
                             "reason": "api returned False (not a Trade)"})
            return result
        except AdapterOrderError:
            raise
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            raise AdapterOrderError(
                code="ADAPTER_ORDER_CANCEL_FAILED",
                context={"method": "cancel_order", "error": str(e)}) from e

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
