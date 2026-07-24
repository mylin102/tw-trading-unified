#!/usr/bin/env python3
"""
Product Specification & Capital Resolution

Implements Sections 6–9 of docs/mts-product-switch-tmf-mtx.md.

Provides:
- ProductSpec dataclass (point_value, tick_size, fees, margin)
- CapitalPolicy enum (same_leverage, fixed, margin_based)
- ResolvedCapital dataclass
- resolve_initial_capital() pure function
- load_product_spec() with UnknownProductError
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional


# ── Errors ──

class UnknownProductError(ValueError):
    """Raised when an unknown ticker is requested."""
    pass


class ProductConfigError(RuntimeError):
    """Raised when product config is missing required fields."""
    pass


# ── Product Specification ──

@dataclass(frozen=True)
class ProductSpec:
    """Immutable product specification tied to a single ticker.

    All monetary values in TWD.
    """
    ticker: str
    point_value: Decimal
    tick_size: Decimal
    broker_fee_per_side: Decimal
    exchange_fee_per_side: Decimal
    tax_rate: Decimal
    margin_per_lot: Decimal
    baseline_initial_capital_twd: Decimal


# ── Capital Policy ──

class CapitalPolicy(str, Enum):
    SAME_LEVERAGE = "same_leverage"
    FIXED = "fixed"
    MARGIN_BASED = "margin_based"


@dataclass(frozen=True)
class ResolvedCapital:
    """Result of capital resolution.

    All monetary values in TWD.
    """
    ticker: str
    policy: CapitalPolicy
    initial_capital_twd: Decimal
    risk_equivalent_capital_twd: Decimal
    margin_required_capital_twd: Optional[Decimal]
    source: str


# ── Product Registry ──

_BUILTIN_PRODUCTS: Dict[str, ProductSpec] = {
    "TMF": ProductSpec(
        ticker="TMF",
        point_value=Decimal("10"),
        tick_size=Decimal("1"),
        broker_fee_per_side=Decimal("22"),
        exchange_fee_per_side=Decimal("0"),
        tax_rate=Decimal("0.00002"),
        margin_per_lot=Decimal("46000"),
        baseline_initial_capital_twd=Decimal("100000"),
    ),
    "MTX": ProductSpec(
        ticker="MTX",
        point_value=Decimal("50"),
        tick_size=Decimal("1"),
        broker_fee_per_side=Decimal("35"),
        exchange_fee_per_side=Decimal("0"),
        tax_rate=Decimal("0.00002"),
        margin_per_lot=Decimal("120000"),
        baseline_initial_capital_twd=Decimal("500000"),
    ),
}


def get_builtin_product(ticker: str) -> ProductSpec:
    """Look up a built-in product spec.

    Raises UnknownProductError for unknown tickers.
    """
    spec = _BUILTIN_PRODUCTS.get(ticker.upper())
    if spec is None:
        raise UnknownProductError(
            f"Unknown ticker '{ticker}'. "
            f"Available built-in products: {', '.join(sorted(_BUILTIN_PRODUCTS))}"
        )
    return spec


# ── Capital Resolution ──

@dataclass(frozen=True)
class CapitalConfig:
    """Capital policy configuration from runtime config.

    All values in TWD except max_margin_utilization and max_drawdown_ratio
    which are unitless ratios in [0, 1].
    """
    policy: CapitalPolicy = CapitalPolicy.SAME_LEVERAGE
    baseline_product: str = "TMF"
    baseline_initial_capital_twd: Decimal = Decimal("100000")
    max_margin_utilization: Decimal = Decimal("0.40")
    max_drawdown_ratio: Decimal = Decimal("0.20")
    fixed_initial_capital_twd: Optional[Decimal] = None


def resolve_initial_capital(
    *,
    target_spec: ProductSpec,
    capital_config: CapitalConfig,
    peak_contracts: int = 1,
) -> ResolvedCapital:
    """Resolve initial capital for a target product under a capital policy.

    This is a pure function — no IO, no side effects.

    Args:
        target_spec: ProductSpec of the target product.
        capital_config: Capital configuration policy.
        peak_contracts: Maximum concurrent contracts for margin calculation.

    Returns:
        ResolvedCapital with the effective initial capital.

    Raises:
        ProductConfigError: If required fields are missing for the chosen policy.
    """
    # Resolve baseline spec for risk-equivalent scaling
    if capital_config.policy == CapitalPolicy.FIXED:
        if capital_config.fixed_initial_capital_twd is None:
            raise ProductConfigError(
                "FIXED policy requires fixed_initial_capital_twd"
            )
        return ResolvedCapital(
            ticker=target_spec.ticker,
            policy=capital_config.policy,
            initial_capital_twd=capital_config.fixed_initial_capital_twd,
            risk_equivalent_capital_twd=capital_config.fixed_initial_capital_twd,
            margin_required_capital_twd=None,
            source="fixed_config",
        )

    # For SAME_LEVERAGE or MARGIN_BASED, compute risk-equivalent capital
    baseline_spec = get_builtin_product(capital_config.baseline_product)

    risk_equivalent = (
        capital_config.baseline_initial_capital_twd
        * target_spec.point_value
        / baseline_spec.point_value
    )

    # Margin-based requirement
    margin_required: Optional[Decimal] = None
    if capital_config.policy == CapitalPolicy.MARGIN_BASED:
        margin_required = (
            target_spec.margin_per_lot
            * Decimal(str(peak_contracts))
            / capital_config.max_margin_utilization
        )

    # Effective capital
    if capital_config.policy == CapitalPolicy.MARGIN_BASED and margin_required is not None:
        effective = max(risk_equivalent, margin_required)
    else:
        effective = risk_equivalent

    return ResolvedCapital(
        ticker=target_spec.ticker,
        policy=capital_config.policy,
        initial_capital_twd=effective,
        risk_equivalent_capital_twd=risk_equivalent,
        margin_required_capital_twd=margin_required,
        source="product_config",
    )


# ── Config Loading ──

def load_product_spec(ticker: str, config_dir: Optional[str] = None) -> ProductSpec:
    """Load product spec from config file or fall back to built-in registry.

    The config file schema (YAML) is defined in
    docs/mts-product-switch-tmf-mtx.md Section 6.

    Currently falls back to built-in registry. Config-file-based
    overrides can be added later.
    """
    return get_builtin_product(ticker)
