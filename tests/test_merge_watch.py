import os
import unittest
from types import SimpleNamespace
from unittest import mock

from phalanx import merge_watch
from phalanx.attended import AttendedRunner
from phalanx.check_in import CheckInOutcome
from phalanx.github import PullRequest
from phalanx.merge_watch import MAX_BACKOFF_SECONDS, MergeWatch
from phalanx.runner_errors import RunnerError

HEAD, BASE, MERGE = (letter * 40 for letter in "abe")
URL = "https://github.com/owner/repo/pull/7"


def pull(**fields):
    values = dict(number=7, node_id="PR_node", url=URL, state="closed", draft=False, merged=True,
                  head_sha=HEAD, head_ref="phalanx/topic", base_sha=BASE, base_ref="main",
                  head_owner="owner", head_repository="owner/repo", base_repository="owner/repo", mergeable=None,
                  merge_commit_sha=MERGE, merged_at="2026-11-23T12:19:00Z")
    values.update(fields)
    return PullRequest(**values)


class FakeAPI:
    def __init__(self, rows):
        self.rows, self.posts, self.conflict = rows, [], False

    def call(self, path, body=None, *, key=None):
        if body is None:
            return self.rows
        if self.conflict:
            raise RunnerError(f"Runner API returned HTTP 409 for {path}")
        self.posts.append((path, body, key))
        return {}


class MergeWatchTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.outcomes = []
        self.api = FakeAPI([{"item_id": "item", "execution_id": "exec", "version": 4, "status": "queued",
                             "stage": "adversarial_review", "repository_id": "repo", "primary_pr": URL,
                             "head_ref": "phalanx/topic", "target_branch": "main", "heads": [HEAD]}])
        self.watch = MergeWatch(self.api, {"repo"}, interval=120, token=lambda: "token",
                                read=self.read, clock=lambda: self.now)

    def read(self, token, url, head_ref, base_ref, heads):
        self.assertEqual((url, head_ref, base_ref, heads), (URL, "phalanx/topic", "main", [HEAD]))
        return self.outcomes.pop(0)

    def test_merge_is_reported_once_per_interval_within_the_latency_target(self):
        self.outcomes = [CheckInOutcome("pending", "open", pull(merged=False, state="open")),
                         CheckInOutcome("merged", "merged", pull())]
        self.assertEqual(self.watch.sweep(), [])
        self.now = 60
        self.assertEqual(self.watch.sweep(), [])  # not due yet: no extra GitHub read
        self.now = 120
        self.assertEqual([row["status"] for row in self.watch.sweep()], ["merged"])
        path, body, key = self.api.posts[0]
        self.assertEqual(path, "/items/item/executions/exec/pr-merged")
        self.assertEqual((body["expected_version"], body["detected_by"], body["pull"]["merge_commit"],
                          body["merged_at"]), (4, "watch", MERGE, "2026-11-23T12:19:00Z"))
        self.assertEqual(key, f"merge:exec:4:{MERGE}")

    def test_forge_errors_back_off_and_never_count_as_merged(self):
        self.outcomes = [CheckInOutcome("pending", "GitHub returned HTTP 502") for _ in range(3)]
        for expected_delay in (120, 240, 480):
            with self.assertRaisesRegex(RunnerError, f"next merge read in {expected_delay}s"):
                self.watch.sweep()
            self.now += expected_delay - 1
            self.assertEqual(self.watch.sweep(), [])
            self.now += 1
        self.assertEqual(self.api.posts, [])
        self.assertLessEqual(max(due for due, _ in self.watch.due.values()) - self.now, MAX_BACKOFF_SECONDS)

    def test_rate_limit_retry_after_is_honored(self):
        self.outcomes = [CheckInOutcome("pending", "GitHub returned HTTP 429", retry_after_seconds=600)]
        with self.assertRaisesRegex(RunnerError, "next merge read in 600s"):
            self.watch.sweep()

    def test_mismatch_is_reported_and_a_stale_version_conflict_is_ignored(self):
        self.outcomes = [CheckInOutcome("mismatch", "other head", pull(head_sha="d" * 40))]
        self.api.conflict = True
        self.assertEqual([row["status"] for row in self.watch.sweep()], ["mismatch"])

    def test_unmapped_repositories_and_missing_token(self):
        self.api.rows[0]["repository_id"] = "elsewhere"
        self.assertEqual(self.watch.sweep(), [])
        self.watch.token = lambda: None
        with self.assertRaisesRegex(RunnerError, "PHALANX_GITHUB_TOKEN"):
            self.watch.sweep()


class AdmissionTests(unittest.TestCase):
    def snapshot(self, stage="adversarial_review", primary_pr=URL):
        return {"claim": {"item_id": "item", "request": {"target_branch": "main"},
                          "execution": {"id": "exec", "version": 9, "state": {
                              "stage": stage, "primary_pr": primary_pr, "candidate": HEAD,
                              "ready_revision": {"head": HEAD, "base": BASE}}}}}

    def admit(self, snapshot, outcome):
        api = FakeAPI([])
        runner = SimpleNamespace(api=api)
        with mock.patch.dict(os.environ, {"PHALANX_GITHUB_TOKEN": "token"}), \
                mock.patch.object(merge_watch, "observe", return_value=outcome) as read:
            result = AttendedRunner._merged_before_admission(runner, snapshot)
        return result, api.posts, read

    def test_a_queued_stage_for_a_merged_pr_is_reported_instead_of_started(self):
        result, posts, read = self.admit(self.snapshot(), CheckInOutcome("merged", "merged", pull()))
        self.assertEqual(result["status"], "merge_observed")
        self.assertEqual((posts[0][1]["detected_by"], posts[0][1]["expected_version"]), ("admission", 9))
        self.assertEqual((read.call_args.args[1], read.call_args.args[3], read.call_args.args[4]),
                         (URL, "main", {HEAD}))

    def test_open_unreadable_or_unwatched_prs_admit_the_stage(self):
        for outcome in (CheckInOutcome("pending", "open", pull(merged=False, state="open")),
                        CheckInOutcome("pending", "GitHub returned HTTP 502")):
            self.assertEqual(self.admit(self.snapshot(), outcome)[:2], (None, []))
        merged = CheckInOutcome("merged", "merged", pull())
        for snapshot in (self.snapshot(stage="check_in"), self.snapshot(primary_pr=None)):
            result, posts, read = self.admit(snapshot, merged)
            self.assertEqual((result, posts, read.called), (None, [], False))


if __name__ == "__main__":
    unittest.main()
