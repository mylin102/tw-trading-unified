#!/usr/bin/env python3
"""Check day/night config differences for ATR params."""
import yaml

day = yaml.safe_load(open("config/futures.yaml"))
night = yaml.safe_load(open("config/futures_night.yaml"))

dp = day.get("mts", {}).get("params", {})
np = night.get("mts", {}).get("params", {})

print("Day atr_multiplier_stop:", dp.get("atr_multiplier_stop"))
print("Night atr_multiplier_stop:", np.get("atr_multiplier_stop"))
print("Day atr_multiplier_trail:", dp.get("atr_multiplier_trail"))
print("Night atr_multiplier_trail:", np.get("atr_multiplier_trail"))
print("Files identical:", dp.get("atr_multiplier_stop") == np.get("atr_multiplier_stop") and dp.get("atr_multiplier_trail") == np.get("atr_multiplier_trail"))
