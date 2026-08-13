from pathlib import Path


def test_calendar_discovery_uses_runtime_data(monkeypatch, tmp_path):
    import ui.dashboard as dashboard

    runtime_data = tmp_path / "runtime-data"
    runtime_data.mkdir()
    data_dir = runtime_data / "data"
    data_dir.mkdir()
    newer = data_dir / "tmf_calendar_spread_20260814.csv"
    newer.write_text("timestamp,spread\n2026-08-14T09:00:00,1\n")
    monkeypatch.setattr(dashboard, "runtime_path",
                        lambda *parts: str(runtime_data.joinpath(*parts)))
    assert dashboard._latest_spread_csv("TMF").name == newer.name


def test_live_atr_reads_latest_finite_live_indicator(tmp_path):
    import ui.dashboard as dashboard

    path = tmp_path / "TMF_20260814_LIVE_indicators.csv"
    path.write_text("timestamp,atr,atr_used\n2026-08-14T09:00:00,,43.4\n")
    assert dashboard._latest_live_atr(tmp_path) == 43.4


def test_live_legacy_fills_are_not_rendered_as_realized_metrics(tmp_path):
    import ui.dashboard as dashboard

    fills = tmp_path / "mts_trade_fills.jsonl"
    fills.write_text("{}\n")
    assert dashboard.can_render_mts_realized_performance(
        False, {"ok": False, "reason": "legacy"}, str(fills)) is False
    assert dashboard.can_render_mts_realized_performance(
        False, {"ok": True}, str(fills)) is True
