# Agent Architecture & Topology

Nexus OVAEL employs a hierarchical multi-agent architecture with strict supervisory boundaries, deterministic token budgeting, and zero prompt-leakage contracts.

## Multi-Agent Roles

- **NavigatorAgent**: Analyzes learner state, determines knowledge boundaries, and forms the navigation plan. Role: maker.
- **IrisAgent**: Generates counterfactual causal gap diagnostics and rival explanations. Role: checker.
- **TutorAgent**: Delivers calibrated Socratic pedagogical dialogue and worked examples. Role: executor.
- **TravAgent**: Audits cross-lead consistency, verifies provenance boundaries, and signs execution traces. Role: auditor.
- **SamAgent**: Assesses student responses, detects misconceptions, and evaluates confidence calibration.
- **CarlAgent**: Solves Bayesian knowledge state transitions and curriculum prerequisite graphs.
- **MemoryCuratorAgent**: Compacts grounded epistemic state and enforces vectorless privacy boundaries.
- **XAgent**: Dispatches bounded ephemeral specialists under strict token budgets.

## Segregation of Duties

The system enforces strict segregation between plan generation and verification:
- The maker role is held by NavigatorAgent.
- The checker role is held by IrisAgent.
These roles are strictly partitioned and cannot be executed by the same agent.

## Capabilities
- Bayesian Knowledge Tracing across continuous concept graphs.
- Counterfactual causal gap forensics with falsification checks.
- Cross-runtime travel across Model Context Protocol, Lyzr, CrewAI, and OpenAI SDK.

## Constraints
- Zero raw learner prompt leakage to external logs.
- Ephemeral specialist quota capped at 3 per lead, 12 total per turn.
- Quality gates must pass before any teaching move is emitted.
