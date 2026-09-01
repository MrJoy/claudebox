import unittest

import _path  # noqa: F401

import signals


class MarkerTest(unittest.TestCase):
    def test_signed_body_is_ours(self):
        self.assertTrue(signals.is_own("Looks fine to me.\n\n-claudebox (sage)"))

    def test_case_is_ignored(self):
        self.assertTrue(signals.is_own("-CLAUDEBOX (red_team)"))

    def test_unsigned_body_is_not_ours(self):
        self.assertFalse(signals.is_own("I disagree, see the ticket."))

    def test_empty_and_missing_bodies_are_not_ours(self):
        # A review submitted with no prose is still somebody acting on the PR.
        self.assertFalse(signals.is_own(""))
        self.assertFalse(signals.is_own(None))


class NewestUnsignedTest(unittest.TestCase):
    def test_picks_the_latest_unsigned_timestamp(self):
        entries = [
            ("2026-08-31T10:00:00Z", "a human"),
            ("2026-08-31T12:00:00Z", "-claudebox (sme)"),
            ("2026-08-31T11:00:00Z", "another human"),
        ]
        self.assertEqual(signals.newest_unsigned(entries), "2026-08-31T11:00:00Z")

    def test_no_unsigned_entries_gives_empty_string(self):
        entries = [("2026-08-31T12:00:00Z", "-claudebox (sage)")]
        self.assertEqual(signals.newest_unsigned(entries), "")

    def test_no_entries_at_all_gives_empty_string(self):
        self.assertEqual(signals.newest_unsigned([]), "")

    def test_entries_without_a_timestamp_are_ignored(self):
        # A malformed gh payload must not crash the cycle.
        self.assertEqual(signals.newest_unsigned([(None, "a human")]), "")


class AgeTest(unittest.TestCase):
    STAMP = "2026-08-31T00:00:00Z"

    def test_age_of_a_known_timestamp(self):
        base = signals._epoch(self.STAMP)
        self.assertEqual(signals.age_seconds(self.STAMP, base + 45.0), 45.0)

    def test_unparseable_timestamp_has_no_age(self):
        self.assertIsNone(signals.age_seconds("not a time", 0.0))

    def test_young_change_is_settling(self):
        base = signals._epoch(self.STAMP)
        self.assertTrue(signals.is_settling(self.STAMP, 30, base + 5.0))

    def test_old_change_is_not_settling(self):
        base = signals._epoch(self.STAMP)
        self.assertFalse(signals.is_settling(self.STAMP, 30, base + 31.0))

    def test_a_clock_behind_github_does_not_settle_forever(self):
        # Negative age means our clock is behind. Clamped to zero would still
        # settle; we run instead, so skew costs the batching and not the review.
        base = signals._epoch(self.STAMP)
        self.assertFalse(signals.is_settling(self.STAMP, 30, base - 600.0))

    def test_unparseable_timestamp_does_not_settle(self):
        self.assertFalse(signals.is_settling("not a time", 30, 0.0))

    def test_zero_disables_settling(self):
        self.assertFalse(
            signals.is_settling(self.STAMP, 0, signals._epoch(self.STAMP)))


class EnabledTest(unittest.TestCase):
    def test_default_is_on(self):
        self.assertTrue(signals.enabled({}))

    def test_off_spellings(self):
        for v in ("0", "false", "FALSE", "no", "off"):
            self.assertFalse(signals.enabled({"REVIEW_ON_CHANGE": v}), v)

    def test_anything_else_is_on(self):
        for v in ("1", "true", "yes", "on", "please"):
            self.assertTrue(signals.enabled({"REVIEW_ON_CHANGE": v}), v)


class ChangeReasonTest(unittest.TestCase):
    NEW = signals.Signal(head_oid="3f2a1b0deadbeef", mode="code", newest_human="")

    def test_no_prior_signal_is_a_first_review(self):
        self.assertEqual(signals.change_reason(None, self.NEW), "first review")

    def test_identical_signal_has_no_reason(self):
        self.assertEqual(signals.change_reason(self.NEW, self.NEW), "")

    def test_moved_head(self):
        old = signals.Signal(head_oid="aaaaaaa", mode="code", newest_human="")
        self.assertEqual(signals.change_reason(old, self.NEW), "new head 3f2a1b0")

    def test_new_comment(self):
        old = signals.Signal(head_oid="3f2a1b0deadbeef", mode="code", newest_human="")
        new = signals.Signal(
            head_oid="3f2a1b0deadbeef", mode="code", newest_human="2026-08-31T12:00:00Z"
        )
        self.assertEqual(signals.change_reason(old, new), "new comment activity")

    def test_mode_change(self):
        old = signals.Signal(head_oid="3f2a1b0deadbeef", mode="code", newest_human="")
        new = signals.Signal(head_oid="3f2a1b0deadbeef", mode="plan", newest_human="")
        self.assertEqual(signals.change_reason(old, new), "mode changed to plan")

    def test_several_reasons_are_all_named(self):
        old = signals.Signal(head_oid="aaaaaaa", mode="code", newest_human="")
        new = signals.Signal(
            head_oid="3f2a1b0deadbeef", mode="plan", newest_human="2026-08-31T12:00:00Z"
        )
        self.assertEqual(
            signals.change_reason(old, new),
            "new head 3f2a1b0, new comment activity, mode changed to plan",
        )


if __name__ == "__main__":
    unittest.main()
