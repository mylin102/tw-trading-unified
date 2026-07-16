# Taiwan Trading Unified - AI Operational Rules

## 🛡️ Financial Safety & Integrity
You are working on a Taiwan futures + options trading system (Shioaji). **BUGS = REAL FINANCIAL LOSS.**
- **Single Source of Truth**: `PaperTrader.position` is absolute.
- **PnL Integrity**: MUST include broker fees + exchange fees + tax.
- **Entry Guards**: Check `position == 0`, `margin sufficient`, `price > 0`, and `not same bar`.
- **Exit Guards**: Zero position BEFORE logging; pass explicit quantity.
- **Live/Paper Execution Boundary**: NEVER gate live order paths with paper-only components (e.g., `paper_fill_sim`). Live code must be able to run with `paper_fill_sim=None`.
- **Deferred Strategy Sync**: In live mode, strategy internal state (e.g., `_has_position`) MUST NOT be synced immediately upon submission. Wait for confirmed broker fill callbacks (`on_fill`) to ensure state and price integrity.
- **MTS Multi-Leg Sync**: For multi-leg spread trades, use a tracking dictionary (e.g., `_mts_pending_fills`) to verify BOTH legs are filled before syncing the overall strategy state.
- **Stop Loss**: Offset >= 10 pts (TMF round-trip cost ~8 pts).
- **Capital Limit**: Paper mode max 100,000 TWD.

## 🚀 GSD (Get Shit Done) Spirit: Spec-Driven Development
To prevent "Context Rot" and logic drift in complex trading modules:
1. **Research First**: Map dependencies using `/gsd-map-codebase` before refactoring.
2. **Requirements -> Roadmap**: For new features, always create/update `.planning/PROJECT.md`.
3. **Atomic Waves**: Execute large changes in small, verified "waves" to keep context fresh.
4. **Precision Prompting**: Use structured XML-style plans for any change involving more than 3 files.

## 🛠️ gstack Spirit: Engineering Ops Rigor
Maintain a production-ready codebase through automated verification:
1. **Health Checks**: Run `/health` or `python3 -m pytest tests/ -v` before and after ANY change.
2. **Systematic Debugging**: Use `/investigate` for signal/execution anomalies. No "vibe-fixing" without a root cause.
3. **Safety Guards**: Keep `/guard` active for destructive shell commands.
4. **UI Validation**: Use `/browse` to verify Streamlit dashboards after frontend changes.

## 📝 Coding Standards
- **Imports**: Never use `from datetime import datetime` if `timedelta` is also needed.
- **Comments**: Add concise technical rationale comments for all modifications.
- **No Hardcoding Rule**: **CRITICAL.** NEVER hardcode trading instruments (e.g., "TMF", "MXF", "TXO") in logic or function defaults. All product tickers MUST be strictly derived from configuration files (`config/*.yaml`) or passed dynamically via parameters. If a ticker is missing in config, the system must fail-fast with a clear error rather than falling back to a hardcoded string.
- **Side Effects**: Write to CSV/logs ONLY after operation success.
- **Plugin Protocol**: Strategy plugins must return `{"action", "reason", "stop_loss"}` or `None`.
- **Code Attribution**: EVERY code modification MUST include a comment specifying the timestamp (ISO 8601 or YYYY-MM-DD) and the author ("Gemini CLI").

### 📜 12 Development Rules (Universal)
These rules apply to every task in this project unless explicitly overridden.
Bias: caution over speed on non-trivial work. Use judgment on trivial tasks.

1.  **Rule 1 — Think Before Coding**: State assumptions explicitly. If uncertain, ask rather than guess. Present multiple interpretations when ambiguity exists. Push back when a simpler approach exists. Stop when confused. Name what's unclear.
2.  **Rule 2 — Simplicity First**: Minimum code that solves the problem. Nothing speculative. No features beyond what was asked. No abstractions for single-use code.
3.  **Rule 3 — Surgical Changes**: Touch only what you must. Clean up only your own mess. Don't "improve" adjacent code, comments, or formatting. Don't refactor what isn't broken. Match existing style.
4.  **Rule 4 — Goal-Driven Execution**: Define success criteria. Loop until verified. Don't follow steps. Define success and iterate.
5.  **Rule 5 — Use the model only for judgment calls**: Use for classification, drafting, summarization, extraction. Do NOT use for routing, retries, deterministic transforms. If code can answer, code answers.
6.  **Rule 6 — Token budgets are not advisory**: Per-task: 4,000 tokens. Per-session: 30,000 tokens. If approaching budget, summarize and start fresh. Surface the breach.
7.  **Rule 7 — Surface conflicts, don't average them**: If two patterns contradict, pick one (more recent / more tested). Explain why. Flag the other for cleanup.
8.  **Rule 8 — Read before you write**: Before adding code, read exports, immediate callers, shared utilities. "Looks orthogonal" is dangerous.
9.  **Rule 9 — Tests verify intent, not just behavior**: Tests must encode WHY behavior matters, not just WHAT it does. A test that can't fail when business logic changes is wrong.
10. **Rule 10 — Checkpoint after every significant step**: Summarize what was done, what's verified, what's left. Don't continue from a state you can't describe back.
11. **Rule 11 — Match the codebase's conventions, even if you disagree**: Conformance > taste inside the codebase. If you genuinely think a convention is harmful, surface it.
12. **Rule 12 — Fail loud**: "Completed" is wrong if anything was skipped silently. "Tests pass" is wrong if any were skipped. Default to surfacing uncertainty.

## 🧠 Cognitive Infrastructure Mandate (CRITICAL)
To prevent "Context Collapse" and ensure deterministic reasoning, **the Agent MUST read the following files at the start of every session:**
1.  `AGENTS.md`: Behavioral constraints and cognitive constitution.
2.  `BOUNDED_CONTEXTS.md`: Ownership boundaries and service layout.
3.  `docs/adrs/`: Architecture Decision Records (Persistent Memory).
4.  `RULES.md`: Core trading and safety rules.

**SSOT**: All Architectural Decision Records live in `docs/adrs/`. `docs/adrs/` is the canonical location.

**Failure to read these files is a violation of the system's "Digital Constitution".**

**Mandate**: Read ALL Cognitive Infrastructure files listed above before every execution wave.
