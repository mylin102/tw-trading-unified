def test_live_flat_proof_skips_historical_fills_restore():
    from strategies.plugins.futures.active.tmf_spread import (
        TMFSpread, RecoveryState)

    strategy = TMFSpread.__new__(TMFSpread)
    strategy._broker_truth_flat = True
    strategy._mts_recovery_state = None
    strategy._mts_state_write_enabled = False
    strategy._read_mts_state = lambda: {
        "has_position": False, "state": "FLAT"}
    strategy._check_fills_has_open_position = lambda: True
    strategy._restore_from_fills_log = lambda: (_ for _ in ()).throw(
        AssertionError("historical fills restore must not run"))

    assert strategy._restore_position_state() is False
    assert strategy._mts_recovery_state is RecoveryState.FLAT_CONFIRMED
    assert strategy._mts_state_write_enabled is True
