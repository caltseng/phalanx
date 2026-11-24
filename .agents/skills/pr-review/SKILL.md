---
name: pr-review
description: Review a Phalanx pull request at its exact head, putting simplicity and reliability first, then the rest of AGENTS.md, and decide merge, fix round, or follow-up. Use for every PR review or merge decision in this repository.
---

# Phalanx pull request review

Review every PR this way, including docs, plan, and test-only PRs. [AGENTS.md](../../../AGENTS.md) is the source of the principles; [SPEC.md](../../../SPEC.md), [STATE_MACHINE.md](../../../STATE_MACHINE.md), and [IMPLEMENTATION_PLAN.md](../../../IMPLEMENTATION_PLAN.md) are the contracts. Don't restate them in findings; cite them.

## Pin the exact head

Record the head SHA first and review only that commit (`git fetch origin pull/<n>/head`, `git diff origin/main...<sha>`). If the head moves, restart. Findings, CI evidence, and the merge decision all bind to that one SHA, and all required CI jobs must pass on it.

## What matters most

1. **Simplicity.** Is this the smallest design that keeps state correct and lets interrupted work resume? For every new abstraction, setting, gate, state, journal field, lane, or helper, ask what concrete problem it solves today. Prefer reusing what is already on `main` over adding a parallel mechanism. Name code that can be deleted or merged without losing correctness. Over-engineering is a finding, not a style nit.
2. **Reliability without over-constraining.** Strict checks that refuse, raise, or block on harmless edge cases are bugs: they turn normal variation into stuck work. For each new validation, refusal, or "reconcile manually" error, ask whether it protects against a real duplicate writer or lost state. If not, the code should accept it, normalize it, or recover by itself. Prefer self-recovery and retry to escalation.
3. **Correctness.** Report concrete defects with file:line and a failure scenario: lost or corrupted state, duplicate writers, leases that renew without progress, interrupted work that cannot resume, non-idempotent effects replayed without reconciling, ambiguous outcomes treated as settled, or drift from the specs (including `state-machine.html` when transitions change).
4. **The rest of AGENTS.md.** Scoped constraints, self-resolution, keep the muster moving, never fail silently, delivery over perfection, and the server/runner/runtime boundaries. Cite the principle a finding breaks.

## Scope and tests

- Match the PR to its IMPLEMENTATION_PLAN.md row, but expect no plan edits in feature PRs. Flag extra scope and acceptance claimed without evidence. End-to-end validation belongs to the PH-V1 gate, not to each PR.
- Behavior changes need a few focused tests that would fail without the change. Tests must be deterministic and host-independent, and must not just restate the implementation. Run the relevant modules with `python -m unittest discover -s tests -p test_x.py`, as AGENTS.md describes. The full suite runs in CI.

## Decide

Rank findings by impact as **blocking** or **follow-up**. Skip style nits. Say explicitly when nothing blocks.

- **Merge** only when the item's current target and publication mode authorize check-in and the review passes at the exact head: `gh pr merge <n> --auto --merge --match-head-commit <sha>`, so GitHub merges when required CI passes on that same head. `ready_for_check_in` leaves merging to the owner; never enable auto-merge or merge it. Draft-only publication never authorizes promotion or merging. A review recommendation does not override those limits.
- **Fix round** otherwise: give file:line, the failure scenario, and the simplest fix direction. Prefer fixes that remove code or constraints over fixes that add them.
- **Bound it.** After two rounds, deliver the verified part within the item's target and publication mode, and file a follow-up plan row. Merge only when authorized above.