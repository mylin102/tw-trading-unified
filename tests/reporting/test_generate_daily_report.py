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
