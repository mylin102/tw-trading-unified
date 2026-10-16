import json


def _write(tmp_path, monkeypatch, **kwargs):
    from strategies.plugins.futures.active import tmf_spread as mod
    state = tmp_path / "mts_state.json"
    monkeypatch.setattr(mod, "_get_state_file_path", lambda: state)
    mod._write_mts_state(
        has_position=kwargs.pop("has_position", True),
        action=kwargs.pop("action", "TEST"),
        reason=kwargs.pop("reason", "test"),
        **kwargs,
    )
    return json.loads(state.read_text())


def test_t1_flat_explicit_clear_does_not_recover_old_side(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, near_side="SHORT", far_side="LONG", trade_id="old")
    state = _write(tmp_path, monkeypatch, has_position=False, near_side=None, far_side=None)
    assert state["near_side"] is None
    assert state["far_side"] is None


def test_t2_flat_is_represented_as_closed(tmp_path, monkeypatch):
    state = _write(tmp_path, monkeypatch, has_position=False, near_side=None, far_side=None)
    assert state["near_status"] == "CLOSED"
    assert state["far_status"] == "CLOSED"


def test_t3_omitted_side_still_protects_existing_value(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, near_side="SHORT")
    state = _write(tmp_path, monkeypatch, near_entry=0)
    assert state["near_side"] == "SHORT"


def test_t4_explicit_none_is_not_treated_as_omitted(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, near_side="SHORT")
    state = _write(tmp_path, monkeypatch, near_side=None)
    assert state["near_side"] is None


def test_mutation_status_must_not_revert_flat_to_open(tmp_path, monkeypatch):
    state = _write(tmp_path, monkeypatch, has_position=False, near_side=None, far_side=None)
    assert state["near_status"] != "OPEN"
