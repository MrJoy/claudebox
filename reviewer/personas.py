"""Persona resolution.

PERSONA_DIR is a parent holding one tree per review mode. A persona is a file in
PERSONA_DIR/<mode>: frontmatter (label, success) plus a body that becomes the
pass's system prompt. Files starting with an underscore are not personas;
_shared.md is the output contract appended to every persona body in that tree.

Everything here raises ConfigError rather than returning a sentinel. A typo that
silently narrowed the review to one persona, or to none, would look exactly like
a working run in the log.
"""

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

from common import ConfigError

REVIEW_MODES: Tuple[str, ...] = ("code", "plan")

# The code default is the subset: advocate's `user` and `good_friend` were
# written against designs and whole projects, so on a narrow diff they reach for
# material that isn't in it. Plan mode is where they finally have something to
# bite on, which is why the plan default is everything.
DEFAULTS: Dict[str, str] = {
    "code": "red_team,adversarial,sme,sage",
    "plan": "adversarial,good_friend,red_team,sage,sme,user",
}

# The selector env var per mode. The bare name means code mode.
SELECTOR_VAR: Dict[str, str] = {"code": "PERSONAS", "plan": "PLAN_PERSONAS"}

# Claimed now, used in phase 2: the pass that reconciles what the personas said
# is the only one allowed to read their findings, which is why it is not itself
# a persona and cannot be selected as one.
RESERVED = frozenset({"aggregate"})

_LABEL_OK = re.compile(r"^[A-Za-z0-9 ._-]+$")


@dataclass(frozen=True)
class Persona:
    id: str
    label: str
    prompt: str


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Return (frontmatter, body). A file with no leading '---' is all body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: Dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :])
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    # Unterminated frontmatter: no body at all.
    return meta, ""


def _available(directory: str) -> List[str]:
    out = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md") or name.startswith("_"):
            continue
        out.append(name[: -len(".md")])
    return out


def _selected(mode: str, env: Mapping[str, str], available: List[str]) -> List[str]:
    var = SELECTOR_VAR[mode]
    raw = env[var] if var in env else DEFAULTS[mode]
    if raw.strip().lower() == "all":
        raw = ",".join(available)

    chosen: List[str] = []
    for token in raw.replace(",", " ").split():
        if token in RESERVED:
            raise ConfigError(f"persona '{token}' is reserved and cannot be selected.")
        if token not in available:
            raise ConfigError(
                f"unknown persona '{token}' for {mode} review; available: "
                + " ".join(available)
            )
        if token in chosen:
            raise ConfigError(f"persona '{token}' is listed twice for {mode} review.")
        chosen.append(token)

    if not chosen:
        raise ConfigError(
            f"{var} is set but names no persona; unset it for the default set "
            f"({DEFAULTS[mode]}), or name one of: " + " ".join(available)
        )
    return chosen


def resolve(mode: str, persona_dir: str, env: Mapping[str, str]) -> List[Persona]:
    if mode not in REVIEW_MODES:
        raise ConfigError(
            f"unknown review mode '{mode}'; expected one of: " + ", ".join(REVIEW_MODES)
        )
    if not os.path.isdir(persona_dir):
        raise ConfigError(
            f"no persona definitions: PERSONA_DIR={persona_dir} is not a directory."
        )

    directory = os.path.join(persona_dir, mode)
    if not os.path.isdir(directory):
        # The flat layout phase 1 shipped is reachable by exactly the
        # mount-your-own-personas workflow the docs advertise, so it has to say
        # what changed rather than dying on a missing file three checks later.
        if any(n.endswith(".md") for n in os.listdir(persona_dir)):
            raise ConfigError(
                "PERSONA_DIR now holds one tree per review mode: "
                f"{persona_dir} needs code/ and plan/ subdirectories, but its "
                "persona files sit directly in it."
            )
        raise ConfigError(
            f"no persona definitions for {mode} review: {directory} is not a directory."
        )

    shared_path = os.path.join(directory, "_shared.md")
    if not os.path.isfile(shared_path):
        raise ConfigError(
            f"no output contract: {shared_path} is missing; every persona body "
            "is appended to it."
        )
    with open(shared_path, encoding="utf-8") as fh:
        shared = fh.read()

    available = _available(directory)
    if not available:
        raise ConfigError(f"no persona definitions found in {directory}.")

    out: List[Persona] = []
    for pid in _selected(mode, env, available):
        with open(os.path.join(directory, f"{pid}.md"), encoding="utf-8") as fh:
            meta, body = _split_frontmatter(fh.read())

        label = meta.get("label", "")
        if not label:
            raise ConfigError(f"persona '{mode}/{pid}' has no label: in its frontmatter.")
        if not _LABEL_OK.match(label):
            raise ConfigError(
                f"persona '{mode}/{pid}' has a label with unexpected characters: "
                f"'{label}' (letters, digits, spaces, dot, underscore and hyphen only)."
            )
        # Judged on the body ALONE, before the contract is appended. See the
        # test of the same name for why.
        if not body.strip():
            raise ConfigError(f"persona '{mode}/{pid}' has an empty prompt body.")

        prompt = (body + "\n" + shared).replace("{{PERSONA}}", label)
        out.append(Persona(id=pid, label=label, prompt=prompt))

    return out
