# 2026-08-23 Hermes Agent: focused dashboard/state rendering tests for the
# Hierarchical VWAP candidate telemetry panel. The helpers under test are
# pure (data assembly + HTML rendering); the Streamlit page calls them
# inside try/except so a failure can never break the dashboard.
# Coverage:
#   1. payload assembly from state + event ledger (all requested fields)
#   2. mode badge (PAPER / LIVE / UNKNOWN + release-wiring active/inactive)
#   3. baseline trend release status passthrough
#   4. candidate release leg + release intent/block status + winner
#   5. explicit no-action/block reason + freshness fields
#   6. markdown rendering contains every required display token
#   7. missing/corrupt inputs fail gracefully (empty payload, no events)
import json

import pytest

from ui.dashboard import (
    _hvwap_execution_mode,
    _hvwap_latest,
    _hvwap_load_events,
    _hvwap_panel_markdown,
    _hvwap_panel_payload,
    _hvwap_status_color,
)


def _cand_event(**over):
    e = {
        "event": "HVWAP_CANDIDATE",
        "ts": "2026-08-22T18:05:00",
        "status": "ALIGNED_PASS",
        "block_reason": None,
        "regime_60m": "BULLISH_TREND",
        "signal_15m": "CONFIRMED_CONTINUATION",
        "consecutive_confirmed_bars": 2,
        "bars_complete": True,
        "session_boundary_ok": True,
        "n_completed_5m_bars": 12,
        "quote_age_ms": 100.0,
        "session_label": "2026-08-22",
        "retained_direction": "BULLISH",
        "hypothetical_release_leg": "FAR",
        "decision_ts": "2026-08-22T18:00:00",
        "near": {"vwap": 100.0, "slope": 0.01, "vwap_source": "SESSION_ACCUMULATED",
                 "aligned": True, "issue": None},
        "far": {"vwap": 100.0, "slope": -0.01, "vwap_source": "SESSION_ACCUMULATED",
                "aligned": True, "issue": None},
        "baseline_enabled": True,
        "baseline_pass_release": False,
        "baseline_release_leg": None,
    }
    e.update(over)
    return e


def _blocked_event(**over):
    e = {
        "event": "HVWAP_RELEASE_BLOCKED",
        "ts": "2026-08-22T18:06:00",
        "reason": "STALE_QUOTE",
        "quote_age_ms": 99_999.0,
        "trade_id": "t1",
    }
    e.update(over)
    return e


def _intent_event(**over):
    e = {
        "event": "HVWAP_RELEASE_INTENT",
        "ts": "2026-08-22T18:07:00",
        "source": "HVWAP_CANDIDATE",
        "release_leg": "FAR",
        "exit_price": 95.0,
        "decision_ts": "2026-08-22T18:00:00",
        "status": "ALIGNED_PASS",
        "trade_id": "t1",
    }
    e.update(over)
    return e


def _state(**over):
    s = {
        "has_position": True,
        "near_side": "LONG",
        "far_side": "SHORT",
        "released_leg": None,
        "remaining_leg": None,
        "remaining_side": None,
        "trail_side": None,
        "trail_stop_price": None,
        "atr": 5.0,
        "state": "BOTH_HELD",
        "reason": "RELEASE_NEAR",
    }
    s.update(over)
    return s


# ── 1. payload assembly (all requested fields) ─────────────────────────────

def test_payload_assembles_all_required_fields(monkeypatch):
    events = [_cand_event(), _intent_event()]
    p = _hvwap_panel_payload(_state(), events)
    # mode
    assert "mode" in p and p["mode"]["mode_label"] in ("PAPER", "LIVE", "UNKNOWN")
    assert "release_wiring_active" in p["mode"]
    # state: near/far side + single-leg stop status
    assert p["state"]["near_side"] == "LONG"
    assert p["state"]["far_side"] == "SHORT"
    assert p["state"]["released_leg"] is None
    assert p["state"]["atr"] == 5.0
    # baseline trend release status (from the candidate event mirror)
    assert p["baseline"]["enabled"] is True
    assert p["baseline"]["pass_release"] is False
    # HVWAP candidate: 60m/15m/5m/freshness/near/far VWAP
    assert p["hvwap"]["status"] == "ALIGNED_PASS"
    assert p["hvwap"]["regime_60m"] == "BULLISH_TREND"
    assert p["hvwap"]["signal_15m"] == "CONFIRMED_CONTINUATION"
    assert p["hvwap"]["consecutive_confirmed_bars"] == 2
    assert p["hvwap"]["bars_complete"] is True
    assert p["hvwap"]["session_boundary_ok"] is True
    assert p["hvwap"]["n_completed_5m_bars"] == 12
    assert p["hvwap"]["quote_age_ms"] == 100.0
    assert p["hvwap"]["near"]["vwap"] == 100.0
    assert p["hvwap"]["near"]["slope"] == 0.01
    assert p["hvwap"]["near"]["source"] == "SESSION_ACCUMULATED"
    assert p["hvwap"]["far"]["vwap"] == 100.0
    assert p["hvwap"]["far"]["slope"] == -0.01
    assert p["hvwap"]["hypothetical_release_leg"] == "FAR"
    # release: candidate release leg + intent status
    assert p["release"]["intent_leg"] == "FAR"
    assert p["release"]["intent_exit_price"] == 95.0
    assert "INTENT FAR" in p["release"]["status"]
    # winner / trigger source: intent source wins over state reason
    assert p["winner"] == "HVWAP_CANDIDATE"


