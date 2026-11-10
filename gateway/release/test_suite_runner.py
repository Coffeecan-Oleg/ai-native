"""Acceptance tests for the suite runner using tiny disposable repositories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).with_name("run-suite.py")


class SuiteRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="gwr-"))
        self.root = Path(self.temp).resolve()
        for folder in ("tests", "onboarding_tests", "release"):
            (self.root / folder).mkdir()
        shutil.copyfile(SOURCE, self.root / "release/run-suite.py")

    def run_fixture(self, files, timeout=10):
        for name, code in files.items():
            (self.root / "tests" / name).write_text(code, encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"}}
        env["GATEWAY_JWT_SECRET"] = "synthetic-value-that-must-not-reach-child"
        run = subprocess.run([sys.executable, str(self.root / "release/run-suite.py"),
                              "--output", str(self.root / "reports"), "--timeout", str(timeout)],
                             cwd=self.root, env=env, capture_output=True, timeout=30)
        return run, json.loads((self.root / "reports/summary.json").read_text())

    def test_module_isolation_and_removal_of_inherited_secrets(self):
        run, summary = self.run_fixture({
            "test_a.py": "import builtins,unittest\nbuiltins.polluted=True\nclass T(unittest.TestCase):\n def test_a(self): self.assertTrue(True)\n",
            "test_b.py": "import builtins,os,unittest\nclass T(unittest.TestCase):\n def test_b(self):\n  self.assertFalse(hasattr(builtins,'polluted'))\n  self.assertNotIn('GATEWAY_JWT_SECRET',os.environ)\n",
        })
        self.assertEqual(run.returncode, 0)
        self.assertEqual(summary["tests"], 2)
        self.assertTrue(summary["successful"])

    def test_assertion_failure_fails_the_whole_run(self):
        run, summary = self.run_fixture({"test_fail.py": "import unittest\nclass T(unittest.TestCase):\n def test_fail(self): self.fail('intentional fixture failure')\n"})
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(summary["failures"], 1)
        self.assertFalse(summary["successful"])

    def test_import_failure_is_not_a_successful_empty_suite(self):
        run, summary = self.run_fixture({"test_import.py": "raise ImportError('intentional fixture import failure')\n"})
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(summary["errors"], 1)

    def test_unexpected_skip_fails_the_whole_run(self):
        run, summary = self.run_fixture({"test_skip.py": "import unittest\nclass T(unittest.TestCase):\n @unittest.skip('intentional skip')\n def test_skip(self): pass\n"})
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(len(summary["unexpected_skips"]), 1)

    def test_empty_module_fails_the_whole_run(self):
        run, summary = self.run_fixture({"test_empty.py": "# no test cases\n"})
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(summary["successful"])

    def test_timeout_is_reported_and_fails_the_whole_run(self):
        run, summary = self.run_fixture({"test_timeout.py": "import time\ntime.sleep(10)\n"}, timeout=1)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(summary["results"][0]["exit_code"], 124)

    def test_external_network_is_denied_before_importing_tests(self):
        run, summary = self.run_fixture({"test_network.py": "import socket,unittest\nclass T(unittest.TestCase):\n def test_network(self):\n  with self.assertRaisesRegex(PermissionError,'ci_external_network_denied'): socket.getaddrinfo('must-not-resolve.example.invalid',443)\n"})
        self.assertEqual(run.returncode, 0)
        self.assertTrue(summary["successful"])


if __name__ == "__main__":
    unittest.main()
