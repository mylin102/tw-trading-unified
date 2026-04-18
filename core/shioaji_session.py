"""
Singleton Shioaji session — shared across all strategies.
Only one sj.Shioaji() instance per process.

Phase 1 enhancements (2026-04-13):
- resolve_actual_contract_code(): Fix rolling contract code resolution (MXFR1 → MXFA6)
- check_order_status(): Full order status with deals parsing
- safe_api_call(): Error handling with retry for Shioaji-specific errors
"""
import os
import time
import logging
import threading
from typing import Any, Optional

import shioaji as sj
from shioaji.error import (
    TokenError,
    SystemMaintenance,
    TimeoutError as SjTimeoutError,
    AccountNotSignError,
    AccountNotProvideError,
    TargetContractNotExistError,
)
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_api: sj.Shioaji | None = None
_lock = threading.Lock()


from enum import Enum
class SystemReadiness(Enum):
    BOOTING = "BOOTING"       # Initializing, logging in
    MONITORING = "MONITORING" # Connected, receiving ticks, but indicators cold
    TRADING = "TRADING"       # Indicators ready, Edge model active
    DEGRADED = "DEGRADED"     # Connection lost or error state

_current_status = SystemReadiness.BOOTING
_status_lock = threading.Lock()

def set_system_status(status: SystemReadiness):
    global _current_status
    with _status_lock:
        _current_status = status
        # [Pillar 4] Log state transitions for observability
        logger.info(f"🚦 [System] State transition: {_current_status.value}")

def get_system_status() -> SystemReadiness:
    with _status_lock:
        return _current_status

def get_api() -> sj.Shioaji:
    """Return the shared API instance, logging in if needed."""
    global _api
    with _lock:
        if _api is not None:
            return _api
        _api = _login()
        return _api


def logout():
    global _api
    with _lock:
        if _api is not None:
            try:
                _api.logout()
            except Exception:
                pass
            _api = None


def _login() -> sj.Shioaji:
    load_dotenv(override=True)
    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_PERSON_ID")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_PASSWD")
    ca_path = os.getenv("SHIOAJI_CA_PATH", "")
    ca_name = os.getenv("SHIOAJI_CA_NAME", "")
    ca_passwd = os.getenv("SHIOAJI_CA_PASSWD", "")
    person_id = os.getenv("SHIOAJI_PERSON_ID", api_key)

    if not api_key or not secret_key:
        raise EnvironmentError("SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY not set")

    api = sj.Shioaji()
    for attempt in range(1, 4):
        try:
            api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=10000)
            print(f"[session] Logged in (attempt {attempt})")
            break
        except Exception as e:
            if "Too Many Connections" in str(e) and attempt < 3:
                wait = attempt * 30
                print(f"[session] Too Many Connections — retrying in {wait}s ({attempt}/3)")
                time.sleep(wait)
            else:
                raise

    ca_full = os.path.join(ca_path, ca_name) if ca_path and ca_name else ""
    if ca_full and os.path.exists(ca_full):
        try:
            ok = api.activate_ca(ca_path=ca_full, ca_passwd=ca_passwd, person_id=person_id)
            print(f"[session] CA {'activated' if ok else 'activation failed'}: {ca_full}")
        except Exception as e:
            print(f"[session] CA error: {e}")

    return api


# ─── Phase 1 Enhancements ──────────────────────────────────────────────────


def resolve_actual_contract_code(api: sj.Shioaji, contract: sj.contracts.BaseContract) -> str:
    """Resolve a rolling contract to its actual trading code.

    Rolling contracts (like MXFR1, TXFR1, TMFR1) have code == symbol (e.g., "MXFR1"),
    but positions are stored with the actual contract code (e.g., "MXFA6").

    This function resolves rolling contracts to their actual code by matching
    category + delivery_month against actual contracts.

    Args:
        api: Shioaji API instance (must be logged in)
        contract: The contract to resolve

    Returns:
        The actual contract code (e.g., "MXFA6" for MXFR1)

    Source: https://github.com/luisleo526/shioaji-api-dashboard (trading.py)
    """
    # If code != symbol, it's already an actual contract
    if contract.code != contract.symbol:
        return contract.code

    # For rolling contracts (code == symbol, e.g., MXFR1),
    # find the actual contract by matching category + delivery_month
    category = getattr(contract, "category", None)
    delivery_month = getattr(contract, "delivery_month", None)

    if category is None or delivery_month is None:
        return contract.code

    for c in api.Contracts.Futures[category]:
        if c.category == category and c.delivery_month == delivery_month and c.code != c.symbol:
            logger.debug(f"Resolved rolling contract {contract.code} → {c.code}")
            return c.code

    # Fallback: return original code if no match found
    logger.warning(f"Could not resolve rolling contract {contract.code}")
    return contract.code