def test_payload_release_blocked_precedence(monkeypatch):
    """A BLOCKED release event (newest) renders as the release status."""
    events = [_cand_event(), _intent_event(ts="2026-08-22T18:07:00"),
              _blocked_event(ts="2026-08-22T18:08:00")]
    p = _hvwap_panel_payload(_state(), events)
    assert p["release"]["block_reason"] == "STALE_QUOTE"
    assert "BLOCKED (STALE_QUOTE)" in p["release"]["status"]


def test_payload_no_events_empty_state(monkeypatch):
    """No events + empty state -> safe defaults (never raises)."""
    p = _hvwap_panel_payload({}, [])
    assert p["hvwap"]["status"] == "—"
    assert p["state"]["near_side"] == "—"
    assert p["release"]["status"] == "—"
    assert p["winner"] is None


def test_payload_winner_falls_back_to_state_reason(monkeypatch):
    """Without a release intent, the state reason is the trigger source."""
    p = _hvwap_panel_payload(_state(reason="TRAIL_EXIT"), [_cand_event()])
    assert p["winner"] == "TRAIL_EXIT"


# ── 2. mode badge ──────────────────────────────────────────────────────────

def test_mode_paper_active(monkeypatch, tmp_path):
    _write_ctx(monkeypatch, tmp_path, "paper_active", False)
    m = _hvwap_execution_mode()
    assert m["mode_label"] == "PAPER"
    assert m["release_wiring_active"] is True
    assert m["live_order_allowed"] is False


def test_mode_paper_env_override(monkeypatch, tmp_path):
    _write_ctx(monkeypatch, tmp_path, None, False)   # missing context
    monkeypatch.setenv("PAPER_MODE", "true")
    m = _hvwap_execution_mode()
    assert m["mode_label"] == "PAPER"
    assert m["release_wiring_active"] is True
    monkeypatch.delenv("PAPER_MODE", raising=False)


def test_mode_live_fail_closed_badge(monkeypatch, tmp_path):
    _write_ctx(monkeypatch, tmp_path, "live_ready", False)
    m = _hvwap_execution_mode()
    assert m["mode_label"] == "LIVE"
    assert m["release_wiring_active"] is False


def test_mode_live_order_allowed_never_active(monkeypatch, tmp_path):
    """live_order_allowed=True disables the release wiring even in paper mode."""
    _write_ctx(monkeypatch, tmp_path, "paper_active", True)
    m = _hvwap_execution_mode()
    assert m["release_wiring_active"] is False


def test_mode_unknown_when_no_context(monkeypatch, tmp_path):
    _write_ctx(monkeypatch, tmp_path, None, False)
    monkeypatch.delenv("PAPER_MODE", raising=False)
    m = _hvwap_execution_mode()
    assert m["mode_label"] == "UNKNOWN"
    assert m["release_wiring_active"] is False


# ── 6. markdown rendering ──────────────────────────────────────────────────

def test_markdown_contains_all_display_tokens(monkeypatch):
    events = [_cand_event(), _intent_event()]
    p = _hvwap_panel_payload(_state(), events)
    md = _hvwap_panel_markdown(p)
    for token in ("HVWAP 候選", "ALIGNED_PASS", "BULLISH_TREND",
                  "CONFIRMED_CONTINUATION", "SESSION_ACCUMULATED",
                  "FAR", "HVWAP_CANDIDATE", "釋放接線", "mode=",
                  "近月", "遠月", "釋放", "基準趨勢釋放", "候選釋放腿",
                  "候選釋放", "無動作/阻擋", "目前觸發來源", "INTENT FAR",
                  "ATR", "5m 確認", "完整度"):
        assert token in md, f"missing token: {token}"


def test_markdown_paper_badge_and_wiring(monkeypatch, tmp_path):
    _write_ctx(monkeypatch, tmp_path, "paper_active", False)
    md = _hvwap_panel_markdown(_hvwap_panel_payload(_state(), [_cand_event()]))
    assert "PAPER" in md
    assert "ACTIVE (paper-only)" in md


