# 2026-07-27 Gemini CLI: UI MTS Manual Entry Mapping & Semantic Invariant Tests
import pytest
from pathlib import Path


def get_mts_manual_entry_mapping(spread_z: float, near_ticker: str = "TMFH6", far_ticker: str = "TMFI6") -> dict:
    """Helper representing the exact UI mapping logic in ui/dashboard.py."""
    side = "SELL_NEAR_BUY_FAR" if spread_z >= 0 else "BUY_NEAR_SELL_FAR"
    action_label = "賣出價差 (SELL_SPREAD)" if spread_z >= 0 else "買進價差 (BUY_SPREAD)"
    near_action = "SELL" if spread_z >= 0 else "BUY"
    far_action = "BUY" if spread_z >= 0 else "SELL"
    return {
        "side": side,
        "action_label": action_label,
        "near_leg": f"{near_ticker} {near_action} 1",
        "far_leg": f"{far_ticker} {far_action} 1",
        "near_action": near_action,
        "far_action": far_action,
        "spread_z": spread_z,
        "spread_definition": "Near - Far",
    }


def test_mts_manual_entry_positive_z():
    # Z >= 0 -> SELL_NEAR_BUY_FAR
    m = get_mts_manual_entry_mapping(1.24, "TMFH6", "TMFI6")
    assert m["side"] == "SELL_NEAR_BUY_FAR"
    assert m["near_action"] == "SELL"
    assert m["far_action"] == "BUY"
    assert m["near_leg"] == "TMFH6 SELL 1"
    assert m["far_leg"] == "TMFI6 BUY 1"


def test_mts_manual_entry_negative_z():
    # Z < 0 -> BUY_NEAR_SELL_FAR
    m = get_mts_manual_entry_mapping(-1.50, "TMFH6", "TMFI6")
    assert m["side"] == "BUY_NEAR_SELL_FAR"
    assert m["near_action"] == "BUY"
    assert m["far_action"] == "SELL"
    assert m["near_leg"] == "TMFH6 BUY 1"
    assert m["far_leg"] == "TMFI6 SELL 1"


def test_mts_manual_entry_zero_z():
    # Z == 0 -> SELL_NEAR_BUY_FAR
    m = get_mts_manual_entry_mapping(0.0, "TMFH6", "TMFI6")
    assert m["side"] == "SELL_NEAR_BUY_FAR"
    assert m["near_action"] == "SELL"
    assert m["far_action"] == "BUY"


def test_dashboard_no_hardcoded_misleading_button_text():
    dashboard_path = Path(__file__).parent.parent.parent / "ui" / "dashboard.py"
    content = dashboard_path.read_text(encoding="utf-8")
    assert "強制賣出價差" not in content
    assert "強制買進價差" not in content
    assert "MTS 手動建倉" in content
