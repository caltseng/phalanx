# GitHub lifecycle provider contracts

These provider constraints inform direct publication and reconciliation. The
[worker policy](../../phalanx/prompts/worker.md#merge-decision-publication-and-delivery),
[design spec](../../SPEC.md) and [state machine](../../STATE_MACHINE.md) own authorization
and lifecycle behavior; this is not a separate runner or publication journal.

| Operation | Provider constraint |
| --- | --- |
| Push a branch | Git can guard a ref update with an explicit expected remote SHA via `--force-with-lease=<ref>:<expected>`. Read back the exact ref after an uncertain push; preserve outside changes. [Git push](https://git-scm.com/docs/git-push) |
| Create a PR | REST selects head/base by ref name, without an expected-head precondition. Verify returned repository/ref/head/base/state. Reconcile a lost response by bounded, paginated lookup, including closed or retargeted matches, before retrying. [Pull requests API](https://docs.github.com/en/rest/pulls/pulls) |
| Mark ready | GraphQL `markPullRequestReadyForReview` has no expected-head/base guard; `clientMutationId` is correlation, not an idempotency guarantee. Read the PR before and after, and revalidate a changed head. [GraphQL reference](https://docs.github.com/en/graphql/reference/mutations#markpullrequestreadyforreview) |
| Merge | REST accepts an exact head `sha` and rejects a mismatch, but has no expected-base parameter. Repository protections still apply; an atomic exact-base requirement or merge queue needs a supported path, not an assumed guarantee. [Merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request) |

Read paginated check runs and commit statuses for the verified revision and expected source
app, plus applicable branch rules. An empty aggregate status is not evidence that required
checks ran. GitHub may use a test-merge commit: correlate it with the current head/base.
Draft/ready workflow triggers and preview gates depend on the repository and integration.
Verify preview environment, revision and immutable URL rather than trusting a branch alias.

After an uncertain merge, reread the primary PR and verify target, head and resulting commit.
Squash/rebase rewrite identity, so candidate ancestry alone cannot establish delivery.
Outside pushes, deletions and retargets need reconciliation and refreshed evidence before
another write. Initial read-only provider observations did not certify live promotion,
preview races or merge-queue behavior; historical references belong in [decisions](../decisions.md#github-provider-research).
