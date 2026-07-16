# Research Knowledge — Overview

**Canonical location:** `docs/research/`

Research is **not** the same as Architecture Decision Records (ADRs).

| Dimension | ADR | Research |
|-----------|-----|----------|
| Purpose | Record a design decision | Test a hypothesis |
| Stability | Intentionally stable | Provisional — updated as evidence accumulates |
| Content | Architecture, theory, governance | Dataset, method, statistical results |
| Lifecycle | Accepted → rarely changed | In Progress → (Accepted / Rejected / Superseded) |
| Authority | Binding — system should follow this | Advisory — informs future ADRs |
| Evidence level | E6 (decision engine) or higher | E0–E5 (pre-decision) |

## Lifecycle

```
Idea
  │
  ▼
In Progress
  │
  ├── Data collection ongoing
  ├── Schema defined
  └── Method specified
  │
  ▼
Accepted  ────→ Superseded (when replaced by better evidence)
     or
Rejected
```

A rejected hypothesis is a successful research outcome. Negative results are permanent
assets — they prevent re-proposal and build institutional knowledge. (See ADR-016.)

Research is organized into numbered entries (R-001, R-002, ...) in three directories:

- in-progress/ — active research with data collection underway
- accepted/ — hypothesis supported by sufficient evidence
- rejected/ — hypothesis not supported; negative results preserved

## Relationship to ADRs

Research informs ADRs, but does not replace them.

- A research entry at **Accepted** may lead to a new ADR that promotes an indicator from Shadow to Decision Engine.
- A research entry at **Rejected** prevents the same indicator from being re-proposed.
- ADR-015 provides the theoretical framework for evaluating indicators.
- ADR-016 defines the evidence lifecycle that every research entry follows.