def test_markdown_live_badge_wiring_inactive(monkeypatch, tmp_path):
    _write_ctx(monkeypatch, tmp_path, "live_ready", False)
    md = _hvwap_panel_markdown(_hvwap_panel_payload(_state(), [_cand_event()]))
    assert "LIVE" in md
    assert "INACTIVE" in md


def test_markdown_block_reason_shown(monkeypatch):
    events = [_cand_event(block_reason="ZERO_VOLUME", status="BLOCK"),
              _blocked_event(reason="ZERO_VOLUME")]
    md = _hvwap_panel_markdown(_hvwap_panel_payload(_state(), events))
    assert "ZERO_VOLUME" in md
    assert "BLOCK" in md


def test_markdown_single_leg_stop_status(monkeypatch):
    st = _state(released_leg="near", remaining_leg="FAR", remaining_side="SHORT",
                trail_side="SHORT", trail_stop_price=88.0)
    md = _hvwap_panel_markdown(_hvwap_panel_payload(st, [_cand_event()]))
    assert "near已釋放" in md or "near已釋放" in md
    assert "88.00" in md or "88.0" in md


def test_markdown_flat_state(monkeypatch):
    st = _state(has_position=False, state="FLAT", reason="WAITING_FOR_SIGNAL")
    md = _hvwap_panel_markdown(_hvwap_panel_payload(st, [_cand_event()]))
    assert "FLAT" in md
    assert "WAITING_FOR_SIGNAL" in md


def test_markdown_empty_payload_never_raises(monkeypatch):
    md = _hvwap_panel_markdown(_hvwap_panel_payload({}, []))
    assert "HVWAP 候選" in md
    assert "釋放接線" in md
    assert "無動作/阻擋" in md


# ── readability (2026-08-24): white/light card, dark text, distinct colors ──

def test_markdown_white_background_dark_text(monkeypatch):
    """The panel card must be light (white background) with dark high-contrast
    text and a readable border — not the previous dark navy card."""
    md = _hvwap_panel_markdown(_hvwap_panel_payload(_state(), [_cand_event()]))
    assert "background:#ffffff" in md            # white card
    assert "color:#111827" in md                 # dark high-contrast text
    assert "border:1px solid #94a3b8" in md      # readable border
    assert "background:#0f172a" not in md        # old dark card removed
    assert "color:#94a3b8" not in md             # old light-on-dark muted text


def test_status_colors_distinct(monkeypatch):
    """ALIGNED_PASS / BLOCK / HOLD / UNKNOWN map to distinct colors and the
    rendered status line carries the mapped color."""
    colors = {st: _hvwap_status_color(st)
              for st in ("ALIGNED_PASS", "BLOCK", "HOLD", "UNKNOWN")}
    assert len(set(colors.values())) == 4        # all four distinct
    # rendered markdown: status-colored <b> for the current status
    md = _hvwap_panel_markdown(
        _hvwap_panel_payload(_state(), [_cand_event(status="BLOCK")]))
    assert f"color:{colors['BLOCK']}" in md
    assert ">BLOCK<" in md
    # unknown status falls back to the slate default, still rendered
    assert _hvwap_status_color("") == "#475569"


# ── ledger helpers ─────────────────────────────────────────────────────────

def test_latest_returns_newest(monkeypatch):
    events = [_cand_event(ts="2026-08-22T18:00:00", status="UNKNOWN"),
              _cand_event(ts="2026-08-22T18:05:00", status="ALIGNED_PASS")]
    latest = _hvwap_latest(events, "HVWAP_CANDIDATE")
    assert latest["status"] == "ALIGNED_PASS"
    assert _hvwap_latest(events, "NOPE") is None
    assert _hvwap_latest([], "HVWAP_CANDIDATE") is None


def test_load_events_handles_corrupt_lines(monkeypatch, tmp_path):
    import ui.dashboard as D
    p = tmp_path / "mts_spread_events.jsonl"
    p.write_text('{"event": "OK"}\nnot-json\n{"event": "HVWAP_CANDIDATE"}\n')
    monkeypatch.setattr(
        D, "runtime_path",
        lambda *a, **k: str(p) if a and a[0] == "logs" else "/dev/null")
    events = _hvwap_load_events()
    assert len(events) == 2
    assert events[-1]["event"] == "HVWAP_CANDIDATE"


def _write_ctx(monkeypatch, tmp_path, mode, live_allowed):
    """Point the dashboard's runtime_path('execution_context.json') at a temp
    context file (execution-mode read is display-only)."""
    import ui.dashboard as D
    _ctx = {"effective_mode": mode, "live_order_allowed": live_allowed}
    _file = tmp_path / "execution_context.json"
    _file.write_text(json.dumps(_ctx))

    def _fake_runtime_path(*args, **kwargs):
        if args and args[0] == "execution_context.json":
            return str(_file)
        if args and args[0] == "logs":
            return str(tmp_path)
        return "/dev/null"

    monkeypatch.setattr(D, "runtime_path", _fake_runtime_path)
