from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from urllib.request import urlopen

from fastapi import HTTPException, Request

from phalanx import captains
from phalanx.auth import require_board_owner
from phalanx.store import write_transaction
from phalanx.session_links import session_target

from test_bootstrap import OWNER, REQUEST_TIMEOUT, RUNNER, ServerFixture


class OperatorWebTest(ServerFixture, unittest.TestCase):
    def test_recorded_session_links_resolve_once_without_guessing_or_replacing_invalid_urls(self):
        identity = "12345678-1234-1234-1234-123456789abc"
        copilot = "ghapp://sessions/" + identity
        codex = "codex://threads/" + identity
        self.assertEqual(session_target(identity=identity, runtime="copilot-app")["url"], copilot)
        self.assertEqual(session_target(identity=identity, runtime="codex")["url"], codex)
        self.assertEqual(session_target(codex)["url"], codex)
        self.assertEqual(session_target(identity, runtime="codex")["url"], codex)
        self.assertEqual(session_target("https://example.com/chat")["url"], "https://example.com/chat")
        for reference in (
            identity, "javascript:alert(1)", "file:///tmp/chat", "unknown://sessions/" + identity,
            copilot + "?run=1", copilot + "#x", "ghapp://sessions/../" + identity,
            "ghapp://user@sessions/" + identity, copilot + "/", "ghapp://sessions/%31" + identity,
            " " + copilot, "https://user:pass@example.com/chat", "https://[bad",
            "https://example.com/\\chat", "https://example.com:bad/chat", "/operator/new",
            "codex://threads/not-a-thread", codex,
        ):
            with self.subTest(reference=reference):
                self.assertIsNone(session_target(
                    reference, runtime="copilot-app", identity=identity, explicit_url=True)["url"])
        for args in ({"identity": identity}, {"reference": identity},
                     {"runtime": "copilot-cli", "identity": identity},
                     {"reference": copilot, "identity": "other"}):
            self.assertIsNone(session_target(**args)["url"])
        for availability in ("missing", "deleted"):
            self.assertEqual(session_target(copilot, availability=availability),
                             {"url": None, "reason": "session " + availability})
        self.assertIn("unverified", session_target(copilot)["reason"])
        self.assertIn("present", session_target(copilot, availability="present")["reason"])

    def test_local_dashboard_settings_share_authority_and_canonical_validation(self):
        directory = self.enterContext(TemporaryDirectory())
        self.start_server(Path(directory) / "local-settings.sqlite3", PHALANX_LOCAL_BOARD="1")
        api = "/v1/dispatch/settings"
        board = "/operator/api/dispatch/settings"
        self.assertEqual(self.request(api, OWNER, {"paused": True},
                                      method="PUT")[0], 200)
        self.assertEqual(self.get(board)[1]["max_writers"], 10)
        headers = {"Origin": self.url, "Sec-Fetch-Site": "same-origin"}
        status, saved = self.request(board, body={"max_writers": 2, "max_building_writers": 1},
                                     headers=headers, method="PUT")
        self.assertEqual((status, saved["max_writers"], saved["paused"]), (200, 2, True))
        self.assertEqual(saved["max_building_writers"], 1)
        self.assertEqual(self.request(board, body={"max_building_writers": 3},
                                      headers=headers, method="PUT")[0], 409)
        for value in (0, -1, None, True, 2.5, "2"):
            self.assertEqual(self.request(board, body={"max_writers": value},
                                          headers=headers, method="PUT")[0], 422)
        self.assertEqual(self.request(board, body={"max_slots": 2},
                                      headers=headers, method="PUT")[0], 422)
        for override in (
            {"Origin": "null"}, {"Origin": "https://evil.example"},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
            {"Host": "localhost.evil.example"}, {"Forwarded": "for=127.0.0.1"},
            {"X-Forwarded-For": "127.0.0.1"}, {"X-Forwarded-Host": "localhost"},
        ):
            with self.subTest(override=override):
                self.assertIn(self.request(board, body={"max_writers": 9},
                                           headers=headers | override, method="PUT")[0], (401, 403))
        self.assertEqual(self.request(board, body={"max_writers": 9}, method="PUT")[0], 403)
        self.assertEqual(self.request(api, body={"max_writers": 9},
                                      headers=headers, method="PUT")[0], 401)
        for token, status in ((RUNNER, 403), ("x" * 32, 401)):
            self.assertEqual(self.request(board, token, {"max_writers": 9},
                                          headers=headers, method="PUT")[0], status)
        self.assertEqual(self.request(board, OWNER, {"max_writers": 9},
                                      headers={"Origin": "null"}, method="PUT")[0], 403)
        self.assertEqual(self.get(api)[1], saved)

    def test_local_dashboard_create_reuses_validated_idempotent_intake(self):
        directory = self.enterContext(TemporaryDirectory())
        self.start_server(Path(directory) / "local-create.sqlite3", PHALANX_LOCAL_BOARD="1")
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "example", "remote": "https://github.com/example/project.git",
        })[0], 200)
        self.assertEqual(self.get("/operator/api/repositories")[1][0]["id"], "example")
        body = {"repository_id": "example", "goal": "Tokenless localhost creation",
                "target": "ready_for_check_in", "publication_mode": "draft"}
        headers = {"Origin": self.url, "Sec-Fetch-Site": "same-origin",
                   "Idempotency-Key": "local-create"}
        status, error = self.request("/operator/api/items", body=dict(body, goal=" "), headers=headers)
        self.assertEqual(status, 422)
        self.assertEqual(error["detail"][0]["loc"], ["body", "goal"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request(
                "/operator/api/items", body=body, headers=headers), range(2)))
        self.assertEqual(results[0][0], 201)
        self.assertEqual(results[0], results[1])
        state = results[0][1]["execution"]["state"]
        self.assertEqual((state["target"], state["publication_mode"]),
                         ("ready_for_check_in", "draft"))
        self.assertEqual(self.request("/operator/api/items", body=dict(body, goal="Changed"),
                                      headers=headers)[0], 409)
        for token, status in ((RUNNER, 403), ("x" * 32, 401)):
            self.assertEqual(self.request("/operator/api/items", token, body, headers)[0], status)
        for overrides in (
            {"Origin": "https://evil.example"}, {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
            {"Host": "localhost.evil.example"}, {"Host": "127.0.0.1.nip.io"},
            {"Forwarded": "for=127.0.0.1;host=localhost"},
            {"X-Forwarded-For": "127.0.0.1"}, {"X-Forwarded-Host": "localhost"},
            {"X-Forwarded-Proto": "http"}, {"X-Real-IP": "127.0.0.1"},
            {"Authorization": "Basic not-a-bearer"},
        ):
            with self.subTest(overrides=overrides):
                self.assertIn(self.request("/operator/api/items", body=body,
                                           headers=headers | overrides)[0], (401, 403))
        for read_headers in ({"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                             {"Host": "evil.example"}, {"X-Forwarded-For": "127.0.0.1"}):
            self.assertIn(self.request("/operator/api/repositories", headers=read_headers)[0], (401, 403))
        self.assertEqual(self.request("/operator/api/items", body=body)[0], 403)
        self.assertEqual(self.request("/v1/items", body=body)[0], 401)
        for path in ("/v1/repositories", "/v1/owner/status", "/v1/runner/status"):
            self.assertEqual(self.get(path)[0], 401)
        self.assertEqual(self.request("/v1/repositories", body={
            "id": "unauthorized", "remote": "https://github.com/example/unauthorized.git",
        })[0], 401)
        self.assertEqual(len(self.get("/v1/operator/executions")[1]["items"]), 1)

    def test_nonlocal_dashboard_stays_owner_authenticated(self):
        directory = self.enterContext(TemporaryDirectory())
        self.start_server(Path(directory) / "remote-create.sqlite3")
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "example", "remote": "https://github.com/example/project.git",
        })[0], 200)
        body = {"repository_id": "example", "goal": "Authenticated CLI and dashboard"}
        for token, status in ((None, 401), (RUNNER, 403), (OWNER, 200)):
            self.assertEqual(self.get("/operator/api/repositories", token)[0], status)
        for token, status in ((None, 401), (RUNNER, 403), (OWNER, 201)):
            self.assertEqual(self.request("/operator/api/items", token, body)[0], status)
        for token, status in ((None, 401), (RUNNER, 403), (OWNER, 200)):
            self.assertEqual(self.get("/operator/api/dispatch/settings", token)[0], status)
            self.assertEqual(self.request("/operator/api/dispatch/settings", token,
                                          {"max_writers": 3, "max_building_writers": 2},
                                          method="PUT")[0], status)
        self.assertEqual(self.request("/operator/api/items", OWNER, body, {
            "Host": "hosted.example", "X-Forwarded-For": "203.0.113.8",
        })[0], 201)
        self.assertEqual(self.request("/operator/api/items", OWNER, body, {
            "Origin": "null", "Sec-Fetch-Site": "cross-site",
        })[0], 403)

    def test_dashboard_authority_checks_actual_peer_and_origin(self):
        app = SimpleNamespace(state=SimpleNamespace(local_board=True))
        for peer, host, origin, site, expected in (
            ("127.0.0.1", "localhost:8000", "http://localhost:8000", "same-origin", "local"),
            ("::1", "[::1]:8000", "http://[::1]:8000", "same-origin", "local"),
            ("203.0.113.8", "localhost:8000", "http://localhost:8000", "same-origin", 401),
            ("::ffff:203.0.113.8", "localhost:8000", "http://localhost:8000", "same-origin", 401),
            (None, "localhost:8000", "http://localhost:8000", "same-origin", 401),
            ("127.0.0.1", "evil.example", "http://evil.example", "same-origin", 401),
            ("127.0.0.1", "localhost:8000", "http://localhost:8001", "same-origin", 403),
            ("127.0.0.1", "localhost:8000", "null", "same-origin", 403),
            ("127.0.0.1", "localhost:8000", None, "same-origin", 403),
            ("127.0.0.1", "localhost:8000", "http://localhost:8000", "cross-site", 403),
        ):
            with self.subTest(peer=peer, host=host, origin=origin, site=site):
                headers = [(b"host", host.encode()), (b"sec-fetch-site", site.encode())]
                if origin is not None:
                    headers.append((b"origin", origin.encode()))
                request = Request({"type": "http", "method": "POST", "scheme": "http",
                                   "path": "/operator/api/items", "headers": headers,
                                   "client": (peer, 1234) if peer else None, "app": app})
                if expected == "local":
                    self.assertEqual(require_board_owner(request, None), expected)
                else:
                    with self.assertRaises(HTTPException) as raised:
                        require_board_owner(request, None)
                    self.assertEqual(raised.exception.status_code, expected)

    def test_owner_intake_from_board_is_validated_idempotent_and_same_origin(self):
        directory = self.enterContext(TemporaryDirectory())
        self.start_server(Path(directory) / "create.sqlite3", PHALANX_LOCAL_BOARD="1")
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "example", "remote": "https://github.com/example/project.git",
        })[0], 200)
        self.assertEqual(self.get("/v1/repositories", OWNER)[1][0]["id"], "example")
        body = {"repository_id": "example", "goal": "**Queued from Scribe**",
                "acceptance_criteria": ["Visible on board"], "label": "PH-29b"}
        headers = {"Origin": self.url, "Sec-Fetch-Site": "same-origin",
                   "Idempotency-Key": "scribe-create"}
        for token, status in ((None, 401), (RUNNER, 403), ("x" * 32, 401)):
            self.assertEqual(self.request("/v1/items", token, body, headers)[0], status)
        for origin, site in (("https://evil.example", "cross-site"), ("null", "same-origin"),
                             (self.url + ".evil.example", "same-origin"),
                             (self.url, "same-site"), (None, "same-origin")):
            cross = dict(headers, **{"Sec-Fetch-Site": site})
            if origin is None:
                cross.pop("Origin")
            else:
                cross["Origin"] = origin
            self.assertEqual(self.request("/v1/items", OWNER, body, cross)[0], 403)
        self.assertEqual(self.request("/v1/items", OWNER, body, {
            "Origin": "https://evil.example",
        })[0], 403)
        status, error = self.request("/v1/items", OWNER, dict(body, goal=" "), headers)
        self.assertEqual(status, 422)
        self.assertEqual(error["detail"][0]["loc"], ["body", "goal"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.request("/v1/items", OWNER, body, headers), range(2)))
        self.assertEqual(results[0][0], 201)
        self.assertEqual(results[0], results[1])
        item = results[0][1]
        self.assertEqual(item["execution"]["state"]["status"], "queued")
        self.assertEqual(item["execution"]["state"]["target"], "checked_in")
        self.assertEqual(self.request("/v1/items", OWNER, body, headers), results[0])
        page = self.get("/v1/operator/executions")[1]
        self.assertEqual([row["item_id"] for row in page["items"]], [item["id"]])

    def test_board_captain_read_uses_server_liveness(self):
        directory = self.enterContext(TemporaryDirectory())
        path = Path(directory) / "captains.sqlite3"
        self.start_server(path, PHALANX_LOCAL_BOARD="1")
        now = datetime.now(UTC)
        ids = {}
        with write_transaction(path) as db:
            for kind in ("live", "stale", "ended"):
                row = captains.register(db, captains.CaptainRegistration(
                    session_ref=("codex://threads/12345678-1234-1234-1234-123456789abc"
                                 if kind == "live" else kind), charter_revision="test"))
                ids[kind] = row["id"]
            db.execute("UPDATE captains SET heartbeat_at=? WHERE id=?",
                       ((now - captains.STALE - timedelta(seconds=1)).isoformat(), ids["stale"]))
            db.execute("UPDATE captains SET state='ended' WHERE id=?", (ids["ended"],))
            expected = {ids[kind]: kind == "live" for kind in ids}
        status, page = self.get("/v1/captains?limit=25")
        self.assertEqual(status, 200)
        rows = {row["id"]: row for row in page["captains"]}
        self.assertEqual({key: row["live"] for key, row in rows.items()}, expected)
        self.assertEqual([rows[ids[kind]]["stale"] for kind in ids], [False, True, False])
        self.assertEqual([rows[ids[kind]]["live"] for kind in ids], [True, False, False])
        self.assertEqual(rows[ids["live"]]["session_link"]["url"],
                         "codex://threads/12345678-1234-1234-1234-123456789abc")
        self.assertIsNone(rows[ids["ended"]]["session_link"]["url"])

    def test_board_writer_link_uses_recorded_host_and_keeps_invalid_url_unavailable(self):
        from test_bootstrap import claim_stage

        directory = self.enterContext(TemporaryDirectory())
        path = Path(directory) / "writer-links.sqlite3"
        self.start_server(path, PHALANX_LOCAL_BOARD="1")
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "example", "remote": "https://github.com/example/project.git",
        })[0], 200)
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "example", "goal": "Recorded writer link",
        })
        self.assertEqual(status, 201)
        claim_stage(path, item["id"], "x" * 32)
        identity = "12345678-1234-1234-1234-123456789abc"
        with write_transaction(path) as db:
            db.execute("UPDATE writers SET runtime='codex', session_id=?", (identity,))
        row = self.get("/v1/operator/executions?limit=1")[1]["items"][0]
        self.assertEqual(row["writer"]["session_link"], {
            "url": "codex://threads/" + identity,
            "reason": "recorded session; existence unverified",
        })
        with write_transaction(path) as db:
            db.execute("UPDATE writers SET session_url=?", ("ghapp://sessions/" + identity,))
        row = self.get("/v1/operator/executions?limit=1")[1]["items"][0]
        self.assertIsNone(row["writer"]["session_link"]["url"])

    def test_shell_and_assets_preserve_api_authentication(self):
        directory = self.enterContext(TemporaryDirectory())
        self.start_server(Path(directory) / "operator.sqlite3")
        for route in ("/operator/", "/operator/queue?repository=example", "/operator/history",
                      f"/operator/items/{'1' * 32}", f"/operator/executions/{'2' * 32}"):
            with self.subTest(route=route), urlopen(self.url + route, timeout=REQUEST_TIMEOUT) as response:
                self.assertEqual(response.headers.get_content_type(), "text/html")
                self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
                self.assertIn("img-src 'self' data:;", response.headers["Content-Security-Policy"])
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                shell = response.read().decode("utf-8")
        self.assertEqual(self.get("/operator/items/invalid")[0], 422)
        self.assertEqual(self.get("/operator/executions/invalid")[0], 422)
        self.assertEqual(self.get("/operator/runners")[0], 404)
        for asset, marker in (
            ("operator-board.css", ".phalanx-board"),
            ("operator-board.js", "PhalanxOperatorBoard"),
            ("operator-helpers.js", "PhalanxOperatorHelpers"),
            ("operator-markdown.js", "PhalanxMarkdown"),
            ("operator-http.js", "PhalanxOperatorHttp"),
            ("operator-shell.js", "/v1/operator/executions"),
        ):
            path = f"/operator/assets/{asset}"
            self.assertIn(path, shell)
            with urlopen(self.url + path, timeout=REQUEST_TIMEOUT) as response:
                content = response.read().decode("utf-8")
                self.assertIn(marker, content)
                self.assertNotIn(OWNER, content)
                self.assertNotIn(RUNNER, content)

        self.assertNotIn(OWNER, shell)
        self.assertNotIn(RUNNER, shell)
        self.assertEqual(self.get("/v1/owner/status")[0], 401)
        self.assertEqual(self.get("/v1/owner/status", RUNNER)[0], 403)
        self.assertEqual(self.get("/v1/owner/status", OWNER), (200, {"role": "owner"}))
        self.assertEqual(self.get("/v1/operator/executions")[0], 401)

    def test_shared_brand_asset_is_served_and_readme_uses_static_lockup(self):
        directory = self.enterContext(TemporaryDirectory())
        self.start_server(Path(directory) / "brand.sqlite3")
        with urlopen(self.url + "/operator/assets/phalanx-brand.svg",
                     timeout=REQUEST_TIMEOUT) as response:
            self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
            asset = response.read().decode("utf-8")
        from xml.etree import ElementTree
        root = ElementTree.fromstring(asset)
        ids = {node.attrib["id"] for node in root.iter() if "id" in node.attrib}
        self.assertTrue({"field", "rim", "rim-cap", "chevron", "tip-outline", "shield"} <= ids)
        for node in root.iter():
            if node.tag.endswith("use"):
                self.assertTrue(node.attrib["href"].startswith("#"))
                self.assertIn(node.attrib["href"][1:], ids)
        repo = Path(__file__).resolve().parents[1]
        readme = (repo / "README.md").read_text(encoding="utf-8")
        self.assertIn('src="phalanx/static/phalanx-brand.svg"', readme)
        self.assertNotIn("<script", asset)
        self.assertNotIn("<style", asset)

    def test_board_compaction_counts_survive_a_server_restart_and_legacy_heartbeat(self):
        directory = self.enterContext(TemporaryDirectory())
        path = Path(directory) / "counts.sqlite3"
        server = self.start_server(path, PHALANX_LOCAL_BOARD="1")
        status, captain = self.request("/v1/captains", RUNNER, {
            "session_ref": "native-session", "charter_revision": "charter",
        })
        self.assertEqual(status, 201)
        route = f"/v1/captains/{captain['id']}"
        beat = {"charter_revision": "charter", "wake_count": 4, "state": "active"}
        self.assertEqual(self.request(route, RUNNER, dict(beat, compaction_count=2),
                                      method="PUT")[0], 200)
        self.stop_server(server)
        self.start_server(path, PHALANX_LOCAL_BOARD="1")
        self.assertEqual(self.request(route, RUNNER, beat, method="PUT")[0], 200)
        for row in (self.get(route)[1], self.get("/v1/captains")[1]["captains"][0]):
            self.assertEqual(row["compaction_count"], 2)
            self.assertTrue(row["live"])
            self.assertEqual(row["wake_count"], 4)

    def test_local_board_reads_without_token_only_for_local_hosts(self):
        board = "/v1/operator/executions"
        report = f"/v1/items/{'0' * 32}/executions/{'1' * 32}/report"
        directory = self.enterContext(TemporaryDirectory())
        self.start_server(Path(directory) / "local.sqlite3", PHALANX_LOCAL_BOARD="1")
        port = self.url.rpartition(":")[2]
        for host in ("127.0.0.1", f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            with self.subTest(host=host):
                status, page = self.request(board, headers={"Host": host})
                self.assertEqual((status, page["items"]), (200, []))
        self.assertEqual(self.get(f"{board}/{'1' * 32}/timeline")[0], 404)
        self.assertEqual(self.get(report)[0], 404)
        for host in ("evil.example", f"evil.example:{port}", "localhost.evil.example", "127.0.0.1.nip.io"):
            with self.subTest(host=host):
                self.assertEqual(self.request(board, headers={"Host": host})[0], 401)
                self.assertEqual(self.request(report, headers={"Host": host})[0], 401)
        self.assertEqual(self.get(board, "x" * 32)[0], 401)
        item_route = f"/v1/operator/items/{'0' * 32}"
        self.assertEqual(self.get(item_route)[0], 404)
        self.assertEqual(self.request(item_route, headers={"Host": "evil.example"})[0], 401)
        for path in ("/v1/owner/status", "/v1/runner/status", "/v1/repositories",
                     f"/v1/items/{'0' * 32}"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 401)
        self.assertEqual(self.request("/v1/items", body={"goal": "x"})[0], 401)
        self.assertEqual(self.request("/v1/writer/attempt", body={}, method="POST")[0], 401)
        self.assertEqual(self.get("/v1/dispatch/settings")[1]["max_writers"], 10)
        self.assertEqual(self.request("/v1/dispatch/settings",
                                      headers={"Host": "evil.example"})[0], 401)
        self.assertEqual(self.request("/v1/dispatch/settings", body={"max_writers": 9},
                                      method="PUT")[0], 401)
