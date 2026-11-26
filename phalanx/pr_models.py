"""GitHub identities and check configuration shared by direct clients and server."""

from datetime import timedelta
from hashlib import sha256
import re
from typing import Annotated

from pydantic import Field, StringConstraints

from . import validation
from .intake_models import Payload

SHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=255)]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Flag = Annotated[bool, Field(strict=True)]
DEPLOYMENT_WINDOW = timedelta(hours=24)


class RequiredCheck(Payload):
    name: Name
    kind: validation.CheckKind
    app_id: PositiveInt | None = None
    use_merge_sha: Flag = False
    require_execution: Flag = False


GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([a-z0-9_.-]+/[a-z0-9_.-]+?)(?:\.git)?/?", re.IGNORECASE | re.ASCII,
)


def github_repository(remote, *, lowercase=True):
    match = GITHUB_REMOTE.fullmatch(remote)
    return (match[1].lower() if lowercase else match[1]) if match else None


def github_issue_key(repository_id, repository, number):
    identity = f"{repository_id}:{repository}:{number}"
    return f"github-issue:{sha256(identity.encode()).hexdigest()}"
