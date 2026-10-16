"""Trading-day attribution tests."""



# Case 1: Friday-night trade must bucket to next TRADING day (holiday-skip aware),
# identical to core/date_utils — NOT dt+1 (which lands on Saturday).
def test_night_trade_buckets_to_next_trading_day():
    from scripts.generate_daily_report import get_trading_day

    # 2026-07-31 is a Friday. Night session 15:00+ belongs to next trading day
    # (2026-08-03, Monday — weekend skipped).
    assert get_trading_day("2026-07-31T15:56:49.4", "night") == "2026-08-03"
    assert get_trading_day("2026-07-31T11:21:41.8", "day") == "2026-07-31"
    assert get_trading_day("2026-07-31T13:24:53.1", "day") == "2026-07-31"


# Case 2: parse_logs completed buckets match dashboard query day
def test_parse_logs_buckets_night_trades(tmp_path, monkeypatch):
    import json as _json
    fills = tmp_path / "fills.jsonl"
    events = tmp_path / "events.jsonl"
    events.write_text("")
    rows = [
        {"timestamp": "2026-07-31T11:21:41.8", "trade_id": "t-day-1", "leg": "NEAR",
         "side": "SHORT", "qty": 1, "price": 43840.0, "fill_type": "ENTRY", "session": "day", "realized_pnl": None},
        {"timestamp": "2026-07-31T11:21:41.8", "trade_id": "t-day-1", "leg": "FAR",
         "side": "LONG", "qty": 1, "price": 43831.0, "fill_type": "ENTRY", "session": "day", "realized_pnl": None},
        {"timestamp": "2026-07-31T11:46:21.0", "trade_id": "t-day-1", "leg": "FAR",
         "side": "SELL", "qty": 1, "price": 43752.0, "fill_type": "RELEASE", "session": "day", "realized_pnl": -847.5},
        {"timestamp": "2026-07-31T11:46:22.1", "trade_id": "t-day-1", "leg": "NEAR",
         "side": "BUY", "qty": 1, "price": 43751.0, "fill_type": "EXIT", "session": "day", "realized_pnl": 832.5},
        # Friday night trade
        {"timestamp": "2026-07-31T15:56:49.4", "trade_id": "t-night-1", "leg": "NEAR",
         "side": "SHORT", "qty": 1, "price": 43707.0, "fill_type": "ENTRY", "session": "night", "realized_pnl": None},
        {"timestamp": "2026-07-31T15:56:49.4", "trade_id": "t-night-1", "leg": "FAR",
         "side": "LONG", "qty": 1, "price": 43857.0, "fill_type": "ENTRY", "session": "night", "realized_pnl": None},
        {"timestamp": "2026-07-31T16:00:59.1", "trade_id": "t-night-1", "leg": "NEAR",
         "side": "BUY", "qty": 1, "price": 43713.0, "fill_type": "COMBINED_EXIT", "session": "night", "realized_pnl": 60.0},
        {"timestamp": "2026-07-31T16:00:59.1", "trade_id": "t-night-1", "leg": "FAR",
         "side": "SELL", "qty": 1, "price": 43895.0, "fill_type": "COMBINED_EXIT", "session": "night", "realized_pnl": 380.0},
    ]
    with open(fills, "w") as f:
        for r in rows:
            f.write(_json.dumps(r) + "\n")

    from scripts.generate_daily_report import parse_logs
    d = parse_logs(str(fills), str(events), "2026-08-03")
    night_ids = [t["trade_id"] for t in d["completed"]]
    assert "t-night-1" in night_ids, f"night trade must bucket to 2026-08-03: {night_ids}"
    assert "t-day-1" not in night_ids, f"day trade must NOT bucket to 2026-08-03: {night_ids}"

    d31 = parse_logs(str(fills), str(events), "2026-07-31")
    day_ids = [t["trade_id"] for t in d31["completed"]]
    assert "t-day-1" in day_ids
    assert "t-night-1" not in day_ids

