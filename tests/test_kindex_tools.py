import os
import tempfile
import unittest

import _path  # noqa: F401

import kindex_tools
from common import ConfigError


def tools_file(case, text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    fh.write(text)
    fh.close()
    case.addCleanup(os.unlink, fh.name)
    return fh.name


class DenyArgsTest(unittest.TestCase):
    def test_a_write_tool_is_denied_and_a_read_tool_is_not(self):
        argv = kindex_tools.deny_args(tools_file(self, "add\nsearch\n"))
        self.assertEqual(argv, ["--disallowedTools", "mcp__kindex__add"])

    def test_an_unclassified_tool_is_denied(self):
        # Fails closed: a tool a kindex bump adds is denied until classified.
        argv = kindex_tools.deny_args(tools_file(self, "search\nbrand_new_tool\n"))
        self.assertEqual(argv, ["--disallowedTools", "mcp__kindex__brand_new_tool"])

    def test_denied_names_are_sorted_and_comma_joined(self):
        argv = kindex_tools.deny_args(tools_file(self, "link\nadd\nsearch\n"))
        self.assertEqual(argv, ["--disallowedTools", "mcp__kindex__add,mcp__kindex__link"])

    def test_only_read_tools_means_no_flag(self):
        self.assertEqual(kindex_tools.deny_args(tools_file(self, "search\nshow\n")), [])

    def test_an_empty_file_is_a_config_error(self):
        with self.assertRaisesRegex(ConfigError, "lists no kindex tools"):
            kindex_tools.deny_args(tools_file(self, "\n\n"))

    def test_a_missing_file_is_a_config_error(self):
        with self.assertRaisesRegex(ConfigError, "cannot read"):
            kindex_tools.deny_args("/nonexistent/tools.txt")


class ClassificationTest(unittest.TestCase):
    def test_side_effecting_lookalikes_are_not_read_tools(self):
        for name in ("coord_read", "remind_check", "stale_check", "graph_heal",
                     "add", "remind_exec", "task_execute", "dream", "learn"):
            self.assertNotIn(name, kindex_tools.READ_TOOLS, name)

    def test_the_search_path_is_readable(self):
        for name in ("search", "context", "show", "ask", "list_nodes"):
            self.assertIn(name, kindex_tools.READ_TOOLS, name)


if __name__ == "__main__":
    unittest.main()
