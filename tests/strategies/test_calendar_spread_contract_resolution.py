"""Tests for update_calendar_spread contract resolution (triple-layer fallback).

Tests are isolated — they do not import tmf_spread or call Shioaji.
They use a fake API object to verify the get_near_far resolver logic.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure the repo root is on sys.path for imports
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# We need to test get_near_far which is a module-level function.
# The easiest approach is to import the script's function directly.
# Since update_calendar_spread uses __name__ == '__main__' guard,
# we can import it safely.
import importlib.util as iu
_spec = iu.spec_from_file_location(
    "update_calendar_spread",
    str(_repo_root / "scripts" / "update_calendar_spread.py"),
)
_mod = iu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
get_near_far = _mod.get_near_far


def _make_fake_api(category_key_present: bool = True, contracts: list | None = None):
    """Create a fake Shioaji API object for testing get_near_far resolution."""
    api = MagicMock()

    # Build Contracts.Futures structure
    if contracts is None:
        contracts = [
            MagicMock(code="TMFH6", symbol="TMF202608", delivery_date="2026/08/19"),
            MagicMock(code="TMFI6", symbol="TMF202609", delivery_date="2026/09/16"),
        ]

    # Simulate group scan: Futures is iterable
    class FakeFuturesCategory:
        def __init__(self, key_present: bool, contracts_list: list):
            self._key_present = key_present
            self._contracts = contracts_list

        def __contains__(self, key: str) -> bool:
            return self._key_present

        def __getitem__(self, key: str) -> list:
            if not self._key_present:
                raise KeyError(key)
            return self._contracts

        def __iter__(self):
            yield self._contracts

    api.Contracts.Futures = FakeFuturesCategory(
        key_present=category_key_present, contracts_list=contracts
    )
    return api


def test_tmf_resolves_tmf_only():
    """TMF request should only accept contracts with TMF prefix."""
    api = _make_fake_api(category_key_present=True)
    near, far = get_near_far(api, category="TMF")
    assert near is not None, "TMF should resolve near contract"
    assert far is not None, "TMF should resolve far contract"
    assert near.code.startswith("TMF")
    assert far.code.startswith("TMF")


def test_mtx_resolves_mtx_only():
    """MTX request should only accept contracts with MTX prefix."""
    mtx_contracts = [
        MagicMock(code="MTXH6", symbol="MTX202608", delivery_date="2026/08/19"),
        MagicMock(code="MTXI6", symbol="MTX202609", delivery_date="2026/09/16"),
    ]
    api = _make_fake_api(category_key_present=True, contracts=mtx_contracts)
    near, far = get_near_far(api, category="MTX")
    assert near is not None, "MTX should resolve near contract"
    assert far is not None, "MTX should resolve far contract"
    assert near.code.startswith("MTX")
    assert far.code.startswith("MTX")


def test_unknown_ticker_fails_closed():
    """Unknown ticker should return (None, None) — no CSV written."""
    api = _make_fake_api(category_key_present=False)
    near, far = get_near_far(api, category="UNKNOWN")
    assert near is None
    assert far is None


def test_empty_category_fails_closed():
    """Empty or None category should fail closed."""
    api = _make_fake_api(category_key_present=False)
    near, far = get_near_far(api, category="")
    assert near is None
    assert far is None


def test_fallback_does_not_cross_ticker():
    """Group scan fallback must not return MTX contracts when TMF is requested.
    
    Tests Layer 3 (group scan) specifically by making Layer 1 fail."""
    tmf_contracts = [
        MagicMock(code="TMFH6", symbol="TMF202608", delivery_date="2026/08/19"),
        MagicMock(code="TMFI6", symbol="TMF202609", delivery_date="2026/09/16"),
    ]
    mtx_contracts = [
        MagicMock(code="MTXH6", symbol="MTX202608", delivery_date="2026/08/19"),
        MagicMock(code="MTXI6", symbol="MTX202609", delivery_date="2026/09/16"),
    ]

    # Build a fake Futures that:
    # - Layer 1: "TMF" NOT in Futures (forces fallback)
    # - Layer 3 group scan: iterates over both TMF and MTX groups
    class FakeFuturesGroup:
        def __init__(self):
            self._groups = [tmf_contracts, mtx_contracts]

        def __contains__(self, key):
            return False  # Force Layer 2+3

        def __getitem__(self, key):
            raise KeyError(key)

        def __iter__(self):
            return iter(self._groups)

    api = MagicMock()
    api.Contracts.Futures = FakeFuturesGroup()
    api.fetch_contracts = MagicMock()

    near, far = get_near_far(api, category="TMF")
    assert near is not None, "TMF should resolve via group scan"
    if near is not None:
        assert near.code.startswith("TMF"), f"Expected TMF, got {near.code}"
    if far is not None:
        assert far.code.startswith("TMF"), f"Expected TMF, got {far.code}"
