#!/usr/bin/env python3
"""Tests for strategies.futures.market_data_collector."""

from typing import Any
import threading
import pytest

from strategies.futures.market_data_collector import (
    ContractInfo,
    ContractResolutionError,
    MarketDataCollector,
    MarketDataSnapshot,
    UnknownLegError,
)


# ── Helpers ──

class _MockContract:
    def __init__(self, code: str, delivery_date: str = "2026/09/16"):
        self.code = code
        self.delivery_date = delivery_date


class _MockTick:
    def __init__(self, close: float | None = None, code: str = "MTXH6"):
        self.close = close
        self.code = code


class _MockBidAsk:
    def __init__(self, price: float | None = None, code: str = "MTXH6"):
        self.price = price
        self.code = code


def _make_resolver(near_code: str = "MTXH6", far_code: str = "MTXI6"):
    """Return a ContractResolverFn that returns fixed contracts."""

    def _resolve(ticker: str) -> tuple[Any, Any]:
        return (
            _MockContract(code=near_code),
            _MockContract(code=far_code),
        )
    return _resolve


def _make_failing_resolver():
    """Return a resolver that always returns None."""
    def _resolve(ticker: str) -> tuple[None, None]:
        return None, None
    return _resolve


def _make_exploding_resolver():
    """Return a resolver that always raises."""
    def _resolve(ticker: str) -> Any:
        raise RuntimeError("resolver exploded")
    return _resolve


# ── Tests ──

