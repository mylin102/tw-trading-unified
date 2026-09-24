"""Deploy evidence must be frozen at validation time, not re-read later.

The running owner rewrites exports/.../broker_snapshot_canonical.json every few
seconds (measured ~10 s cadence on 2026-09-24 with pm2 trading-system online).
The pre-deploy gate takes --position-state and --margin-evidence from the same
artifact, so if the owner writes between the gate's two reads the capture
consistency guard refuses with GUARD_CAPTURE_MISMATCH (observed live
2026-09-24 13:16, deploy aborted at stage 5/11, app untouched).
"""
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import scripts.deployment.capture_flat_evidence as helper  # noqa: E402

ORIGINAL = b'{"source": "live_broker", "positions": [], "open_orders": []}\n'
OWNER_REWRITE = b'{"source": "live_broker", "positions": [], "open_orders": [], "n": 2}\n'


def _artifact(tmp_path: Path) -> Path:
    live = tmp_path / "diagnostics" / "broker_snapshot_canonical.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(ORIGINAL)
    return live


def test_freeze_copies_the_exact_bytes(tmp_path):
    live = _artifact(tmp_path)
    frozen = helper.freeze_validated_bytes(
        str(live), str(tmp_path / "deploy_flat_evidence.json"))
    assert Path(frozen).name.endswith(".snapshot.json"), frozen
    assert Path(frozen).read_bytes() == ORIGINAL
    assert not list(tmp_path.glob("*.tmp")), "atomic write left a temp file"


def test_frozen_bytes_survive_a_later_owner_write(tmp_path):
    live = _artifact(tmp_path)
    frozen = helper.freeze_validated_bytes(
        str(live), str(tmp_path / "deploy_flat_evidence.json"))
    live.write_bytes(OWNER_REWRITE)          # the owner moves on immediately
    assert Path(frozen).read_bytes() == ORIGINAL


def test_payload_points_the_gate_at_the_frozen_copy(tmp_path):
    live = _artifact(tmp_path)
    payload = {"preflight_passed": True, "artifact": None, "failed_checks": []}
    payload = helper.freeze_and_report(
        live, str(tmp_path / "deploy_flat_evidence.json"), payload)
    assert Path(payload["artifact"]).name.endswith(".snapshot.json")
    assert payload["captured_from"] == str(live)
    live.write_bytes(OWNER_REWRITE)
    assert Path(payload["artifact"]).read_bytes() == ORIGINAL
    assert json.loads(Path(payload["artifact"]).read_text())["source"] == "live_broker"


def test_main_reports_the_frozen_copy_the_gate_will_consume(tmp_path, monkeypatch):
    """The wiring, not just the helper: main()'s payload must point at the frozen file."""
    import core.live_broker_preflight as preflight_mod

    diag = tmp_path / "diag"
    diag.mkdir()
    live = diag / "broker_snapshot_canonical.json"
    live.write_bytes(ORIGINAL)

    monkeypatch.setattr(preflight_mod, "diagnostics_dir", lambda: diag)
    monkeypatch.setattr(preflight_mod, "run_once",
                        lambda api_factory, request_id=None, product=None:
                        {"preflight": {"passed": True, "failed_checks": []}})
    monkeypatch.setitem(sys.modules, "shioaji",
                        types.SimpleNamespace(Shioaji=lambda: object()))
    import core.broker.shioaji_compat as compat
    monkeypatch.setattr(compat, "safe_login", lambda *a, **k: None)
    env = tmp_path / "creds.env"
    env.write_text("SHIOAJI_API_KEY=k\nSHIOAJI_SECRET_KEY=s\n")
    out = tmp_path / "deploy_flat_evidence.json"
    monkeypatch.setattr(sys, "argv",
                        ["capture_flat_evidence.py", "--env-file", str(env),
                         "--out", str(out)])

    assert helper.main() == 0
    payload = json.loads(out.read_text())
    frozen = Path(payload["artifact"])
    assert frozen.name.endswith(".snapshot.json"), payload
    assert payload["captured_from"] == str(live), payload
    assert frozen.read_bytes() == ORIGINAL
    assert payload["open_orders"] == 0 and payload["futures_open_positions"] == 0
    # an owner write after the capture cannot change what the gate consumes
    live.write_bytes(OWNER_REWRITE)
    assert frozen.read_bytes() == ORIGINAL
