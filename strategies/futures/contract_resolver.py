#!/usr/bin/env python3
"""
Market-data contract resolver.

Wraps Shioaji contract resolution for passive collectors that don't
have their own ``FuturesMonitor``.  Reuses the same settlement/rollover
semantics as the existing TMF resolver.

Responsibilities:
  1. Exact product lookup via ``api.Contracts.Futures[product]``.
  2. Generic group scan fallback when the product key doesn't exist.
  3. Rolling-contract exclusion (R1, R2, R3).
  4. Expiry filtering via a trading-date provider.
  5. Deterministic near/far ordering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Protocol


# ── Errors ──

class ContractResolutionError(RuntimeError):
    """Raised when contract resolution fails irrecoverably."""


# ── Protocols / Types ──

class TradingDateProvider(Protocol):
    """Provides the current TAIFEX trading date."""

    def __call__(self) -> date:
        ...


# ── Resolved result ──

@dataclass(frozen=True)
class ResolvedContracts:
    """Near and far-month contracts after resolution."""
    near_raw: Any       # Raw Shioaji contract object
    far_raw: Any        # Raw Shioaji contract object
    near_code: str
    far_code: str


# ── Resolver ──

class MarketDataContractResolver:
    """Product-agnostic futures contract resolver with group-scan fallback.

    Usage::

        resolver = MarketDataContractResolver(
            api=api,
            trading_date_provider=get_taifex_trading_date,
        )
        result = resolver.resolve_near_far("MTX")
        if result is not None:
            near_con, far_con = result.near_raw, result.far_raw
    """

    def __init__(
        self,
        api: Any,
        *,
        trading_date_provider: TradingDateProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._api = api
        self._today_provider = trading_date_provider or (lambda: date.today())
        self._logger = logger or logging.getLogger(self.__class__.__name__)

    def resolve_near_far(self, product: str) -> ResolvedContracts | None:
        """Resolve near and far contracts for *product*.

        For TMF/MXF-style products, prefers continuous-contract resolution
        (``R1``/``R2``) when the product matches a known continuous-contract
        root.  Falls back to scanning all futures groups.

        Returns ``None`` when fewer than two eligible contracts exist
        (product not available / not traded today).
        """
        # MXF (Mini TX / 小台) — resolve via continuous contracts
        if product.upper() in ("MXF", "MTX"):
            return self._resolve_via_continuous("MXF")

        # Generic scanning fallback for other products
        contracts = self._collect_eligible_contracts(product)
        if len(contracts) < 2:
            self._logger.info(
                "Product %s: %d eligible contract(s) — need ≥ 2",
                product, len(contracts),
            )
            return None

        near_raw, far_raw = contracts[0], contracts[1]
        return ResolvedContracts(
            near_raw=near_raw,
            far_raw=far_raw,
            near_code=near_raw.code,
            far_code=far_raw.code,
        )

    def _resolve_via_continuous(self, root: str) -> ResolvedContracts | None:
        """Resolve near/far via ``R1``/``R2`` continuous contracts + ``target_code``.

        Uses ``api.Contracts.Futures`` (older Shioaji style) for compatibility
        with Shioaji 1.5.5.  Falls back to ``api.contracts.get()`` for newer SDKs.
        """
        # Strategy 1: old-style Futures dict lookup (Shioaji 1.5.5+)
        try:
            near_alias = self._find_continuous_contract(f"{root}R1")
            far_alias = self._find_continuous_contract(f"{root}R2")
        except Exception:
            near_alias = far_alias = None

        # Strategy 2: new-style contracts.get() (Shioaji 1.7.0+)
        if near_alias is None:
            try:
                near_alias = self._api.contracts.get(f"{root}R1")
                far_alias = self._api.contracts.get(f"{root}R2")
            except (AttributeError, Exception):
                # Shioaji 1.5.5: contracts.get() not available — expected
                pass

        if near_alias is not None and far_alias is not None:
            near_code = getattr(near_alias, "target_code", None) or near_alias.code
            far_code = getattr(far_alias, "target_code", None) or far_alias.code

            near_raw = self._resolve_target_contract(near_code)
            far_raw = self._resolve_target_contract(far_code)

            if near_raw is not None and far_raw is not None:
                result = ResolvedContracts(
                    near_raw=near_raw,
                    far_raw=far_raw,
                    near_code=near_code,
                    far_code=far_code,
                )
                self._logger.info(
                    "Resolved %s via continuous: near=%s far=%s",
                    root, result.near_code, result.far_code,
                )
                return result

        # Strategy 3: group-scan fallback — find all month contracts for this root
        self._logger.info("Continuous contracts %sR1/R2 not available; trying month scan", root)
        contracts = self._collect_eligible_contracts(root)
        if len(contracts) >= 2:
            near_raw, far_raw = contracts[0], contracts[1]
            result = ResolvedContracts(
                near_raw=near_raw,
                far_raw=far_raw,
                near_code=near_raw.code,
                far_code=far_raw.code,
            )
            self._logger.info(
                "Resolved %s via scan: near=%s far=%s",
                root, result.near_code, result.far_code,
            )
            return result

        self._logger.info("Could not resolve %s via any method", root)
        return None

    def _find_continuous_contract(self, code: str) -> Any | None:
        """Find a continuous contract (e.g. MXFR1) by scanning all futures groups."""
        for attr_name in dir(self._api.Contracts.Futures):
            if attr_name.startswith("_"):
                continue
            try:
                grp = getattr(self._api.Contracts.Futures, attr_name)
                if not (hasattr(grp, "__getitem__") or hasattr(grp, "__iter__")):
                    continue
                for c in grp:
                    if getattr(c, "code", "") == code:
                        return c
            except Exception:
                continue
        return None

    def _resolve_target_contract(self, code: str) -> Any | None:
        """Resolve a target contract code to a contract object."""
        # Try old-style lookup first
        for attr_name in dir(self._api.Contracts.Futures):
            if attr_name.startswith("_"):
                continue
            try:
                grp = getattr(self._api.Contracts.Futures, attr_name)
                if not (hasattr(grp, "__getitem__") or hasattr(grp, "__iter__")):
                    continue
                for c in grp:
                    if getattr(c, "code", "") == code:
                        return c
            except Exception:
                continue
        # Try new-style lookup as fallback
        try:
            return self._api.contracts.get(code)
        except Exception:
            return None

    # ── Internal ──

    def _collect_eligible_contracts(self, product: str) -> list[Any]:
        """Collect all eligible contracts for *product*, sorted by delivery date.

        Eligibility:
          - Not a rolling reference (R1/R2/R3).
          - Has a valid delivery date.
          - Delivery date >= trading date (not yet expired).
        """
        raw_contracts = self._fetch_contracts(product)
        if not raw_contracts:
            return []

        trading_date = self._today_provider()
        eligible: list[tuple[datetime, Any]] = []

        for c in raw_contracts:
            code = getattr(c, "code", "") or ""
            if code.endswith(("R1", "R2", "R3")):
                continue

            delivery_str = getattr(c, "delivery_date", None) or ""
            if not delivery_str:
                continue

            delivery = self._parse_delivery(delivery_str)
            if delivery is None:
                continue

            # Exclude expired contracts
            if delivery.date() < trading_date:
                continue

            eligible.append((delivery, c))

        eligible.sort(key=lambda x: x[0])
        return [c for _, c in eligible]

    def _fetch_contracts(self, product: str) -> list[Any]:
        """Fetch contracts for *product* — exact lookup first, then scan."""
        # 1. Exact product-key lookup (dict-style)
        try:
            return list(self._api.Contracts.Futures[product])
        except Exception:
            pass

        # 2. Attribute-style lookup
        try:
            return list(getattr(self._api.Contracts.Futures, product))
        except Exception:
            pass

        # 3. Group-scan fallback — iterate all futures groups
        results = []
        seen_codes: set[str] = set()
        prefix = product.upper()

        for attr_name in dir(self._api.Contracts.Futures):
            if attr_name.startswith("_"):
                continue
            try:
                grp = getattr(self._api.Contracts.Futures, attr_name)
                if hasattr(grp, "__getitem__") or hasattr(grp, "__iter__"):
                    for c in grp:
                        code = getattr(c, "code", "") or ""
                        if code.startswith(prefix) and code not in seen_codes:
                            results.append(c)
                            seen_codes.add(code)
            except Exception:
                continue

        if results:
            self._logger.info(
                "Product %s: exact lookup failed; scan found %d contract(s)",
                product, len(results),
            )

        return results

    @staticmethod
    def _parse_delivery(raw: str) -> datetime | None:
        """Parse Shioaji delivery-date format ``YYYY/MM/DD``."""
        try:
            return datetime.strptime(raw.strip(), "%Y/%m/%d")
        except (ValueError, AttributeError):
            return None
