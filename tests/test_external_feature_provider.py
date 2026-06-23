import json
from pathlib import Path

import pytest

from core.external_feature_provider import (
    ExternalFeatureError,
    ExternalFeatureProvider,
    load_external_feature_settings,
)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def _provider(tmp_path):
    settings = {
        "enabled": True,
        "timeout_seconds": 1,
        "max_staleness_minutes": 60,
        "cache_dir": str(tmp_path / "cache"),
        "stock_features_url": "https://example.test/stock_features.json",
        "ranking_url": "https://example.test/ranking.json",
        "leaders_url": "https://example.test/leaders.json",
    }
    return ExternalFeatureProvider(settings)


def test_load_external_feature_settings_merges_defaults():
    settings = load_external_feature_settings({"stocks": {"external_features": {"enabled": True, "timeout_seconds": 5}}})
    assert settings["enabled"] is True
    assert settings["timeout_seconds"] == 5
    assert "stock_features_url" in settings
    assert "ranking_url" in settings


def test_provider_fetches_and_writes_cache(monkeypatch, tmp_path):
    provider = _provider(tmp_path)

    def fake_get(url, timeout):
        if url.endswith("stock_features.json"):
            return _FakeResponse(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-21T06:30:00+08:00",
                    "features": [
                        {"symbol": "2330", "rev_yoy": 0.3, "revenue_score": 5},
                    ],
                }
            )
        if url.endswith("ranking.json"):
            return _FakeResponse(
                {
                    "schema_version": 1,
                    "ranking": [
                        {"symbol": "2330", "breakout_score": 0.8},
                        {"symbol": "3017", "breakout_score": 0.7},
                    ],
                }
            )
        return _FakeResponse({"schema_version": 1, "universe": [{"symbol": "2330"}, {"symbol": "3017"}]})

    monkeypatch.setattr("core.external_feature_provider.requests.get", fake_get)

    snapshot = provider.get_snapshot(prefer_refresh=True)

    assert snapshot["watchlist_symbols"] == ["2330", "3017"]
    assert snapshot["stock_features_count"] == 1
    assert snapshot["ranking_count"] == 2
    assert provider.latest_cache_path.exists()
    latest = json.loads(provider.latest_cache_path.read_text(encoding="utf-8"))
    assert latest["watchlist_count"] == 2


def test_provider_falls_back_to_cache_on_network_failure(monkeypatch, tmp_path):
    provider = _provider(tmp_path)
    provider.latest_cache_path.parent.mkdir(parents=True, exist_ok=True)
    provider.latest_cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-04-21T06:30:00+08:00",
                "fetched_at": "2026-04-21T06:31:00+08:00",
                "watchlist_symbols": ["2330"],
                "watchlist_count": 1,
                "features_by_symbol": {"2330": {"symbol": "2330"}},
                "ranking_by_symbol": {},
                "stock_features_count": 1,
                "ranking_count": 0,
                "degraded": False,
                "degraded_reason": "",
            }
        ),
        encoding="utf-8",
    )

    def fake_get(url, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr("core.external_feature_provider.requests.get", fake_get)

    snapshot = provider.get_snapshot(prefer_refresh=True)

    assert snapshot["watchlist_symbols"] == ["2330"]
    assert snapshot["degraded"] is True
    assert snapshot["degraded_reason"].startswith("cache_fallback:")


def test_provider_marks_stale_cache(monkeypatch, tmp_path):
    provider = _provider(tmp_path)
    provider.settings["max_staleness_minutes"] = 1
    provider.latest_cache_path.parent.mkdir(parents=True, exist_ok=True)
    provider.latest_cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-04-20T06:30:00+08:00",
                "fetched_at": "2026-04-20T06:31:00+08:00",
                "watchlist_symbols": ["2330"],
                "watchlist_count": 1,
                "features_by_symbol": {"2330": {"symbol": "2330"}},
                "ranking_by_symbol": {},
                "stock_features_count": 1,
                "ranking_count": 0,
                "degraded": False,
                "degraded_reason": "",
            }
        ),
        encoding="utf-8",
    )

    snapshot = provider.load_cached_snapshot()

    assert snapshot is not None
    assert snapshot["is_stale"] is True
    assert snapshot["degraded"] is True


def test_provider_rejects_unsupported_schema_without_cache(monkeypatch, tmp_path):
    provider = _provider(tmp_path)

    def fake_get(url, timeout):
        return _FakeResponse({"schema_version": 99, "features": []})

    monkeypatch.setattr("core.external_feature_provider.requests.get", fake_get)

    with pytest.raises(ExternalFeatureError):
        provider.get_snapshot(prefer_refresh=True)
