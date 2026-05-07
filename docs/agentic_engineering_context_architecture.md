# Agentic Engineering Context Architecture

## Core Thesis

> Vibe Coding raises the floor.  
> Agentic Engineering raises the ceiling.

Traditional software engineering focused on managing logic and state inside deterministic systems.

Agentic Engineering shifts the center of gravity toward:

- context
- cognition
- orchestration
- governance
- memory
- evaluation
- abstraction boundaries

The core challenge is no longer merely writing code.

It is designing systems that can safely and continuously generate, validate, repair, and evolve code and behavior over time.

---

# 1. Foundational Paradigm Shifts

## 1.1 Stateless ↔ Stateful Reversal

Traditional software engineering spent decades managing state:

- local variables
- databases
- caches
- distributed state
- synchronization
- transactions

Core engineering challenge:

> How do we encapsulate and control state?

LLM systems invert this model.

The model itself is largely stateless.

Every inference is effectively a fresh invocation.

What appears to be “memory” is often externalized context injected back into the model:

- memory layers
- retrieval systems
- context injection
- scratchpads
- harnesses
- summaries
- handoffs

The challenge becomes:

> How do we make a stateless cognitive runtime behave like a stateful system?

This creates the rise of:

- Context Engineering
- Memory Systems
- Compaction
- Handoff
- Subagents
- Retrieval
- Long-running cognition

State did not disappear.

State was externalized.

---

## 1.2 Code ↔ Architecture Reversal

Previously:

Architecture → Code

Senior engineers designed:

- bounded contexts
- service ownership
- identity models
- database topology
- trade-offs

Then engineers implemented them.

In the agent era:

Prompt → Generated Architecture + Code

An agent now implicitly decides:

- framework
- folder structure
- service layout
- database patterns
- infrastructure assumptions

The danger:

> Architectural decisions now happen faster than humans review them.

New technical debt increasingly comes from:

- incorrect boundaries
- hidden assumptions
- ownership confusion
- poor abstractions

Not necessarily from poor code quality.

Code is now cheap.

Architecture becomes expensive.

Therefore:

- ADRs become critical
- boundaries become critical
- governance becomes critical
- abstraction becomes critical

---

## 1.3 Deterministic ↔ Non-deterministic Reversal

Traditional engineering pursued deterministic systems:

Same input → same output.

LLMs are probabilistic systems.

This creates a new paradigm:

> Traditional software automates what you can specify.  
> LLM systems automate what you can verify.

The engineering focus shifts from:

“How to do it”

toward:

“How to validate it”

This creates the need for:

- Evals
- Reflection
- Self-verification
- Guardrails
- Sandboxing
- Retry loops
- Deterministic harnesses

Determinism moves from the model into the governance layer.

---

# 2. Bounded Context as Cognitive Boundary

Bounded Context becomes even more important in AI-native systems.

Because the largest failure mode of agents is:

## Context Collapse

Without proper boundaries:

- memory pollutes
- responsibilities blur
- hallucinations amplify
- ownership disappears
- signal-to-noise ratio degrades

Bounded Context now serves as:

- architectural boundary
- cognitive boundary
- memory isolation layer
- governance partition
- orchestration unit

Examples:

- User
- Billing
- Identity
- Notification
- Analytics
- Market Data
- Risk Management
- Lifecycle

These should remain human-defined.

Because agents accelerate both correctness and incorrectness.

---

# 3. Context-as-Architecture

## Key Principle

> Context is the new state design.

Modern AI systems increasingly operate as:

Stateless Cognitive Runtime + Externalized Context Layer

This transforms architecture into:

- context curation
- memory governance
- retrieval topology
- orchestration semantics
- evaluation infrastructure

---

# 4. Recommended Repository Structure

```text
/project-root
│
├── AGENTS.md
├── CLAUDE.md
├── ARCHITECTURE.md
├── BOUNDED_CONTEXTS.md
├── ADR/
├── MEMORY/
├── PLAYBOOK/
├── EVALS/
└── strategies/
```

---

# 5. AGENTS.md

Purpose:

Defines:

- system philosophy
- behavioral constraints
- governance rules
- architectural priorities
- forbidden patterns

Example:

```md
# AGENTS.md

## Core Principles

- Safety over aggressiveness
- Deterministic governance over probabilistic execution
- Never trust stale market data
- Recovery must be idempotent

## Forbidden Patterns

- No hidden mutable global state
- No direct broker calls from strategies
- No silent exception swallowing
```

AGENTS.md is effectively:

> Cognitive Constitution

---

# 6. BOUNDED_CONTEXTS.md

Defines ownership boundaries.

Example:

```md
# Market Data Context

Owns:
- Tick ingestion
- Session alignment
- Freshness validation

Does NOT own:
- Trading decisions
- Risk management
```

This dramatically reduces:

- agent confusion
- hallucinated ownership
- boundary leakage

---

# 7. ARCHITECTURE.md

Defines:

- system topology
- orchestration flow
- ownership model
- recovery semantics

Example:

```text
Tick → Canonicalizer → Enrichment → Router → Strategy → Lifecycle → Broker
```

---

# 8. ADR (Architecture Decision Records)

AI systems repeatedly forget previous architectural reasoning.

ADRs become:

> Persistent Architectural Memory

Examples:

- ADR-001-canonical-bar-layer.md
- ADR-002-router-owns-allocation.md
- ADR-003-no-strategy-broker-access.md

---

# 9. EVALS/

Specification increasingly becomes verification.

Example:

```text
/evals
    stale_data_eval.md
    pnl_consistency_eval.md
    risk_off_eval.md
```

---

# 10. PLAYBOOK/

Operational cognitive memory.

Examples:

- stale_feed_recovery.md
- night_session_recovery.md
- position_rebuild.md

---

# 11. Harness as Deterministic Governance

The model itself is probabilistic.

Production systems therefore require:

- retries
- checkpoints
- validation
- audit trails
- rollback
- replay
- lifecycle governance

The harness becomes the deterministic layer surrounding non-deterministic cognition.

---

# 12. Context-as-Code

Context should be:

- version controlled
- reviewable
- diffable
- testable
- governable

Example:

```text
/context
    /architecture
    /memory
    /playbooks
    /governance
    /evals
```

This creates:

> Cognitive Infrastructure Engineering

---

# 13. Final Insight

The moat of software engineering in the AI era is not raw implementation.

It is:

- abstraction
- boundary design
- governance
- orchestration
- verification
- systems thinking

You can outsource execution.

You cannot fully outsource understanding.

You can outsource implementation.

You cannot fully outsource abstraction.

Bounded Context remains the foundation.

Harness becomes deterministic governance.

Context becomes the new state design.
