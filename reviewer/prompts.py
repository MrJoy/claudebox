"""Prompt assembly.

Four defaults, one per (mode, new-or-resumed) combination, plus the stanzas
appended to them. Two rules govern the whole module and neither is cosmetic:

  * A stanza is appended to the DEFAULTS ONLY. An operator who supplies
    REVIEW_PROMPT gets exactly that prompt, unedited.
  * A SUFFIX is appended to whichever prompt is in effect, default or override.

The bare names (REVIEW_PROMPT, FOLLOWUP_PROMPT) mean code mode, so tuning the
code prompt cannot silently change what a plan PR gets asked.
"""

from dataclasses import dataclass
from typing import Dict, Mapping

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


def build(env: Mapping[str, str]) -> Prompts:
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

    return Prompts(review=review, followup=followup)


def render(template: str, pr: int) -> str:
    """Substitute the {{PR}} token. Overrides included, per entrypoint.sh:483."""
    return template.replace("{{PR}}", str(pr))
