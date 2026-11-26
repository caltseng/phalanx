"""Authenticated Phalanx HTTP primitives shared by direct clients."""

from http.client import HTTPException
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class APIError(ValueError):
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


class APIClient:
    def __init__(self, base_url, token, *, timeout=30):
        try:
            validate_api_url(base_url)
        except ValueError as exc:
            raise APIError(str(exc)) from None
        if not token:
            raise APIError("Set PHALANX_OWNER_TOKEN or PHALANX_RUNNER_TOKEN, "
                           "or register this worktree's writer credential")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def call(self, route, body=None, *, method=None, key=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        call = Request(self.base_url + route, headers=headers, method=method,
                       data=None if body is None else json.dumps(body).encode("utf-8"))
        try:
            with self.opener.open(call, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            with exc:
                detail = exc.read().decode("utf-8", errors="replace")
            raise APIError(f"Phalanx API returned HTTP {exc.code}: {detail} (for {route})") from exc
        except (OSError, HTTPException, ValueError) as exc:
            raise APIError(
                f"Phalanx API unavailable for {route}: {exc}. "
                "Any mutation's acceptance is unknown; reconcile status before retrying. "
                "This is not a stop or an authorization response."
            ) from exc
