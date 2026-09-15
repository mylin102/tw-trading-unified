"""Single stock order chokepoint.

This module owns the only stock broker order/cancel calls.  It deliberately
keeps authorization as one fail-closed layer; broker receipt and
reconciliation are separate follow-up slices.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StockOrderResult:
    accepted: bool
    reason: str
    trade: Any = None
    idempotency_key: str | None = None


class StockOrderChokepoint:
    """Persist, authorize, deduplicate, then optionally call the broker."""

    def __init__(self, broker, *, intent_path: str | Path,
                 config: dict, mode: str, now=None):
        self.broker = broker
        self.intent_path = Path(intent_path)
        self.config = config or {}
        self.mode = mode
        self._now = now or datetime.now

    def _append_intent(self, event: dict) -> None:
        self.intent_path.parent.mkdir(parents=True, exist_ok=True)
        with self.intent_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True,
                                default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _key(intent: dict) -> str:
        explicit = intent.get("idempotency_key")
        if explicit:
            return str(explicit)
        raw = "|".join(str(intent.get(k, "")) for k in (
            "decision_id", "bar_id", "symbol", "side", "qty", "price"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def _seen_keys(self) -> set[str]:
        if not self.intent_path.exists():
            return set()
        seen = set()
        for line in self.intent_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("idempotency_key") and event.get("status") in {
                "AUTHORIZED", "PAPER_ACCEPTED", "BROKER_SUBMITTED", "DUPLICATE",
            }:
                seen.add(event["idempotency_key"])
        return seen

    def _authorize(self, intent: dict) -> tuple[bool, str]:
        try:
            stocks = self.config.get("stocks", {}) or {}
            qty = int(intent["qty"])
            price = float(intent["price"])
            if qty <= 0 or price <= 0:
                return False, "INVALID_QUANTITY_OR_PRICE"
            if intent.get("pending_order"):
                return False, "PENDING_ORDER_EXISTS"
            if intent.get("position_snapshot_ok") is not True:
                return False, "POSITION_SNAPSHOT_UNVERIFIED"
            if intent.get("price_limit_ok") is not True:
                return False, "PRICE_LIMIT_UNVERIFIED"
            if intent.get("available_funds") is not None and \
                    price * qty > float(intent["available_funds"]):
                return False, "FUNDS_UNAVAILABLE"

            now = intent.get("now") or self._now()
            if isinstance(now, str):
                now = datetime.fromisoformat(now)
            if not (time(9, 0) <= now.time() <= time(13, 30)):
                return False, "OUTSIDE_TRADING_SESSION"

            order_value = price * qty
            if order_value > float(stocks.get("capital_per_trade", 0)):
                return False, "PER_ORDER_LIMIT"
            exposure = float(intent.get("current_exposure", 0) or 0)
            if exposure + order_value > float(stocks.get("total_portfolio_budget", 0)):
                return False, "TOTAL_EXPOSURE_LIMIT"
            daily_pnl = float(intent.get("daily_pnl", 0) or 0)
            if daily_pnl < -float((stocks.get("bear_defense", {}) or {}).get(
                    "max_daily_loss", 0)):
                return False, "DAILY_LOSS_LIMIT"

            positions = set(intent.get("open_positions", []) or [])
            max_positions = int((stocks.get("bear_defense", {}) or {}).get(
                "bear_max_positions" if intent.get("is_bear_market") else
                "normal_max_positions", 0))
            if intent.get("side") == "BUY" and intent.get("symbol") not in positions \
                    and len(positions) >= max_positions:
                return False, "MAX_POSITIONS_LIMIT"
            if intent.get("strategy_version") != "mean_reversion_enhanced":
                return False, "STRATEGY_VERSION_UNAUTHORIZED"
            return True, "AUTHORIZED"
        except Exception:
            return False, "AUTHORIZATION_ERROR"

    def submit_stock_order(self, *, intent: dict, contract, order) -> StockOrderResult:
        key = self._key(intent)
        base = dict(intent)
        base.update({"idempotency_key": key, "status": "RECEIVED",
                     "persisted_at": self._now().isoformat()})
        try:
            self._append_intent(base)
        except Exception:
            return StockOrderResult(False, "INTENT_PERSIST_FAILED", idempotency_key=key)

        authorized, reason = self._authorize(intent)
        duplicate = key in self._seen_keys()
        if duplicate and authorized:
            authorized, reason = False, "DUPLICATE_INTENT"
        try:
            self._append_intent({"idempotency_key": key, "status":
                                 "AUTHORIZED" if authorized else "REJECTED",
                                 "reason": reason,
                                 "recorded_at": self._now().isoformat()})
        except Exception:
            return StockOrderResult(False, "INTENT_AUDIT_UPDATE_FAILED", idempotency_key=key)
        if not authorized:
            return StockOrderResult(False, reason, idempotency_key=key)
        if self.mode != "LIVE":
            self._append_intent({"idempotency_key": key, "status": "PAPER_ACCEPTED",
                                 "recorded_at": self._now().isoformat()})
            return StockOrderResult(True, "PAPER_ACCEPTED", idempotency_key=key)
        try:
            trade = self.broker.place_order(contract, order)
            self._append_intent({"idempotency_key": key, "status": "BROKER_SUBMITTED",
                                 "recorded_at": self._now().isoformat()})
            return StockOrderResult(True, "BROKER_SUBMITTED", trade, key)
        except Exception:
            return StockOrderResult(False, "BROKER_SUBMIT_FAILED", idempotency_key=key)

    def cancel_stock_order(self, trade, *, reason: str) -> Any:
        return self.broker.cancel_order(trade)
