import json
import time


def _ctx(legs):
    return {
        "requested_mode": "live",
        "session_id": "sess-1",
        "exit_only_capability": {"legs": legs},
    }


def _snapshot(positions, open_orders=None, **extra):
    data = {
        "source": "live_broker",
        "mode": "live",
        "positions": positions,
        "open_orders": [] if open_orders is None else open_orders,
        "captured_at": int(time.time() * 1000),
        "canonical_input_hash": "b" * 64,
        "session_id": "sess-1",
        "account_identity_hash": "a" * 64,
    }
    data.update(extra)
    return data


def _leg(code="TXFH6", side="buy", qty=1, avg=100):
    return {"symbol": code, "side": side, "remaining_qty": qty,
            "avg_cost": avg}


def _position(code="TXFH6", direction="Action.Buy", qty=1, avg=100,
              account="futures"):
    return {"account": account, "code": code, "quantity": qty,
            "direction": direction, "avg_cost": avg}


def _run(tmp_path, snapshot, ctx, allow=True):
    p = tmp_path / "position_state.json"
    p.write_text(json.dumps(snapshot), encoding="utf-8")
    from core.deployment_safety_gate import guard_flat_no_pending
    return guard_flat_no_pending(str(p), ctx, allow)


def test_ext2_two_leg_matching_pass(tmp_path):
    positions = [_position("TXFH6", "Action.Buy"),
                 _position("TXFM6", "Action.Sell", avg=101)]
    r = _run(tmp_path, _snapshot(positions), _ctx([
        _leg("TXFH6", "buy"), _leg("TXFM6", "sell", avg=101)]))
    assert r.ok, r.reasons


def test_ext2_two_leg_canonical_mismatch_fails(tmp_path):
    positions = [_position("TXFH6"), _position("TXFM6", "Action.Sell")]
    r = _run(tmp_path, _snapshot(positions), _ctx([
        _leg("TXFH6"), _leg("TXFZ6", "sell")]))
    assert not r.ok


def test_ext2_single_leg_canonical_empty_passes(tmp_path):
    r = _run(tmp_path, _snapshot([_position()]), _ctx([]))
    assert r.ok, r.reasons


def test_ext2_single_leg_stale_two_leg_canonical_passes(tmp_path):
    r = _run(tmp_path, _snapshot([_position()]), _ctx([
        _leg("TXFH6"), _leg("TXFM6", "sell")]))
    assert r.ok, r.reasons


def test_ext2_single_leg_missing_flag_fails(tmp_path):
    r = _run(tmp_path, _snapshot([_position()]), _ctx([]), allow=False)
    assert not r.ok


def test_ext2_incomplete_identity_fails(tmp_path):
    r = _run(tmp_path, _snapshot([_position(avg=None)]), _ctx([]))
    assert not r.ok


def test_ext2_open_order_fails(tmp_path):
    r = _run(tmp_path, _snapshot([_position()], [{"id": "o1"}]), _ctx([]))
    assert not r.ok


def test_ext2_three_positions_fails(tmp_path):
    r = _run(tmp_path, _snapshot([
        _position("TXFH6"), _position("TXFM6", "Action.Sell"),
        _position("TXFG6")]), _ctx([]))
    assert not r.ok


def test_ext2_zero_position_flat_regression(tmp_path):
    r = _run(tmp_path, _snapshot([]), _ctx([]))
    assert r.ok, r.reasons