def check_order_status(api: sj.Shioaji, trade: sj.order.Trade) -> dict[str, Any]:
    """Check the actual fill status of an order with full deal details.

    Calls api.update_status(trade=trade) and extracts:
    - Order status (Submitted, Filled, PartFilled, Cancelled, Failed)
    - Deal details (price, quantity, timestamp per deal)
    - Average fill price calculated from deals

    Args:
        api: Shioaji API instance
        trade: Trade object returned by api.place_order()

    Returns:
        Dict with status, deal_quantity, fill_avg_price, deals list, etc.

    Source: https://github.com/luisleo526/shioaji-api-dashboard (trading.py)
    """
    if trade is None:
        return {"status": "no_trade", "error": "No trade object provided"}

    try:
        api.update_status(trade=trade)

        status_obj = trade.status
        order_obj = trade.order

        status_value = status_obj.status.value if hasattr(status_obj.status, "value") else str(status_obj.status)
        deals = status_obj.deals if status_obj.deals else []
        deal_quantity = status_obj.deal_quantity if hasattr(status_obj, "deal_quantity") else 0

        # Calculate average fill price from deals
        total_value = sum(d.price * d.quantity for d in deals) if deals else 0
        total_qty = sum(d.quantity for d in deals) if deals else 0
        fill_avg_price = total_value / total_qty if total_qty > 0 else 0.0

        if deals:
            logger.info(
                f"Order {getattr(order_obj, 'ordno', '?')}: "
                f"{len(deals)} deal(s), avg_price={fill_avg_price:.1f}, qty={deal_quantity}"
            )
            for i, d in enumerate(deals):
                logger.debug(
                    f"  Deal[{i}]: seq={getattr(d, 'seq', '')}, "
                    f"qty={d.quantity}, price={d.price}, ts={getattr(d, 'ts', 0)}"
                )

        return {
            "status": status_value,
            "status_code": getattr(status_obj, "status_code", ""),
            "msg": getattr(status_obj, "msg", ""),
            "order_id": getattr(order_obj, "id", ""),
            "seqno": getattr(order_obj, "seqno", ""),
            "ordno": getattr(order_obj, "ordno", ""),
            "order_quantity": getattr(status_obj, "order_quantity", 0) or order_obj.quantity,
            "deal_quantity": deal_quantity,
            "cancel_quantity": getattr(status_obj, "cancel_quantity", 0),
            "fill_avg_price": fill_avg_price,
            "deals": [
                {
                    "seq": getattr(d, "seq", ""),
                    "price": d.price,
                    "quantity": d.quantity,
                    "ts": getattr(d, "ts", 0),
                }
                for d in deals
            ],
        }

    except Exception as e:
        logger.exception(f"Error checking order status: {e}")
        return {"status": "error", "error": str(e)}


def safe_api_call(func, *args, max_retries: int = 3, retry_delay: float = 2.0, **kwargs) -> Any:
    """Execute a Shioaji API call with automatic retry on recoverable errors.

    Catches Shioaji-specific exceptions and retries:
    - TokenError: Token expired — retry after re-login
    - SystemMaintenance: Server maintenance — wait and retry
    - TimeoutError: Network timeout — retry
    - AccountNotSignError: Account not signed — non-retryable, raises immediately

    Args:
        func: The API function to call (e.g., api.place_order)
        *args: Positional arguments for func
        max_retries: Maximum number of retry attempts (default: 3)
        retry_delay: Seconds between retries (default: 2.0)
        **kwargs: Keyword arguments for func

    Returns:
        Return value of func on success

    Raises:
        The last exception if all retries exhausted
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except (TokenError, SystemMaintenance, SjTimeoutError) as e:
            last_error = e
            error_type = type(e).__name__
            if attempt < max_retries:
                logger.warning(
                    f"Recoverable error ({error_type}) on attempt {attempt}/{max_retries}: {e}. "
                    f"Retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                logger.error(f"All {max_retries} retries exhausted: {error_type}: {e}")
                raise
        except (AccountNotSignError, AccountNotProvideError) as e:
            # Non-retryable account errors
            logger.error(f"Account error (non-retryable): {e}")
            raise
        except TargetContractNotExistError as e:
            # Non-retryable contract error
            logger.error(f"Contract error (non-retryable): {e}")
            raise

    # Should not reach here, but just in case
    raise last_error
