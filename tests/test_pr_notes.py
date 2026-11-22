import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from phalanx.github import ATTRIBUTION, GitHubClient
from phalanx.pr_notes import MAX_ATTEMPTS, PRNotes, known_gaps

from test_github import response

URL = "https://github.com/owner/repo/pull/7"
SHA = "a" * 40


class FakePR:
    def __init__(self):
        self.body = f"Fix tests\n\n{ATTRIBUTION}"
        self.comments = []
        self.writes = 0
        self.down = False

    def __call__(self, request, _timeout):
        if self.down:
            raise TimeoutError("GitHub unavailable")
        path = urlsplit(request.full_url).path
        if path.endswith("/issues/7/comments"):
            if request.method == "POST":
                self.writes += 1
                self.comments.append(json.loads(request.data)["body"])
                return response({}, status=201)
            return response([{"body": body} for body in self.comments])
        if request.method == "PATCH":
            self.writes += 1
            self.body = json.loads(request.data)["body"]
        return response({"body": self.body})


class PRNotesTests(unittest.TestCase):
    def setUp(self):
        self.pr = FakePR()
        self.notes = PRNotes(Path(self.enterContext(TemporaryDirectory())))
        self.enterContext(patch("phalanx.pr_notes.GitHubClient", lambda owner, repo, *, token: GitHubClient(
            owner, repo, token=token, transport=self.pr)))

    def test_findings_comment_once_and_known_gaps_track_the_latest_revision(self):
        self.notes.record(URL, SHA, "Returned to development", "Rename the helper.")
        self.assertEqual(self.notes.flush("token"), [])
        # A replay of the same revision and findings finds the earlier comment.
        self.notes.record(URL, SHA, "Returned to development", "Rename the helper.")
        self.assertEqual(self.notes.flush("token"), [])
        self.assertEqual(len(self.pr.comments), 1)
        self.assertIn(f"at `{SHA}`", self.pr.comments[0])
        self.assertIn("### Known gaps", self.pr.body)
        self.assertEqual(self.pr.body.count(ATTRIBUTION), 1)
        self.assertTrue(self.pr.body.endswith(ATTRIBUTION))
        self.notes.clear_gaps(URL)
        self.notes.flush("token")
        self.assertEqual(self.pr.body, f"Fix tests\n\n{ATTRIBUTION}")
        writes = self.pr.writes
        self.notes.clear_gaps(URL)
        self.notes.flush("token")
        self.assertEqual(self.pr.writes, writes)

    def test_failed_writes_stay_visible_and_stop_after_bounded_retries(self):
        self.notes.comment(URL, f"converted-to-draft:{SHA}", "Converted to draft.")
        self.assertEqual(self.notes.flush(None)[0]["error"], "Set PHALANX_GITHUB_TOKEN to publish PR notes")
        self.pr.down = True
        for _ in range(MAX_ATTEMPTS + 1):
            pending = self.notes.flush("token")
        self.assertEqual((pending[0]["attempts"], pending[0]["retrying"]), (MAX_ATTEMPTS, False))
        self.pr.down = False
        self.notes.comment(URL, f"converted-to-draft:{SHA}", "Converted to draft.")
        self.assertEqual(self.notes.flush("token"), [])
        self.assertEqual(len(self.pr.comments), 1)

    def test_known_gaps_replace_the_section_and_keep_one_footer_last(self):
        first = known_gaps(f"Body\n{ATTRIBUTION}", "Open: lint")
        second = known_gaps(first, "Open: tests")
        self.assertNotIn("lint", second)
        self.assertEqual(second.count("### Known gaps"), 1)
        self.assertTrue(second.startswith("Body\n\n"))
        self.assertTrue(second.endswith(f"\n\n{ATTRIBUTION}"))
        self.assertEqual(known_gaps(second, None), f"Body\n\n{ATTRIBUTION}")


if __name__ == "__main__":
    unittest.main()
