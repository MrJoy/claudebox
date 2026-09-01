"""PR selection and review-mode routing.

Which PRs to review is chosen by exactly one selector env var. Review mode is
decided here too, at the one seam that already decides what gets reviewed at
all, so nothing downstream asks GitHub a second time.

Everything that reads gh output treats "exited 0 with nothing usable" as the
default hazard rather than an edge case. That is not defensiveness for its own
sake: a successful-but-empty label query once dropped a PR with no log line, and
an unguarded number field once produced a candidate PR literally named `null`.
"""

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping

from common import ConfigError, log


@dataclass(frozen=True)
class PRSnapshot:
    """One candidate PR as stage one sees it.

    head_oid and updated_at ride along in the call that was already being made
    for labels, so change detection costs no request on a cycle where nothing
    happened. A field gh did not return becomes "", which never matches a
    recorded fingerprint and so reviews the PR: the safe direction.
    """

    number: int
    mode: str
    head_oid: str
    updated_at: str


def pr_truthy(value) -> bool:
    """True when the value is a truthy flag: 1 / true / yes, any case.

    Deliberately does not strip: the shell version piped through `tr` without
    trimming, so " true" was falsy there and stays falsy here.
    """
    return (value or "").lower() in ("1", "true", "yes")


def parse_pr_ids(raw: str) -> List[int]:
    """Split a comma/whitespace-separated list into ints, refusing anything else."""
    out: List[int] = []
    for token in (raw or "").replace(",", " ").split():
        # ASCII digits only, which is what the shell's `*[!0-9]*` case guard
        # accepted. A bare str.isdigit() gate is both too wide and too narrow:
        # it lets through characters int() then refuses ('\u00b2' among them),
        # turning a typo into an uncaught ValueError traceback, and it accepts
        # non-ASCII decimal digits, so PR_IDS=١٢ would quietly review PR 12.
        if not (token.isascii() and token.isdigit()):
            raise ConfigError(
                f"PR_IDS contains a non-numeric value: '{token}' (expected e.g. 12,15,20)"
            )
        out.append(int(token))
    return out


def resolve_pr_selection(env: Mapping[str, str]) -> str:
    """Return the single active selector, or raise."""
    active: List[str] = []
    if pr_truthy(env.get("PR_ALL")):
        active.append("all")
    if env.get("PR_ASSIGNEE"):
        active.append("assignee")
    if env.get("PR_IDS"):
        active.append("ids")
    if env.get("PR_SEARCH"):
        active.append("search")

    if not active:
        raise ConfigError(
            "no PR selector set; provide exactly one of PR_ALL, PR_ASSIGNEE, "
            "PR_IDS, PR_SEARCH (launcher: --all / --assignee / --prs / --search)."
        )
    if len(active) > 1:
        raise ConfigError(
            "multiple PR selectors set; provide exactly one of PR_ALL, "
            "PR_ASSIGNEE, PR_IDS, PR_SEARCH."
        )
    selector = active[0]
    # Validate the ID list up front so a bad value fails fast, not every cycle.
    if selector == "ids":
        parse_pr_ids(env.get("PR_IDS", ""))
    return selector


def pr_modes(payload: Any, plan_label: str) -> List[PRSnapshot]:
    """Turn gh --json number,labels,headRefOid,updatedAt output into snapshots.

    Accepts an array (gh pr list) or a single object (gh pr view). A PR carrying
    plan_label is plan mode; everything else is code mode, so an operator who
    never labels anything sees exactly the pre-modes behavior.
    """
    entries = payload if isinstance(payload, list) else [payload]
    out: List[PRSnapshot] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if not isinstance(number, int):
            continue
        labels = entry.get("labels") or []
        is_plan = any(
            isinstance(lbl, dict) and lbl.get("name") == plan_label for lbl in labels
        )
        out.append(PRSnapshot(
            number=number,
            mode="plan" if is_plan else "code",
            head_oid=entry.get("headRefOid") or "",
            updated_at=entry.get("updatedAt") or "",
        ))
    return out


def _stderr_tail(result) -> List[str]:
    """The last few lines of gh's own stderr, ready to log.

    capture_output means gh no longer writes into the container log itself, and
    without this a rate limit, a 401 and a malformed --search query are the same
    generic WARN. Truncated for the reason the usage-limit line is: gh's stderr
    is not a stream that can be assumed credential-free.
    """
    text = getattr(result, "stderr", "") or ""
    return [f"  {line[:400]}" for line in text.splitlines()[-5:] if line.strip()]


def _read_json(result) -> Any:
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def enumerate_candidate_prs(
    selector: str,
    env: Mapping[str, str],
    run: Callable[..., Any] = subprocess.run,
) -> List[PRSnapshot]:
    """One PRSnapshot per candidate PR."""
    repo = env["GITHUB_REPOSITORY"]
    plan_label = env.get("PLAN_LABEL") or "plan"

    def gh_run(argv):
        try:
            return run(
                argv, capture_output=True, text=True, check=False
            )
        except OSError as exc:
            # A gh that cannot be spawned at all -- EAGAIN against
            # --pids-limit, ENOMEM against --memory, a gh that is not on PATH.
            # The shell's `|| true` treated that as an empty result, and an
            # empty cycle costs one log line where a traceback out of PID 1
            # restarts the container and loses the session map.
            return subprocess.CompletedProcess(argv, 1, "", f"could not run gh: {exc}")

    if selector == "ids":
        out: List[PRSnapshot] = []
        for number in parse_pr_ids(env.get("PR_IDS", "")):
            argv = [
                "gh", "pr", "view", str(number), "-R", repo, "--json",
                "number,labels,headRefOid,updatedAt",
            ]
            result = gh_run(argv)
            got = pr_modes(_read_json(result) or [], plan_label)
            if got:
                out.extend(got)
            else:
                log(f"WARN: could not read labels for PR #{number}; skipping it this cycle.")
                for line in _stderr_tail(result):
                    log(line)
        return out

    base = ["gh", "pr", "list", "-R", repo]
    if selector == "all":
        argv = base + [
            "--state", "open", "--limit", "100", "--json",
            "number,labels,headRefOid,updatedAt",
        ]
    elif selector == "assignee":
        argv = base + [
            "--state", "open", "--assignee", env["PR_ASSIGNEE"],
            "--limit", "100", "--json", "number,labels,headRefOid,updatedAt",
        ]
    elif selector == "search":
        argv = base + [
            "--search", env["PR_SEARCH"], "--limit", "100", "--json",
            "number,labels,headRefOid,updatedAt",
        ]
    else:
        raise ConfigError(f"unknown PR selector '{selector}'.")

    result = gh_run(argv)
    payload = _read_json(result)
    if payload is None:
        # Deliberate departure from the shell, which logged the same
        # "No candidate PRs" line whether gh failed or there simply were none.
        log(f"WARN: `gh pr list` failed or returned nothing usable for selector '{selector}'.")
        for line in _stderr_tail(result):
            log(line)
        return []
    return pr_modes(payload, plan_label)
