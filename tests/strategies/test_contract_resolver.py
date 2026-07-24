#!/usr/bin/env python3
"""Tests for strategies.futures.contract_resolver."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable
import pytest

from strategies.futures.contract_resolver import (
    ContractResolutionError,
    MarketDataContractResolver,
    ResolvedContracts,
)


# ── Test Doubles ──

@dataclass
class _MockContract:
    code: str
    delivery_date: str


class _MockGroup:
    """Simulates a Shioaji contract group (iterable)."""

    def __init__(self, contracts: list[_MockContract]) -> None:
        self._contracts = contracts

    def __iter__(self):
        return iter(self._contracts)

    def __getitem__(self, key):
        return self._contracts[key]


class _MockFutures:
    """Simulates ``api.Contracts.Futures``."""

    def __init__(self) -> None:
        self._groups: dict[str, _MockGroup] = {}

    def add_group(self, name: str, contracts: list[_MockContract]) -> None:
        self._groups[name] = _MockGroup(contracts)

    def __getitem__(self, key: str) -> _MockGroup | None:
        group = self._groups.get(key)
        if group is None:
            raise KeyError(f"Contract not found: {key}")
        return group

    def __getattr__(self, name: str) -> Any:
        try:
            return self._groups[name]
        except KeyError:
            raise AttributeError(f"ContractCategory 'FUT' has no group '{name}'")

    def __dir__(self) -> list[str]:
        return list(self._groups.keys())


class _MockContinuousContract:
    """Simulates a continuous contract (R1/R2) with target_code."""

    def __init__(self, code: str, target_code: str) -> None:
        self.code = code
        self.target_code = target_code


class _MockContractsApi:
    """Simulates ``api.contracts`` (lowercase, new-style API)."""

    def __init__(self) -> None:
        self._cache: dict[str, _MockContract | _MockContinuousContract] = {}

    def add(self, code: str, contract: _MockContract | _MockContinuousContract) -> None:
        self._cache[code] = contract

    def get(self, code: str) -> _MockContract | _MockContinuousContract | None:
        return self._cache.get(code)


class _MockApi:
    def __init__(self) -> None:
        self.Contracts = _MockFuturesContainer()
        self.contracts = _MockContractsApi()


class _MockFuturesContainer:
    """Simulates ``api.Contracts`` with ``.Futures``."""
    def __init__(self) -> None:
        self.Futures = _MockFutures()

def _fixed_date_provider(d: date) -> Callable[[], date]:
    return lambda: d


# ── Fixtures ──

@pytest.fixture
def tmf_api() -> _MockApi:
    """API with only TMF contracts."""
    api = _MockApi()
    api.Contracts.Futures.add_group("TMF", [
        _MockContract(code="TMFH6", delivery_date="2026/09/16"),
        _MockContract(code="TMFF6", delivery_date="2026/07/15"),
        _MockContract(code="TMFI6", delivery_date="2026/10/21"),
        _MockContract(code="TMFM6", delivery_date="2026/11/18"),
    ])
    return api


@pytest.fixture
def mtx_api() -> _MockApi:
    """API with MTX contracts under 'MTX' group."""
    api = _MockApi()
    api.Contracts.Futures.add_group("MTX", [
        _MockContract(code="MTXH6", delivery_date="2026/09/16"),
        _MockContract(code="MTXI6", delivery_date="2026/10/21"),
    ])
    return api


@pytest.fixture
def mixed_api() -> _MockApi:
    """API with MTX contracts spread across groups (simulating Shioaji 1.5.5)."""
    api = _MockApi()
    api.Contracts.Futures.add_group("TMF", [
        _MockContract(code="TMFH6", delivery_date="2026/09/16"),
        _MockContract(code="TMFI6", delivery_date="2026/10/21"),
    ])
    api.Contracts.Futures.add_group("MXF", [
        _MockContract(code="MXFH6", delivery_date="2026/09/16"),
        _MockContract(code="MXFI6", delivery_date="2026/10/21"),
    ])
    return api


@pytest.fixture
def empty_api() -> _MockApi:
    """API with no MTX contracts at all."""
    api = _MockApi()
    api.Contracts.Futures.add_group("TMF", [
        _MockContract(code="TMFH6", delivery_date="2026/09/16"),
    ])
    return api


# ── Tests ──

class TestResolveExactLookup:
    def test_resolves_tmf_near_far(self, tmf_api: _MockApi) -> None:
        resolver = MarketDataContractResolver(
            tmf_api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("TMF")
        assert result is not None
        assert result.near_code == "TMFF6"    # Jul (nearest)
        assert result.far_code == "TMFH6"     # Sep

    def test_excludes_expired_contracts(self, tmf_api: _MockApi) -> None:
        resolver = MarketDataContractResolver(
            tmf_api,
            trading_date_provider=_fixed_date_provider(date(2026, 10, 22)),
        )
        result = resolver.resolve_near_far("TMF")
        # Jul(TMFF6), Sep(TMFH6), Oct(TMFI6) all expired — only Nov(TMFM6) remains
        assert result is None

    def test_excludes_rolling_contracts(self) -> None:
        api = _MockApi()
        api.Contracts.Futures.add_group("TMF", [
            _MockContract(code="TMFH6", delivery_date="2026/09/16"),
            _MockContract(code="TMFR1", delivery_date="2050/12/31"),
            _MockContract(code="TMFI6", delivery_date="2026/10/21"),
            _MockContract(code="TMFR2", delivery_date="2050/12/31"),
        ])
        resolver = MarketDataContractResolver(
            api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("TMF")
        assert result is not None
        assert result.near_code == "TMFH6"
        assert result.far_code == "TMFI6"


class TestResolveGroupScanFallback:
    def test_scan_finds_tmf_across_groups(self, mixed_api: _MockApi) -> None:
        resolver = MarketDataContractResolver(
            mixed_api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("TMF")
        assert result is not None
        assert result.near_code == "TMFH6"
        assert result.far_code == "TMFI6"

    def test_scan_no_contracts_returns_none(self, empty_api: _MockApi) -> None:
        resolver = MarketDataContractResolver(
            empty_api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("MTX")
        assert result is None

    def test_scan_deduplicates_contracts(self) -> None:
        api = _MockApi()
        api.Contracts.Futures.add_group("MXF", [
            _MockContract(code="TMFH6", delivery_date="2026/09/16"),
            _MockContract(code="TMFI6", delivery_date="2026/10/21"),
        ])
        # No exact TMF group — forces scan fallback
        resolver = MarketDataContractResolver(
            api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("TMF")
        assert result is not None
        assert result.near_code == "TMFH6"
        assert result.far_code == "TMFI6"


class TestParseDelivery:
    def test_parses_valid_date(self) -> None:
        dt = MarketDataContractResolver._parse_delivery("2026/09/16")
        assert dt is not None
        assert dt == datetime(2026, 9, 16)

    def test_parses_empty_string(self) -> None:
        assert MarketDataContractResolver._parse_delivery("") is None

    def test_parses_none(self) -> None:
        assert MarketDataContractResolver._parse_delivery(None) is None  # type: ignore[arg-type]

    def test_parses_invalid_format(self) -> None:
        assert MarketDataContractResolver._parse_delivery("16/09/2026") is None


class TestTradingDateInjection:
    def test_uses_provided_trading_date(self) -> None:
        api = _MockApi()
        api.Contracts.Futures.add_group("TMF", [
            _MockContract(code="TMFH6", delivery_date="2026/09/16"),
            _MockContract(code="TMFI6", delivery_date="2026/10/21"),
        ])

        resolver = MarketDataContractResolver(
            api,
            trading_date_provider=_fixed_date_provider(date(2026, 10, 22)),
        )
        result = resolver.resolve_near_far("TMF")
        assert result is None  # both expired

    def test_defaults_to_today(self) -> None:
        api = _MockApi()
        api.Contracts.Futures.add_group("TMF", [
            _MockContract(code="TMFH6", delivery_date="2100/09/16"),
        ])
        resolver = MarketDataContractResolver(api)
        # Should not crash — uses date.today() as default
        result = resolver.resolve_near_far("TMF")
        assert result is None  # only 1 contract


class TestResolvedContracts:
    def test_frozen(self) -> None:
        raw = object()
        r = ResolvedContracts(near_raw=raw, far_raw=raw, near_code="A", far_code="B")
        assert r.near_code == "A"
        assert r.far_code == "B"
        with pytest.raises(AttributeError):
            r.near_code = "X"  # type: ignore[misc]


class TestNoShioajiAtRuntime:
    def test_resolver_handles_missing_product_key_gracefully(self) -> None:
        api = _MockApi()
        api.Contracts.Futures.add_group("TMF", [
            _MockContract(code="TMFH6", delivery_date="2026/09/16"),
        ])
        # No ZZZ group exists
        resolver = MarketDataContractResolver(
            api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("ZZZ")
        assert result is None  # scan also finds nothing


class TestMXFContinuousResolution:
    def test_resolves_mxf_via_continuous(self) -> None:
        api = _MockApi()
        # Old-style Futures groups (Shioaji 1.5.5)
        api.Contracts.Futures.add_group("MXF", [
            _MockContract(code="MXFR1", delivery_date="2026/08/19"),
            _MockContract(code="MXFR2", delivery_date="2026/09/16"),
        ])
        api.Contracts.Futures.add_group("MXFC", [
            _MockContract(code="MXFH6", delivery_date="2026/08/19"),
            _MockContract(code="MXFI6", delivery_date="2026/09/16"),
            _MockContract(code="MXFR1", delivery_date="2026/08/19"),  # continuous in target
            _MockContract(code="MXFR2", delivery_date="2026/09/16"),
        ])

        resolver = MarketDataContractResolver(
            api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("MXF")
        assert result is not None
        # Without target_code, falls back to continuous contract code
        assert result.near_code == "MXFR1"
        assert result.far_code == "MXFR2"

    def test_resolves_mtx_alias_to_mxf(self) -> None:
        api = _MockApi()
        api.Contracts.Futures.add_group("MXF", [
            _MockContract(code="MXFR1", delivery_date="2026/08/19"),
            _MockContract(code="MXFR2", delivery_date="2026/09/16"),
        ])
        api.Contracts.Futures.add_group("MXFC", [
            _MockContract(code="MXFH6", delivery_date="2026/08/19"),
            _MockContract(code="MXFI6", delivery_date="2026/09/16"),
            _MockContract(code="MXFR1", delivery_date="2026/08/19"),
            _MockContract(code="MXFR2", delivery_date="2026/09/16"),
        ])

        resolver = MarketDataContractResolver(
            api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("MTX")
        assert result is not None
        assert result.near_code == "MXFR1"

    def test_continuous_not_available_returns_none(self) -> None:
        api = _MockApi()
        resolver = MarketDataContractResolver(
            api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("MXF")
        assert result is None

    def test_continuous_missing_target_falls_back_to_code(self) -> None:
        api = _MockApi()
        api.Contracts.Futures.add_group("MXF", [
            _MockContract(code="MXFR1", delivery_date="2026/08/19"),
            _MockContract(code="MXFR2", delivery_date="2026/09/16"),
        ])
        resolver = MarketDataContractResolver(
            api,
            trading_date_provider=_fixed_date_provider(date(2026, 7, 1)),
        )
        result = resolver.resolve_near_far("MXF")
        assert result is not None
        assert result.near_code == "MXFR1"
        assert result.far_code == "MXFR2"
