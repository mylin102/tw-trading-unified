"""
Tests for core/derivatives/surface_engine.py

Verifies:
- surface_snapshot() returns valid IV data from quote_store
- surface_snapshot() handles insufficient data gracefully
- surface_snapshot() is backward compatible with compute_if_ready()
- DTE parsing from expiry strings
"""

import datetime
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.derivatives.surface_engine import OptionSurfaceEngine
from core.derivatives.models import OptionQuoteEvent, SurfaceSnapshot


def _quote(opt_type, strike, mid, expiry="202606"):
    """Helper to create an OptionQuoteEvent."""
    return OptionQuoteEvent(
        timestamp=datetime.datetime(2026, 5, 17, 10, 30, 0),
        symbol=f"TXO{int(strike)}{opt_type[0]}6",
        option_type=opt_type,
        strike=strike,
        bid=max(mid - 1, 0.5),
        ask=mid + 1,
        mid=mid,
        expiry=expiry,
    )


def test_surface_snapshot_valid():
    """surface_snapshot returns valid data when all three strikes are present."""
    engine = OptionSurfaceEngine(otm_points=300)

    # Simulate a TXO scenario: futures ~34000, strikes at 33700 (OTM put), 34000 (ATM), 34300 (OTM call)
    engine.on_quote(_quote("PUT", 33700, 120.0))   # OTM put
    engine.on_quote(_quote("PUT", 34000, 250.0))   # ATM put
    engine.on_quote(_quote("CALL", 34000, 250.0))  # ATM call
    engine.on_quote(_quote("CALL", 34300, 80.0))   # OTM call

    snapshot = engine.surface_snapshot(futures_price=34000.0)

    assert isinstance(snapshot, SurfaceSnapshot), "Should return SurfaceSnapshot"
    assert snapshot.is_valid(), "Should be valid with all three strikes"
    assert snapshot.atm_iv > 0, "ATM IV should be positive"
    assert snapshot.otm_put_iv > 0, "OTM put IV should be positive"
    assert snapshot.otm_call_iv > 0, "OTM call IV should be positive"
    assert snapshot.underlying_price == 34000.0
    assert snapshot.dte > 0, "DTE should be > 0"


def test_surface_snapshot_otm_both_sides():
    """OTM put and call IV should differ from ATM IV due to skew."""
    engine = OptionSurfaceEngine(otm_points=300)

    # Simulate left-skew: OTM put expensive, OTM call cheap
    engine.on_quote(_quote("PUT", 33700, 300.0))   # OTM put has high premium (fear)
    engine.on_quote(_quote("PUT", 34000, 250.0))   # ATM
    engine.on_quote(_quote("CALL", 34000, 250.0))  # ATM
    engine.on_quote(_quote("CALL", 34300, 60.0))   # OTM call cheap

    snapshot = engine.surface_snapshot(futures_price=34000.0)
    assert snapshot.is_valid()
    # OTM put IV should be higher than both ATM and OTM call IV
    assert snapshot.otm_put_iv > snapshot.atm_iv, (
        f"OTM put IV ({snapshot.otm_put_iv}) should exceed ATM IV ({snapshot.atm_iv}) in left-skew"
    )
    assert snapshot.otm_call_iv < snapshot.otm_put_iv, (
        f"OTM call IV ({snapshot.otm_call_iv}) should be less than OTM put IV ({snapshot.otm_put_iv})"
    )


def test_surface_snapshot_insufficient_data():
    """surface_snapshot returns invalid when too few strikes."""
    engine = OptionSurfaceEngine(otm_points=300)
    engine.on_quote(_quote("PUT", 34000, 250.0))
    # Only ATM put — no call, no OTM
    snapshot = engine.surface_snapshot(futures_price=34000.0)
    assert isinstance(snapshot, SurfaceSnapshot)
    # At least one IV will be 0, so is_valid should be False
    # (atm_iv > 0 since we have ATM put, but otm_call_iv = 0)
    assert not snapshot.is_valid(), "Should be invalid with only one strike"


def test_surface_snapshot_empty_store():
    """Empty quote_store → all IV values zero."""
    engine = OptionSurfaceEngine(otm_points=300)
    snapshot = engine.surface_snapshot(futures_price=34000.0)
    assert snapshot.atm_iv == 0
    assert snapshot.otm_put_iv == 0
    assert snapshot.otm_call_iv == 0
    assert not snapshot.is_valid()


def test_compute_if_ready_still_works():
    """Backward compat: compute_if_ready unchanged after surface_snapshot addition."""
    engine = OptionSurfaceEngine(otm_points=300)
    engine.on_quote(_quote("PUT", 33700, 120.0))
    engine.on_quote(_quote("PUT", 34000, 250.0))
    engine.on_quote(_quote("CALL", 34000, 250.0))
    engine.on_quote(_quote("CALL", 34300, 80.0))

    signal = engine.compute_if_ready(futures_price=34000.0, force=True)
    assert signal is not None
    assert signal.direction != "UNKNOWN", "Should compute signal"

    # surface_snapshot should also work independently
    snapshot = engine.surface_snapshot(futures_price=34000.0)
    assert snapshot.is_valid()


def test_dte_parsing_yyyymm():
    """DTE parsing from YYYYMM expiry string."""
    engine = OptionSurfaceEngine(otm_points=300)
    now = datetime.datetime(2026, 5, 17, 10, 30, 0)
    # Expiry in June 2026 → ~29 days from May 17
    engine.on_quote(_quote("PUT", 34000, 250.0, expiry="202606"))
    engine.on_quote(_quote("PUT", 33700, 120.0, expiry="202606"))
    engine.on_quote(_quote("CALL", 34300, 80.0, expiry="202606"))

    snapshot = engine.surface_snapshot(futures_price=34000.0, timestamp=now)
    assert snapshot.dte > 10, f"DTE should be ~29 days, got {snapshot.dte}"
    assert snapshot.dte < 40, f"DTE too large: {snapshot.dte}"


def test_dte_parsing_iso_date():
    """DTE parsing from ISO date string (YYYY-MM-DD)."""
    engine = OptionSurfaceEngine(otm_points=300)
    now = datetime.datetime(2026, 5, 17, 10, 30, 0)
    engine.on_quote(_quote("PUT", 34000, 250.0, expiry="2026-06-17"))
    engine.on_quote(_quote("PUT", 33700, 120.0, expiry="2026-06-17"))
    engine.on_quote(_quote("CALL", 34300, 80.0, expiry="2026-06-17"))

    snapshot = engine.surface_snapshot(futures_price=34000.0, timestamp=now)
    assert 20 < snapshot.dte < 40, f"DTE should be ~31 days, got {snapshot.dte}"
