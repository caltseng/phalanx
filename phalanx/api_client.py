"""Authenticated Phalanx HTTP primitives shared by direct clients."""

from http.client import HTTPException
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class APIError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_api_url(base_url):
    url = urlsplit(base_url)
    if (
        not url.hostname or url.username or url.password or url.query or url.fragment
        or (url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ))
    ):
        raise ValueError("Use HTTPS or a loopback HTTP API URL without credentials")


def request(base_url, token, route, *, body=None, method=None, key=None, timeout=30, opener=None):
    validate_api_url(base_url)
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    call = Request(base_url.rstrip("/") + route, headers=headers, method=method,
                   data=None if body is None else json.dumps(body).encode())
    with (opener or build_opener(NoRedirect())).open(call, timeout=timeout) as response:
        return json.load(response)


class APIClient:
    error = APIError
    timeout = 10
    detailed_errors = False

    def __init__(self, base_url, token):
        try:
            validate_api_url(base_url)
        except ValueError as exc:
            raise self.error(str(exc)) from None
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = build_opener(NoRedirect())

    def call(self, path, body=None):
        try:
            return request(self.base_url, self.token, "/v1" + path, body=body,
                           timeout=self.timeout, opener=self.opener)
        except HTTPError as exc:
            with exc:
                detail = (": " + exc.read()[:500].decode("utf-8", errors="replace")
                          if self.detailed_errors else "")
            prefix = "Phalanx " if self.detailed_errors else ""
            raise self.error(f"{prefix}API returned HTTP {exc.code} for {path}{detail}") from None
        except (OSError, HTTPException, ValueError) as exc:
            message = (f"Phalanx API unavailable for {path}: {exc!r}" if self.detailed_errors
                       else f"API response unavailable for {path}")
            raise self.error(message) from None
