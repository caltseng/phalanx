"""The server and the runner/client stay separate components that share only contracts."""

import ast
from pathlib import Path
import subprocess
import sys
import unittest


PACKAGE = Path(__file__).resolve().parents[1] / "phalanx"
# Server-only: queue storage, HTTP routes and server-side lifecycle handlers.
SERVER = {
    "api", "auth", "captains", "config", "dependencies", "dependency_schema", "direct_schema", "dispatch",
    "dispatch_api", "dispatch_schema", "finalization", "finalization_schema", "flags", "intake", "intake_api",
    "intake_schema", "muster_api", "operator_api", "operator_schema", "operator_web", "pr_observations", "reconciliation",
    "reconciliation_schema", "server_admin", "store", "wait_schema", "waits", "writers",
}
# Wire contracts both sides import. They depend only on each other.
SHARED = {"intake_models", "pr_models", "transitions", "validation"}
SERVER_PACKAGES = {"fastapi", "starlette", "uvicorn"}
MODULES = sorted(path.stem for path in PACKAGE.glob("*.py") if path.stem != "__init__")
CLIENT = [name for name in MODULES if name not in SERVER | SHARED]


def imports(name):
    """Yield (phalanx module or external package, imported at module level)."""
    tree = ast.parse((PACKAGE / f"{name}.py").read_text(encoding="utf-8"))
    top = set(map(id, tree.body))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            if node.level or parts[0] == "phalanx":
                local = parts[node.level == 0:] if parts != [""] else []
                targets = local[:1] or [alias.name for alias in node.names]
            else:
                targets = [f"external:{parts[0]}"]
        elif isinstance(node, ast.Import):
            targets = [alias.name.split(".")[1] if alias.name.startswith("phalanx.")
                       else f"external:{alias.name.split('.')[0]}" for alias in node.names]
        else:
            continue
        for target in targets:
            yield target, id(node) in top


class ImportBoundaryTest(unittest.TestCase):
    def test_modules_import_only_their_side_and_shared_contracts(self):
        self.assertLessEqual(SERVER | SHARED, set(MODULES))
        for name in MODULES:
            for target, top_level in imports(name):
                with self.subTest(module=name, imports=target):
                    if target.startswith("external:"):
                        if name not in SERVER:
                            self.assertNotIn(target.removeprefix("external:"), SERVER_PACKAGES)
                    elif name in SHARED:
                        self.assertIn(target, SHARED)
                    elif name in SERVER:
                        self.assertIn(target, SERVER | SHARED)
                    elif target in SERVER:
                        # The CLI dispatches server-host-only admin commands without loading them.
                        self.assertEqual((name, target, top_level), ("__main__", "server_admin", False))

    def test_client_modules_import_without_server_packages(self):
        blocked = "; ".join(f"sys.modules[{package!r}] = None" for package in sorted(SERVER_PACKAGES))
        code = f"import importlib, sys; {blocked}\n" + "\n".join(
            f"importlib.import_module('phalanx.{name}')" for name in CLIENT + sorted(SHARED))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
