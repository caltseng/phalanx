"""Authenticated onboarding-to-server HTTP client."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class APIError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class APIClient:
    def __init__(self, base_url, token):
        url = urlsplit(base_url)
        if (
            not url.hostname or url.username or url.password or url.query or url.fragment
            or (url.scheme != "https" and not (
                url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            ))
        ):
            raise APIError("Use HTTPS or a loopback HTTP API URL without credentials")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = build_opener(NoRedirect())

    def call(self, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + "/v1" + path, headers=headers,
                          data=None if body is None else json.dumps(body).encode())
        try:
            with self.opener.open(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            with exc:
                raise APIError(f"API returned HTTP {exc.code} for {path}") from None
        except (URLError, TimeoutError, ValueError):
            raise APIError(f"API response unavailable for {path}") from None
