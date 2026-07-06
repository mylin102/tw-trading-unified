from core.futures_bar_regime import (
    FuturesBarRegimeConfig,
    classify_futures_bar_regime,
    describe_futures_bar_regime,
)
from core.market_regime import MarketRegime


def _row(**overrides):
    row = {
        "adx": 18.0,
        "breakout_strength": 0.20,
        "price_vs_vwap": 0.0,
        "trend_strength_raw": 0.0,
        "sqz_on": False,
        "in_pb_zone": False,
        "in_bear_pb_zone": False,
        "in_bull_pb_zone": False,
        "bull_align": False,
        "bullish_align": False,
        "bear_align": False,
        "bearish_align": False,
        "opening_bullish": False,
        "opening_bearish": False,
        "close": 100.0,
        "ema_fast": 100.0,
        "ema_slow": 100.0,
        "volume_spike": 0.0,
    }
    row.update(overrides)
    return row


def test_classify_squeeze_when_compressed_without_trend_confirmation():
    result = classify_futures_bar_regime(
        _row(
            sqz_on=True,
            adx=20.0,  # must be < adx_trend_threshold(22) to trigger SQUEEZE
            bear_align=True,
            bearish_align=True,
            opening_bearish=True,
            price_vs_vwap=-0.001,
            close=99.0,
            ema_fast=100.0,
            ema_slow=101.0,
            trend_strength_raw=-0.001,
        )
    )

    assert result.regime == "SQUEEZE"
    assert result.bias == "SHORT"


def test_classify_stretched_when_far_from_vwap_inside_pullback_zone():
    result = classify_futures_bar_regime(
        _row(
            in_bear_pb_zone=True,
            price_vs_vwap=-0.0042,
            bear_align=True,
            bearish_align=True,
            opening_bearish=True,
            close=98.5,
            ema_fast=99.5,
            ema_slow=100.5,
            trend_strength_raw=-0.0015,
        )
    )

    assert result.regime == "STRETCHED"
    assert result.bias == "SHORT"


def test_classify_trend_long_when_breakout_is_confirmed():
    result = classify_futures_bar_regime(
        _row(
            adx=33.0,
            is_structural_breakout=1,  # V-Model V2 需要結構突破
            breakout_strength=0.72,
            price_vs_vwap=0.0015,
            bull_align=True,
            bullish_align=True,
            opening_bullish=True,
            close=102.0,
            ema_fast=101.0,
            ema_slow=100.0,
            trend_strength_raw=0.002,
            volume_spike=1.5,
        ),
        session_regime=MarketRegime.TRENDING,
    )

    assert result.regime == "TREND"
    assert result.bias == "LONG"
    assert result.session_regime == "TRENDING"
    assert result.confidence > 0.85


def test_classify_weak_when_directional_pressure_exists_but_breakout_missing():
    result = classify_futures_bar_regime(
        _row(
            adx=24.0,
            breakout_strength=0.30,
            price_vs_vwap=0.0010,
            bull_align=True,
            bullish_align=True,
            close=101.0,
            ema_fast=100.5,
            ema_slow=100.0,
            trend_strength_raw=0.0012,
            volume_spike=1.2,
        ),
        session_regime="TRENDING",
    )

    assert result.regime == "WEAK"
    assert result.bias == "LONG"
    assert "breakout confirmation is incomplete" in " ".join(result.reasons)


def test_classify_neutral_bias_when_evidence_is_balanced():
    result = classify_futures_bar_regime(
        _row(
            bull_align=True,
            bear_align=True,
            price_vs_vwap=0.0,
            close=100.0,
            ema_fast=100.0,
            ema_slow=100.0,
        ),
        config=FuturesBarRegimeConfig(min_alignment_score=2),
    )

    assert result.bias == "NEUTRAL"
    assert result.regime == "WEAK"


def test_describe_futures_bar_regime_includes_core_fields():
    result = classify_futures_bar_regime(
        _row(
            adx=31.0,
            is_structural_breakout=-1,  # V-Model V2 需要結構突破
            bear_breakout_strength=0.65,  # bear 結構突破強度
            breakout_strength=0.65,
            price_vs_vwap=-0.0015,
            bear_align=True,
            bearish_align=True,
            opening_bearish=True,
            close=98.0,
            ema_fast=99.0,
            ema_slow=100.0,
            trend_strength_raw=-0.002,
        ),
        session_regime="choppy",
    )

    text = describe_futures_bar_regime(result)

    assert "regime=WEAK" in text
    assert "bias=SHORT" in text
    assert "session=CHOPPY" in text