class TestCollectorInitialization:
    def test_ticker_is_upper_cased(self) -> None:
        c = MarketDataCollector(ticker="mtx")
        assert c.ticker == "MTX"

    def test_initial_state_is_empty(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        assert c.near_contract is None
        assert c.far_contract is None
        assert c.near_last is None
        assert c.far_last is None
        assert c.generation == 0
        assert c.is_resolved is False

    def test_default_repr(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        r = repr(c)
        assert "MTX" in r
        assert "resolved=False" in r
        assert "generation=0" in r


class TestContractResolution:
    def test_resolve_success(self) -> None:
        c = MarketDataCollector(ticker="MTX", resolver=_make_resolver())
        assert c.resolve_contracts() is True
        assert c.is_resolved is True
        assert c.near_contract is not None
        assert c.near_contract.code == "MTXH6"
        assert c.far_contract is not None
        assert c.far_contract.code == "MTXI6"

    def test_resolve_failure_returns_false(self) -> None:
        c = MarketDataCollector(ticker="MTX", resolver=_make_failing_resolver())
        assert c.resolve_contracts() is False
        assert c.is_resolved is False

    def test_resolve_exploding_resolver_raises(self) -> None:
        c = MarketDataCollector(ticker="MTX", resolver=_make_exploding_resolver())
        with pytest.raises(ContractResolutionError, match="resolver exploded"):
            c.resolve_contracts()

    def test_resolve_no_resolver_returns_false(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        assert c.resolve_contracts() is False

    def test_resolve_is_idempotent(self) -> None:
        c = MarketDataCollector(ticker="MTX", resolver=_make_resolver())
        assert c.resolve_contracts() is True
        assert c.resolve_contracts() is True  # second call is no-op


class TestTickIngestion:
    def test_near_tick_updates_price(self) -> None:
        c = MarketDataCollector(ticker="MTX", resolver=_make_resolver())
        c.resolve_contracts()
        tick = _MockTick(close=43150.0)

        c.on_tick("near", tick)

        assert c.near_last == 43150.0
        assert c.generation == 1

    def test_far_tick_updates_price(self) -> None:
        c = MarketDataCollector(ticker="MTX", resolver=_make_resolver())
        c.on_tick("far", _MockTick(close=43300.0))

        assert c.far_last == 43300.0
        assert c.generation == 1

    def test_multiple_ticks_increment_generation(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        for price in [43100, 43150, 43200]:
            c.on_tick("near", _MockTick(close=float(price)))

        assert c.generation == 3
        assert c.near_last == 43200.0

    def test_near_and_far_ticks(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        c.on_tick("near", _MockTick(close=43100.0))
        c.on_tick("far", _MockTick(close=43300.0))

        assert c.near_last == 43100.0
        assert c.far_last == 43300.0
        assert c.generation == 2

    def test_unknown_leg_raises(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        with pytest.raises(UnknownLegError, match="Unknown leg: 'middle'"):
            c.on_tick("middle", _MockTick(close=100.0))

    def test_skips_tick_with_no_price(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        c.on_tick("near", _MockTick(close=None))
        assert c.near_last is None
        assert c.generation == 0

    def test_accepts_bidask_price(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        c.on_tick("near", _MockBidAsk(price=43150.0))
        assert c.near_last == 43150.0

    def test_on_tick_before_resolve(self) -> None:
        """Tick ingestion works even before contract resolution."""
        c = MarketDataCollector(ticker="MTX")
        c.on_tick("near", _MockTick(close=43100.0))
        assert c.near_last == 43100.0


class TestSnapshot:
    def test_snapshot_is_frozen(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        snap = c.snapshot_for_persistence()
        assert isinstance(snap, MarketDataSnapshot)
        with pytest.raises(AttributeError):
            snap.ticker = "OTHER"  # type: ignore[misc]

    def test_snapshot_contains_generation(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        c.on_tick("near", _MockTick(close=43100.0))
        c.on_tick("far", _MockTick(close=43300.0))

        snap = c.snapshot_for_persistence()
        assert snap.generation == 2
        assert snap.ticker == "MTX"

    def test_snapshot_contains_prices(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        c.on_tick("near", _MockTick(close=43100.0))
        c.on_tick("far", _MockTick(close=43300.0))

        snap = c.snapshot_for_persistence()
        assert snap.near_last == 43100.0
        assert snap.far_last == 43300.0

    def test_snapshot_has_data_flag(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        assert c.snapshot_for_persistence().has_data is False
        c.on_tick("near", _MockTick(close=43100.0))
        c.on_tick("far", _MockTick(close=43300.0))
        assert c.snapshot_for_persistence().has_data is True

    def test_snapshot_spread(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        c.on_tick("near", _MockTick(close=43150.0))
        c.on_tick("far", _MockTick(close=43300.0))
        assert c.snapshot_for_persistence().spread == -150.0

    def test_snapshot_is_immutable_copy(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        snap1 = c.snapshot_for_persistence()
        c.on_tick("near", _MockTick(close=43100.0))
        snap2 = c.snapshot_for_persistence()
        assert snap1.generation == 0
        assert snap2.generation == 1
        assert snap1.near_last is None
        assert snap2.near_last == 43100.0

    def test_snapshot_contract_info(self) -> None:
        c = MarketDataCollector(ticker="MTX", resolver=_make_resolver())
        c.resolve_contracts()
        snap = c.snapshot_for_persistence()
        assert snap.near_contract is not None
        assert snap.near_contract.code == "MTXH6"
        assert snap.far_contract is not None
        assert snap.far_contract.code == "MTXI6"


class TestSafety:
    def test_no_order_capability(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        assert c.has_no_order_capability is True

    def test_no_strategy_attribute(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        assert not hasattr(c, "_lifecycle")
        assert not hasattr(c, "_has_position")
        assert not hasattr(c, "_lifecycle_oca")

    def test_no_submit_method(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        assert not hasattr(c, "submit")
        assert not hasattr(c, "place_order")

    def test_lock_attribute_exists(self) -> None:
        c = MarketDataCollector(ticker="MTX")
        assert hasattr(c, "_lock")
        assert hasattr(c._lock, "acquire")
        assert hasattr(c._lock, "release")


class TestConcurrency:
    def test_concurrent_on_tick_and_snapshot(self) -> None:
        """Rapid concurrent tick+snpashot should not crash or corrupt."""
        import concurrent.futures

        c = MarketDataCollector(ticker="MTX")

        def writer() -> None:
            for _ in range(100):
                snap = c.snapshot_for_persistence()
                assert snap is not None

        def ticker() -> None:
            for i in range(100):
                c.on_tick("near", _MockTick(close=float(43000 + i)))
                c.on_tick("far", _MockTick(close=float(43200 + i)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(fn) for fn in [writer, writer, ticker, ticker]]
            concurrent.futures.wait(futs)
            for f in futs:
                f.result()  # re-raise if any failed

        final = c.snapshot_for_persistence()
        assert final.generation > 0
        assert final.near_last is not None
        assert final.far_last is not None
