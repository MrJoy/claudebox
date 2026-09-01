"""What changed on a pull request since we last reviewed it.

A cycle used to re-review every candidate whether or not anything had happened
to it, which costs (PRs x personas) provider sessions per cycle to conclude
that nothing had. This module holds the decision: a per-PR fingerprint, the
test that tells claudebox's own comments from everybody else's, and the settle
arithmetic that batches a burst of pushes into one review.

Everything here is pure. The GitHub calls live in gh.py, and the state that
outlives a cycle lives on Supervisor.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Tuple

# Comments claudebox posts carry this, because personas/*/_shared.md tells every
# persona to sign its findings with it. Author login is deliberately NOT
# consulted: claudebox is commonly run under the operator's own PAT, where
# matching on login would classify the operator's own comments as ours and the
# comment trigger would never fire for the person most likely to want it.
MARKER = "-claudebox"

_OFF = ("0", "false", "no", "off")


@dataclass(frozen=True)
class Signal:
    """One PR's fingerprint. Two of these differ when a re-review is owed.

    `updatedAt` is deliberately absent. It moves for edits this design decided
    to ignore (a typo fix in the PR body, a title change), and including it
    would turn each of those into a full persona fan-out. Its job is to gate
    the second GitHub lookup, which is Tracker's business, not this record's.
    """

    head_oid: str
    mode: str
    # Timestamp of the newest comment that does not carry MARKER, or "".
    # ISO-8601 Z strings compare correctly as strings, so nothing here parses.
    newest_human: str


def is_own(body: Optional[str]) -> bool:
    """True when this comment body is one claudebox posted."""
    return MARKER in (body or "").lower()


def newest_unsigned(entries: Iterable[Tuple[Optional[str], Optional[str]]]) -> str:
    """The latest timestamp among entries whose body lacks the marker.

    Entries are (timestamp, body). One missing a timestamp is skipped rather
    than raising: a gh payload we cannot read must not take the cycle with it.
    """
    best = ""
    for stamp, body in entries:
        if not isinstance(stamp, str) or not stamp:
            continue
        if is_own(body):
            continue
        if stamp > best:
            best = stamp
    return best


def _epoch(stamp: str) -> float:
    """RFC 3339 UTC as GitHub writes it, e.g. 2026-08-31T12:00:00Z."""
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def age_seconds(updated_at: str, now: float) -> Optional[float]:
    """Seconds since the timestamp, or None when it cannot be read."""
    try:
        return now - _epoch(updated_at)
    except (AttributeError, ValueError):
        return None


def is_settling(updated_at: str, settle_seconds: int, now: float) -> bool:
    """True when this change is too fresh to review yet.

    A negative age means the container clock is behind GitHub's. It runs rather
    than settling, so skew costs the batching and never the review. An
    unreadable timestamp runs for the same reason.
    """
    if settle_seconds <= 0:
        return False
    age = age_seconds(updated_at, now)
    if age is None:
        return False
    return 0 <= age < settle_seconds


def enabled(env: Mapping[str, str]) -> bool:
    """Whether the change gate is on. Default on; REVIEW_ON_CHANGE=0 turns it off."""
    return (env.get("REVIEW_ON_CHANGE", "") or "").strip().lower() not in _OFF


def change_reason(old: Optional[Signal], new: Signal) -> str:
    """Why this PR is being reviewed, for the log. Empty when nothing changed."""
    if old is None:
        return "first review"
    reasons = []
    if old.head_oid != new.head_oid:
        reasons.append(f"new head {new.head_oid[:7]}")
    if old.newest_human != new.newest_human:
        reasons.append("new comment activity")
    if old.mode != new.mode:
        reasons.append(f"mode changed to {new.mode}")
    return ", ".join(reasons)
