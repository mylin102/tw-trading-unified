# Wave 1D Commit Scope Audit Report

**Date**: 2026-07-24  
**Audit Target**: Commit `10c7c1a7` / `6027333e`  
**Auditor**: Gemini CLI  

---

### 📌 Summary of Scope Separation

In accordance with Wave 1D governance, commit `10c7c1a7` contains both core Wave 1D Telemetry & Strategy files as well as separable environment/config adjustments.

| Category | File Path | Scope Classification | Description |
| :--- | :--- | :--- | :--- |
| **Wave 1D Core** | `strategies/futures/mts/telemetry.py` | Core Telemetry | Non-blocking telemetry spooler & accounting counters |
| **Wave 1D Core** | `strategies/futures/mts/soak_manifest.py` | Core Manifest | Immutable Shadow Soak Manifest generator |
| **Wave 1D Core** | `strategies/futures/mts/dispatcher.py` | Core Dispatcher | Deep state parity & telemetry integration |
| **Wave 1D Core** | `tests/strategies/test_mts_telemetry_soak.py` | Core Test | Dual-track accounting & manifest unit tests |
| **Separable Config** | `config/futures_mtx.yaml` | Separable Exception | MTX futures product configuration adjustment |
| **Separable Environment** | `.gitignore` | Separable Exception | Local environment artifact pattern additions |
| **Strategy Fix** | `strategies/plugins/futures/active/tmf_spread.py` | Separable Fix | Reanchor restart guard fix |

---

### 🔒 Governance Status

1. **Production Authority**: `authority = legacy` remains 100% sole decision maker.
2. **Shadow Authority**: Pure Policy operates strictly in diagnostic shadow mode (`authority = none`).
3. **Commit Scope Exception**: Commit `10c7c1a7` is explicitly registered as containing separable strategy/config files; only `strategies/futures/mts/*` are part of the Wave 1D pure domain provenance chain.
