"""2026-10-08 Codex: real producer main path with fake broker and atomic publish checks."""
import hashlib
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core.spread_loader import SpreadLoader


module = importlib.import_module("scripts.update_calendar_spread")


class Contract:
    def __init__(self, code):
        self.code = code
        self.delivery_date = "2099/01/01"


class FakeApi:
    def __init__(self):
        near = Contract("TMF_NEAR")
        far = Contract("TMF_FAR")
        self.Contracts = SimpleNamespace(Futures={"TMF": [near, far]})
        self.calls = []

    def kbars(self, *, contract, start, end):
        self.calls.append((contract.code, start, end))
        return contract.code


def _install_fake_inputs(monkeypatch, tmp_path, *, invalid_near=False):
    api = FakeApi()
    monkeypatch.setattr(module, "get_api", lambda: api)

    def to_frame(kbars):
        index = pd.date_range("2026-09-30 08:45", periods=40, freq="min", name="ts")
        seq = np.arange(40, dtype=float)
        if kbars == "TMF_NEAR":
            close = 50000 + seq * 0.8 + np.sin(seq / 3)
            if invalid_near:
                close[25] = np.nan
        else:
            close = 50180 + seq * 0.9 + np.cos(seq / 4)
        return pd.DataFrame({"Close": close}, index=index)

    monkeypatch.setattr(module, "kbars_to_dataframe", to_frame)
    data_dir = tmp_path / "runtime" / "data"
    resolved = []
    monkeypatch.setattr(module, "DATA_DIR", str(data_dir), raising=False)
    monkeypatch.setattr(module, "runtime_path",
                        lambda name: resolved.append(name) or data_dir if name == "data" else pytest.fail(name),
                        raising=False)
    monkeypatch.setattr(sys, "argv", ["update_calendar_spread.py", "--ticker", "tmf"])
    return api, data_dir, resolved


def _formal(data_dir):
    day = pd.Timestamp.now().strftime("%Y%m%d")
    return data_dir / f"tmf_calendar_spread_{day}.csv"


def test_main_atomically_publishes_real_metrics_and_loader_reads_same_runtime(tmp_path, monkeypatch):
    api, data_dir, resolved = _install_fake_inputs(monkeypatch, tmp_path)
    data_dir.mkdir(parents=True)
    final = _formal(data_dir)
    old = pd.DataFrame({"ts": ["2026-09-30 08:44"], "Close_near": [1.0],
                        "Close_far": [2.0], "spread": [-1.0]})
    old.to_csv(final, index=False)
    old_bytes = final.read_bytes()
    real_replace = module.os.replace
    observed = []

    def observe_replace(source, destination):
        assert Path(destination) == final
        assert final.read_bytes() == old_bytes
        assert len(pd.read_csv(final)) == 1
        temporary = pd.read_csv(source)
        assert len(temporary) == 40
        assert {"spread", "spread_z", "vwap_z"} <= set(temporary)
        observed.append(True)
        return real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", observe_replace)
    assert module.main() == 0
    assert len(api.calls) == 2
    assert resolved == ["data"]
    assert observed == [True]
    assert final.is_file()
    assert list(data_dir.iterdir()) == [final]

    published = pd.read_csv(final)
    assert {"ts", "Close_near", "Close_far", "spread", "spread_ma",
            "spread_std", "spread_z", "vwap", "vwap_std", "vwap_z"} <= set(published)
    assert len(published) == 40
    assert published.loc[:18, ["spread_ma", "spread_std", "spread_z",
                               "vwap", "vwap_std", "vwap_z"]].isna().all().all()
    assert np.isfinite(published.loc[19:, ["Close_near", "Close_far", "spread",
                                           "spread_ma", "spread_std"]].to_numpy()).all()
    assert np.isfinite(published.loc[19:, "spread_z"].to_numpy()).all()

    loader = SpreadLoader(str(data_dir))
    assert loader.load_latest_csv("TMF") is True
    bar = loader.enrich_bar({"ts": pd.Timestamp(published["ts"].iloc[-1]),
                             "Close": float(published["Close_near"].iloc[-1])})
    assert bar["spread_z"] == pytest.approx(float(published["spread_z"].iloc[-1]))


def test_bad_real_input_does_not_replace_existing_formal_csv(tmp_path, monkeypatch):
    _, data_dir, _ = _install_fake_inputs(monkeypatch, tmp_path, invalid_near=True)
    data_dir.mkdir(parents=True)
    final = _formal(data_dir)
    original = b"previous-valid-formal-file\\n"
    original_hash = hashlib.sha256(original).hexdigest()
    final.write_bytes(original)

    assert module.main() == 1
    assert hashlib.sha256(final.read_bytes()).hexdigest() == original_hash
    assert list(data_dir.iterdir()) == [final]

def test_bad_real_input_without_existing_formal_publishes_nothing(tmp_path, monkeypatch):
    _, data_dir, _ = _install_fake_inputs(monkeypatch, tmp_path, invalid_near=True)
    assert module.main() == 1
    assert not data_dir.exists()


def test_atomic_replace_failure_keeps_old_formal_and_cleans_temp(tmp_path, monkeypatch):
    _, data_dir, _ = _install_fake_inputs(monkeypatch, tmp_path)
    data_dir.mkdir(parents=True)
    final = _formal(data_dir)
    original = b"previous-valid-formal-file\\n"
    original_hash = hashlib.sha256(original).hexdigest()
    final.write_bytes(original)

    def fail_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    assert module.main() == 1
    assert hashlib.sha256(final.read_bytes()).hexdigest() == original_hash
    assert list(data_dir.iterdir()) == [final]


def test_calculation_failure_preserves_existing_formal_hash(tmp_path, monkeypatch):
    _, data_dir, _ = _install_fake_inputs(monkeypatch, tmp_path)
    data_dir.mkdir(parents=True)
    final = _formal(data_dir)
    original = b"old-formal"
    original_hash = hashlib.sha256(original).hexdigest()
    final.write_bytes(original)
    monkeypatch.setattr(module, "calculate_spread_metrics", lambda near, far: None)

    assert module.main() == 1
    assert hashlib.sha256(final.read_bytes()).hexdigest() == original_hash
    assert list(data_dir.iterdir()) == [final]


def test_write_failure_preserves_existing_formal_hash_and_cleans_temp(tmp_path, monkeypatch):
    _, data_dir, _ = _install_fake_inputs(monkeypatch, tmp_path)
    data_dir.mkdir(parents=True)
    final = _formal(data_dir)
    original = b"old-formal"
    original_hash = hashlib.sha256(original).hexdigest()
    final.write_bytes(original)

    def fail_write(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(module.pd.DataFrame, "to_csv", fail_write)
    assert module.main() == 1
    assert hashlib.sha256(final.read_bytes()).hexdigest() == original_hash
    assert list(data_dir.iterdir()) == [final]

