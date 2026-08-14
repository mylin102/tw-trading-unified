# 2026-08-14 Antigravity AI: Task 12 RED — Settings page writes ACTIVE profile + preserves comments
import tempfile
from pathlib import Path
import pytest
from ruamel.yaml import YAML

from ui.dashboard import (
    save_yaml,
    resolve_active_futures_cfg_name,
    save_futures_settings,
)


def test_save_yaml_preserves_comments():
    """Verify save_yaml preserves top-level and inline YAML comments (e.g. Live Route Certification invariant)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "futures_live.yaml"
        original_content = (
            "# Live Route Certification Invariant: do not delete or overwrite\n"
            "# Tracked Account Hash: abc123def456\n"
            "live_trading: true\n"
            "trade_mgmt:\n"
            "  max_positions: 2  # Hard risk limit per leg\n"
            "  lots_per_trade: 1\n"
            "mts:\n"
            "  enabled: true\n"
            "  params:\n"
            "    min_atr: 10.0\n"
        )
        cfg_path.write_text(original_content, encoding="utf-8")

        new_data = {
            "live_trading": True,
            "trade_mgmt": {
                "max_positions": 4,
                "lots_per_trade": 1,
            },
            "mts": {
                "enabled": True,
                "params": {
                    "min_atr": 15.0,
                },
            },
        }

        save_yaml(cfg_path, new_data)
        saved_text = cfg_path.read_text(encoding="utf-8")

        # Comments must remain intact
        assert "# Live Route Certification Invariant: do not delete or overwrite" in saved_text
        assert "# Tracked Account Hash: abc123def456" in saved_text
        assert "# Hard risk limit per leg" in saved_text

        # Modified values must be accurately saved
        assert "max_positions: 4" in saved_text
        assert "min_atr: 15.0" in saved_text or "min_atr: 15" in saved_text


def test_resolve_active_futures_cfg_name():
    """Verify resolve_active_futures_cfg_name returns futures_live.yaml for live runtimes and futures.yaml/futures_night.yaml for paper."""
    # Live runtime states
    assert resolve_active_futures_cfg_name({"is_live_runtime": True}) == "futures_live.yaml"
    assert resolve_active_futures_cfg_name({"is_exit_only_runtime": True}) == "futures_live.yaml"
    assert resolve_active_futures_cfg_name({"profile_identity": "futures_live.yaml"}) == "futures_live.yaml"
    assert resolve_active_futures_cfg_name({"requested_mode": "live"}) == "futures_live.yaml"

    # Paper runtime states
    assert resolve_active_futures_cfg_name({"is_paper_runtime": True}, is_night_session=False) == "futures.yaml"
    assert resolve_active_futures_cfg_name({"is_paper_runtime": True}, is_night_session=True) == "futures_night.yaml"
    assert resolve_active_futures_cfg_name({}, is_night_session=False) == "futures.yaml"
    assert resolve_active_futures_cfg_name({}, is_night_session=True) == "futures_night.yaml"


def test_save_futures_settings_writes_live_profile_and_requires_recert_prompt():
    """Verify that saving settings under a live runtime writes to futures_live.yaml, keeps comments, and returns a recert prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        cfg_dir = base / "config"
        cfg_dir.mkdir(parents=True)

        paper_cfg = cfg_dir / "futures.yaml"
        paper_cfg.write_text("live_trading: false\ntrade_mgmt:\n  max_positions: 2\n", encoding="utf-8")

        night_cfg = cfg_dir / "futures_night.yaml"
        night_cfg.write_text("live_trading: false\ntrade_mgmt:\n  max_positions: 2\n", encoding="utf-8")

        live_cfg = cfg_dir / "futures_live.yaml"
        live_cfg.write_text(
            "# Live Route Certification Invariant: do not delete\nlive_trading: true\ntrade_mgmt:\n  max_positions: 2\n",
            encoding="utf-8",
        )

        active_truth = {
            "is_live_runtime": True,
            "profile_identity": "futures_live.yaml",
            "requested_mode": "live",
        }

        updated_cfg = {
            "live_trading": True,
            "strategy": {"active_strategy": "counter_vwap", "regime_filter": "mid", "entry_score": 20},
            "risk_mgmt": {"atr_multiplier": 2.0, "stop_loss_pts": 60},
            "execution": {"broker_fee_per_side": 20.0},
            "trade_mgmt": {"lots_per_trade": 1, "max_positions": 4},
            "mts": {"enabled": True, "params": {"min_atr": 10.0}},
        }

        res = save_futures_settings(
            base_dir=base,
            futures_cfg=updated_cfg,
            active_runtime_truth=active_truth,
            is_night_session=False,
        )

        assert res["is_live_target"] is True
        assert res["recert_prompt_required"] is True
        assert "re-freeze" in res["recert_prompt_message"].lower() or "re-cert" in res["recert_prompt_message"].lower() or "quarantine" in res["recert_prompt_message"].lower()
        assert res["primary_path"] == live_cfg

        # Verify futures_live.yaml was updated and comments preserved
        live_text = live_cfg.read_text(encoding="utf-8")
        assert "max_positions: 4" in live_text
        assert "# Live Route Certification Invariant: do not delete" in live_text

        # Verify paper futures.yaml was untouched
        paper_text = paper_cfg.read_text(encoding="utf-8")
        assert "max_positions: 2" in paper_text


def test_save_futures_settings_writes_paper_baseline_when_paper():
    """Verify that saving settings under a paper runtime writes to futures.yaml & futures_night.yaml without recert prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        cfg_dir = base / "config"
        cfg_dir.mkdir(parents=True)

        paper_cfg = cfg_dir / "futures.yaml"
        paper_cfg.write_text("# Paper Day Config\nlive_trading: false\ntrade_mgmt:\n  max_positions: 2\n", encoding="utf-8")

        night_cfg = cfg_dir / "futures_night.yaml"
        night_cfg.write_text("# Paper Night Config\nlive_trading: false\ntrade_mgmt:\n  max_positions: 2\n", encoding="utf-8")

        live_cfg = cfg_dir / "futures_live.yaml"
        live_cfg.write_text("# Live Config\nlive_trading: true\ntrade_mgmt:\n  max_positions: 2\n", encoding="utf-8")

        active_truth = {
            "is_paper_runtime": True,
            "profile_identity": "futures.yaml (Paper baseline)",
            "requested_mode": "paper",
        }

        updated_cfg = {
            "live_trading": False,
            "strategy": {"active_strategy": "counter_vwap", "regime_filter": "mid", "entry_score": 20},
            "risk_mgmt": {"atr_multiplier": 2.0, "stop_loss_pts": 60},
            "execution": {"broker_fee_per_side": 20.0},
            "trade_mgmt": {"lots_per_trade": 1, "max_positions": 3},
            "mts": {"enabled": True, "params": {"min_atr": 10.0, "atr_multiplier_stop": 1.5, "atr_multiplier_trail": 3.0}},
        }

        res = save_futures_settings(
            base_dir=base,
            futures_cfg=updated_cfg,
            active_runtime_truth=active_truth,
            is_night_session=False,
        )

        assert res["is_live_target"] is False
        assert res["recert_prompt_required"] is False
        assert res["primary_path"] == paper_cfg

        # Verify paper_cfg and night_cfg updated
        paper_text = paper_cfg.read_text(encoding="utf-8")
        assert "max_positions: 3" in paper_text
        assert "# Paper Day Config" in paper_text

        # Verify live_cfg untouched
        live_text = live_cfg.read_text(encoding="utf-8")
        assert "max_positions: 2" in live_text
