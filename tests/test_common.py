import io
import unittest
from unittest import mock

import _path  # noqa: F401

import common


class PairTest(unittest.TestCase):
    def test_str_is_the_log_prefix_body(self):
        self.assertEqual(str(common.Pair(12, "code", "sage")), "#12 code/sage")

    def test_is_hashable_and_usable_as_a_dict_key(self):
        a = common.Pair(12, "code", "sage")
        b = common.Pair(12, "code", "sage")
        d = {a: "x"}
        self.assertEqual(d[b], "x")

    def test_differs_by_mode(self):
        self.assertNotEqual(
            common.Pair(12, "code", "sage"), common.Pair(12, "plan", "sage")
        )


class LogTest(unittest.TestCase):
    def test_bare_line_has_a_utc_timestamp_and_no_prefix(self):
        buf = io.StringIO()
        with mock.patch.object(common, "_stamp", return_value="14:22:07"):
            common.log("Fetching latest refs...", stream=buf)
        self.assertEqual(buf.getvalue(), "[14:22:07] Fetching latest refs...\n")

    def test_pair_line_carries_the_pair(self):
        buf = io.StringIO()
        with mock.patch.object(common, "_stamp", return_value="14:22:07"):
            common.log("review complete", pair=common.Pair(12, "code", "sage"), stream=buf)
        self.assertEqual(
            buf.getvalue(), "[14:22:07] [#12 code/sage] review complete\n"
        )


if __name__ == "__main__":
    unittest.main()
