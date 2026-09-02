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

    def test_a_quoted_signature_is_a_human_reply(self):
        # GitHub's Quote reply button copies the quoted comment verbatim into
        # the new body behind "> ", so a human disputing a claudebox finding
        # carries the marker. Replying to a finding is the single most likely
        # human action on a reviewed PR, and it is exactly the input the
        # followup prompt exists to consume.
        body = (
            "> Nit: this leaks a file descriptor.\n"
            "> \n"
            "> -claudebox (red_team)\n"
            "\n"
            "It does not; the context manager closes it."
        )
        self.assertFalse(signals.is_own(body))

    def test_an_indented_quote_is_still_a_quote(self):
        self.assertFalse(signals.is_own("   > -claudebox (sage)\n\nWrong."))

    def test_a_quoting_reply_that_also_signs_is_still_ours(self):
        # A persona quoting its own earlier finding still signs below the
        # quote, so the unquoted half decides.
        self.assertTrue(signals.is_own("> earlier\n\nStill stands.\n\n-claudebox (sage)"))

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


class Snap:
    """Stands in for gh.PRSnapshot so this module's tests do not import gh."""

    def __init__(self, number=12, mode="code", head_oid="abc", updated_at="t1"):
        self.number = number
        self.mode = mode
        self.head_oid = head_oid
        self.updated_at = updated_at


class DegradedTest(unittest.TestCase):
    def test_carries_head_oid_and_mode_from_the_snapshot(self):
        sig = signals.degraded(Snap(head_oid="deadbeef", mode="plan", updated_at="t1"))
        self.assertEqual(sig.head_oid, "deadbeef")
        self.assertEqual(sig.mode, "plan")

    def test_empty_updated_at_yields_none(self):
        # Nothing to key a review-once-per-change on, so the caller must keep
        # failing open on every poll rather than caching a review it can never
        # re-trigger -- same reasoning as Tracker refusing to cache "".
        self.assertIsNone(signals.degraded(Snap(updated_at="")))

    def test_never_collides_with_a_real_fingerprint_for_the_same_pr(self):
        # Recovery from an outage must review once: a degraded and a real
        # Signal built from the same snapshot have to compare unequal.
        snap = Snap(head_oid="abc", mode="code", updated_at="2026-08-31T12:00:00Z")
        deg = signals.degraded(snap)
        real = signals.Signal(
            head_oid=snap.head_oid, mode=snap.mode, newest_human="2026-08-31T12:00:00Z"
        )
        self.assertNotEqual(deg, real)

    def test_is_degraded(self):
        deg = signals.degraded(Snap(updated_at="t1"))
        real = signals.Signal(head_oid="abc", mode="code", newest_human="")
        self.assertTrue(signals.is_degraded(deg))
        self.assertFalse(signals.is_degraded(real))


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

    def test_recovering_from_a_failed_lookup_does_not_claim_comment_activity(self):
        # The degraded newest_human and the real one almost always differ, so
        # naively comparing them would report "new comment activity" for a
        # transition where no comment was ever observed -- the lookup just
        # started working again.
        old = signals.degraded(Snap(head_oid="abc", mode="code", updated_at="t1"))
        new = signals.Signal(head_oid="abc", mode="code", newest_human="2026-08-31T12:00:00Z")
        reason = signals.change_reason(old, new)
        self.assertNotIn("new comment activity", reason)
        self.assertIn("recovered", reason)

    def test_failing_from_a_real_signal_does_not_claim_comment_activity(self):
        old = signals.Signal(head_oid="abc", mode="code", newest_human="2026-08-31T12:00:00Z")
        new = signals.degraded(Snap(head_oid="abc", mode="code", updated_at="t2"))
        reason = signals.change_reason(old, new)
        self.assertNotIn("new comment activity", reason)
        self.assertIn("failed", reason)

    def test_updated_at_moving_during_a_sustained_failure_is_not_comment_activity(self):
        old = signals.degraded(Snap(head_oid="abc", mode="code", updated_at="t1"))
        new = signals.degraded(Snap(head_oid="abc", mode="code", updated_at="t2"))
        reason = signals.change_reason(old, new)
        self.assertNotIn("new comment activity", reason)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fetch(self, result):
        def f(snapshot):
            self.calls.append(snapshot.number)
            return result
        return f

    def test_first_sight_fetches(self):
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        self.assertEqual(t.signal_for(Snap(), self.fetch(sig)), sig)
        self.assertEqual(self.calls, [12])

    def test_unchanged_update_time_does_not_fetch_again(self):
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        snap = Snap(updated_at="t1")
        t.signal_for(snap, self.fetch(sig))
        again = t.signal_for(snap, self.fetch(sig))
        self.assertEqual(self.calls, [12])
        self.assertEqual(again, sig)

    def test_moved_update_time_fetches_again(self):
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        t.signal_for(Snap(updated_at="t1"), self.fetch(sig))
        t.signal_for(Snap(updated_at="t2"), self.fetch(sig))
        self.assertEqual(self.calls, [12, 12])

    def test_a_failed_fetch_returns_none_and_still_records_the_poll(self):
        # Fail open once per updatedAt change, not once per cycle: a persistent
        # gh outage must not re-review every PR on every poll.
        t = signals.Tracker()
        snap = Snap(updated_at="t1")
        self.assertIsNone(t.signal_for(snap, self.fetch(None)))
        self.assertIsNone(t.signal_for(snap, self.fetch(None)))
        self.assertEqual(self.calls, [12])

    def test_a_failed_fetch_retries_once_the_update_time_moves(self):
        t = signals.Tracker()
        t.signal_for(Snap(updated_at="t1"), self.fetch(None))
        sig = signals.Signal("abc", "code", "")
        self.assertEqual(t.signal_for(Snap(updated_at="t2"), self.fetch(sig)), sig)
        self.assertEqual(self.calls, [12, 12])

    def test_a_cache_hit_reports_the_snapshots_mode_not_the_cached_one(self):
        # The most load-bearing line in this class: a label flip does not have
        # to move updatedAt to reach us as a new value, so mode comes from the
        # snapshot in hand and only the comment timestamp comes from the cache.
        # Without it a PR relabelled mid-poll is gated against the mode it was
        # last reviewed under.
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "2026-08-30T00:00:00Z")
        t.signal_for(Snap(updated_at="t1"), self.fetch(sig))
        again = t.signal_for(Snap(updated_at="t1", mode="plan"), self.fetch(sig))
        self.assertEqual(self.calls, [12])
        self.assertEqual(again.mode, "plan")
        self.assertEqual(again.newest_human, "2026-08-30T00:00:00Z")

    def test_a_cache_hit_reports_the_snapshots_head_not_the_cached_one(self):
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        t.signal_for(Snap(updated_at="t1"), self.fetch(sig))
        again = t.signal_for(Snap(updated_at="t1", head_oid="def"), self.fetch(sig))
        self.assertEqual(again.head_oid, "def")

    def test_a_snapshot_with_no_update_time_always_fetches(self):
        # "" would otherwise cache as a legitimate value and pin a PR forever.
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        t.signal_for(Snap(updated_at=""), self.fetch(sig))
        t.signal_for(Snap(updated_at=""), self.fetch(sig))
        self.assertEqual(self.calls, [12, 12])


if __name__ == "__main__":
    unittest.main()
