"""The repo and org config files: `.github/claudebox.json`.

Read by the hosted control plane (phase 2a on) from each repo's default
branch, and from the org's `.github` repository for org defaults. Local mode
reads neither; local_mode_notice is all it does with the file.

JSON rather than the design's YAML, by founder ruling (2026-10-09). The keys,
nesting and meaning are the design's.
"""

import os
import re
from dataclasses import dataclass, fields
from typing import Dict, List, Optional, Tuple

import personas as personas_mod
from common import ConfigError
from config_load import Problems, is_int, is_obj, is_str, join, read_json

REPO_FILE = ".github/claudebox.json"
REPO_FILE_CAP = 64 * 1024

TRIGGERS = ("auto", "requested")
# `repo` joins in phase 3a, with per-repo stores (plan decision 3).
KINDEX_STORES = ("none", "org")
DEFAULT_ROUNDS_CAP = 4

# The allowed keys, as a tree. A leaf is None. `$schema` is allowed so an
# editor can be pointed at schema/claudebox.schema.json, and is ignored.
KEYS = {
    "$schema": None,
    "version": None,
    "trigger": None,
    "team": None,
    "profile": None,
    "model": None,
    "plan": {"label": None, "personas": None},
    "code": {"personas": None},
    "max_concurrent_personas": None,
    "rounds": {"cap": None},
    "kindex": {"store": None},
}

_NAME_OK = re.compile(r"[A-Za-z0-9._-]+")


def _enum(problems, path, value, allowed):
    if value in allowed:
        return True
    problems.add(path, f"must be one of {', '.join(allowed)}; got {value!r}")
    return False


def _name(problems, path, value, what):
    # fullmatch, not match: Python's `$` also matches before a final newline,
    # so "ollama\n" would pass and then miss every lookup.
    if is_str(value) and _NAME_OK.fullmatch(value):
        return True
    problems.add(path, f"must be a {what} (letters, digits, '.', '_' and '-')")
    return False


def _count(problems, path, value):
    if is_int(value) and value >= 0:
        return True
    problems.add(path, "must be a non-negative integer")
    return False


def _personas(problems, path, value, mode, persona_dir):
    if not is_str(value):
        problems.add(path, "must be a persona selector string, e.g. \"red_team,sage\" or \"all\"")
        return False
    try:
        personas_mod.resolve(mode, persona_dir, {personas_mod.SELECTOR_VAR[mode]: value})
    except ConfigError as exc:
        problems.add(path, str(exc))
        return False
    return True


def _leaf(problems, path, value, persona_dir) -> bool:
    if path == "trigger":
        return _enum(problems, path, value, TRIGGERS)
    if path == "team":
        return _name(problems, path, value, "team slug")
    if path == "profile":
        return _name(problems, path, value, "profile name")
    if path == "model":
        if is_str(value) and value.strip() and not any(c.isspace() for c in value):
            return True
        problems.add(path, "must be a model name with no spaces")
        return False
    if path == "plan.label":
        if is_str(value) and value.strip() and value == value.strip():
            return True
        problems.add(path, "must be a non-empty label with no surrounding spaces")
        return False
    if path == "plan.personas":
        return _personas(problems, path, value, "plan", persona_dir)
    if path == "code.personas":
        return _personas(problems, path, value, "code", persona_dir)
    if path in ("max_concurrent_personas", "rounds.cap"):
        return _count(problems, path, value)
    if path == "kindex.store":
        return _enum(problems, path, value, KINDEX_STORES)
    raise AssertionError(f"no rule for {path}")  # KEYS and this function disagree


def _walk(raw: dict, tree: dict, path: str, values, problems, persona_dir) -> None:
    for key, value in raw.items():
        here = join(path, key)
        if key not in tree:
            problems.unknown(path, key, tree)
            continue
        sub = tree[key]
        if sub is not None:
            if not is_obj(value):
                problems.add(here, "must be an object")
                continue
            _walk(value, sub, here, values, problems, persona_dir)
            continue
        if here == "$schema":
            if not is_str(value):
                problems.add(here, "must be a string")
            continue
        if here == "version":
            if not (is_int(value) and value == 1):
                problems.add(here, f"unknown version {value!r}; this claudebox reads version 1")
            continue
        if _leaf(problems, here, value, persona_dir):
            values[here] = value


def check(raw: dict, persona_dir: str) -> Tuple[Dict[str, object], Problems]:
    values: Dict[str, object] = {}
    problems = Problems()
    _walk(raw, KEYS, "", values, problems, persona_dir)
    return values, problems


@dataclass(frozen=True)
class RepoConfig:
    trigger: str = "auto"
    team: Optional[str] = None
    profile: Optional[str] = None
    model: Optional[str] = None
    plan_label: str = "plan"
    plan_personas: str = personas_mod.DEFAULTS["plan"]
    code_personas: str = personas_mod.DEFAULTS["code"]
    max_concurrent_personas: int = 0
    rounds_cap: int = DEFAULT_ROUNDS_CAP
    kindex_store: str = "none"


def merge(org: Dict[str, object], repo: Dict[str, object]) -> RepoConfig:
    """Built-in defaults, then the org's leaves, then the repo's: key by key.

    A checked path with no RepoConfig field raises rather than being dropped:
    KEYS, _leaf and RepoConfig are three spellings of one key set, and a key
    added to the first two alone would otherwise validate, print OK, and be
    ignored.
    """
    known = {f.name for f in fields(RepoConfig)}
    values = {}
    for layer in (org, repo):
        for path, value in layer.items():
            name = path.replace(".", "_")
            if name not in known:
                raise AssertionError(f"{path} is a checked key with no RepoConfig field")
            values[name] = value
    return RepoConfig(**values)


@dataclass(frozen=True)
class Resolution:
    config: Optional[RepoConfig]
    source: str
    repo_problems: List[str]
    org_problems: List[str]


def _load(data: Optional[bytes], label: str, persona_dir: str):
    """(values, problems) for a file; (None, []) when it does not exist."""
    if data is None:
        return None, []
    try:
        raw = read_json(data, REPO_FILE_CAP, label)
    except ConfigError as exc:
        return None, [str(exc)]
    values, problems = check(raw, persona_dir)
    if problems:
        return None, [f"{label}: {line}" for line in problems.lines()]
    return values, []


def resolve(org_data: Optional[bytes], repo_data: Optional[bytes], persona_dir: str) -> Resolution:
    """The config a repo is reviewed under.

    An invalid repo file is dropped whole and the org defaults apply; there
    is no "last valid config", which would need a stored copy of repo content
    with its own lifetime. An invalid org file under a valid repo file is
    dropped and the repo file applies over the built-in defaults. Both
    invalid: the repo is not reviewed. Problems are returned either way.
    """
    org, org_problems = _load(org_data, "org file", persona_dir)
    repo, repo_problems = _load(repo_data, "repo file", persona_dir)

    if repo is not None:
        source = "repo+org" if org is not None else "repo"
        return Resolution(merge(org or {}, repo), source, repo_problems, org_problems)
    if org is not None:
        return Resolution(merge(org, {}), "org", repo_problems, org_problems)
    if org_problems:
        return Resolution(None, "none", repo_problems, org_problems)
    return Resolution(merge({}, {}), "defaults", repo_problems, org_problems)


def local_mode_notice(work_repo: str) -> Optional[str]:
    """The one line local mode logs when a repo carries the hosted config."""
    if not os.path.isfile(os.path.join(work_repo, REPO_FILE)):
        return None
    return (f"{REPO_FILE} configures the hosted GitHub App; this local container "
            "does not read it. Configure local mode through its env file "
            "(.env.claudebox) instead.")
