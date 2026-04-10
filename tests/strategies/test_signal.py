"""Level 1 Unit Tests — core/signal.py"""
import pytest
from core.signal import Signal


class TestSignalValidation:
    """Verify Signal.validate() accepts valid signals and rejects bad ones."""

    def test_valid_buy(self):
        s = Signal("BUY", "TEST", 35000.0)
        ok, msg = s.validate()
        assert ok is True
        assert msg == ""

    def test_valid_sell(self):
        s = Signal("SELL", "TEST", 35200.0)
        ok, msg = s.validate()
        assert ok is True

    def test_valid_exit(self):
        s = Signal("EXIT", "SL", 0.0)
        ok, msg = s.validate()
        assert ok is True  # exit doesn't need stop_loss

    def test_valid_partial_exit(self):
        s = Signal("PARTIAL_EXIT", "TP1", 0.0)
        ok, msg = s.validate()
        assert ok is True

    def test_invalid_action(self):
        s = Signal("HOLD", "TEST", 35000.0)
        ok, msg = s.validate()
        assert ok is False
        assert "Invalid action" in msg

    def test_missing_reason(self):
        s = Signal("BUY", "", 35000.0)
        ok, msg = s.validate()
        assert ok is False
        assert "Missing reason" in msg

    def test_zero_stop_loss_buy(self):
        s = Signal("BUY", "TEST", 0.0)
        ok, msg = s.validate()
        assert ok is False
        assert "Invalid stop_loss" in msg

    def test_negative_stop_loss(self):
        s = Signal("BUY", "TEST", -100.0)
        ok, msg = s.validate()
        assert ok is False

    def test_confidence_too_high(self):
        s = Signal("BUY", "TEST", 35000.0, confidence=1.5)
        ok, msg = s.validate()
        assert ok is False
        assert "out of range" in msg

    def test_confidence_negative(self):
        s = Signal("BUY", "TEST", 35000.0, confidence=-0.1)
        ok, msg = s.validate()
        assert ok is False

    def test_to_dict_backward_compat(self):
        s = Signal("BUY", "COUNTER", 34900.0, target=35100.0, confidence=0.8)
        d = s.to_dict()
        assert d["action"] == "BUY"
        assert d["reason"] == "COUNTER"
        assert d["stop_loss"] == 34900.0
        assert d["target"] == 35100.0
        assert d["confidence"] == 0.8
