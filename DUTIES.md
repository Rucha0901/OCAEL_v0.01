# Duties

System-wide segregation of duties policy and multi-agent supervisory boundaries.

## Roles

| Role | Agent | Permissions | Description |
|------|-------|-------------|-------------|
| maker | navigator | create, submit | Synthesizes navigation plans and generates candidate Socratic moves |
| checker | iris | review, approve, reject | Validates counterfactual causal gaps and verifies quality gate invariants |
| auditor | trav | audit, report | Audits cross-agent execution provenance and verifies privacy compliance |
| executor | tutor | execute | Delivers verified pedagogical interactions to the learner |

## Conflict Matrix

No single agent may hold conflicting roles in any execution lifecycle:

- An agent assigned the maker role cannot perform verification or auditing
- An agent assigned the checker role cannot perform plan generation or execution
- An agent assigned the executor role cannot perform compliance auditing
- An agent assigned the auditor role cannot perform action execution

## Handoff Workflows

1. The maker generates the initial navigation plan and candidate instructional interventions.
2. The checker evaluates causal gap counterfactuals and verifies quality gate constraints.
3. The auditor checks trace provenance, token budgets, and data privacy redaction.
4. The executor delivers the finalized, verified pedagogical move to the external runtime.

## Isolation Policy

- **State isolation:** full
- **Credential segregation:** separate

## Enforcement

strict
