from base64 import urlsafe_b64encode
from hashlib import sha256
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from phalanx.config import GoogleSignIn
from phalanx.store import connect
from test_bootstrap import OWNER, REQUEST_TIMEOUT, RUNNER, ServerFixture


CLIENT_ID = "client.apps.googleusercontent.com"
GOOGLE_ENV = {
    "PHALANX_GOOGLE_CLIENT_ID": CLIENT_ID,
    "PHALANX_GOOGLE_CLIENT_SECRET": "client-secret",
    "PHALANX_OWNER_EMAILS": "owner@example.com, second@example.com",
}
ITEM = "0" * 32


def b64(data):
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


class FakeGoogle:
    """Google's token endpoint, returning the ID token claims a test sets."""

    def __init__(self):
        self.claims = {}
        self.status = 200
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode("ascii"))
                fake.requests.append({key: value[0] for key, value in form.items()})
                token = ".".join((b64(b'{"alg":"RS256"}'), b64(json.dumps(fake.claims).encode()), "sig"))
                body = json.dumps({"id_token": token, "access_token": "unused"}).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OperatorWebTest(ServerFixture, unittest.TestCase):
    def test_shell_and_assets_preserve_api_authentication(self):
        with TemporaryDirectory() as directory:
            server = self.start_server(Path(directory) / "operator.sqlite3")
            try:
                with urlopen(self.url + "/operator/", timeout=REQUEST_TIMEOUT) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/html")
                    self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                    shell = response.read().decode("utf-8")
                for asset, marker in (
                    ("operator-board.css", ".phalanx-board"),
                    ("operator-board.js", "PhalanxOperatorBoard"),
                    ("operator-http.js", "/v1/operator/executions"),
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
                # Without Google settings the board keeps token entry only.
                self.assertEqual(self.get("/operator/auth/session"), (200, {"google": False, "email": None}))
                self.assertEqual(self.get("/operator/auth/login")[0], 404)
                report = f"/v1/operator/items/{ITEM}/executions/{ITEM}/report"
                self.assertEqual(self.get(report)[0], 401)
                self.assertEqual(self.get(report, OWNER)[0], 404)
            finally:
                self.stop_server(server)


class GoogleSignInTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.google = FakeGoogle()
        self.addCleanup(self.google.close)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "google.sqlite3"
        setup = (
            "import phalanx.operator_auth as google\n"
            f"google.AUTHORIZATION_ENDPOINT = {self.google.url + '/auth'!r}\n"
            f"google.TOKEN_ENDPOINT = {self.google.url + '/token'!r}\n"
        )
        self.server = self.start_server(self.path, GOOGLE_ENV, setup)

    def tearDown(self):
        self.stop_server(self.server)

    def call(self, method, path, cookies=None, token=None):
        connection = HTTPConnection(urlsplit(self.url).netloc, timeout=REQUEST_TIMEOUT)
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            connection.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            jar = SimpleCookie()
            for header in response.headers.get_all("Set-Cookie") or ():
                jar.load(header)
            return response.status, response.headers, jar, body
        finally:
            connection.close()

    def start_login(self, **claims):
        status, headers, jar, _ = self.call("GET", "/operator/auth/login")
        self.assertEqual(status, 303)
        location = urlsplit(headers["Location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", self.google.url + "/auth")
        params = {key: value[0] for key, value in parse_qs(location.query).items()}
        self.google.claims = {
            "iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": int(time.time()) + 300,
            "nonce": params["nonce"], "sub": "1234567890", "email": "Owner@Example.com", "email_verified": True, **claims,
        }
        return params, jar["phalanx_login"]

    def finish_login(self, params, login, *, state=None):
        query = f"code=good-code&state={state or params['state']}"
        cookies = {"phalanx_login": login.value} if login else {}
        return self.call("GET", f"/operator/auth/callback?{query}", cookies)

    def sessions(self):
        with connect(self.path) as db:
            return [tuple(row) for row in db.execute("SELECT id_hash, subject, email, role FROM user_sessions")]

    def test_session_reads_only_operator_routes_until_expiry_or_sign_out(self):
        self.assertEqual(self.call("GET", "/operator/auth/session")[3], '{"google":true,"email":null}')
        params, login = self.start_login()
        self.assertEqual(params["client_id"], CLIENT_ID)
        self.assertEqual(params["redirect_uri"], self.url + "/operator/auth/callback")
        self.assertEqual(params["scope"], "openid email")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertTrue(login["httponly"])
        self.assertEqual(login["samesite"].lower(), "lax")
        self.assertEqual(login["path"], "/operator/auth/")

        status, headers, jar, _ = self.finish_login(params, login)
        self.assertEqual((status, headers["Location"]), (303, "/operator/"))
        exchanged = self.google.requests[-1]
        self.assertEqual(exchanged["code"], "good-code")
        self.assertEqual(exchanged["client_secret"], "client-secret")
        self.assertEqual(exchanged["redirect_uri"], params["redirect_uri"])
        self.assertEqual(b64(sha256(exchanged["code_verifier"].encode()).digest()), params["code_challenge"])
        self.assertEqual(jar["phalanx_login"].value, "")
        session = jar["phalanx_session"]
        self.assertTrue(session["httponly"])
        self.assertEqual(session["samesite"].lower(), "lax")
        self.assertFalse(session["secure"])
        cookie = {"phalanx_session": session.value}
        self.assertEqual(self.sessions(), [
            (sha256(session.value.encode()).hexdigest(), "1234567890", "owner@example.com", "owner")])

        self.assertEqual(self.call("GET", "/operator/auth/session", cookie)[3],
                         '{"google":true,"email":"owner@example.com"}')
        self.assertEqual(self.call("GET", "/v1/operator/executions", cookie)[0], 200)
        self.assertEqual(self.call("GET", f"/v1/operator/items/{ITEM}/executions/{ITEM}/report", cookie)[0], 404)
        for method, path in (
            ("GET", "/v1/owner/status"), ("POST", "/v1/items"), ("GET", f"/v1/items/{ITEM}"),
            ("GET", "/v1/runner/status"), ("POST", "/v1/dispatch/claim"),
        ):
            with self.subTest(refused=path):
                self.assertEqual(self.call(method, path, cookie)[0], 401)
        # A bearer token is checked on its own merits even beside a valid session.
        self.assertEqual(self.call("GET", "/v1/operator/executions", cookie, "invalid")[0], 401)

        with connect(self.path) as db:
            db.execute("UPDATE user_sessions SET expires_at = ?", (int(time.time()) - 1,))
        self.assertEqual(self.call("GET", "/v1/operator/executions", cookie)[0], 401)
        with connect(self.path) as db:
            db.execute("UPDATE user_sessions SET expires_at = ?", (int(time.time()) + 60,))
        self.assertEqual(self.call("GET", "/v1/operator/executions", cookie)[0], 200)

        status, _, jar, _ = self.call("POST", "/operator/auth/logout", cookie)
        self.assertEqual((status, jar["phalanx_session"].value), (204, ""))
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.call("GET", "/v1/operator/executions", cookie)[0], 401)

    def test_callback_refuses_bad_state_nonce_claims_and_unlisted_accounts(self):
        for case, claims, expected in (
            ("bad state", {}, 400),
            ("no login cookie", {}, 400),
            ("bad nonce", {"nonce": "other"}, 403),
            ("unlisted", {"email": "stranger@example.com"}, 403),
            ("unverified", {"email_verified": False}, 403),
            ("no account ID", {"sub": ""}, 403),
            ("other client", {"aud": "other"}, 403),
            ("other issuer", {"iss": "https://evil.example"}, 403),
            ("expired token", {"exp": int(time.time()) - 1}, 403),
            ("token endpoint error", {}, 502),
        ):
            with self.subTest(case):
                params, login = self.start_login(**claims)
                self.google.status = 400 if case == "token endpoint error" else 200
                exchanges = len(self.google.requests)
                status, _, jar, body = self.finish_login(
                    params, None if case == "no login cookie" else login,
                    state="forged" if case == "bad state" else None,
                )
                self.assertEqual(status, expected, body)
                self.assertIn("Google sign-in failed", body)
                self.assertNotIn("phalanx_session", jar)
                if expected == 400:
                    self.assertEqual(len(self.google.requests), exchanges)
        self.assertEqual(self.sessions(), [])


class GoogleSettingsTest(unittest.TestCase):
    def test_settings_are_all_or_nothing(self):
        with mock.patch.dict(os.environ, {name: "" for name in GOOGLE_ENV}):
            self.assertIsNone(GoogleSignIn.from_env())
        with mock.patch.dict(os.environ, GOOGLE_ENV):
            self.assertEqual(GoogleSignIn.from_env().owner_emails,
                             {"owner@example.com", "second@example.com"})
        for name in GOOGLE_ENV:
            with self.subTest(missing=name), mock.patch.dict(os.environ, {**GOOGLE_ENV, name: ""}):
                self.assertRaises(ValueError, GoogleSignIn.from_env)
