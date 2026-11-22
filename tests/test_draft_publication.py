from collections import deque
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from phalanx.draft_publication import DraftIntent, DraftPublisher, PublicationClaim
from phalanx.git_publication import PublicationResult
from phalanx.github import ATTRIBUTION, GitHubClient
from phalanx.workspaces import _item_branch
from test_github import response

CANDIDATE = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
BRANCH = _item_branch("item")


class GitFixture:
    expected_remote = ("github", "owner/repo")
    ref = f"refs/heads/{BRANCH}"

    def __init__(self):
        self.head = None
        self.base = BASE
        self.pushes = []
        self.on_push = lambda: None
        self.uncertain_push = False
        self.drop_push = False

    def read_head(self, *, authorize):
        authorize()
        return self.head

    def read_branch(self, branch, *, authorize):
        authorize()
        assert branch == "main"
        return self.base

    def publish(self, candidate, *, expected, authorize, rewrite=False):
        authorize()
        self.on_push()
        self.pushes.append((candidate, expected))
        if self.drop_push:
            self.drop_push = False
            return PublicationResult("uncertain", "Fixture push was lost before it applied")
        if self.head != expected:
            return PublicationResult("conflict", "Stale lease")
        self.head = candidate
        return PublicationResult("uncertain" if self.uncertain_push else "success", "Fixture push")


class DraftPublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git = GitFixture()
        self.claim = PublicationClaim("first", 3)
        self.active = self.claim
        self.intent = DraftIntent("item", "owner/repo", BRANCH, CANDIDATE, None, "main", BASE,
                                  "Title", f"Description\n{ATTRIBUTION}")
        self.client = GitHubClient("owner", "repo", token="fake", transport=self.send)
        self.pulls = {}
        self.posts = []
        self.post_effects = deque()
        self.fail_lookup = False
        self.unlisted = set()

    def runner(self):
        return DraftPublisher(
            self.directory, self.git, self.client,
            check_claim=lambda intent, claim: self.active == claim, sleep=lambda _: None,
        )

    def publish(self, intent=None):
        return self.runner().publish(intent or self.intent, self.claim)

    def successor(self):
        self.claim = self.active = PublicationClaim("successor", 4)

    def journal(self):
        with closing(sqlite3.connect(self.directory / "draft-publication.sqlite3")) as db:
            return db.execute("SELECT phase, claim, known_number, verified_number "
                              "FROM draft_publication").fetchone()

    def pull(self, number, *, base="main", head=CANDIDATE, draft=True):
        return {
            "number": number, "node_id": f"PR_{number}", "state": "open",
            "html_url": f"https://github.com/owner/repo/pull/{number}",
            "draft": draft, "merged": False, "mergeable": None, "merge_commit_sha": None,
            "head": {"ref": BRANCH, "sha": head, "user": {"login": "owner"},
                     "repo": {"full_name": "owner/repo"}},
            "base": {"ref": base, "sha": BASE, "repo": {"full_name": "owner/repo"}},
        }

    def send(self, request, _timeout):
        path = urlsplit(request.full_url)
        if request.method == "POST":
            self.assertEqual(self.journal()[0], "draft")
            self.assertEqual(json.loads(self.journal()[1])["attempt_id"], self.claim.attempt_id)
            payload = json.loads(request.data)
            self.posts.append(payload)
            self.assertTrue(payload["draft"])
            self.assertEqual(payload["body"].count(ATTRIBUTION), 1)
            number = len(self.posts) + 10
            effect = self.post_effects.popleft() if self.post_effects else "ok"
            if effect == "lost_before":
                raise TimeoutError
            if effect == "reject":
                return response({}, status=422)
            self.pulls[number] = self.pull(number)
            if effect == "lost_after":
                raise TimeoutError
            if effect == "lost_and_unreadable":
                self.fail_lookup = True
                raise TimeoutError
            if effect == "unlisted":
                self.unlisted.add(number)
            if effect == "expire":
                self.active = None
            return response(self.pulls[number], status=201)
        if self.fail_lookup:
            raise TimeoutError
        if path.path.endswith("/pulls"):
            query = parse_qs(path.query)
            self.assertEqual(query["head"], [f"owner:{BRANCH}"])
            self.assertEqual(query["state"], ["all"])
            rows = [pull for number, pull in self.pulls.items() if number not in self.unlisted
                    and ("base" not in query or pull["base"]["ref"] in query["base"])]
            return response(rows)
        return response(self.pulls[int(path.path.rsplit("/", 1)[1])])

    def test_push_and_lost_draft_response_verify_once(self):
        self.git.uncertain_push = True
        self.git.on_push = lambda: self.assertEqual(self.journal()[0], "push")
        self.post_effects.append("lost_after")
        result = self.publish()
        self.assertEqual((result.status, result.primary.number), ("verified", 11))
        self.assertEqual(self.journal()[2:], (None, 11))
        self.assertEqual(self.publish().primary.number, 11)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.git.pushes, [(CANDIDATE, None)])

    def test_successor_recovers_lost_draft_from_disk_before_superseding(self):
        self.post_effects.append("lost_and_unreadable")
        self.assertEqual(self.publish().status, "pending")
        self.assertEqual(self.journal()[0], "draft")
        self.successor()
        # A new candidate must first see the possible draft; no write happens blind.
        self.assertEqual(self.publish(replace(self.intent, candidate=OTHER)).status, "pending")
        self.assertEqual((len(self.posts), self.git.pushes), (1, [(CANDIDATE, None)]))
        self.fail_lookup = False
        self.assertEqual(self.publish().primary.number, 11)
        self.assertEqual(len(self.posts), 1)

    def test_empty_successful_lookup_permits_one_retry(self):
        self.post_effects.extend(("lost_before", "ok"))
        self.assertEqual(self.publish().primary.number, 12)
        self.assertEqual(len(self.posts), 2)

    def test_422_recovers_a_matching_draft_and_bounds_empty_retries(self):
        self.post_effects.extend(("reject", "reject"))
        self.assertEqual(self.publish().status, "pending")
        self.assertEqual(len(self.posts), 2)
        self.pulls[19] = self.pull(19)
        self.assertEqual(self.publish().primary.number, 19)
        self.assertEqual(len(self.posts), 2)

    def test_verified_primary_stays_stable_and_duplicates_are_visible(self):
        self.git.head = CANDIDATE
        self.pulls.update({n: self.pull(n) for n in (7, 5)})
        result = self.publish()
        self.assertEqual(result.primary.number, 5)
        self.assertEqual([p.number for p in result.duplicates], [7])
        self.pulls[3] = self.pull(3)
        result = self.publish()
        self.assertEqual(result.primary.number, 5)
        self.assertEqual([p.number for p in result.duplicates], [3, 7])
        self.assertEqual(self.posts, [])

    def test_known_created_number_is_verified_even_before_listing_catches_up(self):
        self.post_effects.append("unlisted")
        result = self.publish()
        self.assertEqual((result.status, result.primary.number), ("verified", 11))
        self.assertEqual(len(self.posts), 1)

    def test_head_only_search_surfaces_retargeted_pr_without_mutation(self):
        self.pulls[7] = self.pull(7, base="release")
        result = self.publish()
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.conflicts[0].base_ref, "release")
        self.assertFalse(self.git.pushes or self.posts)

    def test_rebased_candidate_replaces_journaled_tip_under_derived_lease(self):
        self.git.on_push = lambda: setattr(self, "active", None)
        self.assertEqual(self.publish().status, "claim_lost")
        self.assertEqual(self.journal()[0], "pushed")
        self.git.on_push = lambda: None
        self.successor()
        # Development rebuilt the candidate before any draft existed. The runner
        # does not know the remote tip; the journal supplies it as the only lease.
        revised = replace(self.intent, candidate=OTHER, expected_base=OTHER)
        self.git.drop_push = True
        self.assertEqual(self.publish(revised).status, "pending")
        self.assertEqual(self.journal()[0], "push")
        original = self.pull
        self.pull = lambda number: original(number, head=OTHER)
        # The retry reuses the journaled superseding intent and its lease.
        self.assertEqual(self.publish(revised).status, "verified")
        self.assertEqual(self.git.pushes, [(CANDIDATE, None), (OTHER, CANDIDATE), (OTHER, CANDIDATE)])
        self.assertEqual(len(self.posts), 1)

    def test_superseding_candidate_refuses_an_outside_item_ref(self):
        self.git.on_push = lambda: setattr(self, "active", None)
        self.assertEqual(self.publish().status, "claim_lost")
        self.git.on_push = lambda: None
        self.successor()
        self.git.head = "d" * 40
        revised = replace(self.intent, candidate=OTHER, expected_base=OTHER)
        self.assertEqual(self.publish(revised).status, "conflict")
        self.assertEqual(len(self.git.pushes), 1)

    def test_base_move_during_unresolved_push_still_opens_the_draft(self):
        self.git.drop_push = True
        self.git.on_push = lambda: setattr(self.git, "base", OTHER)
        self.assertEqual(self.publish().status, "pending")
        self.assertEqual(self.journal()[0], "push")
        self.git.on_push = lambda: None
        self.assertEqual(self.publish().status, "verified")
        self.assertEqual((len(self.git.pushes), len(self.posts)), (2, 1))

    def test_moved_base_opens_the_draft_but_deleted_base_or_outside_push_blocks(self):
        self.git.base = None
        self.assertEqual(self.publish().status, "base_changed")
        self.git.base = BASE
        self.git.head = OTHER
        self.assertEqual(self.publish().status, "conflict")
        self.assertFalse(self.git.pushes or self.posts)
        self.git.head, self.git.base = None, OTHER
        self.assertEqual(self.publish().status, "verified")
        self.assertEqual(len(self.posts), 1)

    def test_lost_claim_after_post_preserves_known_pr_for_successor(self):
        self.post_effects.append("expire")
        self.assertEqual(self.publish().status, "claim_lost")
        self.assertEqual(self.journal()[2:], (11, None))
        self.successor()
        self.assertEqual(self.publish().primary.number, 11)
        self.assertEqual(len(self.posts), 1)

    def test_paused_writer_holds_item_lock_and_cannot_overwrite_outside_push(self):
        paused, release, successor_started = (threading.Event() for _ in range(3))
        results = {}
        first = self.claim
        second = PublicationClaim("successor", 4)

        def pause():
            paused.set()
            self.assertTrue(release.wait(30))

        def check_claim(_intent, claim):
            if claim == second:
                successor_started.set()
            return claim == self.active

        def publish(key, intent, claim):
            runner = DraftPublisher(self.directory, self.git, self.client,
                                    check_claim=check_claim, sleep=lambda _: None)
            results[key] = runner.publish(intent, claim)

        self.git.on_push = pause
        writer = threading.Thread(target=publish, args=("first", self.intent, first))
        successor = None
        writer.start()
        try:
            self.assertTrue(paused.wait(30))
            self.active = second
            self.git.head = OTHER
            newer = replace(self.intent, candidate=OTHER)
            successor = threading.Thread(target=publish, args=("second", newer, second))
            successor.start()
            self.assertTrue(successor_started.wait(30))
            self.assertNotIn("second", results)
        finally:
            release.set()
            for thread in (writer, successor):
                if thread is not None:
                    thread.join(30)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(results["first"].status, "conflict")
        self.assertEqual(results["second"].status, "conflict")
        self.assertEqual(self.git.head, OTHER)
        self.assertEqual(self.posts, [])
