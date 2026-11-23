# Phalanx roadmap

This is the order of the big steps. Each stage links to the specs that define it and the [implementation plan](IMPLEMENTATION_PLAN.md) rows that deliver it. Decisions are logged in [docs/decisions.md](docs/decisions.md).

**v1 comes first.** No hosting, multi-user or dashboard work starts until v1 is done, validated, polished and running smoothly (decision 51).

| Stage | Goal | Defined in | Plan rows | Status |
| --- | --- | --- | --- | --- |
| **1. v1 local release** | One person runs a large queue with many concurrent workers on one machine, from dispatch through a verified PR or stored report, recovering from restarts without duplicate work. | [SPEC.md](SPEC.md), [STATE_MACHINE.md](STATE_MACHINE.md) | Every row up to PH-V1, plus PH-19c | **Done**: PH-V1 passed in [#113](https://github.com/caltseng/phalanx/pull/113) and PH-19c merged in [#115](https://github.com/caltseng/phalanx/pull/115) |
| **2. v1 polished and running smoothly** | Phalanx delivers its own backlog through the muster with the owner only watching. **Done when** PH-V1b and PH-V1c pass and `main` CI stays green throughout (decision 53). Only blocking findings are fixed first: anything that forced a manual step or raw API call, stalled an item without a visible reason, or produced wrong state. Everything else goes to the backlog. | [SPEC.md](SPEC.md), [v1 acceptance findings](docs/pilots/v1-acceptance.md#findings) | PH-V1a, PH-V1b, PH-V1c, PH-26 (report required; the code fix is optional and only if simple) | **In progress** |
| **3. Hosted for one user** | Run the same server on the internet over HTTPS. The owner signs in with Google, then watches and acts on work from anywhere. | [SPEC.md](SPEC.md#operator-view), [DASHBOARD.md](DASHBOARD.md) phases D1 and D2 | PH-20, PH-27, PH-29, PH-30, PH-21 | After stage 2. PH-27 is paused, with its work in progress on branch `caltseng-ph-27-google-sign-in` at `765b326`. |
| **4. Many users** | Other people and teams use one hosted server, each in their own workspace with roles, their own runners and their own Git credentials. | [SPEC.md](SPEC.md#users-and-workspaces), [DASHBOARD.md](DASHBOARD.md) | PH-28, PH-31 | After stage 3 |
| **5. Wider reach** | More machines, more runtimes, larger goals, and a port decision. | [SPEC.md](SPEC.md) | PH-22, PH-23, PH-24, PH-25 | Unordered, after stage 2 |

Rules for every stage:

- A stage is done when its plan rows are done and its acceptance evidence is recorded, as [v1-acceptance.md](docs/pilots/v1-acceptance.md) does for stage 1.
- New architecture still follows [AGENTS.md](AGENTS.md) from the start, including building for many users, so later stages don't need rework.
- The Captain may reorder rows within a stage. Moving work between stages needs Caleb.
