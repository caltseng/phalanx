"""Runner-owned notes on a primary PR: deficiency comments and a Known gaps section.

Notes go through a local outbox so a failed GitHub write never blocks the item.
Each prepare call flushes it. Comments carry a marker keyed on the revision and
finding set, so a replay finds the earlier comment instead of posting another.
"""

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .github import ATTRIBUTION, GitHubClient, GitHubError

MAX_ATTEMPTS = 5
GAPS = "known-gaps"
_GAPS_START = "<!-- phalanx:known-gaps -->"
_GAPS_END = "<!-- /phalanx:known-gaps -->"
_GAPS_SECTION = re.compile(re.escape(_GAPS_START) + r".*?" + re.escape(_GAPS_END), re.S)
_PR_URL = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)")
_CLAIM_SECONDS = 300


def _marker(key):
    return f"<!-- phalanx:{key} -->"


class PRNotes:
    def __init__(self, root):
        self.path = Path(root) / "publication" / "pr-notes.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS notes (pr_url TEXT, key TEXT, body TEXT, "
                       "attempts INTEGER DEFAULT 0, error TEXT, claim TEXT, claimed_until REAL, "
                       "PRIMARY KEY (pr_url, key))")
            yield db

    def _put(self, pr_url, key, body):
        # Replacing a row moves it to the end, so the latest Known gaps text wins.
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO notes (pr_url, key, body) VALUES (?, ?, ?)",
                       (pr_url, key, body))

    def record(self, pr_url, revision, title, findings):
        """Comment on the findings and list them as the PR's open Known gaps."""
        findings = findings.strip()[:20000]
        digest = hashlib.sha256(findings.encode()).hexdigest()[:12]
        key = f"findings:{revision}:{digest}"
        self._put(pr_url, key, f"{_marker(key)}\n**{title}** at `{revision}`:\n\n{findings}")
        self._put(pr_url, GAPS, f"Open at revision `{revision}`:\n\n{findings}")

    def comment(self, pr_url, key, text):
        self._put(pr_url, key, f"{_marker(key)}\n{text}")

    def clear_gaps(self, pr_url):
        self._put(pr_url, GAPS, None)

    def flush(self, token):
        """Apply due notes and return those still outstanding, so none fail silently."""
        if token:
            claim = secrets.token_hex(8)
            with self._db() as db:
                db.execute("UPDATE notes SET claim=?, claimed_until=? WHERE attempts<? "
                           "AND (claimed_until IS NULL OR claimed_until<?)",
                           (claim, time.time() + _CLAIM_SECONDS, MAX_ATTEMPTS, time.time()))
                rows = db.execute("SELECT pr_url, key, body FROM notes WHERE claim=? ORDER BY rowid",
                                  (claim,)).fetchall()
            for pr_url, key, body in rows:
                try:
                    _apply(token, pr_url, key, body)
                except (GitHubError, ValueError) as error:
                    with self._db() as db:
                        db.execute("UPDATE notes SET attempts=attempts+1, error=?, claim=NULL, "
                                   "claimed_until=NULL WHERE pr_url=? AND key=? AND claim=?",
                                   (str(error)[:500], pr_url, key, claim))
                else:
                    with self._db() as db:
                        db.execute("DELETE FROM notes WHERE pr_url=? AND key=? AND claim=?",
                                   (pr_url, key, claim))
        with self._db() as db:
            rows = db.execute("SELECT pr_url, key, attempts, error FROM notes ORDER BY rowid").fetchall()
        missing = None if token else "Set PHALANX_GITHUB_TOKEN to publish PR notes"
        return [{"pr": pr_url, "note": key, "attempts": attempts,
                 "error": error or missing or "pending", "retrying": attempts < MAX_ATTEMPTS}
                for pr_url, key, attempts, error in rows]


def _apply(token, pr_url, key, body):
    match = _PR_URL.fullmatch(pr_url)
    if match is None:
        raise ValueError(f"unsupported PR URL {pr_url}")
    github = GitHubClient(match[1], match[2], token=token)
    number = int(match[3])
    if key == GAPS:
        old = github.get_pull_request_body(number)
        new = known_gaps(old, body)
        if new != old and (body is not None or _GAPS_START in old):
            github.update_pull_request_body(number, new)
    elif not any(_marker(key) in comment for comment in github.list_pull_request_comments(number)):
        github.comment_on_pull_request(number, body[:60000])


def known_gaps(description, gaps):
    """Return the description with its Known gaps section replaced, or removed when gaps is None."""
    lines = [line for line in _GAPS_SECTION.sub("", description).splitlines() if line.strip() != ATTRIBUTION]
    parts = ["\n".join(lines).strip()]
    if gaps is not None:
        parts.append(f"{_GAPS_START}\n### Known gaps\n\n{gaps}\n{_GAPS_END}")
    text = "\n\n".join(part for part in parts if part)
    return f"{text}\n\n{ATTRIBUTION}" if text else ATTRIBUTION
