#!/usr/bin/env python3
"""Test config save and reload."""
import yaml

# 1. Test basic YAML round-trip
data = {"mts": {"params": {"atr_multiplier_stop": 2.5, "atr_multiplier_trail": 1.8}}}
with open("/tmp/test_config.yaml", "w") as f:
    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
with open("/tmp/test_config.yaml") as f:
    loaded = yaml.safe_load(f)
print("Round-trip OK:", data == loaded)

# 2. Load actual config, modify mts params, save, reload
cfg = yaml.safe_load(open("config/futures.yaml"))

# Navigate to mts.params
mts_params = cfg.get("mts", {}).get("params", {})
print("\nCurrent mts.params keys:", list(mts_params.keys()))
print("atr_multiplier_stop:", mts_params.get("atr_multiplier_stop"))
print("atr_multiplier_trail:", mts_params.get("atr_multiplier_trail"))

# Modify and save
cfg["mts"]["params"]["atr_multiplier_stop"] = 2.5
cfg["mts"]["params"]["atr_multiplier_trail"] = 1.8
with open("/tmp/test_futures.yaml", "w") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# Read back
cfg2 = yaml.safe_load(open("/tmp/test_futures.yaml"))
mts2 = cfg2.get("mts", {}).get("params", {})
print("\nAfter save:")
print("atr_multiplier_stop:", mts2.get("atr_multiplier_stop"))
print("atr_multiplier_trail:", mts2.get("atr_multiplier_trail"))
print("Values match:", mts2.get("atr_multiplier_stop") == 2.5 and mts2.get("atr_multiplier_trail") == 1.8)
