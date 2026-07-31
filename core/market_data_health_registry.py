#!/usr/bin/env python3
"""
MarketDataHealthRegistry - tracks full pipeline health per contract/channel.
Subscription intent, raw callbacks, routing, strategy consumption, state writes.
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_HEALTH_FILE = Path("/tmp/market_data_health.json")
_lock = threading.Lock()
_registry: dict[str, "FeedHealth"] = {}


@dataclass
class FeedHealth:
    contract_code: str
    channel: str  # "tick" or "bidask"

    # Subscription intent
    subscription_desired: bool = False
    subscribe_called_at: Optional[str] = None
    subscribe_result: Optional[str] = None

    # Raw callback
    callback_count: int = 0
    last_callback_at: Optional[str] = None
    last_exchange_ts: Optional[str] = None
    last_price: Optional[float] = None

    # Routing
    route_count: int = 0
    last_routed_at: Optional[str] = None
    route_target: Optional[str] = None
    route_failed: bool = False

    # Strategy consumption
    consume_count: int = 0
    last_consumed_at: Optional[str] = None

    # State write
    state_write_count: int = 0
    last_state_written_at: Optional[str] = None

    # Errors
    last_error: Optional[str] = None

    # Process identity
    pid: int = 0
    process_started_at: Optional[str] = None

    @property
    def callback_age_secs(self) -> Optional[float]:
        if self.last_callback_at:
            t = datetime.fromisoformat(self.last_callback_at)
            return (datetime.now() - t).total_seconds()
        return None

    @property
    def route_age_secs(self) -> Optional[float]:
        if self.last_routed_at:
            t = datetime.fromisoformat(self.last_routed_at)
            return (datetime.now() - t).total_seconds()
        return None

    @property
    def consume_age_secs(self) -> Optional[float]:
        if self.last_consumed_at:
            t = datetime.fromisoformat(self.last_consumed_at)
            return (datetime.now() - t).total_seconds()
        return None

    @property
    def state_age_secs(self) -> Optional[float]:
        if self.last_state_written_at:
            t = datetime.fromisoformat(self.last_state_written_at)
            return (datetime.now() - t).total_seconds()
        return None

    def to_dict(self) -> dict:
        return {
            "contract_code": self.contract_code,
            "channel": self.channel,
            "subscription_desired": self.subscription_desired,
            "subscribe_called_at": self.subscribe_called_at,
            "subscribe_result": self.subscribe_result,
            "callback_count": self.callback_count,
            "last_callback_at": self.last_callback_at,
            "last_price": self.last_price,
            "callback_age_secs": self.callback_age_secs,
            "route_count": self.route_count,
            "last_routed_at": self.last_routed_at,
            "route_target": self.route_target,
            "route_failed": self.route_failed,
            "route_age_secs": self.route_age_secs,
            "consume_count": self.consume_count,
            "last_consumed_at": self.last_consumed_at,
            "consume_age_secs": self.consume_age_secs,
            "state_write_count": self.state_write_count,
            "last_state_written_at": self.last_state_written_at,
            "state_age_secs": self.state_age_secs,
            "last_error": self.last_error,
            "pid": self.pid,
            "process_started_at": self.process_started_at,
        }


def _key(contract_code: str, channel: str) -> str:
    return f"{contract_code}_{channel}"


def record_subscribe(contract_code: str, channel: str, result: str):
    with _lock:
        k = _key(contract_code, channel)
        if k not in _registry:
            _registry[k] = FeedHealth(contract_code=contract_code, channel=channel,
                                       pid=os.getpid(),
                                       process_started_at=datetime.now().isoformat())
        h = _registry[k]
        h.subscription_desired = True
        h.subscribe_called_at = datetime.now().isoformat()
        h.subscribe_result = result
        h.last_error = None


def record_callback(contract_code: str, channel: str, price: Optional[float] = None,
                    exchange_ts: Optional[str] = None):
    with _lock:
        k = _key(contract_code, channel)
        if k not in _registry:
            _registry[k] = FeedHealth(contract_code=contract_code, channel=channel,
                                       pid=os.getpid(),
                                       process_started_at=datetime.now().isoformat())
        h = _registry[k]
        h.callback_count += 1
        h.last_callback_at = datetime.now().isoformat()
        if price is not None:
            h.last_price = price
        if exchange_ts:
            h.last_exchange_ts = exchange_ts


def record_route(contract_code: str, target: str, failed: bool = False):
    with _lock:
        k = _key(contract_code, "tick")
        if k not in _registry:
            _registry[k] = FeedHealth(contract_code=contract_code, channel="tick",
                                       pid=os.getpid(),
                                       process_started_at=datetime.now().isoformat())
        h = _registry[k]
        h.route_count += 1
        h.last_routed_at = datetime.now().isoformat()
        h.route_target = target
        h.route_failed = failed


def record_consume(contract_code: str):
    with _lock:
        k = _key(contract_code, "tick")
        if k not in _registry:
            _registry[k] = FeedHealth(contract_code=contract_code, channel="tick",
                                       pid=os.getpid(),
                                       process_started_at=datetime.now().isoformat())
        h = _registry[k]
        h.consume_count += 1
        h.last_consumed_at = datetime.now().isoformat()


def record_state_write(contract_code: str):
    with _lock:
        k = _key(contract_code, "tick")
        if k not in _registry:
            _registry[k] = FeedHealth(contract_code=contract_code, channel="tick",
                                       pid=os.getpid(),
                                       process_started_at=datetime.now().isoformat())
        h = _registry[k]
        h.state_write_count += 1
        h.last_state_written_at = datetime.now().isoformat()


def record_error(contract_code: str, channel: str, error: str):
    with _lock:
        k = _key(contract_code, channel)
        if k not in _registry:
            _registry[k] = FeedHealth(contract_code=contract_code, channel=channel,
                                       pid=os.getpid(),
                                       process_started_at=datetime.now().isoformat())
        _registry[k].last_error = error


def _snapshot() -> dict:
    """Build a serialisable snapshot of all tracked contracts."""
    with _lock:
        data = {}
        for k, h in _registry.items():
            data[k] = h.to_dict()
        return {
            "generated_at": datetime.now().isoformat(),
            "host": os.uname().nodename,
            "pid": os.getpid(),
            "contracts": data,
        }


def write_snapshot(path: Path = _HEALTH_FILE):
    """Atomically write the current health snapshot."""
    try:
        s = _snapshot()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        pass  # fail-open: never block trading


def get_health(contract_code: str, channel: str = "tick") -> Optional[FeedHealth]:
    with _lock:
        return _registry.get(_key(contract_code, channel))
