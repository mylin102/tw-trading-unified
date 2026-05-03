from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = ROOT / "cache" / "external_alpha"
SUPPORTED_SCHEMA_VERSION = 1

DEFAULT_FEATURE_SETTINGS = {
    "enabled": False,
    "timeout_seconds": 10,
    "max_staleness_minutes": 1440,
    "cache_dir": "cache/external_alpha",
    "stock_features_url": "https://raw.githubusercontent.com/mylin102/tw-canslim-web/master/api/stock_features.json",
    "ranking_url": "https://raw.githubusercontent.com/mylin102/tw-canslim-web/master/api/ranking.json",
    "leaders_url": "https://raw.githubusercontent.com/mylin102/tw-canslim-web/master/data/leaders.json",
}


class ExternalFeatureError(RuntimeError):
    """Raised when external feature snapshots cannot be produced safely."""


def load_stock_config(config_path: str | Path | None = None) -> dict[str, Any]:
    config_file = Path(config_path) if config_path else ROOT / "config" / "stocks.yaml"
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_external_feature_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_stock_config()
    settings = dict(DEFAULT_FEATURE_SETTINGS)
    settings.update((cfg.get("stocks", {}) or {}).get("external_features", {}) or {})
    return settings


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat()


def _parse_iso(value: Any):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_symbol(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("features", "ranking", "universe", "data", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            rows: list[dict[str, Any]] = []
            for symbol, row in value.items():
                if isinstance(row, dict):
                    rows.append({"symbol": symbol, **row})
            if rows:
                return rows
    return []


def _schema_version(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 1
    version = payload.get("schema_version", 1)
    try:
        return int(version)
    except (TypeError, ValueError):
        return 1


def _extract_generated_at(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("generated_at", "updated_at", "timestamp", "created_at"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _is_valid_leader(row: dict[str, Any]) -> tuple[bool, str]:
    """Filter out low-quality entries from external alpha leader feed.

    Returns (is_valid: bool, reason: str) so caller can track drop stats.

    Rules:
    1. Exclude ETFs / warrant-like codes (00-prefix)
    2. Exclude stocks with no relative strength (rs_rating <= 0)
    3. Exclude stocks with no industry ranking (industry_rank >= 999)
    """
    symbol = str(row.get("symbol") or "").strip()
    if symbol.startswith("00"):
        return False, "etf"

    rs_rating = float(row.get("rs_rating") or 0)
    if rs_rating <= 0:
        return False, "rs_zero"

    industry_rank = int(row.get("industry_rank") or 999)
    if industry_rank >= 999:
        return False, "no_industry"

    return True, ""


def _rows_to_symbols(rows: list[dict[str, Any]]) -> list[str]:
    symbols: list[str] = []
    for row in rows:
        symbol = _normalize_symbol(row.get("symbol") or row.get("ticker") or row.get("code"))
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _normalize_snapshot(
    stock_features_payload: Any,
    ranking_payload: Any,
    leaders_payload: Any,
    settings: dict[str, Any],
) -> dict[str, Any]:
    feature_schema = _schema_version(stock_features_payload)
    ranking_schema = _schema_version(ranking_payload)
    leaders_schema = _schema_version(leaders_payload)
    max_schema = max(feature_schema, ranking_schema, leaders_schema)
    if max_schema > SUPPORTED_SCHEMA_VERSION:
        raise ExternalFeatureError(f"unsupported schema_version={max_schema}")

    feature_rows = _extract_rows(stock_features_payload)
    ranking_rows = _extract_rows(ranking_payload)
    leader_rows = _extract_rows(leaders_payload)

    features_by_symbol: dict[str, dict[str, Any]] = {}
    for row in feature_rows:
        symbol = _normalize_symbol(row.get("symbol") or row.get("ticker") or row.get("code"))
        if symbol:
            normalized_row = dict(row)
            normalized_row["symbol"] = symbol
            features_by_symbol[symbol] = normalized_row

    ranking_by_symbol: dict[str, dict[str, Any]] = {}
    for row in ranking_rows:
        symbol = _normalize_symbol(row.get("symbol") or row.get("ticker") or row.get("code"))
        if symbol:
            normalized_row = dict(row)
            normalized_row["symbol"] = symbol
            ranking_by_symbol[symbol] = normalized_row

    # If ranking is empty but leaders exist, filter + sort by industry_rank/rs/composite
    if not ranking_rows and leader_rows:
        max_watchlist = int(settings.get("max_watchlist_size", 20))
        before = len(leader_rows)

        # Drop reason tracking
        drop_stats: dict[str, int] = {"etf": 0, "rs_zero": 0, "no_industry": 0}
        filtered: list[dict[str, Any]] = []
        for r in leader_rows:
            valid, reason = _is_valid_leader(r)
            if valid:
                filtered.append(r)
            elif reason in drop_stats:
                drop_stats[reason] += 1

        # Score drift guard: check composite_score distribution before sorting
        comp_scores = [float(r.get("composite_score") or 0) for r in filtered]
        if comp_scores:
            cmin, cmax = min(comp_scores), max(comp_scores)
            if cmax - cmin < 0.15:
                logger.warning(
                    "[ExternalAlpha] SCORE DRIFT: composite_score range too flat "
                    "(min=%.3f max=%.3f delta=%.3f < 0.15)",
                    cmin, cmax, cmax - cmin,
                )
            if cmax > 1.0 or cmin < 0.0:
                logger.error(
                    "[ExternalAlpha] SCORE DRIFT: composite_score out of [0,1] range "
                    "(min=%.3f max=%.3f)",
                    cmin, cmax,
                )

        # Industry concentration guard: max N per industry
        MAX_PER_INDUSTRY = 5
        industry_count: dict[int, int] = {}
        deduped: list[dict[str, Any]] = []
        for r in filtered:
            ir = int(r.get("industry_rank") or 999)
            if industry_count.get(ir, 0) >= MAX_PER_INDUSTRY:
                continue
            industry_count[ir] = industry_count.get(ir, 0) + 1
            deduped.append(r)

        sorted_leaders = sorted(
            deduped,
            key=lambda r: (
                int(r.get("industry_rank") or 999),       # lower = stronger industry
                -float(r.get("rs_rating") or 0),           # higher = stronger stock
                -float(r.get("composite_score") or 0),     # higher = better blend
            ),
        )

        # Floor guard: if filtered list is too thin, fall back to relaxed filter
        MIN_REQUIRED = 5
        if len(sorted_leaders) < MIN_REQUIRED:
            logger.warning(
                "[ExternalAlpha] FILTER TOO AGGRESSIVE: only %d leaders remain "
                "(min_required=%d). Falling back to unfiltered sort.",
                len(sorted_leaders),
                MIN_REQUIRED,
            )
            sorted_leaders = sorted(
                leader_rows,
                key=lambda r: (
                    int(r.get("industry_rank") or 999),
                    -float(r.get("rs_rating") or 0),
                    -float(r.get("composite_score") or 0),
                ),
            )

        watchlist_symbols = _rows_to_symbols(sorted_leaders[:max_watchlist])
        logger.info(
            "[ExternalAlpha] leaders filter: before=%s after=%s removed=%s → top %s "
            "| drop: etf=%s rs_zero=%s no_industry=%s "
            "| industry_cap=%s/per",
            before,
            len(filtered),
            before - len(filtered),
            len(watchlist_symbols),
            drop_stats["etf"],
            drop_stats["rs_zero"],
            drop_stats["no_industry"],
            MAX_PER_INDUSTRY,
        )
    else:
        watchlist_symbols = _rows_to_symbols(ranking_rows) or _rows_to_symbols(leader_rows) or list(features_by_symbol.keys())

    generated_at = (
        _extract_generated_at(stock_features_payload)
        or _extract_generated_at(ranking_payload)
        or _extract_generated_at(leaders_payload)
        or _now_iso()
    )

    snapshot = {
        "schema_version": max_schema,
        "fetched_at": _now_iso(),
        "generated_at": generated_at,
        "source": "tw-canslim-web",
        "settings": {
            "stock_features_url": settings["stock_features_url"],
            "ranking_url": settings["ranking_url"],
            "leaders_url": settings["leaders_url"],
            "max_staleness_minutes": settings["max_staleness_minutes"],
        },
        "stock_features_count": len(features_by_symbol),
        "ranking_count": len(ranking_by_symbol),
        "watchlist_count": len(watchlist_symbols),
        "watchlist_symbols": watchlist_symbols,
        "features_by_symbol": features_by_symbol,
        "ranking_by_symbol": ranking_by_symbol,
        "degraded": False,
        "degraded_reason": "",
    }
    return apply_snapshot_health(snapshot, settings)


def apply_snapshot_health(snapshot: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    current = dict(snapshot)
    fetched_at = _parse_iso(current.get("generated_at")) or _parse_iso(current.get("fetched_at"))
    is_stale = False
    age_minutes = None
    if fetched_at is not None:
        from datetime import datetime

        age_minutes = (datetime.now().astimezone() - fetched_at.astimezone()).total_seconds() / 60.0
        is_stale = age_minutes > float(settings.get("max_staleness_minutes", DEFAULT_FEATURE_SETTINGS["max_staleness_minutes"]))

    current["age_minutes"] = age_minutes
    current["is_stale"] = is_stale
    if is_stale and not current.get("degraded"):
        current["degraded"] = True
        current["degraded_reason"] = "stale_feature_snapshot"
    return current


@dataclass
class ExternalFeatureProvider:
    settings: dict[str, Any]

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> "ExternalFeatureProvider":
        return cls(load_external_feature_settings(config))

    @property
    def cache_dir(self) -> Path:
        cache_dir = Path(self.settings.get("cache_dir", DEFAULT_FEATURE_SETTINGS["cache_dir"]))
        if not cache_dir.is_absolute():
            cache_dir = ROOT / cache_dir
        return cache_dir

    @property
    def latest_cache_path(self) -> Path:
        return self.cache_dir / "latest.json"

    def dated_cache_path(self, snapshot: dict[str, Any]) -> Path:
        generated_at = _parse_iso(snapshot.get("generated_at")) or _parse_iso(snapshot.get("fetched_at"))
        date_str = generated_at.strftime("%Y-%m-%d") if generated_at else "unknown"
        return self.cache_dir / f"leaders_{date_str}.json"

    def _fetch_json(self, url: str) -> Any:
        response = requests.get(url, timeout=float(self.settings["timeout_seconds"]))
        response.raise_for_status()
        return response.json()

    def fetch_remote_snapshot(self) -> dict[str, Any]:
        stock_features_payload = self._fetch_json(self.settings["stock_features_url"])
        ranking_payload = self._fetch_json(self.settings["ranking_url"])
        try:
            leaders_payload = self._fetch_json(self.settings["leaders_url"])
        except Exception:
            leaders_payload = {}
        return _normalize_snapshot(stock_features_payload, ranking_payload, leaders_payload, self.settings)

    def write_cache(self, snapshot: dict[str, Any]) -> None:
        _atomic_write_json(self.latest_cache_path, snapshot)
        _atomic_write_json(self.dated_cache_path(snapshot), snapshot)

    def load_cached_snapshot(self) -> dict[str, Any] | None:
        if not self.latest_cache_path.exists():
            return None
        with open(self.latest_cache_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        return apply_snapshot_health(snapshot, self.settings)

    def get_snapshot(self, prefer_refresh: bool = True) -> dict[str, Any]:
        if prefer_refresh:
            try:
                snapshot = self.fetch_remote_snapshot()
                self.write_cache(snapshot)
                return snapshot
            except Exception as exc:
                cached = self.load_cached_snapshot()
                if cached is not None:
                    cached["degraded"] = True
                    cached["degraded_reason"] = f"cache_fallback:{type(exc).__name__}"
                    return cached
                raise ExternalFeatureError(f"failed to fetch external features and no cache available: {exc}") from exc

        cached = self.load_cached_snapshot()
        if cached is not None:
            return cached
        return self.get_snapshot(prefer_refresh=True)


def get_external_feature_provider(config: dict[str, Any] | None = None) -> ExternalFeatureProvider:
    return ExternalFeatureProvider.from_config(config)
