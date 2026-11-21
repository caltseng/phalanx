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

    def read_head(self, *, authorize):
        authorize()
        return self.head

    def read_branch(self, branch, *, authorize):
        authorize()
        assert branch == "main"
        return self.base

    def publish(self, candidate, *, expected, authorize):
        authorize()
        self.on_push()
        self.pushes.append((candidate, expected))
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

    def test_successor_recovers_lost_draft_from_disk_and_blocks_conflicting_intents(self):
        self.post_effects.append("lost_and_unreadable")
        self.assertEqual(self.publish().status, "pending")
        self.assertEqual(self.journal()[0], "draft")
        self.successor()
        self.assertEqual(self.publish(replace(self.intent, candidate=OTHER)).status, "conflict")
        self.assertEqual(len(self.posts), 1)
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

    def test_base_move_after_confirmed_push_allows_new_candidate_with_original_tip(self):
        self.git.on_push = lambda: setattr(self.git, "base", OTHER)
        self.assertEqual(self.publish().status, "base_changed")
        self.assertEqual(self.journal()[0], "pushed")
        self.assertEqual(self.publish(replace(self.intent, candidate=OTHER)).status, "conflict")
        self.git.on_push = lambda: None
        revised = replace(self.intent, candidate=OTHER, expected_head=CANDIDATE,
                          expected_base=OTHER)
        # GitHub observes the revised head and base once the draft is created.
        original = self.pull
        def revised_pull(number):
            data = original(number, head=OTHER)
            data["base"]["sha"] = OTHER
            return data
        self.pull = revised_pull
        self.assertEqual(self.publish(revised).status, "verified")
        self.assertEqual(self.git.pushes, [(CANDIDATE, None), (OTHER, CANDIDATE)])

    def test_base_or_outside_push_conflict_before_any_write(self):
        self.git.base = OTHER
        self.assertEqual(self.publish().status, "base_changed")
        self.git.base = BASE
        self.git.head = OTHER
        self.assertEqual(self.publish().status, "conflict")
        self.assertFalse(self.git.pushes or self.posts)

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
            self.assertTrue(release.wait(5))

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
        writer.start()
        try:
            self.assertTrue(paused.wait(5))
            self.active = second
            self.git.head = OTHER
            newer = replace(self.intent, candidate=OTHER)
            successor = threading.Thread(target=publish, args=("second", newer, second))
            successor.start()
            self.assertTrue(successor_started.wait(5))
            self.assertNotIn("second", results)
        finally:
            release.set()
            writer.join(5)
        successor.join(5)
        self.assertEqual(results["first"].status, "conflict")
        self.assertEqual(results["second"].status, "conflict")
        self.assertEqual(self.git.head, OTHER)
        self.assertEqual(self.posts, [])
