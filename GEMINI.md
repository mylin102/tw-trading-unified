# Taiwan Trading Unified - AI Operational Rules

## 🛡️ Financial Safety & Integrity
You are working on a Taiwan futures + options trading system (Shioaji). **BUGS = REAL FINANCIAL LOSS.**
- **Single Source of Truth**: `PaperTrader.position` is absolute.
- **PnL Integrity**: MUST include broker fees + exchange fees + tax.
- **Entry Guards**: Check `position == 0`, `margin sufficient`, `price > 0`, and `not same bar`.
- **Exit Guards**: Zero position BEFORE logging; pass explicit quantity.
- **Stop Loss**: Offset >= 10 pts (TMF round-trip cost ~8 pts).
- **Capital Limit**: Paper mode max 40,000 TWD.

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
- **Side Effects**: Write to CSV/logs ONLY after operation success.
- **Plugin Protocol**: Strategy plugins must return `{"action", "reason", "stop_loss"}` or `None`.

## 🧠 Cognitive Infrastructure Mandate (CRITICAL)
To prevent "Context Collapse" and ensure deterministic reasoning, **the Agent MUST read the following files at the start of every session:**
1.  `AGENTS.md`: Behavioral constraints and cognitive constitution.
2.  `BOUNDED_CONTEXTS.md`: Ownership boundaries and service layout.
3.  `ADR/`: Architectural Decision Records (Persistent Memory).
4.  `RULES.md`: Core trading and safety rules.

**Failure to read these files is a violation of the system's "Digital Constitution".**

**Mandate**: Read ALL Cognitive Infrastructure files listed above before every execution wave.
