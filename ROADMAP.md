# Phalanx roadmap

This is the order of the big steps. Each stage links to the specs that define it and the [implementation plan](IMPLEMENTATION_PLAN.md) rows that deliver it. Decisions are logged in [docs/decisions.md](docs/decisions.md).

**v1 comes first.** No hosting or multi-user work starts until v1 is done, validated,
polished and running smoothly (decision 51). Decision 57 explicitly allows the local
dashboard during stage 2; hosting/sign-in remain later work.

| Stage | Goal | Defined in | Plan rows | Status |
| --- | --- | --- | --- | --- |
| **1. v1 local release** | One person runs a large queue with many concurrent workers on one machine, from dispatch through a verified PR or stored report, recovering from restarts without duplicate work. | [SPEC.md](SPEC.md), [STATE_MACHINE.md](STATE_MACHINE.md) | Every row up to PH-V1, plus PH-19c | **Done**: PH-V1 accepted and PH-19c delivered; historical evidence is in [decisions](docs/decisions.md). |
| **2. v1 polished and running smoothly** | Phalanx delivers its own backlog through the direct muster with the owner only watching. **Done when** PH-V1b and PH-V1c pass and `main` CI stays green throughout (decision 53). Fix blocking findings first; send other work to the backlog. Both PH-DS5 removals are delivered; this does not certify remaining live acceptance cases. | [SPEC.md](SPEC.md), [direct pilot evidence](docs/pilots/direct-sessions.md) | PH-V1a, PH-V1b, PH-V1c, PH-DS4/5, PH-26 (report required; code optional and only if simple) | **In progress** |
| **3. Hosted for one user** | Run the same server on the internet over HTTPS. The owner signs in with Google, then watches and acts on work from anywhere. | [SPEC.md](SPEC.md#operator-view), [DASHBOARD.md](DASHBOARD.md) phases D1 and D2 | PH-20, PH-27, PH-29, PH-30, PH-21 | After stage 2, except PH-29's local parts, which run during stage 2 (decision 57). PH-27 remains paused. |
| **4. Many users** | Other people and teams use one hosted server, each in their own workspace with roles, their own runners and their own Git credentials. | [SPEC.md](SPEC.md#users-and-workspaces), [DASHBOARD.md](DASHBOARD.md) | PH-28, PH-31 | After stage 3 |
| **5. Wider reach** | More machines, more runtimes, larger goals, and a port decision. | [SPEC.md](SPEC.md) | PH-22, PH-23, PH-24, PH-25 | Unordered, after stage 2 |

Rules for every stage:

- A stage is done when its plan rows are done and its acceptance evidence is recorded.
  Stage 1's historical acceptance report remains in Git history; retired operating
  paths are not current instructions.
- New architecture still follows [AGENTS.md](AGENTS.md) from the start, including building for many users, so later stages don't need rework.
- The Captain may reorder rows within a stage. Moving work between stages needs Caleb.
