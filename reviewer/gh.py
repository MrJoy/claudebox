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
from typing import Any, Callable, List, Mapping, Tuple

from common import ConfigError, log

Candidate = Tuple[int, str]


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
        if not token.isdigit():
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


def pr_modes(payload: Any, plan_label: str) -> List[Candidate]:
    """Turn gh --json number,labels output into (number, mode) pairs.

    Accepts an array (gh pr list) or a single object (gh pr view). A PR carrying
    plan_label is plan mode; everything else is code mode, so an operator who
    never labels anything sees exactly the pre-modes behavior.
    """
    entries = payload if isinstance(payload, list) else [payload]
    out: List[Candidate] = []
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
        out.append((number, "plan" if is_plan else "code"))
    return out


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
) -> List[Candidate]:
    """One (number, mode) pair per candidate PR."""
    repo = env["GITHUB_REPOSITORY"]
    plan_label = env.get("PLAN_LABEL") or "plan"

    def gh_run(argv):
        return run(
            argv, capture_output=True, text=True, check=False
        )

    if selector == "ids":
        out: List[Candidate] = []
        for number in parse_pr_ids(env.get("PR_IDS", "")):
            argv = [
                "gh", "pr", "view", str(number), "-R", repo, "--json", "number,labels",
            ]
            got = pr_modes(_read_json(gh_run(argv)) or [], plan_label)
            if got:
                out.extend(got)
            else:
                log(f"WARN: could not read labels for PR #{number}; skipping it this cycle.")
        return out

    base = ["gh", "pr", "list", "-R", repo]
    if selector == "all":
        argv = base + ["--state", "open", "--limit", "100", "--json", "number,labels"]
    elif selector == "assignee":
        argv = base + [
            "--state", "open", "--assignee", env["PR_ASSIGNEE"],
            "--limit", "100", "--json", "number,labels",
        ]
    elif selector == "search":
        argv = base + [
            "--search", env["PR_SEARCH"], "--limit", "100", "--json", "number,labels",
        ]
    else:
        raise ConfigError(f"unknown PR selector '{selector}'.")

    payload = _read_json(gh_run(argv))
    if payload is None:
        # Deliberate departure from the shell, which logged the same
        # "No candidate PRs" line whether gh failed or there simply were none.
        log(f"WARN: `gh pr list` failed or returned nothing usable for selector '{selector}'.")
        return []
    return pr_modes(payload, plan_label)
