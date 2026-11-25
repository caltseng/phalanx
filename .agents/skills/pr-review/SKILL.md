---
name: pr-review
description: Review a Phalanx pull request at its exact head, putting simplicity and reliability first, then the rest of AGENTS.md, and decide merge, fix round, or follow-up. Use for every PR review or merge decision in this repository.
---

# Phalanx pull request review

Review every PR this way, including docs, plan, and test-only PRs. [AGENTS.md](../../../AGENTS.md) is the source of the principles; [SPEC.md](../../../SPEC.md), [STATE_MACHINE.md](../../../STATE_MACHINE.md), and [IMPLEMENTATION_PLAN.md](../../../IMPLEMENTATION_PLAN.md) are the contracts. Don't restate them in findings; cite them.

## Pin the exact head

Record the head SHA first and review only that commit (`git fetch origin pull/<n>/head`, `git diff origin/main...<sha>`). If the head moves, restart. Findings, CI evidence, and the merge decision all bind to that one SHA, and all required CI jobs must pass on it.

Any qualified reviewer independent of the author may provide final exact-head review; no native
session or person is a universal dependency. Reuse valid recorded approval covering the current
exact head and effective scope with no unresolved actionable findings instead of adding another
review. Preserve genuine GitHub required reviewers, CODEOWNERS and protections. A preferred
reporting recipient or stale routing note does not make that reviewer exclusive.
Authorized writers may choose and reach a qualified independent peer or read-only review agent
with no authorship without Captain assignment. Record reviewer identity, verdict, scope and final
SHA; self-review is never substitute independent approval.

## What matters most

1. **Simplicity.** Is this the smallest design that keeps state correct and lets interrupted work resume? For every new abstraction, setting, gate, state, journal field, lane, or helper, ask what concrete problem it solves today. Prefer reusing what is already on `main` over adding a parallel mechanism. Name code that can be deleted or merged without losing correctness. Over-engineering is a finding, not a style nit.
2. **Reliability without over-constraining.** Strict checks that refuse, raise, or block on harmless edge cases are bugs: they turn normal variation into stuck work. For each new validation, refusal, or "reconcile manually" error, identify the supported invariant it protects and a concrete reachable failure without it. Where no safeguard is needed, the code should accept it, normalize it, or recover by itself. Prefer self-recovery and retry to escalation.
3. **Correctness.** Report concrete defects with file:line and a failure scenario: lost or corrupted state, duplicate writers, leases that renew without progress, interrupted work that cannot resume, non-idempotent effects replayed without reconciling, ambiguous outcomes treated as settled, or drift from the specs (including `state-machine.html` when transitions change).
4. **The rest of AGENTS.md.** Scoped constraints, self-resolution, keep the muster moving, never fail silently, delivery over perfection, and the server/runner/runtime boundaries. Cite the principle a finding breaks.

## Complexity-killer pass

Apply the canonical [complexity-killer skill](../complexity-killer/SKILL.md)'s evidence-based method at the pinned head, rather than a separate audit checklist. Keep the pass proportionate to the actual PR scope: trace affected paths and their aggregate interactions with existing callers, state, checks and gates, not just each addition in isolation. For docs, plan or test-only changes, examine the contracts or constraints they introduce; do not turn every review into a repository-wide audit. No new agents, dependencies or framework are required.

Use that method to challenge speculative edge cases, obsolete compatibility, unnecessary blockers, duplicated state/checks and avoidable indirection. Preserve essential correctness, security, authorization, privacy, concurrency, idempotency, data-preservation and recovery safeguards; rarity or a passing happy path does not justify removing one. Require concrete reachable evidence, the supported requirement or invariant, and a simpler alternative that names behavior preserved and removed. Fold supported findings into the existing **blocking** or **follow-up** output below, with focused validation and material uncertainty; keep unsupported suspicions separate and state when there are no supported findings. The pass adds no approval gate or implementation authority.

## Scope and tests

- Match the PR to its IMPLEMENTATION_PLAN.md row, but expect no plan edits in feature PRs. Flag extra scope and acceptance claimed without evidence. End-to-end validation belongs to the PH-V1 gate, not to each PR.
- Behavior changes need a few focused tests that would fail without the change. Tests must be deterministic and host-independent, and must not just restate the implementation. Run the relevant modules with `python -m unittest discover -s tests -p test_x.py`, as AGENTS.md describes. The full suite runs in CI.

## Decide

Rank findings by impact as **blocking** or **follow-up**. Skip style nits. Say explicitly when nothing blocks.

- **Merge** only after the item's current target and publication mode authorize check-in and the [canonical merge decision gates](../../../docs/writer-operations.md#merge-decision-before-auto-merge) pass at the exact head. Until then, publish and retain the writer in delivery with auto-merge off. Re-read the current execution and GitHub head immediately before `gh pr merge <n> --auto --merge --match-head-commit <sha>`; respect repository-specific merge-owner restrictions and never use an admin bypass. Disable auto-merge before rework and reassess a changed head. `ready_for_check_in` leaves merging to the owner; never enable auto-merge or merge it. Draft-only publication never authorizes promotion or merging. A review recommendation does not override those limits.
- **Fix round** otherwise: give file:line, the failure scenario, and the simplest fix direction. Prefer fixes that remove code or constraints over fixes that add them.
- **Bound it.** Ordinary performance targets have a two-round optimization budget. Retain baseline/round measurements, remaining numeric miss and a concrete accountable follow-up; never claim a missed number was met. Explicit safety/SLO/resource-contract gates stay hard, as do correctness, required CI and independent exact-head review. Recorded acceptance criteria need an owner-approved change through [supported versioned controls](../../../docs/writer-operations.md#bounded-performance-work), not an implicit waiver after two reviews. Deliver the authorized verified scope within the current target/publication ceiling, and pass follow-up plan updates to the Captain. Merge only when authorized above.
  Retain test-set identity (exact test IDs or a manifest/digest and count); changed sets are unmatched
  evidence, not a speedup. An aspirational timing or percentage miss is not a failed hard contract.
  Already-ordinary criteria or recorded owner classification need no redundant approval. Explicit
  current owner check-in direction supersedes contradictory historical instructions; unrelated
  service/control upgrades are not prerequisites. Genuine unresolved authority or requirement
  decisions use Needs-you with reason, options, actor and resume trigger, never a hidden external wait.