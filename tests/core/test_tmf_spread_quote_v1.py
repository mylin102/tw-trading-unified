from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import pytest

from core.market_data_registry import build_tmf_spread_quote


NOW = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)


def leg(code, bid=100.0, ask=101.0, quote_time=NOW, last=99999.0):
    return {
        "code": code,
        "bid": bid,
        "ask": ask,
        "quote_time": quote_time,
        # A misleading last price must never be used for executable spread.
        "last": last,
    }


def quote(near=None, far=None, **kwargs):
    kwargs.setdefault("max_quote_age_ms", 30000)
    kwargs.setdefault("max_leg_time_skew_ms", 500)
    return build_tmf_spread_quote(
        leg("TMFJ6", 100.0, 101.0) if near is None else near,
        leg("TMFK6", 90.0, 91.0) if far is None else far,
        now=NOW,
        session_confirmed=True,
        trading_date_confirmed=True,
        **kwargs,
    )


def test_valid_tmf_bbo_computes_executable_spreads_and_full_schema():
    result = quote()

    assert result.data_quality == "VALID"
    assert result.rejection_reason is None
    assert result.near_code == "TMFJ6"
    assert result.far_code == "TMFK6"
    assert result.near_bid == 100.0
    assert result.near_ask == 101.0
    assert result.far_bid == 90.0
    assert result.far_ask == 91.0
    # Sell Near/Buy Far: Near bid - Far ask; Buy Near/Sell Far: Near ask - Far bid.
    assert result.spread_bid == 9.0
    assert result.spread_ask == 11.0
    assert result.spread_mid == 10.0
    assert result.spread_width == 2.0
    assert result.tick_size == 1.0
    assert result.price_unit == "index_points"
    assert result.near_quote_age_ms == 0.0
    assert result.far_quote_age_ms == 0.0
    assert result.leg_time_skew_ms == 0.0


@pytest.mark.parametrize(
    "near,far,reason",
    [
        (leg("TMFJ6", bid=None), leg("TMFK6"), "MISSING_BIDASK"),
        (leg("TMFJ6"), leg("TMFK6", ask=None), "MISSING_BIDASK"),
        (leg("TMFJ6", bid=0.0), leg("TMFK6"), "INVALID_BIDASK"),
        (leg("TMFJ6"), leg("TMFK6", ask=0.0), "INVALID_BIDASK"),
        (leg("TMFJ6", bid=100.0, ask=0.0), leg("TMFK6"), "INVALID_BIDASK"),
        (leg("TMFJ6"), leg("TMFK6", bid=0.0, ask=91.0), "INVALID_BIDASK"),
        (leg("TMFJ6", bid=102.0, ask=101.0), leg("TMFK6"), "CROSSED_MARKET"),
    ],
)
def test_invalid_or_missing_bbo_is_blocked(near, far, reason):
    result = quote(near, far)

    assert result.data_quality == "BLOCKED"
    assert result.rejection_reason == reason
    assert result.spread_bid is None
    assert result.spread_ask is None


def test_missing_far_leg_is_blocked():
    result = build_tmf_spread_quote(
        leg("TMFJ6"),
        None,
        now=NOW,
        max_quote_age_ms=30000,
        max_leg_time_skew_ms=500,
        session_confirmed=True,
        trading_date_confirmed=True,
    )

    assert result.data_quality == "BLOCKED"
    assert result.rejection_reason == "MISSING_FAR_QUOTE"


def test_far_stale_is_blocked_and_old_far_price_is_not_reused():
    result = quote(
        far=leg("TMFK6", 90.0, 91.0, NOW - timedelta(seconds=3)),
        max_quote_age_ms=2000,
        max_leg_time_skew_ms=5000,
    )

    assert result.data_quality == "BLOCKED"
    assert result.rejection_reason == "STALE_QUOTE"
    assert result.far_quote_age_ms > 2000
    assert result.spread_bid is None
    assert result.spread_ask is None


def test_pair_skew_over_existing_sync_threshold_is_blocked():
    result = quote(far=leg("TMFK6", 90.0, 91.0, NOW - timedelta(milliseconds=501)))

    assert result.data_quality == "BLOCKED"
    assert result.rejection_reason == "QUOTE_SKEW_TOO_LARGE"
    assert result.leg_time_skew_ms == 501.0


def test_quote_age_comes_from_futures_config_not_the_old_default():
    config = Path("config/futures.yaml").read_text()
    configured = int(re.search(r"max_quote_age_ms:\s*(\d+)", config).group(1))
    assert configured == 30000
    result = quote(
        far=leg("TMFK6", 90.0, 91.0, NOW - timedelta(seconds=2.5)),
        max_leg_time_skew_ms=5000,
    )
    assert result.data_quality == "VALID"
    assert result.far_quote_age_ms == 2500.0


def test_spread_bid_and_ask_are_only_defined_in_canonical_s1_module():
    root = Path(__file__).parents[2]
    offenders = []
    for path in (root / "core", root / "strategies"):
        for source in path.rglob("*.py"):
            if source == root / "core" / "market_data_registry.py":
                continue
            text = source.read_text()
            if re.search(r"\bspread_(?:bid|ask)\s*=", text):
                offenders.append(str(source.relative_to(root)))
    assert offenders == []


def test_mtx_contract_cannot_mix_into_tmf_pair():
    result = quote(far=leg("MTXK6"))

    assert result.data_quality == "BLOCKED"
    assert result.rejection_reason == "CONTRACT_ALIAS_MISMATCH"


@pytest.mark.parametrize("session_confirmed,trading_date_confirmed,reason", [
    (False, True, "SESSION_UNCONFIRMED"),
    (True, False, "TRADING_DATE_UNCONFIRMED"),
])
def test_session_and_trading_date_must_be_confirmed(
    session_confirmed, trading_date_confirmed, reason
):
    result = build_tmf_spread_quote(
        leg("TMFJ6"),
        leg("TMFK6"),
        now=NOW,
        session_confirmed=session_confirmed,
        trading_date_confirmed=trading_date_confirmed,
    )

    assert result.data_quality == "BLOCKED"
    assert result.rejection_reason == reason
