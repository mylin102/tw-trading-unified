"""RED — display-only: LIVE_QUARANTINED must render the fresh canonical
futures position (the broker holds the remaining leg while the order gate
is quarantined — hiding it hides real risk).  Stale / missing / session-
mismatch stays fail-closed N/A.  No /tmp legacy state read in live, no
Policy-J banner for SINGLE_LEG, no gateway/strategy/order changes.
"""
from pathlib import Path

from ui.dashboard import _live_canonical_position_allowed


def _ctx(mode="live_quarantined", session_id="20260818_084924"):
    return {"effective_mode": mode, "session_id": session_id}


def _canon(session_id="20260818_084924", capture="OK"):
    return {"session_id": session_id, "fetch_status": {"capture": capture}}


def test_quarantined_fresh_canonical_position_allowed():
    assert _live_canonical_position_allowed(
        _ctx("live_quarantined"), _canon()) is True


def test_live_ready_fresh_canonical_position_allowed():
    assert _live_canonical_position_allowed(
        _ctx("live_ready"), _canon()) is True


def test_session_mismatch_fails_closed():
    assert _live_canonical_position_allowed(
        _ctx(session_id="a"), _canon(session_id="b")) is False


def test_missing_or_failed_capture_fails_closed():
    assert _live_canonical_position_allowed(
        _ctx(), _canon(capture="FAIL")) is False
    assert _live_canonical_position_allowed(_ctx(), {}) is False


def test_other_modes_fail_closed():
    for mode in ("paper_active", "reconciled_exit_only", "unknown"):
        assert _live_canonical_position_allowed(
            _ctx(mode), _canon()) is False


def test_render_gate_uses_the_display_helper():
    """The canonical render block must call the display helper (the
    inline live_ready-only gate is the bug)."""
    source = Path(__file__).parents[2] / "ui" / "dashboard.py"
    text = source.read_text(encoding="utf-8")
    assert "_live_canonical_position_allowed(_ctx_live, _canon)" in text


def test_no_policy_j_banner_for_single_leg_display():
    """Policy-J banner stays BOTH_HELD-only; SINGLE_LEG never shows it."""
    source = Path(__file__).parents[2] / "ui" / "dashboard.py"
    text = source.read_text(encoding="utf-8")
    assert '_is_both_held = (_release_state == "BOTH_HELD")' in text


def test_live_canonical_block_present_in_worktree():
    """GREEN collection guard: the live canonical display block (helper +
    gate + live-runtime /tmp guard) must exist in the working tree — a
    sibling refactor once deleted it, breaking collection."""
    source = Path(__file__).parents[2] / "ui" / "dashboard.py"
    text = source.read_text(encoding="utf-8")
    assert "_live_canonical_position_allowed(_ctx_live, _canon)" in text
    assert '_mts_state_file = None if _live_runtime else "/tmp/mts_position_state.json"' in text
    assert "broker_snapshot_canonical.json" in text
