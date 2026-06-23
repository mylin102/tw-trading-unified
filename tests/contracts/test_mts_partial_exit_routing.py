"""
Contract: MTS PARTIAL_EXIT routing must use signal.reason, not released_leg.

released_leg = historical state / guard (is one leg already released?)
signal.reason = current action intent (which leg to release now)

Test matrix:

  released_leg=None + reason=TMF_RELEASE_NEAR → submit NEAR release
  released_leg=None + reason=TMF_RELEASE_FAR  → submit FAR release
  released_leg=near + reason=TMF_RELEASE_FAR  → block already released
  released_leg=None + unknown reason          → block unknown reason
"""
import json
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = __file__  # marker for test discovery


def _check_partial_exit_contract(strategy, reason) -> str:
    """
    Simulate the contract logic from _submit_mts_order_signal.

    Returns one of: "near", "far", "blocked", "ambiguous", "unknown", "schema_missing".
    """
    if not hasattr(strategy, "_released_leg"):
        return "schema_missing"

    if strategy._released_leg is not None:
        return "blocked"

    reason_str = (reason or "").upper()
    is_near = "RELEASE_NEAR" in reason_str
    is_far = "RELEASE_FAR" in reason_str

    if is_near and is_far:
        return "ambiguous"
    if is_near:
        return "near"
    if is_far:
        return "far"
    return "unknown"


class FakeStrategy:
    """Minimal TMFSpread stand-in with just _released_leg."""
    def __init__(self, released_leg=None):
        self._released_leg = released_leg
        self._near_side = "SHORT"
        self._far_side = "LONG"
        self._trade_id = "mts-test-001"


class TestMtsPartialExitRoutingContract:

    def test_release_near_when_none_released(self):
        """released_leg=None + TMF_RELEASE_NEAR → submit NEAR release"""
        result = _check_partial_exit_contract(
            FakeStrategy(released_leg=None),
            "TMF_RELEASE_NEAR",
        )
        assert result == "near", (
            f"Expected 'near', got '{result}'"
        )

    def test_release_far_when_none_released(self):
        """released_leg=None + TMF_RELEASE_FAR → submit FAR release"""
        result = _check_partial_exit_contract(
            FakeStrategy(released_leg=None),
            "TMF_RELEASE_FAR",
        )
        assert result == "far", (
            f"Expected 'far', got '{result}'"
        )

    def test_blocked_when_near_already_released(self):
        """released_leg=near + TMF_RELEASE_FAR → block already released"""
        result = _check_partial_exit_contract(
            FakeStrategy(released_leg="near"),
            "TMF_RELEASE_FAR",
        )
        assert result == "blocked", (
            f"Expected 'blocked', got '{result}'"
        )

    def test_blocked_when_far_already_released(self):
        """released_leg=far + TMF_RELEASE_NEAR → block already released"""
        result = _check_partial_exit_contract(
            FakeStrategy(released_leg="far"),
            "TMF_RELEASE_NEAR",
        )
        assert result == "blocked", (
            f"Expected 'blocked', got '{result}'"
        )

    def test_unknown_reason_returns_unknown(self):
        """released_leg=None + unknown reason → block unknown reason"""
        result = _check_partial_exit_contract(
            FakeStrategy(released_leg=None),
            "EXIT",
        )
        assert result == "unknown", (
            f"Expected 'unknown', got '{result}'"
        )

    def test_ambiguous_when_both_release_in_reason(self):
        """released_leg=None + reason containing both → block ambiguous"""
        result = _check_partial_exit_contract(
            FakeStrategy(released_leg=None),
            "BOTH_RELEASE_NEAR_AND_RELEASE_FAR",
        )
        assert result == "ambiguous", (
            f"Expected 'ambiguous', got '{result}'"
        )

    def test_schema_missing_when_no_released_leg_attr(self):
        """Strategy without _released_leg attr → schema missing"""
        strategy = object()
        result = _check_partial_exit_contract(strategy, "TMF_RELEASE_NEAR")
        assert result == "schema_missing", (
            f"Expected 'schema_missing', got '{result}'"
        )

