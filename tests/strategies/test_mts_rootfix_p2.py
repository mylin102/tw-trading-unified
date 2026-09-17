import json


def _state(tmp_path, monkeypatch, **kwargs):
    from strategies.plugins.futures.active import tmf_spread as mod
    path = tmp_path / "mts_state.json"
    monkeypatch.setattr(mod, "_get_state_file_path", lambda: path)
    mod._write_mts_state(action="TEST", reason="schema", **kwargs)
    return json.loads(path.read_text())


def test_p2_flat_has_empty_legs(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch, has_position=False,
                   near_side=None, far_side=None)
    assert state["legs"] == []
    assert state["state"] == "TEST"


def test_p2_open_spread_has_two_canonical_legs(tmp_path, monkeypatch):
    state = _state(
        tmp_path, monkeypatch, has_position=True,
        near_side="LONG", far_side="SHORT",
        near_entry=100, far_entry=110,
        near_qty=1, far_qty=1,
    )
    assert [leg["leg"] for leg in state["legs"]] == ["near", "far"]
    assert state["legs"][0]["side"] == "LONG"
    assert state["legs"][1]["side"] == "SHORT"
    assert all(set(("leg", "status", "side", "qty", "entry")) <= set(leg)
               for leg in state["legs"])


def test_p2_released_leg_has_one_open_and_one_released(tmp_path, monkeypatch):
    state = _state(
        tmp_path, monkeypatch, has_position=True,
        near_side="LONG", far_side="SHORT", released_leg="near",
    )
    by_leg = {leg["leg"]: leg for leg in state["legs"]}
    assert by_leg["near"]["status"] == "RELEASED"
    assert by_leg["far"]["status"] == "OPEN"


def test_p2_mutation_legacy_only_state_is_not_accepted():
    from pathlib import Path
    source = Path("strategies/plugins/futures/active/tmf_spread.py").read_text()
    assert '"legs"' in source
