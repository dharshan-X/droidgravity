#!/usr/bin/env python3
"""
test_shims.py - Unit tests for xdg-open shim URL extraction and browser delegates.
"""
import unittest
import os
import subprocess
import tempfile
import shutil

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_ROOT = os.path.dirname(TESTS_DIR)
MOCK_SH = "/system/bin/sh" if os.path.exists("/system/bin/sh") else "/bin/sh"

class TestShims(unittest.TestCase):
    def setUp(self):
        self.system_xdg = os.path.join(MODULE_ROOT, "shims", "system", "xdg_open.sh")
        self.browser_xdg = os.path.join(MODULE_ROOT, "shims", "browser", "xdg_open.sh")

    def _run_shim(self, shim_path: str, args: list) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        # Ensure LD_* are not inherited interfering
        env.pop("LD_PRELOAD", None)
        env.pop("LD_LIBRARY_PATH", None)
        return subprocess.run(
            ["sh", shim_path] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )

    def test_01_http_url_extraction(self):
        for shim in (self.system_xdg, self.browser_xdg):
            self.assertTrue(os.path.isfile(shim), f"Missing shim: {shim}")
            res = self._run_shim(shim, ["http://localhost:3000/dashboard"])
            self.assertEqual(res.returncode, 0)
            self.assertIn("AGY_OPEN_URL=http://localhost:3000/dashboard", res.stdout)
            self.assertIn("ANTIGRAVITY_OPEN_URL: http://localhost:3000/dashboard", res.stdout)

    def test_02_https_with_leading_flags(self):
        for shim in (self.system_xdg, self.browser_xdg):
            res = self._run_shim(shim, ["--new-window", "-u", "https://antigravity.google.com/oauth"])
            self.assertEqual(res.returncode, 0)
            self.assertIn("AGY_OPEN_URL=https://antigravity.google.com/oauth", res.stdout)
            self.assertIn("ANTIGRAVITY_OPEN_URL: https://antigravity.google.com/oauth", res.stdout)

    def test_03_fallback_non_dash_argument(self):
        for shim in (self.system_xdg, self.browser_xdg):
            res = self._run_shim(shim, ["-a", "-b", "file:///storage/emulated/0/docs/manual.html"])
            self.assertEqual(res.returncode, 0)
            self.assertIn("AGY_OPEN_URL=file:///storage/emulated/0/docs/manual.html", res.stdout)

    def test_04_no_arguments_clean_exit(self):
        for shim in (self.system_xdg, self.browser_xdg):
            res = self._run_shim(shim, [])
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), "")

    def test_05_flags_only_clean_exit(self):
        for shim in (self.system_xdg, self.browser_xdg):
            res = self._run_shim(shim, ["--help", "-v", "--version"])
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), "")

    def test_06_browser_delegates_to_xdg_open(self):
        # Create a temp dir with xdg-open and delegate shims
        tmp = tempfile.mkdtemp()
        try:
            xdg_dest = os.path.join(tmp, "xdg-open")
            shutil.copyfile(self.system_xdg, xdg_dest)
            os.chmod(xdg_dest, 0o755)

            chrome_dest = os.path.join(tmp, "chrome")
            shutil.copyfile(os.path.join(MODULE_ROOT, "shims", "browser", "chrome.sh"), chrome_dest)
            os.chmod(chrome_dest, 0o755)

            res = subprocess.run(
                ["sh", chrome_dest, "--incognito", "https://google.com"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("AGY_OPEN_URL=https://google.com", res.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _make_mock_node_prefix(self, tmp: str, script: str) -> str:
        prefix = os.path.join(tmp, "usr")
        os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
        node_bin = os.path.join(prefix, "bin", "node")
        with open(node_bin, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(node_bin, 0o755)
        return prefix

    def test_07_agy_node_runs_existing_node(self):
        agy_node = os.path.join(MODULE_ROOT, "shims", "system", "agy_node.sh")
        tmp = tempfile.mkdtemp()
        try:
            prefix = self._make_mock_node_prefix(
                tmp, f"#!{MOCK_SH}\necho \"NODE_ARGS=$*\"\n"
            )
            env = os.environ.copy()
            env["PREFIX"] = prefix
            env["TMPDIR"] = tmp
            res = subprocess.run(
                ["sh", agy_node, "/tmp/sidecar.mjs", "--flag"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("NODE_ARGS=/tmp/sidecar.mjs --flag", res.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_08_agy_node_auto_installs_nodejs_via_pkg(self):
        agy_node = os.path.join(MODULE_ROOT, "shims", "system", "agy_node.sh")
        tmp = tempfile.mkdtemp()
        try:
            prefix = os.path.join(tmp, "usr")
            os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
            os.makedirs(os.path.join(prefix, "var", "lib", "pkg", "index"), exist_ok=True)
            log_file = os.path.join(tmp, "pkg.log")

            pkg_bin = os.path.join(prefix, "bin", "pkg")
            with open(pkg_bin, "w", encoding="utf-8") as f:
                f.write(
                    f"#!{MOCK_SH}\n"
                    f"echo \"$*\" >> \"{log_file}\"\n"
                    "if [ \"$1\" = \"update\" ]; then\n"
                    "  touch \"$PREFIX/var/lib/pkg/index/repo.test.Packages\"\n"
                    "elif [ \"$1\" = \"install\" ] && [ \"$2\" = \"-y\" ] && [ \"$3\" = \"nodejs\" ]; then\n"
                    "  cat > \"$PREFIX/bin/node\" <<'EOF'\n"
                    f"#!{MOCK_SH}\n"
                    "echo \"INSTALLED_NODE_RUN=$*\"\n"
                    "EOF\n"
                    "  chmod +x \"$PREFIX/bin/node\"\n"
                    "fi\n"
                )
            os.chmod(pkg_bin, 0o755)

            env = os.environ.copy()
            env["PREFIX"] = prefix
            env["TMPDIR"] = tmp
            res = subprocess.run(
                ["sh", agy_node, "/tmp/swarm.mjs", "--port", "9000"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("INSTALLED_NODE_RUN=/tmp/swarm.mjs --port 9000", res.stdout)
            with open(log_file, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines()]
            self.assertEqual(lines, ["update", "install -y nodejs"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_09_agy_node_fails_cleanly_without_pkg_or_node(self):
        agy_node = os.path.join(MODULE_ROOT, "shims", "system", "agy_node.sh")
        tmp = tempfile.mkdtemp()
        try:
            prefix = os.path.join(tmp, "usr")
            os.makedirs(prefix, exist_ok=True)
            with open(os.path.join(prefix, ".installed"), "w", encoding="utf-8") as f:
                f.write("1\n")

            env = os.environ.copy()
            env["PREFIX"] = prefix
            env["TMPDIR"] = tmp
            res = subprocess.run(
                ["sh", agy_node, "/tmp/swarm.mjs"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 127)
            self.assertIn("node binary not installed", res.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_10_agy_node_sets_openssl_conf_when_unset_or_unreadable(self):
        agy_node = os.path.join(MODULE_ROOT, "shims", "system", "agy_node.sh")
        tmp = tempfile.mkdtemp()
        try:
            prefix = self._make_mock_node_prefix(
                tmp, f"#!{MOCK_SH}\necho \"OPENSSL_CONF=$OPENSSL_CONF\"\n"
            )
            expected = os.path.join(prefix, "etc", "tls", "openssl.cnf")
            for initial in (None, os.path.join(tmp, "missing_openssl.cnf")):
                env = os.environ.copy()
                env["PREFIX"] = prefix
                env["TMPDIR"] = tmp
                if initial is None:
                    env.pop("OPENSSL_CONF", None)
                else:
                    env["OPENSSL_CONF"] = initial
                res = subprocess.run(
                    ["sh", agy_node, "/tmp/sidecar.mjs"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                self.assertEqual(res.returncode, 0, msg=res.stderr)
                self.assertIn(f"OPENSSL_CONF={expected}", res.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_11_agy_node_preserves_readable_openssl_conf(self):
        agy_node = os.path.join(MODULE_ROOT, "shims", "system", "agy_node.sh")
        tmp = tempfile.mkdtemp()
        try:
            prefix = self._make_mock_node_prefix(
                tmp, f"#!{MOCK_SH}\necho \"OPENSSL_CONF=$OPENSSL_CONF\"\n"
            )
            custom_conf = os.path.join(tmp, "custom.cnf")
            with open(custom_conf, "w", encoding="utf-8") as f:
                f.write("# custom openssl conf\n")

            env = os.environ.copy()
            env["PREFIX"] = prefix
            env["TMPDIR"] = tmp
            env["OPENSSL_CONF"] = custom_conf
            res = subprocess.run(
                ["sh", agy_node, "/tmp/sidecar.mjs"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn(f"OPENSSL_CONF={custom_conf}", res.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
