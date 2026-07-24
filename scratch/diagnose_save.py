#!/usr/bin/env python3
"""Diagnose config save issue."""
import os
import yaml
from pathlib import Path

# 1. Check config file exists and is writable
cfg_path = Path("config/futures_night.yaml")
print(f"Config file: {cfg_path.absolute()}")
print(f"Exists: {cfg_path.exists()}")
print(f"Writable: {os.access(cfg_path, os.W_OK) if hasattr(os, 'access') else 'N/A'}")

# 2. Check current values
cfg = yaml.safe_load(open(cfg_path))
mts_params = cfg.get("mts", {}).get("params", {})
print(f"\nCurrent mts.params.atr_multiplier_stop: {mts_params.get('atr_multiplier_stop')}")
print(f"Current mts.params.atr_multiplier_trail: {mts_params.get('atr_multiplier_trail')}")

# 3. Simulate dashboard save
cfg["mts"]["params"]["atr_multiplier_stop"] = 9.99  # test value
cfg["mts"]["params"]["atr_multiplier_trail"] = 8.88
with open(cfg_path, "w") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# 4. Read back
cfg2 = yaml.safe_load(open(cfg_path))
mts2 = cfg2.get("mts", {}).get("params", {})
print(f"\nAfter save:")
print(f"atr_multiplier_stop: {mts2.get('atr_multiplier_stop')}")
print(f"atr_multiplier_trail: {mts2.get('atr_multiplier_trail')}")
print(f"Write+read OK: {mts2.get('atr_multiplier_stop') == 9.99}")

# 5. Restore original values (read from day config)
day = yaml.safe_load(open("config/futures.yaml"))
day_params = day.get("mts", {}).get("params", {})
orig_stop = day_params.get("atr_multiplier_stop", 2.1)
orig_trail = day_params.get("atr_multiplier_trail", 1.1)
cfg["mts"]["params"]["atr_multiplier_stop"] = orig_stop
cfg["mts"]["params"]["atr_multiplier_trail"] = orig_trail
with open(cfg_path, "w") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
print(f"\nRestored night config: stop={orig_stop}, trail={orig_trail}")

# 6. Check day config too
day2 = yaml.safe_load(open("config/futures.yaml"))
day2_params = day2.get("mts", {}).get("params", {})
d_stop = day2_params.get("atr_multiplier_stop")
d_trail = day2_params.get("atr_multiplier_trail")
print(f"\nDay config: stop={d_stop}, trail={d_trail}")

import os
