import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import _path  # noqa: F401

import codex_mcp
import codex_pass
import graph_snapshot
import kindex_tools
from common import ConfigError

try:
    import tomllib
except ImportError:  # Python < 3.11 on a developer's host; the image has 3.11.
    tomllib = None


def flags(args):
    """{key: value} from a list of -c key=value pairs."""
    pairs = [args[i + 1] for i in range(0, len(args), 2)]
    assert all(args[i] == "-c" for i in range(0, len(args), 2))
    return dict(p.split("=", 1) for p in pairs)


class BuildArgsTest(unittest.TestCase):
    def test_neither(self):
        self.assertEqual(codex_mcp.build_args("/d", False, False, "/k"), [])

    def test_kindex(self):
        f = flags(codex_mcp.build_args("/d", True, False, "/opt/kindex/bin/kin-mcp",
                                       python="/usr/bin/python3", helper="/h.py"))
        self.assertEqual(f["mcp_servers.kindex.command"], '"/usr/bin/python3"')
        self.assertEqual(f["mcp_servers.kindex.args"],
                         codex_pass.toml_array(["/h.py", "kindex", "/d", "/opt/kindex/bin/kin-mcp"]))
        self.assertEqual(f["mcp_servers.kindex.enabled_tools"],
                         codex_pass.toml_array(sorted(kindex_tools.READ_TOOLS)))
        self.assertEqual(f["mcp_servers.kindex.required"], "true")
        self.assertFalse(any(k.startswith("mcp_servers.linear") for k in f))

    def test_linear(self):
        f = flags(codex_mcp.build_args("/d", False, True, "/k", python="/usr/bin/python3", helper="/h.py"))
        self.assertEqual(f["mcp_servers.linear.url"], '"https://mcp.linear.app/mcp"')
        self.assertEqual(f["mcp_servers.linear.http_headers_helper"],
                         '"/usr/bin/python3 /h.py linear-headers /d"')
        self.assertNotIn("mcp_servers.linear.required", f)
        self.assertNotIn("mcp_servers.linear.enabled_tools", f)
        self.assertFalse(any(k.startswith("mcp_servers.kindex") for k in f))

    def test_whitespace_in_the_helper_command_is_refused(self):
        # Codex splits http_headers_helper on spaces (measured).
        for kw in ({"helper": "/a b/h.py"}, {"python": "/a b/python3"}):
            with self.assertRaises(ConfigError):
                codex_mcp.build_args("/d", False, True, "/k", **kw)
        with self.assertRaises(ConfigError):
            codex_mcp.build_args("/a b", False, True, "/k")

    def test_whitespace_is_harmless_under_kindex_alone(self):
        codex_mcp.build_args("/a b", True, False, "/k b", python="/a b/p", helper="/a b/h.py")

    def test_no_credential_shaped_key_anywhere(self):
        args = codex_mcp.build_args("/d", True, True, "/k")
        self.assertFalse(any(s in a for a in args
                             for s in ("env_vars", "bearer_token", "http_headers=", ".env=")))

    def test_every_value_parses_as_toml(self):
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11")
        for k, v in flags(codex_mcp.build_args("/d", True, True, "/k")).items():
            tomllib.loads("x = " + v)


class KeyDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name, text):
        with open(os.path.join(self.dir.name, name), "w") as f:
            f.write(text)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = codex_mcp.main(argv)
        return rc, out.getvalue(), err.getvalue()


class LinearHeadersTest(KeyDirTest):
    def test_prints_the_header_json(self):
        self.write("linear.key", 'lin "x" y')
        rc, out, _ = self.run_main(["linear-headers", self.dir.name])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"Authorization": 'Bearer lin "x" y'})

    def test_one_trailing_newline_is_dropped(self):
        self.write("linear.key", "k\n")
        _, out, _ = self.run_main(["linear-headers", self.dir.name])
        self.assertEqual(json.loads(out), {"Authorization": "Bearer k"})

    def test_missing_file_exits_nonzero(self):
        rc, out, err = self.run_main(["linear-headers", self.dir.name])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("linear.key", err)

    def test_empty_file_exits_nonzero(self):
        self.write("linear.key", "")
        rc, out, err = self.run_main(["linear-headers", self.dir.name])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("linear.key", err)


class KindexWrapperTest(KeyDirTest):
    def run_exec(self, env=None):
        calls = []

        def fake(path, argv, env_):
            calls.append((path, argv, env_))
            raise SystemExit(0)

        with mock.patch.object(codex_mcp.os, "execve", fake), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            with self.assertRaises(SystemExit):
                codex_mcp.main(["kindex", self.dir.name, "/opt/kin-mcp"])
        return calls[0]

    def test_execs_kin_mcp_with_the_container_profile(self):
        path, argv, env = self.run_exec()
        self.assertEqual(path, "/opt/kin-mcp")
        self.assertEqual(argv, ["/opt/kin-mcp"])
        self.assertEqual(env["KIN_PROFILE"], graph_snapshot.CONTAINER_PROFILE)

    def test_voyage_key_from_the_file(self):
        self.write("voyage.key", "v\n")
        _, _, env = self.run_exec()
        self.assertEqual(env["VOYAGE_API_KEY"], "v")

    def test_no_voyage_key_without_the_file(self):
        _, _, env = self.run_exec({"VOYAGE_API_KEY": "leaked"})
        self.assertNotIn("VOYAGE_API_KEY", env)

    def test_an_empty_voyage_file_sets_nothing(self):
        self.write("voyage.key", "")
        _, _, env = self.run_exec({"VOYAGE_API_KEY": "leaked"})
        self.assertNotIn("VOYAGE_API_KEY", env)


class CliTest(unittest.TestCase):
    def test_unknown_subcommand_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(codex_mcp.main(["nope"]), 2)

    def test_wrong_arity_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(codex_mcp.main(["kindex", "/d"]), 2)
            self.assertEqual(codex_mcp.main([]), 2)


if __name__ == "__main__":
    unittest.main()
