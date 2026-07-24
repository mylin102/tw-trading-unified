#!/usr/bin/env python3
"""Tests for core.product_spec — implements Section 21 of mts-product-switch-tmf-mtx.md."""

from decimal import Decimal
import pytest

from core.product_spec import (
    CapitalConfig,
    CapitalPolicy,
    ProductSpec,
    ResolvedCapital,
    UnknownProductError,
    get_builtin_product,
    load_product_spec,
    resolve_initial_capital,
)


class TestProductSpec:
    """Section 21.1: Product config tests."""

    def test_tmf_product_spec(self):
        spec = get_builtin_product("TMF")
        assert spec.ticker == "TMF"
        assert spec.point_value == Decimal("10")
        assert spec.tick_size == Decimal("1")
        assert spec.broker_fee_per_side == Decimal("22")
        assert spec.margin_per_lot == Decimal("46000")
        assert spec.baseline_initial_capital_twd == Decimal("100000")

    def test_mtx_product_spec(self):
        spec = get_builtin_product("MTX")
        assert spec.ticker == "MTX"
        assert spec.point_value == Decimal("50")
        assert spec.tick_size == Decimal("1")
        assert spec.broker_fee_per_side == Decimal("35")
        assert spec.margin_per_lot == Decimal("120000")
        assert spec.baseline_initial_capital_twd == Decimal("500000")

    def test_unknown_ticker_fails_closed(self):
        with pytest.raises(UnknownProductError):
            get_builtin_product("UNKNOWN")

    def test_case_insensitive(self):
        spec_lower = get_builtin_product("tmf")
        spec_upper = get_builtin_product("TMF")
        assert spec_lower == spec_upper

    def test_load_product_spec_fallback(self):
        spec = load_product_spec("TMF")
        assert spec.ticker == "TMF"


class TestCapitalScaling:
    """Section 21.2: Capital scaling tests."""

    def test_same_leverage_scales_mtx_capital_five_times(self):
        tmf = get_builtin_product("TMF")
        mtx = get_builtin_product("MTX")
        config = CapitalConfig(
            policy=CapitalPolicy.SAME_LEVERAGE,
            baseline_product="TMF",
            baseline_initial_capital_twd=Decimal("100000"),
        )

        resolved = resolve_initial_capital(
            target_spec=mtx,
            capital_config=config,
        )

        # MTX point_value (50) / TMF point_value (10) = 5x
        expected = Decimal("500000")
        assert resolved.initial_capital_twd == expected, (
            f"Expected {expected}, got {resolved.initial_capital_twd}"
        )
        assert resolved.policy == CapitalPolicy.SAME_LEVERAGE
        assert resolved.ticker == "MTX"

    def test_tmf_same_leverage_returns_baseline(self):
        tmf = get_builtin_product("TMF")
        config = CapitalConfig(
            policy=CapitalPolicy.SAME_LEVERAGE,
            baseline_product="TMF",
            baseline_initial_capital_twd=Decimal("100000"),
        )

        resolved = resolve_initial_capital(
            target_spec=tmf,
            capital_config=config,
        )

        # TMF same as baseline → unchanged
        assert resolved.initial_capital_twd == Decimal("100000")

    def test_fixed_policy(self):
        mtx = get_builtin_product("MTX")
        config = CapitalConfig(
            policy=CapitalPolicy.FIXED,
            fixed_initial_capital_twd=Decimal("300000"),
        )

        resolved = resolve_initial_capital(
            target_spec=mtx,
            capital_config=config,
        )

        assert resolved.initial_capital_twd == Decimal("300000")
        assert resolved.policy == CapitalPolicy.FIXED

    def test_fixed_policy_requires_value(self):
        mtx = get_builtin_product("MTX")
        config = CapitalConfig(policy=CapitalPolicy.FIXED)

        with pytest.raises(RuntimeError):
            resolve_initial_capital(target_spec=mtx, capital_config=config)

    def test_margin_based_policy(self):
        mtx = get_builtin_product("MTX")
        config = CapitalConfig(
            policy=CapitalPolicy.MARGIN_BASED,
            baseline_product="TMF",
            baseline_initial_capital_twd=Decimal("100000"),
            max_margin_utilization=Decimal("0.40"),
        )

        resolved = resolve_initial_capital(
            target_spec=mtx,
            capital_config=config,
            peak_contracts=2,
        )

        # margin_required = 120000 * 2 / 0.40 = 600000
        # risk_equivalent = 100000 * 50 / 10 = 500000
        # effective = max(600000, 500000) = 600000
        assert resolved.initial_capital_twd == Decimal("600000")
        assert resolved.margin_required_capital_twd == Decimal("600000")
        assert resolved.risk_equivalent_capital_twd == Decimal("500000")
