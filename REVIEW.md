REVIEW — tw-trading-unified (standard-depth)

Summary
-------
Focused review across security, watchdog/data-freshness, trading safety rules (RULES.md), nested .github/skills, Streamlit dashboard, tests import failures, and the adaptive engine/watchdog changes.

Critical
--------
- Tests failing during collection (blocking CI): ImportError: core.decision_logger does not export _HEADERS; core.diagnostic_engine missing diagnose_losing_streak and TradeDiagnosis expected by tests. (See pytest output: cannot import name '_HEADERS'; cannot import name 'diagnose_losing_streak').
  Suggestion: Restore backward-compatible API surface in core.decision_logger (export _HEADERS, provide read/read_by_session, accept optional path param) and in core.diagnostic_engine provide the top-level functions/classes (diagnose_losing_streak, TradeDiagnosis, DiagnosticAction) or supply lightweight wrappers delegating to the new DiagnosticEngine class.
- Password fallback in ui/dashboard.py uses hardcoded default DASHBOARD_PASSWORD="5888" when env missing — insecure. Require env or interactive setup.

High
----
- DecisionLogger API changed: tests assume DecisionLogger.log(..., path=...) and DecisionLogger.read(path=...), read_by_session, and _HEADERS constant. Current implementation uses different names and signatures (log() without path, read_decisions()). This breaks clients/tests.
  Fix: add argument-compatible wrappers + exported constants.
- Diagnostic engine refactor removed public helpers/classes used by downstream code/tests. Provide compatibility layer and unit tests.
- Watchdog script scripts/runners/watchdog.sh contains absolute user-specific paths (/Users/mylin/...). This is brittle and may leak environment-specific paths. Also it restarts main.py without consulting the process' data-freshness heartbeat; main.py already updates last_tick_at but external watchdog only checks process liveness.
  Fix: make watchdog use repo-root-relative paths, or accept $UNIFIED_DIR env; add heartbeat file or check recent logs/heartbeat timestamp to avoid restarting healthy processes.

Medium
------
- Nested .github directory found inside .github/skills/get-stuff-done-for-github-copilot/.github — accidental nested repo metadata may confuse tooling, CI, and skill packaging. (Not a nested .git, but nested .github directory and .gsd templates exist.)
  Fix: remove nested .github or move templates out of skills subfolder; add .gitignore to prevent accidental inclusion of nested metadata.
- Adaptive engine (strategies/adaptive_engine.py) is lightweight and OK but lacks explicit safety checks linking to RULES.md (e.g., ensure any adaptive stop/threshold changes never set stop_loss < 10 pts). Add guard rails.
- Streamlit dashboard (ui/dashboard.py): imports dotenv and loads .env — ensure .env is gitignored. The dashboard writes and triggers RESTART_FLAG via trigger_restart(). Default behavior to st_rerun on password match is acceptable but remove hardcoded fallback password and ensure DASHBOARD_PASSWORD is mandatory.

Low
---
- scripts/runners/watchdog.sh backoff doubling logic multiplies BEFORE sleep; initial backoff comment mismatches implementation. Minor clarity fix.
- Several docs mention adaptive components; ensure consolidation to single docs/ location (docs-consolidation-plan.md already exists).
- Minor naming inconsistencies: decision_logger's read_decisions vs tests expecting read.

Suggested fixes (concrete)
-------------------------
1. core/decision_logger.py: add backward-compatible surface:
   - export _HEADERS = _DECISION_HEADERS
   - implement DecisionLogger.read(path: Path|str=None) -> list[Decision] wrapper calling read_decisions
   - implement DecisionLogger.read_by_session(session: str, path: Path|str=None)
   - allow DecisionLogger.log(..., path: Path|str=None) to write to custom file when provided
2. core/diagnostic_engine.py: add dataclasses TradeDiagnosis and DiagnosticAction (or alias existing classes) and a diagnose_losing_streak(trades, current_strategy=None) function delegating to DiagnosticEngine.
3. ui/dashboard.py: remove fallback password; require os.environ['DASHBOARD_PASSWORD'] or show setup instructions on first run. Do not hardcode default.
4. scripts/runners/watchdog.sh: replace absolute paths with ${UNIFIED_DIR:-$(cd "$(dirname "$0")/../../" && pwd)} or similar; have the watchdog check a heartbeat file or `pgrep -f 'main.py' && test $(stat -f %m logs/heartbeat.txt) -gt $(expr $(date +%s) - 180)`; or have main.py touch a heartbeat file every minute. Limit restarts and write clear logs.
5. Remove or relocate nested .github directory under .github/skills/get-stuff-done-for-github-copilot/.github — move templates to .github/templates or delete if not needed.
6. Adaptive engine: enforce stop loss floor when computing suggested stop values; add unit tests ensuring stop_loss >= 10 pts and strategy plugin contract returns required keys.

Action items (short)
--------------------
- [ ] Add compatibility shims in core/decs/diagnostic modules (high priority). Fix tests. (Estimated: 1-2 dev hours)
- [ ] Remove hardcoded dashboard password; require env var and document. (30–60 min)
- [ ] Update watchdog to use repo-relative paths and heartbeat check (1–2 hrs)
- [ ] Remove nested .github folder from .github/skills and run `git status` to confirm no stray metadata (15 min)
- [ ] Add unit tests verifying adaptive engine never proposes stop_loss < 10 and strategy plugin contract. (1–2 hrs)

Test failures (raw)
-------------------
- pytest collection errors (examples):
  - ImportError: cannot import name '_HEADERS' from core.decision_logger
  - ImportError: cannot import name 'diagnose_losing_streak' from core.diagnostic_engine
  (See pytest run for full trace.)

Notes on safety & RULES.md
-------------------------
- RULES.md is clear and must remain authoritative. Any adaptive logic that mutates strategy parameters at runtime MUST validate against RULES.md (stop_loss >= 10 pts, PaperTrader.position is SSOT, PnL includes fees). Add automated guards where adaptive recommendations are applied.

Streamlit dashboard quick fixes
------------------------------
- Require DASHBOARD_PASSWORD env var
- Avoid st.rerun loops on login failure (rate-limit login attempts)
- Ensure .env is gitignored and secrets not committed

Adaptive engine & watchdog
--------------------------
- main.py already updates mon.last_tick_at; prefer a simple heartbeat file touched by main.py every 30s and read by watchdog to decide restart vs log-only.
- Adaptive engine should log any parameter changes to DecisionLogger (audit trail). Ensure DecisionLogger supports this programmatically (see API mismatch above).

Appendix: Immediate quick patches
--------------------------------
- Create small compatibility wrappers in core/decision_logger.py and core/diagnostic_engine.py to restore test passability.

-- End of REVIEW --
