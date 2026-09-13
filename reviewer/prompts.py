"""Prompt assembly.

Four defaults, one per (mode, new-or-resumed) combination, plus the stanzas
appended to them. Two rules govern the whole module and neither is cosmetic:

  * A stanza is appended to the DEFAULTS ONLY. An operator who supplies
    REVIEW_PROMPT gets exactly that prompt, unedited. WORKTREE_STANZA is the
    single exception, for the reason written above it.
  * A SUFFIX is appended to whichever prompt is in effect, default or override.

The bare names (REVIEW_PROMPT, FOLLOWUP_PROMPT) mean code mode, so tuning the
code prompt cannot silently change what a plan PR gets asked.
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping

# Extracted from entrypoint.sh by tools/extract-stanzas.py. See the long
# comments there for why each exists; the short version:
#   GH     - what the privilege-minimized token can actually do.
#   TEST   - "review the tests" as a runnable procedure rather than a quality.
#   PLAN   - what to review in a proposal, and what NOT to flag in one.
#   LINEAR - read the ticket the PR claims to implement. Leading space included.
from _stanzas import GH_STANZA, LINEAR_STANZA as _LINEAR_STANZA, PLAN_STANZA, TEST_STANZA

_DEFAULT_REVIEW_CODE = (
    "Perform a thorough review of pull request #{{PR}} in this repository. Inspect it "
    "with `gh pr diff {{PR}}` and `gh pr view {{PR}} --json number,title,body,author,"
    "url,state,isDraft,headRefName,headRefOid,baseRefName,labels,files,commits,"
    "comments,reviews`, and be sure you're looking at the most recent commit on its "
    "branch. " + GH_STANZA + " Pay particular attention to test quality/robustness, "
    "security, correctness, and architectural coherence/consistency, and whether the "
    "approach the PR takes is prudent and robust in light of the issue it addresses. "
    + TEST_STANZA + " Post findings as comments on the PR, one comment per finding."
)

_DEFAULT_FOLLOWUP_CODE = (
    "I've fetched the latest refs. Re-check pull request #{{PR}} for new commits or "
    "changes since your last review of it. Apply the same review standard, and only "
    "post findings you haven't already raised on this PR. Be sure you're looking at "
    "the most recent commit on its branch. " + GH_STANZA + " " + TEST_STANZA
)

_DEFAULT_REVIEW_PLAN = (
    "Review the plan or design proposed in pull request #{{PR}} in this repository. "
    "Read it with `gh pr diff {{PR}}` and `gh pr view {{PR}} --json number,title,body,"
    "author,url,state,isDraft,headRefName,headRefOid,baseRefName,labels,files,commits,"
    "comments,reviews`, and be sure you're looking at the most recent commit on its "
    "branch. " + GH_STANZA + " " + PLAN_STANZA + " Post findings as comments on the "
    "PR, one comment per finding."
)

_DEFAULT_FOLLOWUP_PLAN = (
    "I've fetched the latest refs. Re-read the plan in pull request #{{PR}} for "
    "revisions since your last review of it. Apply the same review standard, and only "
    "post findings you haven't already raised on this PR. A point you raised that the "
    "revision addresses is settled; say nothing further about it. Be sure you're "
    "looking at the most recent commit on its branch. " + GH_STANZA + " " + PLAN_STANZA
)


# Emitted only for a mode whose personas actually run together. It goes onto an
# operator OVERRIDE as well as onto the defaults, which is the one place the
# verbatim-prompt guarantee gives way: a persona that checks out a branch under
# concurrency corrupts what its siblings are reading, and the operator whose
# prompt is being edited is not the one who pays for that. The other half of the
# defense is review_loop.lock_git_dir, which drops the write bit on the clone's
# .git directory and on the .git/refs subtree. That stops the commands taking a
# lock file in either -- add, commit, checkout, reset, stash, config, and the
# ref writers behind branch, tag, update-ref and notes -- and its docstring
# lists what it does not stop; this stanza keeps a persona off both.
WORKTREE_STANZA = (
    "One more constraint. The git working copy in your current directory is shared "
    "with other reviewers reading this same pull request at this same moment, so "
    "anything you write there lands in the middle of their review. Read the change "
    "through `gh pr diff` and `gh pr view`. Run no git command that writes, whether or "
    "not it appears here: checkout, fetch, pull, branch, stash, commit, reset, add, "
    "merge, rm and clean are all out, and so is any other subcommand that touches the "
    "working tree, the index or the object store. Read-only "
    "git is fine, so `git log`, `git show`, `git diff` and `git cat-file` remain "
    "available. Do not edit, create or delete files in the working copy, and do not "
    "copy or re-clone it somewhere writable to work around this. A writing command "
    "will fail on a permission error; when one does, reach for gh instead of "
    "retrying it."
)


# The round ladder. A round is one completed pass of one (pr, mode, persona)
# pair, whatever session it ran in, so it survives rotation and failure. The
# rungs are constants rather than an env var: an operator who wants a different
# ladder overrides the prompts and writes their own. The line goes in the
# SYSTEM prompt beside the persona, for the same two reasons the persona does:
# it must be re-passed on --resume, and it must reach an operator override
# without editing it. Plan mode has no ladder; its personas review a document,
# and the failure modes the ladder exists for are code-shaped.
_ROUND_DELTA = (
    "Read closely only the commits since your last review: find your own most "
    "recent comment on this pull request, the newest one signed with your label, "
    "and read the commits `gh pr view` lists with a date after it. If you have no "
    "earlier comment, read the whole change."
)

_ROUND_FIRST = (
    "This is round 1 of your review of this pull request. Review the whole change. "
    "Post nothing tagged below `should-fix`."
)

_ROUND_SECOND = (
    "This is round 2 of your review of this pull request. " + _ROUND_DELTA
    + " Post nothing tagged below `should-fix`."
)

_ROUND_LATER = (
    "This is round {n} of your review of this pull request, which has already "
    "survived {k} rounds of it. " + _ROUND_DELTA + " Post only `blocking` findings: "
    "a `should-fix` in the new commits is not posted this round. Presume that what "
    "you have already reviewed is sound. Finding nothing new is the expected "
    "outcome, and saying so is the right report."
)


def round_stanza(mode: str, round: int) -> str:
    """The round line for the system prompt, or "" for a mode with no ladder."""
    if round < 1:
        raise ValueError(f"round must be >= 1, got {round}")
    if mode != "code":
        return ""
    if round == 1:
        return _ROUND_FIRST
    if round == 2:
        return _ROUND_SECOND
    return _ROUND_LATER.format(n=round, k=round - 1)


@dataclass(frozen=True)
class Prompts:
    review: Dict[str, str]
    followup: Dict[str, str]


def linear_stanza(env: Mapping[str, str]) -> str:
    """The Linear instruction, or empty when Linear is not configured.

    Leading space included: it is appended to a prompt.
    """
    if not env.get("LINEAR_API_KEY"):
        return ""
    return _LINEAR_STANZA


def build(
    env: Mapping[str, str], shared_worktree_modes: FrozenSet[str] = frozenset()
) -> Prompts:
    ls = linear_stanza(env)

    # Stanza on the default only. An override is verbatim.
    review = {
        "code": env.get("REVIEW_PROMPT") or (_DEFAULT_REVIEW_CODE + ls),
        "plan": env.get("PLAN_REVIEW_PROMPT") or (_DEFAULT_REVIEW_PLAN + ls),
    }
    followup = {
        "code": env.get("FOLLOWUP_PROMPT") or (_DEFAULT_FOLLOWUP_CODE + ls),
        "plan": env.get("PLAN_FOLLOWUP_PROMPT") or (_DEFAULT_FOLLOWUP_PLAN + ls),
    }

    # Suffix on whichever is in effect. A single space joins, since the defaults
    # end in '.'.
    for key, mode in (("REVIEW_PROMPT_SUFFIX", "code"), ("PLAN_REVIEW_PROMPT_SUFFIX", "plan")):
        if env.get(key):
            review[mode] = f"{review[mode]} {env[key]}"
    for key, mode in (("FOLLOWUP_PROMPT_SUFFIX", "code"), ("PLAN_FOLLOWUP_PROMPT_SUFFIX", "plan")):
        if env.get(key):
            followup[mode] = f"{followup[mode]} {env[key]}"

    # Last, so it is the final thing in the prompt, and after the suffix so an
    # operator's own last word cannot displace it.
    for mode in ("code", "plan"):
        if mode in shared_worktree_modes:
            review[mode] = f"{review[mode]} {WORKTREE_STANZA}"
            followup[mode] = f"{followup[mode]} {WORKTREE_STANZA}"

    return Prompts(review=review, followup=followup)


def render(template: str, pr: int) -> str:
    """Substitute the {{PR}} token. Overrides included, per entrypoint.sh:231-233."""
    return template.replace("{{PR}}", str(pr))
