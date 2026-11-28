"""Historical corpus integrity does not require refreezing current Go sources."""

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


_PATH = Path(__file__).resolve().parents[1] / "server" / "conformance" / "harness.py"
_SPEC = importlib.util.spec_from_file_location("conformance_harness", _PATH)
harness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harness)


class ConformanceManifestTest(unittest.TestCase):
    def test_dirty_go_source_does_not_require_regeneration(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {}
            for name in ("golden-fresh.json", "golden-retained.json", "example.go"):
                path = root / name
                path.write_bytes(b"original")
                files["server/conformance/" + name] = harness.digest(path.read_bytes())
            manifest = root / "vectors-manifest.json"
            manifest.write_text(json.dumps({"files": files}), encoding="utf-8")
            original = manifest.read_bytes()
            (root / "example.go").write_bytes(b"edited source, not a golden")
            with patch.object(harness, "HERE", root):
                harness.verify_vectors()
            self.assertEqual(manifest.read_bytes(), original)

    def test_changed_historical_goldens_still_fail(self):
        for changed in ("golden-fresh.json", "golden-retained.json"):
            with self.subTest(changed=changed), TemporaryDirectory() as temporary:
                root = Path(temporary)
                files = {}
                for name in ("golden-fresh.json", "golden-retained.json"):
                    (root / name).write_bytes(b"original")
                    files["server/conformance/" + name] = harness.digest(b"original")
                (root / "vectors-manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")
                (root / changed).write_bytes(b"changed")
                with patch.object(harness, "HERE", root):
                    with self.assertRaisesRegex(AssertionError, "Immutable foundation golden changed"):
                        harness.verify_vectors()
