"""A2 FPK 源包契约测试：不需要 Docker 或 fnpack。"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_fpk", ROOT / "scripts" / "build_fpk.py")
assert SPEC and SPEC.loader
build_fpk = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_fpk
SPEC.loader.exec_module(build_fpk)


class FpkPackagingTests(unittest.TestCase):
    def test_fnpack_source_has_required_contract(self):
        appname, version = build_fpk.validate_source(build_fpk.SOURCE)
        self.assertEqual(appname, "miaoyu")
        self.assertEqual(version, "0.1.0")
        self.assertTrue((build_fpk.SOURCE / "wizard").is_dir())

    def test_resource_and_entry_are_valid_json(self):
        for relative in ("config/privilege", "config/resource", "app/ui/config"):
            data = json.loads((build_fpk.SOURCE / relative).read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)

    def test_compose_uses_fnos_runtime_paths_and_no_host_searxng_port(self):
        compose = (build_fpk.SOURCE / "app/docker/docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("${TRIM_SERVICE_PORT}:5000", compose)
        self.assertIn("${TRIM_PKGVAR}/data:/app/data", compose)
        self.assertIn("${TRIM_PKGETC}/.env:/app/.env:ro", compose)
        self.assertNotIn("8080:", compose)

    def test_source_does_not_contain_runtime_secret_or_data(self):
        paths = [path.relative_to(build_fpk.SOURCE).as_posix() for path in build_fpk.SOURCE.rglob("*")]
        self.assertFalse(any(path == ".env" or path.startswith("data/") for path in paths))
        self.assertTrue(any(path == "app/docker/.env.example" for path in paths))


if __name__ == "__main__":
    unittest.main(verbosity=2)
