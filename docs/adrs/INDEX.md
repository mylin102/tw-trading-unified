# Architecture Decision Records — Index

**Canonical location:** `docs/adrs/`
**Format:** `ADR-NNN-kebab-case-title.md`

All ADRs share a sequential numeric namespace. Numbers are permanent identifiers — renumbering is avoided unless resolving collisions.

---

## Renumbering History

| Old Number | New Number | Reason | Date |
|------------|-----------|--------|------|
| ADR-007 | ADR-004 | Collision: two ADRs assigned 007 | 2026-07-16 |
| ADR-008 | ADR-014 | Collision: two ADRs assigned 008 | 2026-07-16 |
| ADR-009 | ADR-012 | Collision: two ADRs assigned 009 | 2026-07-16 |

---

## Active ADRs

| ID | Title | Category | Layer | Status |
|----|-------|----------|-------|--------|
| ADR-001 | Disable ThetaGang Strategy | Options | Execution | Active |
| ADR-002 | Vertical Spread as Default Options Execution | Options | Execution | Active |
| ADR-003 | Router Trace — Per-Bar Decision Observability | Observability | Architecture | Active |
| ADR-004 | Test Leakage Prevention and Order Export Isolation | Testing | Governance | Active |
| ADR-005 | MTS Synchronization and Real-Time Execution Fix | MTS | Execution | Active |
| ADR-006 | TMF Spread Strategy Risk Decomposition | MTS | Architecture | Active |
| ADR-007 | MTS Manual Trade Price Authority | MTS | Architecture | Active |
| ADR-008 | ATR Standardization Across Assets | Risk | Architecture | Active |
| ADR-009 | Position Lifecycle OCA (ReleaseGroup + TrailGroup) | MTS | Architecture | Active |
| ADR-010 | Broker-Level Release OCO | MTS | Execution | Active |
| ADR-011 | OCO Ghost Order Export & Phase Injection | MTS | Execution | Active |
| ADR-012 | MTS Decoupled Risk Engine & Layered Architecture | MTS | Architecture | Active |
| ADR-013 | MTS Ghost Position Race Condition | MTS | State | Active |
| ADR-014 | Stock CANSLIM & RS Rating Integration | Stocks | Strategy | Active |
| ADR-015 | Reframe MTS Calendar Spread as Statistical Arbitrage | Theory | Framework | Active (Stable) |
| ADR-016 | Evidence-Based Indicator Lifecycle | Governance | Framework | Active |

---

## By Category

### MTS (Core Trading)
- ADR-005 — MTS Synchronization & Real-Time Fix
- ADR-006 — TMF Spread Risk Decomposition
- ADR-007 — MTS Manual Trade Price Authority
- ADR-009 — Position Lifecycle OCA
- ADR-010 — Broker-Level Release OCO
- ADR-011 — OCO Ghost Order Export
- ADR-012 — Decoupled Risk Engine Architecture
- ADR-013 — Ghost Position Race Condition

### Theory & Governance
- ADR-015 — Statistical Arbitrage Framework
- ADR-016 — Evidence-Based Indicator Lifecycle

### Options
- ADR-001 — Disable ThetaGang
- ADR-002 — Vertical Spread Default

### Stocks
- ADR-014 — CANSLIM & RS Rating Integration

### Risk
- ADR-008 — ATR Standardization

### Observability
- ADR-003 — Router Trace

### Testing
- ADR-004 — Test Leakage Prevention

---

## System Map

```
           Governance
            ADR-016
                ▲
                │
           Theory
           ADR-015
                ▲
                │
Architecture ───────────────
ADR-009  ADR-010  ADR-011
                │
                ▼
     Implementation ADRs
  ADR-005  ADR-006  ADR-007
  ADR-012  ADR-013  ADR-014
```

### Reading order

#### For developers (recommended)
1. **INDEX** — overview of what exists
2. **ADR-009 → ADR-010 → ADR-011** — understand how the system executes (lifecycle, OCO, state)
3. **ADR-015** — understand why the strategy is designed this way (Statistical Arbitrage)
4. **ADR-016** — understand what evidence qualifies a change
5. **Individual implementation ADRs** — specific features and decisions

#### For strategy researchers
1. **ADR-015** — theoretical framework
2. **ADR-016** — evidence standard
3. **Relevant implementation ADRs** — specific feature context
4. **`docs/research/`** — past experiments and negative results

---

## By Layer

### Framework (stable methodology documents)
- ADR-015 — Statistical Arbitrage Framework
- ADR-016 — Evidence-Based Indicator Lifecycle

### Architecture (system design, decision ownership)
- ADR-003 — Router Trace
- ADR-006 — TMF Spread Risk Decomposition
- ADR-007 — Manual Trade Price Authority
- ADR-008 — ATR Standardization
- ADR-009 — Position Lifecycle OCA
- ADR-012 — Risk Engine Architecture

### Execution (correctness, order flow)
- ADR-001 — Disable ThetaGang
- ADR-002 — Vertical Spread Default
- ADR-005 — MTS Synchronization
- ADR-010 — Broker-Level Release OCO
- ADR-011 — OCO Ghost Order Export

### State (consistency, invariants)
- ADR-013 — Ghost Position Race Condition

### Governance (testing, research process)
- ADR-004 — Test Leakage Prevention
- ADR-016 — Evidence-Based Indicator Lifecycle

### Strategy (trading logic)
- ADR-014 — Stock CANSLIM Integration
