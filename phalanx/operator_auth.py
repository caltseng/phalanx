"""Optional Google sign-in for the owner's read-only operator board.

A signed-in session authorizes only the GET /v1/operator/* routes. Every other route keeps
bearer tokens. The ID token comes straight from Google's token endpoint over TLS, so its
claims are checked without verifying its signature (OpenID Connect Core 3.1.3.7).
"""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
import hmac
import json
import secrets
import time
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as HTTPRequest, urlopen

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials

from .auth import Role, authenticate, bearer
from .store import read_transaction, write_transaction


# Tests point these at a local fake; production always uses Google over HTTPS.
AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SESSION_COOKIE = "phalanx_session"
LOGIN_COOKIE = "phalanx_login"
AUTH_PATH = "/operator/auth/"
SESSION_SECONDS = 12 * 60 * 60
LOGIN_SECONDS = 10 * 60
# Pending logins live in a cookie signed with a per-process key: no table or cleanup,
# and a restart only cancels logins in flight.
LOGIN_KEY = secrets.token_bytes(32)
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff"}

router = APIRouter(include_in_schema=False)


class SignInError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def b64(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    return urlsafe_b64decode(text + "=" * (-len(text) % 4))


def digest(session_id: str) -> str:
    return sha256(session_id.encode("ascii")).hexdigest()


def sign(values) -> str:
    data = b64(json.dumps(values, separators=(",", ":")).encode("utf-8"))
    return data + "." + b64(hmac.new(LOGIN_KEY, data.encode("ascii"), sha256).digest())


def unsign(cookie):
    """Return the unexpired [state, nonce, verifier] from a login cookie, or None."""
    try:
        data, mac = cookie.split(".")
        expected = b64(hmac.new(LOGIN_KEY, data.encode("ascii"), sha256).digest())
        if hmac.compare_digest(mac, expected):
            *values, expires = json.loads(unb64(data))
            if expires > time.time() and len(values) == 3:
                return values
    except (AttributeError, ValueError, TypeError):
        pass
    return None


def is_https(request: Request) -> bool:
    return request.url.scheme == "https"


def callback_uri(request: Request) -> str:
    return str(request.url_for("google_callback"))


def signed_in(request: Request):
    """Return the session's subject, email and role, or None."""
    google = request.app.state.google
    cookie = request.cookies.get(SESSION_COOKIE)
    if google is None or not cookie or len(cookie) > 200 or not cookie.isascii():
        return None
    with read_transaction(request.app.state.database_path) as db:
        row = db.execute(
            "SELECT subject, email, role FROM user_sessions WHERE id_hash = ? AND expires_at > ?",
            (digest(cookie), int(time.time())),
        ).fetchone()
    # Removing an address from the allowlist ends its sessions at the next restart.
    return dict(row) if row and row["email"] in google.owner_emails else None


def operator_reader(
    request: Request,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Role:
    """Authorize a read-only operator route with a bearer token or a Google session."""
    if authorization is None and request.method in ("GET", "HEAD"):
        user = signed_in(request)
        if user:
            return user["role"]
    return authenticate(request, authorization)


def exchange(google, code, verifier, redirect_uri):
    form = urlencode({
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": google.client_id, "client_secret": google.client_secret,
        "redirect_uri": redirect_uri,
    }).encode("ascii")
    request = HTTPRequest(TOKEN_ENDPOINT, data=form, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            body = json.load(response)
    except HTTPError as error:
        raise SignInError(502, f"Google rejected the authorization code (HTTP {error.code})") from None
    except (URLError, TimeoutError, OSError):
        raise SignInError(502, "Could not reach Google's token endpoint") from None
    except ValueError:
        raise SignInError(502, "Google's token endpoint returned invalid JSON") from None
    try:
        _, payload, _ = body["id_token"].split(".")
        return json.loads(unb64(payload))
    except (KeyError, TypeError, AttributeError, ValueError):
        raise SignInError(502, "Google's token response had no valid ID token") from None


def verified_identity(google, claims, nonce):
    """Return the checked (subject, email, role) from Google's ID token claims."""
    if not isinstance(claims, dict) or claims.get("iss") not in ISSUERS:
        raise SignInError(403, "The ID token has the wrong issuer")
    if claims.get("aud") != google.client_id:
        raise SignInError(403, "The ID token is for a different client")
    expires = claims.get("exp")
    if type(expires) not in (int, float) or expires <= time.time():
        raise SignInError(403, "The ID token has expired")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise SignInError(403, "The ID token nonce did not match")
    if claims.get("email_verified") not in (True, "true"):
        raise SignInError(403, "The Google account email is not verified")
    subject, email = claims.get("sub"), claims.get("email")
    if not isinstance(subject, str) or not 0 < len(subject) <= 255:
        raise SignInError(403, "The ID token has no account ID")
    if not isinstance(email, str) or email.lower() not in google.owner_emails:
        raise SignInError(403, "This Google account is not an allowed owner; add it to "
                               "PHALANX_OWNER_EMAILS and restart the server")
    return subject, email.lower(), "owner"


def configured(request: Request):
    google = request.app.state.google
    if google is None:
        raise HTTPException(404, "Google sign-in is not configured")
    return google


@router.get("/operator/auth/session")
def session(request: Request):
    user = signed_in(request)
    return JSONResponse(
        {"google": request.app.state.google is not None, "email": user and user["email"]},
        headers=NO_STORE,
    )


@router.get("/operator/auth/login")
def login(request: Request):
    google = configured(request)
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    query = urlencode({
        "response_type": "code", "client_id": google.client_id,
        "redirect_uri": callback_uri(request), "scope": "openid email",
        "state": state, "nonce": nonce, "code_challenge_method": "S256",
        "code_challenge": b64(sha256(verifier.encode("ascii")).digest()),
    })
    response = RedirectResponse(f"{AUTHORIZATION_ENDPOINT}?{query}", 303, headers=NO_STORE)
    response.set_cookie(
        LOGIN_COOKIE, sign([state, nonce, verifier, int(time.time()) + LOGIN_SECONDS]),
        max_age=LOGIN_SECONDS, path=AUTH_PATH, secure=is_https(request), httponly=True,
        samesite="lax",
    )
    return response


@router.get("/operator/auth/callback", name="google_callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None):
    google = configured(request)
    pending = unsign(request.cookies.get(LOGIN_COOKIE))
    try:
        if error is not None:
            raise SignInError(403, "Google did not complete sign-in")
        if pending is None:
            raise SignInError(400, "The sign-in expired or started in another browser")
        expected_state, nonce, verifier = pending
        if not state or not hmac.compare_digest(state, expected_state):
            raise SignInError(400, "The sign-in state did not match")
        if not code:
            raise SignInError(400, "Google returned no authorization code")
        identity = verified_identity(
            google, exchange(google, code, verifier, callback_uri(request)), nonce)
        session_id = secrets.token_urlsafe(32)
        now = int(time.time())
        with write_transaction(request.app.state.database_path) as db:
            db.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now,))
            db.execute("INSERT INTO user_sessions VALUES (?, ?, ?, ?, ?)",
                       (digest(session_id), *identity, now + SESSION_SECONDS))
        response = RedirectResponse("/operator/", 303, headers=NO_STORE)
        response.set_cookie(
            SESSION_COOKIE, session_id, max_age=SESSION_SECONDS, path="/",
            secure=is_https(request), httponly=True, samesite="lax",
        )
    except SignInError as failure:
        response = PlainTextResponse(
            f"Google sign-in failed: {failure}. Open /operator/ to try again.",
            failure.status, headers=NO_STORE,
        )
    response.delete_cookie(LOGIN_COOKIE, path=AUTH_PATH, secure=is_https(request),
                           httponly=True, samesite="lax")
    return response


@router.post("/operator/auth/logout", status_code=204)
def logout(request: Request):
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and len(cookie) <= 200 and cookie.isascii():
        with write_transaction(request.app.state.database_path) as db:
            db.execute("DELETE FROM user_sessions WHERE id_hash = ?", (digest(cookie),))
    response = Response(status_code=204, headers=NO_STORE)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=is_https(request), httponly=True,
                           samesite="lax")
    return response
