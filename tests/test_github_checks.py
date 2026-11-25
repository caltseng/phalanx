import unittest
from collections import deque
from dataclasses import replace

from phalanx.github import GitHubClient
from phalanx.github_checks import collect_github_checks
from phalanx.validation import (
    CheckKind, CheckOutcome, GateState, Phase, RequiredCheck,
)

from test_github import API, BASE, HEAD, MERGE, pull, response


ROOT = f"{API}/repos/owner/repo"
OTHER = "d" * 40
RUN = RequiredCheck("ci", CheckKind.CHECK_RUN, app_id=42)
STATUS = RequiredCheck("ci", CheckKind.STATUS)


def run(run_id, *, sha=HEAD, app=42, status="completed", conclusion="success", prs=()):
    return {
        "id": run_id, "head_sha": sha, "app": {"id": app}, "name": "ci",
        "status": status, "conclusion": conclusion, "pull_requests": list(prs),
    }


class GitHubCollectionTests(unittest.TestCase):
    def setUp(self):
        self.responses = deque()
        self.urls = []
        self.client = GitHubClient("owner", "repo", token="fake", api_url=API,
                                   transport=self.send)

    def send(self, request, timeout):
        self.assertEqual(request.method, "GET")
        self.urls.append(request.full_url)
        return self.responses.popleft()

    def collect(self, policy, *pages, phase=Phase.DRAFT, draft=True,
                merge=None, after=None):
        snapshot = pull(draft=draft)
        if merge:
            snapshot.update(mergeable=True, merge_commit_sha=merge)
        self.responses.append(response(snapshot))
        expected = self.client.get_pull_request(7)
        self.urls.clear()
        self.responses.append(response(snapshot))
        self.responses.extend(pages)
        if after is not False:
            self.responses.append(response(after if after is not None else snapshot))
        result = collect_github_checks(self.client, expected, phase, policy)
        self.assertFalse(self.responses)
        return result

    def test_ci_with_same_name_needs_both_sources_and_uses_newest_unknown_status(self):
        result = self.collect(
            (RUN, STATUS),
            response({"check_runs": [run(11)]}),
            response([{"context": "ci", "state": "future"}, {"context": "ci", "state": "success"}]),
            phase=Phase.READY, draft=False,
        )
        self.assertEqual(result.gate.state, GateState.WAITING)
        self.assertEqual([row.outcome for row in result.observations],
                         [CheckOutcome.PASSED, CheckOutcome.PENDING])

    def test_newer_run_on_second_page_prevents_old_pass(self):
        next_url = f"{ROOT}/commits/{HEAD}/check-runs?page=2"
        result = self.collect(
            (RUN,),
            response({"check_runs": [run(11)]}, next_url=next_url),
            response({"check_runs": [run(13, status="queued"), run(14, app=99)]}),
        )
        self.assertIn("filter=all", self.urls[1])
        self.assertEqual(self.urls[2], next_url)
        self.assertEqual(result.gate.state, GateState.WAITING)
        self.assertTrue(all(row.phase == Phase.DRAFT for row in result.observations))

    def test_unknown_newest_conclusion_waits_and_known_failure_fails(self):
        for conclusion, state in (("future", GateState.WAITING), ("failure", GateState.FAILED)):
            with self.subTest(conclusion=conclusion):
                result = self.collect(
                    (RUN,), response({"check_runs": [run(10), run(11, conclusion=conclusion)]}),
                )
                self.assertEqual(result.gate.state, state)

    def test_app_bound_status_cannot_use_unattributed_success(self):
        result = self.collect(
            (replace(STATUS, app_id=42),),
            response([{"context": "ci", "state": "success"}]), after=False,
        )
        self.assertEqual(result.gate.state, GateState.BLOCKED)
        self.assertIn("source app", result.gate.reasons[0])

    def test_rejects_runs_for_other_pr_or_revision(self):
        association = {
            "number": 7, "head": {"sha": HEAD, "ref": "phalanx/topic"},
            "base": {"sha": OTHER, "ref": "main"},
        }
        result = self.collect(
            (RUN,), response({"check_runs": [
                run(10, sha=OTHER), run(11, prs=({"number": 8},)),
                run(12, prs=(association,)),
            ]}),
        )
        self.assertEqual(result.gate.state, GateState.WAITING)
        self.assertEqual(result.observations, ())

    def test_exact_test_merge_for_ready_check(self):
        result = self.collect(
            (replace(RUN, use_merge_sha=True),),
            response({"sha": MERGE, "parents": [{"sha": BASE}, {"sha": HEAD}]}),
            response({"check_runs": [run(11, sha=MERGE)]}),
            phase=Phase.READY, draft=False, merge=MERGE,
        )
        self.assertEqual(result.gate.state, GateState.SATISFIED)
        self.assertEqual(result.candidate.merge_sha, MERGE)
        self.assertEqual({e.tested_sha for e in result.observations}, {MERGE})

    def test_wrong_merge_parents_wait_without_head_fallback(self):
        result = self.collect(
            (replace(RUN, use_merge_sha=True),),
            response({"sha": MERGE, "parents": [{"sha": OTHER}, {"sha": HEAD}]}),
            merge=MERGE,
        )
        self.assertEqual(result.gate.state, GateState.WAITING)
        self.assertIn("test merge SHA", result.gate.reasons[0])

    def test_explicit_empty_unknown_policy_and_inaccessible_reads_are_distinct(self):
        self.assertEqual(self.collect(()).gate.state, GateState.NOT_CONFIGURED)
        self.assertEqual(self.collect(None).gate.state, GateState.BLOCKED)
        denied = self.collect((STATUS,), response({}, status=403), after=False)
        self.assertEqual(denied.gate.state, GateState.BLOCKED)
        self.assertIn("403", denied.gate.reasons[0])
        self.assertEqual(self.collect((STATUS,), response([])).gate.state, GateState.WAITING)

    def test_ready_checks_are_ignored_while_draft_and_missing_draft_check_on_ready_is_skipped(self):
        self.assertEqual(self.collect((RUN,), phase=Phase.READY).gate.state, GateState.WAITING)
        result = self.collect((RUN,), response({"check_runs": []}), draft=False)
        self.assertEqual((result.gate.state, len(result.gate.skipped)), (GateState.SATISFIED, 1))

    def test_mid_collection_base_change_discards_passing_evidence(self):
        moved = pull()
        moved["base"]["sha"] = OTHER
        result = self.collect((RUN,), response({"check_runs": [run(10)]}), after=moved)
        self.assertEqual(result.gate.state, GateState.BLOCKED)
        self.assertEqual(result.observations, ())

    def test_moved_target_tip_does_not_block_ready_or_draft_evidence(self):
        for phase, draft in ((Phase.READY, False), (Phase.DRAFT, True)):
            with self.subTest(phase=phase):
                result = self.collect((RUN,), response({"check_runs": [run(10)]}),
                                      phase=phase, draft=draft)
                self.assertEqual(result.gate.state, GateState.SATISFIED)
                self.assertFalse(any("/git/ref" in url for url in self.urls))

    def test_page_budget_discards_partial_passing_evidence(self):
        pages = [
            response({"check_runs": [run(11)]},
                     next_url=f"{ROOT}/commits/{HEAD}/check-runs?page={page + 1}")
            for page in range(1, 11)
        ]
        result = self.collect((RUN,), *pages, after=False)
        self.assertEqual(result.gate.state, GateState.BLOCKED)
        self.assertIn("pagination limit", result.gate.reasons[0])
        self.assertEqual(result.observations, ())
