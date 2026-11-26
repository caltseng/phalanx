"""One bounded scouting page per existing Captain wake; no scheduler or local journal."""

import os

from .api_client import APIError
from .github import GitHubClient, GitHubError, github_repository


def scout(api, captain_id):
    try:
        return _scout(api, captain_id)
    except APIError:
        # A scouting outage must not turn a successfully heartbeating Captain into a
        # failed/replacement shift. Never include a response body in diagnostics.
        try:
            current = api("/v1/issue-scout")
        except APIError:
            current = {"state": "failed", "last_pass": None}
        return {**current, "observation_error": "api",
                "resume_trigger": "Next Captain wake reconciles the durable claim/cursor; do not replace the Captain"}


def _scout(api, captain_id):
    current = api("/v1/issue-scout")
    if not current["settings"]["enabled"]:
        return current
    result = api("/v1/issue-scout/claim", body={"captain_id": captain_id})
    claim = result["claim"]
    if claim is None:
        return result
    route = f"/v1/issue-scout/passes/{claim['id']}"
    token = os.environ.get("PHALANX_GITHUB_TOKEN")
    failure = None
    http_status, retry_after = None, 300
    if not token:
        failure = "credential"
    else:
        repository = github_repository(claim["remote"])
        owner, name = repository.split("/")
        client = GitHubClient(owner, name, token=token)
        try:
            identity = client.authenticated_user()
            configuration = result["settings"]
            if (identity["id"] != configuration["author_id"]
                    or identity["login"].casefold() != configuration["author_login"].casefold()):
                failure = "identity"
            else:
                issues, has_more = client.scout_issues_page(claim["page"])
        except GitHubError as error:
            failure = "github"
            http_status = error.status
            retry_after = min(604800, error.retry_after_seconds or 300)
    if failure:
        return api(route + "/failure", body={
            "claim_id": claim["claim_id"], "kind": failure,
            "http_status": http_status, "retry_after_seconds": retry_after})
    # An uncertain POST is not a failed/empty scan. Its claim/cursor survive until the
    # next wake; atomic server intake and per-page receipts reconcile a lost response.
    return api(route + "/page", body={
        "claim_id": claim["claim_id"], "repository_id": claim["repository_id"], "page": claim["page"],
        "verified_login": identity["login"], "verified_id": identity["id"],
        "issues": issues, "has_more": has_more})
