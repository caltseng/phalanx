---
name: complexity-killer
description: Audit unnecessary code complexity and blocking behavior using supported requirements, real callers and observed effects. Use when asked to simplify code or investigate speculative edge cases, obsolete compatibility, duplicated defenses, excessive abstraction, or needless gates, waits, approvals, retries and fallbacks.
---

# Complexity killer

Find the smallest design that supports real requirements and lets useful work proceed
safely. Prioritize unnecessary blocking and maintenance burden, not line counts or style.
An audit may find nothing worth removing; never invent findings or numeric complexity
quotas.

## Establish scope and the real path

1. Read the governing user request and applicable repository instructions. Establish the
   requested scope, revision, supported consumers/platforms and behavior contracts from
   current specifications, callers and tests. Label historical documents and proposals;
   they are not current requirements by themselves.
2. Trace a representative normal operation from its entry point through callers, state
   changes, external effects and delivery or useful output. Include indirect entry points
   such as jobs, hooks and public APIs where relevant. Check actual reachability before
   calling code dead or redundant; no in-repository caller does not prove a public
   consumer absent.
3. Trace concrete blocking conditions on that path: the predicate, who controls it, what
   work it stops, its scope and duration, and the event or actor that releases it.
   Inspect failure, interruption and recovery paths where they affect the finding.
   Distinguish an observed stall from a reachable scenario and from a hypothesis.

Use focused searches, code reads, existing tests and safe reproductions. Do not trigger
production effects merely to gather evidence. No framework, dependencies, new runtime
layer or multi-agent fanout is needed to perform this audit.

## Challenge complexity against evidence

For each candidate, identify the supported requirement it serves and the effect of
removing or consolidating it:

- **Speculative or obsolete edge cases:** what reachable input/state requires this branch
  today? Check current requirements and tests, not just its age or rarity.
- **Compatibility scaffolding:** which supported consumer, version or platform still
  needs the adapter, alternate schema or legacy path? Search callers and support policy;
  state uncertainty about external consumers rather than declaring them nonexistent.
- **Repeated defenses and state:** which boundary owns validation or the source of truth?
  Determine whether checks enforce different trust boundaries or timing guarantees.
  Look for state that can drift, duplicated predicates and repeated transformations.
- **Configuration, indirection and abstraction:** which real variation or independently
  used implementation justifies the setting, wrapper, factory or layer? Compare the
  maintenance cost with a direct call, one representation or an existing shared helper.
- **Gates and waiting:** do approvals, retries, sleeps, fallbacks or prerequisites prevent
  useful work without protecting an actual invariant? Look for overbroad admission,
  unrelated work held behind a local failure, waits without a release trigger, retries
  that cannot change the outcome, and fallbacks that hide failure or postpone delivery.
  Prefer scoped recovery and visible errors over either needless escalation or false
  success. Verify authority before proposing removal of an approval.

Distinguish accidental complexity and obsolete gates from essential correctness,
security, authorization, privacy, validation, concurrency/admission, idempotency,
data-preservation and recovery safeguards. Check each against concrete requirements,
callers and tests, including races and interrupted or ambiguous external effects.
Do not preserve every existing branch by default, delete a guard just because it covers
an edge case, or treat a passing happy-path test as proof a safeguard is unnecessary.
A test that merely encodes old behavior is evidence to interpret, not an immutable
product requirement.

## Produce a concise, actionable audit

Lead with the most consequential unnecessary blocking, then maintenance burden. Combine
findings with the same cause. For each finding provide:

- **Location and evidence:** exact `path:line` or range at the inspected revision, relevant
  callers/entry points, concrete condition and observed effect (or explicitly labeled
  reachable scenario), with reproduction or test evidence where available.
- **Requirement assessment:** the supported contract/invariant and its source, or exactly
  where support evidence was sought and not found. Explain why this is accidental
  complexity rather than a necessary safeguard.
- **Simpler alternative and behavior:** the smallest removal, consolidation or scoped
  recovery; name behavior intentionally preserved and behavior intentionally removed.
  State impact on blocking, maintenance and any affected consumers.
- **Validation and disposition:** focused tests/checks for the proposed change, material
  uncertainty and how to resolve it; classify as actionable now, requiring a product or
  authority decision, or needing more evidence.

Keep unsupported suspicions separate from confirmed findings. State scope and evidence
limits, including paths not traced or checks not run. If no supported finding remains,
say so without padding the output with cosmetic suggestions.

## Simplify only within authorization

Auditing alone does not authorize implementation, broad refactors, weaker safeguards or
product behavior changes. For an audit-only request, stop at findings. If the governing
request authorizes remediation, address supported findings in small, coherent changes;
surface decisions outside that authority instead of silently choosing a new contract.

Reuse existing mechanisms and remove obsolete branches and directly dependent callers,
tests or documentation together. Avoid replacing a small problem with configurable
machinery. Preserve live data, trust boundaries, cancellation and recovery guarantees.
For behavior intentionally removed, update the contract only when authorized.

Run the smallest existing checks covering the affected path and a few focused regression
cases that would fail before the fix. For a removed blocker, demonstrate that useful
work now proceeds in the concrete condition while the relevant invalid, unauthorized,
concurrent or interrupted case remains safe. Check affected supported callers and report
the exact validation performed and any gaps. Follow repository review and delivery rules;
this skill creates no new approval gate and bypasses none.
