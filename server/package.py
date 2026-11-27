"""Package a pinned server binary and canonical Git assets; never read live config/data."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def package(binary, output):
    binary = binary.resolve(strict=True)
    identity = json.loads(subprocess.check_output(
        [str(binary), "--version"], timeout=10,
        env={key: value for key, value in os.environ.items() if not key.startswith("PHALANX_")}))
    revision = identity["revision"]
    if (not isinstance(revision, str) or not re.fullmatch("[0-9a-f]{40}", revision)
            or identity["go"] != "go1.27.4" or identity["schema"] != 26
            or identity["application_id"] != 0x50484C58
            or identity["os"] not in ("windows", "linux", "darwin")):
        raise ValueError("Binary needs a pinned supported build identity")
    actual = subprocess.check_output(
        ["git", "rev-parse", revision + "^{commit}"], cwd=ROOT, timeout=10).decode().strip()
    if actual != revision:
        raise ValueError("Binary revision is not a local immutable commit")
    subprocess.run(["git", "diff", "--exit-code", revision, "--", "server",
                    "phalanx/deployment.py", "phalanx/static", "requirements.lock",
                    "docs/go-server-release.md"], cwd=ROOT, check=True, timeout=10,
                   stdout=subprocess.DEVNULL)
    if subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "server",
             "phalanx/deployment.py", "phalanx/static", "requirements.lock", "docs/go-server-release.md"],
            cwd=ROOT, timeout=10).strip():
        raise ValueError("Untracked release sources cannot be packaged as a pinned revision")
    archive = subprocess.check_output(
        ["git", "-c", "core.autocrlf=false", "archive", "--format=zip", revision,
         "phalanx/static", "server/go.mod", "server/go.sum", "requirements.lock",
         "docs/go-server-release.md"], cwd=ROOT, timeout=30)
    files = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for name in sorted(source.namelist()):
            if name.endswith("/"):
                continue
            data = source.read(name)
            if name.startswith("phalanx/static/"):
                target = "assets/" + name.removeprefix("phalanx/static/")
            elif name.startswith("server/"):
                target = name.removeprefix("server/")
            else:
                target = name.split("/")[-1]
            files[target] = data
    name = "phalanx-server.exe" if identity["os"] == "windows" else "phalanx-server"
    files[name] = binary.read_bytes()
    manifest = {"format": "phalanx-go-server-package-v1", "build": identity,
                "files": {name: sha256(data) for name, data in sorted(files.items())},
                "build_flags": ["-trimpath", "-buildvcs=false", "-ldflags=-X main.revision=" + revision],
                "clients": "unchanged Python CLI/reconciler and canonical Scribe",
                "deployment": "supported default Go HTTP server; operator-owned adoption; "
                              "explicit deprecated Python rollback retained"}
    files["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("Package output must be outside the checkout")
    output.mkdir(parents=True, exist_ok=False)
    destination = output / f"phalanx-server-{revision}-{identity['os']}-{identity['arch']}.zip"
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED) as target:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (0o100755 if name.startswith("phalanx-server") else 0o100644) << 16
            target.writestr(entry, data)
    (output / "manifest.json").write_bytes(files["manifest.json"])
    print(json.dumps({"package": str(destination), "sha256": sha256(destination.read_bytes()),
                      "build": identity}, sort_keys=True))
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.binary, args.output)


if __name__ == "__main__":
    main()
