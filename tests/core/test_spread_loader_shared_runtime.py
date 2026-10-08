# 2026-10-08 Codex: runtime-root selection, verified cache and actual enrich callsite.
import os

import numpy as np
import pandas as pd
import pytest

from core import spread_loader as module


def frame(start="2026-09-30 08:45", count=40, offset=0.0):
    timestamps = pd.date_range(start, periods=count, freq="min")
    spread = -181.0 + np.arange(count) * 0.4 + offset * np.sin(np.arange(count) / 3.0)
    near = 49854.0 + np.arange(count) * 0.2
    far = near - spread
    rows = pd.DataFrame({"ts": timestamps, "Close_near": near, "Close_far": far,
                         "spread": spread})
    rows["spread_ma"] = rows["spread"].rolling(20, min_periods=20).mean()
    rows["spread_std"] = rows["spread"].rolling(20, min_periods=20).std()
    rows["spread_z"] = (rows["spread"] - rows["spread_ma"]) / rows["spread_std"]
    rows["vwap"] = rows["Close_near"].rolling(20, min_periods=20).mean()
    rows["vwap_std"] = rows["Close_near"].rolling(20, min_periods=20).std()
    rows["vwap_z"] = (rows["Close_near"] - rows["vwap"]) / rows["vwap_std"]
    return rows


def publish(root, day, data=None):
    path = root / f"tmf_calendar_spread_{day}.csv"
    (frame() if data is None else data).to_csv(path, index=False)
    return path


def enrich(loader):
    row = frame().iloc[-1]
    return loader.enrich_bar({"ts": row["ts"], "Close": float(row["Close_near"])})


def test_default_root_uses_runtime_helper_and_explicit_root_wins(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(runtime))
    default = module.SpreadLoader()
    assert default.data_dir == str(runtime / "data")
    explicit = tmp_path / "explicit"
    assert module.SpreadLoader(str(explicit)).data_dir == str(explicit)


def test_actual_enrich_loads_warmup_nans_without_dropping_columns_or_rows(tmp_path):
    publish(tmp_path, "20260930")
    loader = module.SpreadLoader(str(tmp_path))
    assert loader.load_latest_csv("TMF") is True
    assert len(loader._far_df) == 40
    assert loader._far_df["spread_z"].iloc[:19].isna().all()
    assert loader._far_df["spread_std"].iloc[:19].isna().all()
    assert loader._far_df["spread_z"].iloc[19:].notna().all()
    bar = enrich(loader)
    assert bar["spread_z"] == pytest.approx(float(loader._far_df["spread_z"].iloc[-1]))
    assert loader.status()["csv"].endswith("tmf_calendar_spread_20260930.csv")


def test_actual_enrich_discovers_new_date_and_ignores_decoys(tmp_path):
    publish(tmp_path, "20260930")
    loader = module.SpreadLoader(str(tmp_path))
    assert loader.load_latest_csv("TMF") is True
    expected = frame(offset=20.0)["spread_z"].iloc[-1]
    publish(tmp_path, "20261001", frame(offset=20.0))
    publish(tmp_path, "20261002.tmp", frame(offset=50.0))
    publish(tmp_path, "20269999", frame(offset=60.0))
    publish(tmp_path, "other_calendar_spread_20261003.csv", frame(offset=80.0))
    bar = enrich(loader)
    assert bar["spread_z"] == pytest.approx(expected)
    assert loader.status()["csv"].endswith("tmf_calendar_spread_20261001.csv")


def test_actual_enrich_detects_atomic_same_path_replacement(tmp_path):
    path = publish(tmp_path, "20260930")
    loader = module.SpreadLoader(str(tmp_path))
    assert loader.load_latest_csv("TMF") is True
    old = loader.get_spread_z()
    replacement = tmp_path / "replacement.csv"
    frame(offset=20.).to_csv(replacement, index=False)
    os.replace(replacement, path)
    bar = enrich(loader)
    assert bar["spread_z"] != pytest.approx(old)
    assert bar["spread_z"] == pytest.approx(frame(offset=20.)["spread_z"].iloc[-1])


@pytest.mark.parametrize("damage", ["schema", "timestamp", "quote"])
def test_failed_refresh_keeps_last_valid_source_and_values(tmp_path, damage):
    path = publish(tmp_path, "20260930")
    loader = module.SpreadLoader(str(tmp_path))
    assert loader.load_latest_csv("TMF") is True
    before_data = loader._far_df.copy(deep=True)
    before_path = loader.status()["csv"]
    before_success = loader.status()["last_successful_refresh"]
    broken = frame(offset=20.)
    if damage == "schema":
        broken["Close_far"] = ""
    elif damage == "timestamp":
        broken["ts"] = "not-a-timestamp"
    else:
        broken.loc[25, "Close_near"] = "not-a-price"
    replacement = tmp_path / "replacement.csv"
    broken.to_csv(replacement, index=False)
    os.replace(replacement, path)
    bar = enrich(loader)
    assert loader.status()["csv"] == before_path
    assert loader.status()["last_successful_refresh"] == before_success
    assert loader.status()["last_refresh_error"]
    pd.testing.assert_frame_equal(loader._far_df, before_data)
    assert bar["spread_z"] == pytest.approx(float(before_data["spread_z"].iloc[-1]))

