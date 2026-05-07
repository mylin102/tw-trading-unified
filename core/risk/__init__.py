"""
core.risk package — re-exports from core/risk.py module
When both core/risk.py (module) and core/risk/ (package) exist,
Python prefers the package. This __init__.py bridges the gap
by importing from the module and re-exporting.
"""
from pathlib import Path
import importlib.util
import sys

_risk_module_path = Path(__file__).resolve().parent.parent / "risk.py"
_spec = importlib.util.spec_from_file_location("core.risk_module", str(_risk_module_path))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["core.risk_module"] = _mod
_spec.loader.exec_module(_mod)

# Re-export everything from core/risk.py
for _attr in dir(_mod):
    if not _attr.startswith("_"):
        globals()[_attr] = getattr(_mod, _attr)

# Cleanup namespace
del Path, importlib, sys, _risk_module_path, _spec, _mod, _attr
