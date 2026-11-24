"""Read-only discovery of an existing direct muster. Never provision or repair it."""

from argparse import SUPPRESS
import json
import os
from pathlib import Path

from .reconciler import API, ReconcilerError


class MusterError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MusterError("Ambiguous muster locator: duplicate fields")
        result[key] = value
    return result


def resolve_home(home=None):
    override = home if home is not None else os.environ.get("PHALANX_HOME")
    if override is not None:
        if not str(override).strip():
            raise MusterError("PHALANX_HOME/--home must name an existing absolute muster directory")
        path = Path(override).expanduser()
    else:
        locator = Path.home() / ".phalanx" / "muster.json"
        try:
            data = json.loads(locator.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique)
        except OSError:
            raise MusterError(f"Cannot read muster locator {locator}; create it with one absolute "
                              "'home' path or set PHALANX_HOME") from None
        except (ValueError, UnicodeError):
            raise MusterError(f"Invalid or ambiguous muster locator {locator}; "
                              "expected one JSON object with only a 'home' path") from None
        if (not isinstance(data, dict) or set(data) != {"home"}
                or not isinstance(data["home"], str) or not data["home"].strip()):
            raise MusterError(f"Invalid or ambiguous muster locator {locator}; "
                              "expected one JSON object with only a 'home' path")
        path = Path(data["home"]).expanduser()
    if not path.is_absolute():
        raise MusterError(f"Muster directory must be absolute: {path}")
    if not path.is_dir():
        raise MusterError(f"Muster directory is missing or not a directory: {path}")
    return path.resolve()


def muster_file(home, name):
    path = home / name
    if not path.is_file():
        raise MusterError(f"Muster is missing {name}: {path}")
    return path


def read_environment(path):
    """Parse private settings without executing shell syntax or exposing values."""
    allowed = {"PHALANX_OWNER_TOKEN", "PHALANX_RUNNER_TOKEN", "PHALANX_GITHUB_TOKEN",
               "PHALANX_DB", "PHALANX_LOCAL_BOARD", "PHALANX_API_URL",
               "PHALANX_HOME", "PHALANX_SHIFT_CONFIG"}
    values = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise MusterError(f"Cannot read private environment {path}") from None
    if path.suffix.lower() == ".json":
        try:
            values = json.loads(text, object_pairs_hook=_unique)
        except ValueError:
            raise MusterError(f"Invalid private environment {path}") from None
        if (not isinstance(values, dict) or set(values) - allowed
                or not all(isinstance(value, str) and value.strip() for value in values.values())):
            raise MusterError(f"Invalid private environment {path}; expected supported string settings")
        return values
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in allowed or key in values or not value:
            raise MusterError(f"Invalid private environment {path} at line {number}; "
                              "expected a unique supported KEY=VALUE")
        if value[0] in "\"'":
            if len(value) < 2 or value[-1] != value[0]:
                raise MusterError(f"Invalid quoted value in {path} at line {number}")
            value = value[1:-1]
        if not value:
            raise MusterError(f"Empty private environment value in {path} at line {number}")
        values[key] = value
    return values


def load_environment(home):
    """Load literal private env values, never execute a shell or print their contents."""
    values = read_environment(muster_file(home, "private.env"))
    for key, value in values.items():
        os.environ.setdefault(key, value)


def direct_config(kind, home=None):
    root = resolve_home(home)
    path = muster_file(root, f"{kind}.json")
    if kind == "shift":
        muster_file(root, "charter.toml")
    load_environment(root)
    return root, path


def add_muster_command(commands):
    parser = commands.add_parser("muster", help="inspect an existing direct muster (never initializes state)")
    parser.add_argument("--home", help="existing muster directory; overrides PHALANX_HOME and the locator")
    actions = parser.add_subparsers(dest="muster_command", required=True)
    status = actions.add_parser("status", help="read API identity, Captains, writers and reconciler freshness")
    status.add_argument("--home", default=SUPPRESS)


def run_muster(args):
    home, path = direct_config("shift", args.home)
    from .shift import load_config
    config = load_config(path, home=home)
    # This client intentionally uses the no-redirect API transport.
    api = API(config["base_url"], os.environ.get("PHALANX_RUNNER_TOKEN"))
    try:
        result = api.call("/muster/status")
    except ReconcilerError:
        raise MusterError("Muster status unavailable: API unreachable, authentication failed, "
                          "or this server does not support /v1/muster/status; "
                          "check the selected shift config and private environment") from None
    reported = result.get("identity", {}).get("home")
    if not isinstance(reported, str) or Path(reported) != home:
        raise MusterError("API muster identity does not match the selected directory; "
                          "start the intended API with PHALANX_HOME set to that directory")
    print(json.dumps({"home": str(home), "api_url": api.base_url, **result}, indent=2))
