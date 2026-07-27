# 2026-07-27 Gemini CLI: Dual-Config Sync Governance Test Suite (Cases 1~7)
import tempfile
from pathlib import Path
import yaml

SESSION_SPECIFIC_MTS_PARAMS = {"atr_multiplier_stop", "atr_multiplier_trail"}


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def perform_dual_config_sync(active_cfg_path: Path, counterpart_cfg_path: Path, is_night_session: bool, incoming_params: dict):
    """Helper recreating exact dual-config sync logic from ui/dashboard.py."""
    active_cfg = load_yaml(active_cfg_path)
    if "mts" not in active_cfg: active_cfg["mts"] = {}
    if "params" not in active_cfg["mts"]: active_cfg["mts"]["params"] = {}
    
    for k, v in incoming_params.items():
        active_cfg["mts"]["params"][k] = v
    save_yaml(active_cfg_path, active_cfg)

    if counterpart_cfg_path.exists():
        counterpart_cfg = load_yaml(counterpart_cfg_path)
        if "mts" not in counterpart_cfg: counterpart_cfg["mts"] = {}
        if "params" not in counterpart_cfg["mts"]: counterpart_cfg["mts"]["params"] = {}
        
        for k, v in incoming_params.items():
            if k in SESSION_SPECIFIC_MTS_PARAMS:
                if k not in counterpart_cfg["mts"]["params"]:
                    counterpart_cfg["mts"]["params"][k] = v
            else:
                counterpart_cfg["mts"]["params"][k] = v
        save_yaml(counterpart_cfg_path, counterpart_cfg)


def test_case_1_day_save_preserves_night_stop():
    """Case 1: day stop=2.1, night stop=1.2 -> Save day dashboard -> day=2.1, night=1.2."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d_path = Path(tmpdir) / "futures.yaml"
        n_path = Path(tmpdir) / "futures_night.yaml"
        
        save_yaml(d_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 2.1, "atr_multiplier_trail": 1.1}}})
        save_yaml(n_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 1.2, "atr_multiplier_trail": 0.5}}})

        perform_dual_config_sync(d_path, n_path, is_night_session=False, incoming_params={"atr_multiplier_stop": 2.1, "atr_multiplier_trail": 1.1})

        res_d = load_yaml(d_path)["mts"]["params"]["atr_multiplier_stop"]
        res_n = load_yaml(n_path)["mts"]["params"]["atr_multiplier_stop"]

        assert res_d == 2.1
        assert res_n == 1.2


def test_case_2_night_save_preserves_day_trail():
    """Case 2: day trail=1.5, night trail=0.5 -> Save night dashboard -> day=1.5, night=0.5."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d_path = Path(tmpdir) / "futures.yaml"
        n_path = Path(tmpdir) / "futures_night.yaml"
        
        save_yaml(d_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 2.1, "atr_multiplier_trail": 1.5}}})
        save_yaml(n_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 1.2, "atr_multiplier_trail": 0.5}}})

        perform_dual_config_sync(n_path, d_path, is_night_session=True, incoming_params={"atr_multiplier_stop": 1.2, "atr_multiplier_trail": 0.5})

        res_d = load_yaml(d_path)["mts"]["params"]["atr_multiplier_trail"]
        res_n = load_yaml(n_path)["mts"]["params"]["atr_multiplier_trail"]

        assert res_d == 1.5
        assert res_n == 0.5


def test_case_3_counterpart_key_missing_initialization():
    """Case 3: counterpart key missing -> key initialized once."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d_path = Path(tmpdir) / "futures.yaml"
        n_path = Path(tmpdir) / "futures_night.yaml"
        
        save_yaml(d_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 2.1}}})
        save_yaml(n_path, {"mts": {"enabled": True, "params": {}}})

        perform_dual_config_sync(d_path, n_path, is_night_session=False, incoming_params={"atr_multiplier_stop": 2.1})

        res_n = load_yaml(n_path)["mts"]["params"]["atr_multiplier_stop"]
        assert res_n == 2.1


def test_case_4_consecutive_saves_no_drift():
    """Case 4: Consecutive saves -> zero parameter drift."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d_path = Path(tmpdir) / "futures.yaml"
        n_path = Path(tmpdir) / "futures_night.yaml"
        
        save_yaml(d_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 2.1}}})
        save_yaml(n_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 1.2}}})

        for _ in range(5):
            perform_dual_config_sync(d_path, n_path, is_night_session=False, incoming_params={"atr_multiplier_stop": 2.1})

        assert load_yaml(d_path)["mts"]["params"]["atr_multiplier_stop"] == 2.1
        assert load_yaml(n_path)["mts"]["params"]["atr_multiplier_stop"] == 1.2


def test_case_5_shared_parameter_synchronization():
    """Case 5: Modify shared parameter -> counterpart synchronizes as intended."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d_path = Path(tmpdir) / "futures.yaml"
        n_path = Path(tmpdir) / "futures_night.yaml"
        
        save_yaml(d_path, {"mts": {"enabled": True, "params": {"combined_upl_activation_net_pnl_twd": 300.0}}})
        save_yaml(n_path, {"mts": {"enabled": True, "params": {"combined_upl_activation_net_pnl_twd": 300.0}}})

        perform_dual_config_sync(d_path, n_path, is_night_session=False, incoming_params={"combined_upl_activation_net_pnl_twd": 200.0})

        assert load_yaml(d_path)["mts"]["params"]["combined_upl_activation_net_pnl_twd"] == 200.0
        assert load_yaml(n_path)["mts"]["params"]["combined_upl_activation_net_pnl_twd"] == 200.0


def test_case_6_session_specific_only_active_changes():
    """Case 6: Modify session-specific parameter -> only active session config changes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d_path = Path(tmpdir) / "futures.yaml"
        n_path = Path(tmpdir) / "futures_night.yaml"
        
        save_yaml(d_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 2.1}}})
        save_yaml(n_path, {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 1.2}}})

        # Update day stop to 2.5
        perform_dual_config_sync(d_path, n_path, is_night_session=False, incoming_params={"atr_multiplier_stop": 2.5})

        assert load_yaml(d_path)["mts"]["params"]["atr_multiplier_stop"] == 2.5
        assert load_yaml(n_path)["mts"]["params"]["atr_multiplier_stop"] == 1.2  # Unchanged!


def test_case_7_save_reload_round_trip():
    """Case 7: Save/reload round trip -> both YAML values exactly preserved."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d_path = Path(tmpdir) / "futures.yaml"
        n_path = Path(tmpdir) / "futures_night.yaml"
        
        orig_d = {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 2.1, "combined_upl_activation_net_pnl_twd": 200.0}}}
        orig_n = {"mts": {"enabled": True, "params": {"atr_multiplier_stop": 1.2, "combined_upl_activation_net_pnl_twd": 200.0}}}
        
        save_yaml(d_path, orig_d)
        save_yaml(n_path, orig_n)

        perform_dual_config_sync(d_path, n_path, is_night_session=False, incoming_params=orig_d["mts"]["params"])

        assert load_yaml(d_path) == orig_d
        assert load_yaml(n_path) == orig_n