def test_parse_logs_active_trade_without_exit_is_renderable(tmp_path):
    import json
    fills = tmp_path / "fills.jsonl"
    events = tmp_path / "events.jsonl"
    events.write_text("")
    rows = [
        {"timestamp": "2026-08-18T09:00:00", "trade_id": "active-1",
         "leg": "NEAR", "side": "SHORT", "qty": 1, "price": 45500.0,
         "fill_type": "ENTRY", "session": "day", "realized_pnl": None},
        {"timestamp": "2026-08-18T09:00:00", "trade_id": "active-1",
         "leg": "FAR", "side": "LONG", "qty": 1, "price": 45600.0,
         "fill_type": "ENTRY", "session": "day", "realized_pnl": None},
    ]
    fills.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    from scripts.generate_daily_report import parse_logs
    report = parse_logs(str(fills), str(events), "2026-08-18")
    assert len(report["active"]) == 1
    assert report["active"][0]["near_pnl_gross"] == 0.0
    assert report["active"][0]["far_pnl_gross"] == 0.0


# 2026-10-05 Codex: real parser -> actual dashboard closed-loop row regression.
def _render_closed_loop_callsite(fills, events):
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "ui" / "dashboard.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "calculate_mts_daily_performance"]
    loops = [node for node in ast.walk(tree) if isinstance(node, ast.For)
             and ast.unparse(node.target) == "t"
             and ast.unparse(node.iter) == "_completed_filtered"]
    assert len(functions) == len(loops) == 1, "Execute exact production callsites"
    namespace = {"_loop_rows": [], "_fmt_policy_j_reason": lambda reason: reason}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    report = namespace["calculate_mts_daily_performance"](str(fills), str(events), "2026-10-05")
    namespace["_completed_filtered"] = report["completed"]
    exec(compile(ast.Module(body=loops, type_ignores=[]), str(path), "exec"), namespace)
    return report["completed"][0], namespace["_loop_rows"][0]


def _write_closed_loop_fixture(tmp_path, near_pnl, far_pnl, reverse=False,
                               partial=False, single_release=False):
    import json

    trade_id = "mts-paper-real-shape"
    entries = [{"timestamp": "2026-10-05T10:40:00.299327", "trade_id": trade_id,
                "session": "day", "mode": "paper", "leg": leg, "qty": 1,
                "side": side, "price": price, "fill_type": "ENTRY",
                "realized_pnl": None, "execution_source": "PAPER_COMBO_FILL"}
               for leg, side, price in (("NEAR", "LONG", 50045.0),
                                        ("FAR", "SHORT", 50224.0))]
    closes = []
    for leg, side, entry_price, pnl in (("NEAR", "SELL", 50045.0, near_pnl),
                                       ("FAR", "BUY", 50224.0, far_pnl)):
        for part in range(2 if partial else 1):
            qty = 1
            amount = pnl / 2 if partial else pnl
            # Actual Oct 5 shape: two explicit per-leg COMBINED_EXIT records,
            # followed by a zero-quantity lifecycle marker, not a RELEASE.
            closes.append({"timestamp": "2026-10-05T11:14:42.697648",
                           "trade_id": trade_id, "session": "day", "mode": "paper",
                           "leg": leg, "side": side, "qty": qty,
                           "price": entry_price + amount / 10 if leg == "NEAR" else entry_price - amount / 10,
                           "realized_pnl": amount, "fill_type": "COMBINED_EXIT",
                           "position_effect": "CLOSE",
                           "position_side_before_exit": "LONG" if leg == "NEAR" else "SHORT",
                           "paper_fill_id": f"PF-{leg}-{part}", "execution_source": "PAPER_EXIT_FILL"})
    if partial:
        for entry in entries:
            entry["qty"] = 2
    if reverse:
        closes.reverse()
    if single_release:
        closes[0]["fill_type"] = "RELEASE"
        closes[1]["fill_type"] = "EXIT"
    settled = {"timestamp": "2026-10-05T11:14:42.670245", "trade_id": trade_id,
               "session": "day", "mode": "paper", "leg": "SETTLED", "qty": 0,
               "price": 0.0, "realized_pnl": None, "fill_type": "COMBINED_EXIT_SETTLED",
               "event_type": "COMBINED_EXIT_SETTLED", "near_realized_pnl_gross": near_pnl,
               "far_realized_pnl_gross": far_pnl, "combined_realized_pnl_gross": near_pnl + far_pnl,
               "combined_realized_pnl_net": None, "pnl_status": "GROSS_ONLY"}
    marker = {"timestamp": "2026-10-05T11:14:42.704246", "trade_id": trade_id,
              "session": "day", "mode": "paper", "leg": "NEAR", "qty": 0,
              "price": 0.0, "realized_pnl": None, "fill_type": "COMBINED_EXIT_COMPLETED"}
    rows = entries + ([] if single_release else [settled]) + closes + [marker]
    fills = tmp_path / "mts_trade_fills.jsonl"
    events = tmp_path / "mts_spread_events.jsonl"
    fills.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    events.write_text(json.dumps({"event": "COMBINED_EXIT_SUBMITTED", "trade_id": trade_id,
                                  "ts": "2026-10-05T11:14:42", "reason": "POLICY_J"}) + "\n")
    return fills, events


