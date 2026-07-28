# 2026-07-28: Shared Quote Coherence Contract Tests
# Tests for evaluate_quote_coherence — the shared contract used by
# both CombinedUplTrailPolicy and PolicyJShadowEvaluator.
import pytest

from strategies.futures.mts.quote_coherence import (
    QuoteCoherenceInput,
    QuoteCoherenceReason,
    evaluate_quote_coherence,
)


def test_fresh_coherent_quotes():
    """Fresh, present quotes with valid PnL → READY."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_quote_age_ms=10,
        far_quote_age_ms=20,
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        max_quote_age_ms=1000,
        gross_pnl=500.0,
    ))
    assert result.fresh
    assert result.coherent
    assert result.reason == QuoteCoherenceReason.READY.value


def test_not_spread_phase():
    """Non-SPREAD phase → POSITION_INCOMPLETE."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        is_spread_phase=False,
    ))
    assert not result.fresh
    assert not result.coherent
    assert result.reason == QuoteCoherenceReason.POSITION_INCOMPLETE.value


def test_position_incomplete():
    """Missing position → POSITION_INCOMPLETE."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        is_spread_phase=True,
        near_open_qty=0,
        far_open_qty=0,
    ))
    assert not result.coherent
    assert result.reason == QuoteCoherenceReason.POSITION_INCOMPLETE.value


def test_exit_inflight():
    """Exit inflight → not coherent."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        has_exit_inflight=True,
    ))
    assert not result.coherent
    assert result.reason == QuoteCoherenceReason.PAIR_SKEW.value


def test_both_quotes_missing():
    """Both legs missing quotes → BOTH_STALE."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        near_quote_age_ms=None,
        far_quote_age_ms=None,
    ))
    assert not result.fresh
    assert result.near_missing
    assert result.far_missing
    assert result.reason == QuoteCoherenceReason.BOTH_STALE.value


def test_near_quote_missing():
    """Only near missing → NEAR_QUOTE_MISSING."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        near_quote_age_ms=None,
        far_quote_age_ms=10,
        max_quote_age_ms=1000,
    ))
    assert not result.fresh
    assert result.near_missing
    assert not result.far_missing
    assert result.reason == QuoteCoherenceReason.NEAR_QUOTE_MISSING.value


def test_far_quote_missing():
    """Only far missing → FAR_QUOTE_MISSING."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        near_quote_age_ms=10,
        far_quote_age_ms=None,
        max_quote_age_ms=1000,
    ))
    assert not result.fresh
    assert not result.near_missing
    assert result.far_missing
    assert result.reason == QuoteCoherenceReason.FAR_QUOTE_MISSING.value


def test_near_quote_stale():
    """Near stale (age > max) → NEAR_STALE."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        near_quote_age_ms=9999,
        far_quote_age_ms=10,
        max_quote_age_ms=1000,
    ))
    assert not result.fresh
    assert result.near_stale
    assert not result.far_stale
    assert result.reason == QuoteCoherenceReason.NEAR_STALE.value


def test_far_quote_stale():
    """Far stale → FAR_STALE."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        near_quote_age_ms=10,
        far_quote_age_ms=9999,
        max_quote_age_ms=1000,
    ))
    assert not result.fresh
    assert not result.near_stale
    assert result.far_stale
    assert result.reason == QuoteCoherenceReason.FAR_STALE.value


def test_both_quotes_stale():
    """Both stale → BOTH_STALE."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        near_quote_age_ms=9999,
        far_quote_age_ms=8888,
        max_quote_age_ms=1000,
    ))
    assert not result.fresh
    assert result.near_stale
    assert result.far_stale
    assert result.reason == QuoteCoherenceReason.BOTH_STALE.value


def test_pnl_invalid():
    """None PnL → PNL_INVALID."""
    result = evaluate_quote_coherence(QuoteCoherenceInput(
        near_open_qty=1,
        far_open_qty=1,
        is_spread_phase=True,
        near_quote_age_ms=10,
        far_quote_age_ms=20,
        max_quote_age_ms=1000,
        gross_pnl=None,
    ))
    assert result.fresh
    assert not result.coherent
    assert result.reason == QuoteCoherenceReason.PNL_INVALID.value