import pytest


@pytest.mark.parametrize("near_pnl,far_pnl", [(0.0, 80.0), (-1370.0, 1000.0), (-350.0, 120.0)])
@pytest.mark.parametrize("reverse", [False, True])
def test_combined_exit_all_legs_reach_real_dashboard_rows(tmp_path, near_pnl, far_pnl, reverse):
    fills, events = _write_closed_loop_fixture(tmp_path, near_pnl, far_pnl, reverse=reverse)
    original = fills.read_bytes(), events.read_bytes()
    trade, row = _render_closed_loop_callsite(fills, events)
    # Total remains canonical; the omitted losing NEAR leg must not become +0.
    assert trade["gross_pnl"] == trade["net_pnl"] == near_pnl + far_pnl
    assert trade["pnl_status"] == "GROSS_ONLY"
    assert trade["near_pnl_gross"] == near_pnl
    assert trade["far_pnl_gross"] == far_pnl
    assert row["近月 PnL"] == f"{near_pnl:+,.0f}"
    assert row["遠月 PnL"] == f"{far_pnl:+,.0f}"
    assert (fills.read_bytes(), events.read_bytes()) == original


@pytest.mark.parametrize("reverse", [False, True])
def test_partial_combined_exits_keep_every_leg_fill(tmp_path, reverse):
    fills, events = _write_closed_loop_fixture(tmp_path, -1370.0, 1000.0,
                                              reverse=reverse, partial=True)
    trade, row = _render_closed_loop_callsite(fills, events)
    assert trade["gross_pnl"] == -370.0
    assert trade["near_pnl_gross"] == -1370.0
    assert trade["far_pnl_gross"] == 1000.0
    assert row["近月 PnL"] == "-1,370"
    assert row["遠月 PnL"] == "+1,000"


@pytest.mark.parametrize("reverse", [False, True])
def test_single_release_leg_mapping_remains_unchanged(tmp_path, reverse):
    fills, events = _write_closed_loop_fixture(tmp_path, -1370.0, 1000.0,
                                              reverse=reverse, single_release=True)
    trade, row = _render_closed_loop_callsite(fills, events)
    assert trade["gross_pnl"] == -370.0
    assert trade["near_pnl_gross"] == -1370.0
    assert trade["far_pnl_gross"] == 1000.0
    assert row["近月 PnL"] == "-1,370"
    assert row["遠月 PnL"] == "+1,000"
