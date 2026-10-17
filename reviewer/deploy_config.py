"""The deployment file: `claudebox.deploy.json`.

The operator's file. It lives with the deployment, never in a reviewed repo,
and names secrets only by reference: `credential` is the name of an entry in
the platform's secret store, never a value. JSON rather than the design's
YAML, by founder ruling (2026-10-09).

`against_deploy` checks a repo's effective config against it.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from common import ConfigError
from config_load import Problems, is_int, is_obj, is_str, join, read_json
from providers import PROVIDERS

DEPLOY_FILE = "claudebox.deploy.json"
DEPLOY_FILE_CAP = 256 * 1024

# Provisional (plan decision 6): which provider types report usage on every
# response, so a `tokens` budget can be counted. Phase 2a narrows this from
# live responses. `custom` joins only when its profile says `usage: reported`.
TOKEN_REPORTING = frozenset({"ollama", "anthropic", "cloudflare", "workersai"})

KEYS = {
    "profiles": {"*": {
        "provider": None, "models": None, "default_model": None, "credential": None,
        "concurrency": None, "usage": None, "limit_backoff_seconds": None,
        "budget": {"unit": None, "daily": None, "repo_share": None},
        "per_pair": {"requests": None, "tokens": None},
    }},
    "max_concurrent_jobs": None,
    "timing": {"poll_seconds": None, "settle_seconds": None, "max_passes_per_session": None},
    "caps": {"github_reads_per_pair": None, "comments_per_pass": None, "comment_bytes": None,
             "clone_bytes": {"default": None, "overrides": {"*": None}}},
    "retention": {"closed_pr_days": None},
    "kindex": {"org_store": {"share_with": None}},
}

# Used with fullmatch: Python's `$` matches before a final newline.
_NAME_OK = re.compile(r"[A-Za-z0-9._-]+")
_ENV_NAME_OK = re.compile(r"[A-Z_][A-Z0-9_]*")


class DeployError(ConfigError):
    """Every schema problem in the deployment file, one per line."""

    def __init__(self, lines: List[str]):
        self.lines = lines
        super().__init__("; ".join(lines))


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    models: Tuple[str, ...]
    default_model: str
    credential: str
    concurrency: int
    budget_unit: str
    budget_daily: int
    repo_share: float
    limit_backoff_seconds: int
    per_pair_requests: int
    per_pair_tokens: Optional[int]
    usage_reported: bool


@dataclass(frozen=True)
class Deploy:
    profiles: Dict[str, Profile]
    max_concurrent_jobs: int
    poll_seconds: int
    settle_seconds: int
    max_passes_per_session: int
    github_reads_per_pair: int
    comments_per_pass: int
    comment_bytes: int
    clone_bytes_default: int
    clone_bytes_overrides: Dict[str, int]
    closed_pr_days: int
    org_store_share_with: Optional[Union[Tuple[str, ...], str]]


def _unknown_keys(problems: Problems, raw: dict, tree: dict, path: str) -> None:
    """Report keys not in the tree, recursing into known sub-objects."""
    for key, value in raw.items():
        sub_key = key if key in tree else ("*" if "*" in tree else None)
        if sub_key is None:
            problems.unknown(path, key, [k for k in tree if k != "*"])
            continue
        sub = tree[sub_key]
        if sub is not None and is_obj(value):
            _unknown_keys(problems, value, sub, join(path, key))


def _section(problems, raw, key, path) -> dict:
    value = raw.get(key, {})
    if not is_obj(value):
        problems.add(join(path, key), "must be an object")
        return {}
    return value


def _int(problems, raw, key, path, default, minimum=0) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if is_int(value) and value >= minimum:
        return value
    word = "positive" if minimum == 1 else "non-negative"
    problems.add(join(path, key), f"must be a {word} integer")
    return default


def _profile(problems: Problems, name: str, raw) -> Optional[Profile]:
    path = join("profiles", name)
    before = len(problems.lines())
    if not _NAME_OK.fullmatch(name):
        problems.add(path, "profile names use letters, digits, '.', '_' and '-'")
    if not is_obj(raw):
        problems.add(path, "must be an object")
        return None

    provider = raw.get("provider")
    if provider not in PROVIDERS:
        problems.add(join(path, "provider"), f"must be one of {', '.join(PROVIDERS)}; got {provider!r}")

    models = raw.get("models")
    # The same rule as the repo file's `model`: no whitespace, since a model
    # name with a space fails only at request time, for every repo on the profile.
    if not (isinstance(models, list) and models
            and all(is_str(m) and m and not any(c.isspace() for c in m) for m in models)
            and len(set(models)) == len(models)):
        problems.add(join(path, "models"),
                     "must be a non-empty list of distinct model names with no spaces")
        models = []
    default_model = raw.get("default_model")
    if default_model not in models:
        problems.add(join(path, "default_model"), "must be one of this profile's models")

    credential = raw.get("credential")
    if not (is_str(credential) and _ENV_NAME_OK.fullmatch(credential)):
        problems.add(join(path, "credential"),
                     "must name a secret in the platform secret store (UPPER_SNAKE_CASE), "
                     "never hold its value")

    usage = raw.get("usage")
    if usage is not None and not (provider == "custom" and usage == "reported"):
        problems.add(join(path, "usage"), "only 'reported', and only on a custom profile")
    usage_reported = usage == "reported" and provider == "custom"

    budget = _section(problems, raw, "budget", path)
    bpath = join(path, "budget")
    unit = budget.get("unit")
    if unit not in ("tokens", "passes"):
        problems.add(join(bpath, "unit"), "is required: tokens or passes")
    elif (unit == "tokens" and provider in PROVIDERS and provider not in TOKEN_REPORTING
          and not usage_reported):
        # An unknown provider is already a problem; judging its usage too
        # would report one mistake twice.
        problems.add(join(bpath, "unit"),
                     f"tokens needs a provider that reports usage, and {provider} does not "
                     "(a custom profile may vouch for its upstream with usage: reported)")
    if "daily" not in budget:
        problems.add(join(bpath, "daily"), "is required")
    daily = _int(problems, budget, "daily", bpath, 1, minimum=1)
    # The spec's default: "so one busy repo parks only itself".
    repo_share = budget.get("repo_share", 0.25)
    if isinstance(repo_share, bool) or not isinstance(repo_share, (int, float)) \
            or not 0 < repo_share <= 1:
        problems.add(join(bpath, "repo_share"), "must be a number above 0 and at most 1")
        repo_share = 0.25

    per_pair = _section(problems, raw, "per_pair", path)
    ppath = join(path, "per_pair")
    requests = _int(problems, per_pair, "requests", ppath, 200, minimum=1)
    tokens: Optional[int] = None
    if unit == "passes":
        if "tokens" in per_pair:
            problems.add(join(ppath, "tokens"), "does not apply to a passes budget")
    else:
        tokens = _int(problems, per_pair, "tokens", ppath, 4_000_000, minimum=1)

    concurrency = _int(problems, raw, "concurrency", path, 8, minimum=1)
    backoff = _int(problems, raw, "limit_backoff_seconds", path, 1800)

    if len(problems.lines()) > before:
        return None
    return Profile(name, provider, tuple(models), default_model, credential, concurrency,
                   unit, daily, float(repo_share), backoff, requests, tokens, usage_reported)


def load_deploy(data: bytes) -> Deploy:
    raw = read_json(data, DEPLOY_FILE_CAP, "deploy file")
    problems = Problems()
    _unknown_keys(problems, raw, KEYS, "")

    profiles_raw = raw.get("profiles")
    profiles: Dict[str, Profile] = {}
    if not is_obj(profiles_raw) or not profiles_raw:
        problems.add("profiles", "must define at least one profile")
    else:
        for name, body in profiles_raw.items():
            prof = _profile(problems, name, body)
            if prof is not None:
                profiles[name] = prof

    timing = _section(problems, raw, "timing", "")
    caps = _section(problems, raw, "caps", "")
    clone = _section(problems, caps, "clone_bytes", "caps")
    overrides_raw = _section(problems, clone, "overrides", "caps.clone_bytes")
    overrides: Dict[str, int] = {}
    for repo, value in overrides_raw.items():
        if is_int(value) and value >= 1:
            overrides[repo] = value
        else:
            problems.add(join("caps.clone_bytes.overrides", repo), "must be a positive integer")
    retention = _section(problems, raw, "retention", "")
    kindex = _section(problems, raw, "kindex", "")
    share_with: Optional[Union[Tuple[str, ...], str]] = None
    if "org_store" in kindex:
        org_store = _section(problems, kindex, "org_store", "kindex")
        value = org_store.get("share_with")
        if value == "all-private":
            share_with = value
        elif isinstance(value, list) and all(is_str(v) and _NAME_OK.fullmatch(v) for v in value):
            share_with = tuple(value)
        else:
            problems.add("kindex.org_store.share_with",
                         "must be a list of repo names or 'all-private'")

    deploy = Deploy(
        profiles=profiles,
        max_concurrent_jobs=_int(problems, raw, "max_concurrent_jobs", "", 4, minimum=1),
        # The spec's value "when webhooks are off"; webhooks arrive in phase 4.
        poll_seconds=_int(problems, timing, "poll_seconds", "timing", 60, minimum=1),
        settle_seconds=_int(problems, timing, "settle_seconds", "timing", 30),
        max_passes_per_session=_int(problems, timing, "max_passes_per_session", "timing", 0),
        github_reads_per_pair=_int(problems, caps, "github_reads_per_pair", "caps", 300, minimum=1),
        comments_per_pass=_int(problems, caps, "comments_per_pass", "caps", 25, minimum=1),
        comment_bytes=_int(problems, caps, "comment_bytes", "caps", 60000, minimum=1),
        clone_bytes_default=_int(problems, clone, "default", "caps.clone_bytes",
                                 2_000_000_000, minimum=1),
        clone_bytes_overrides=overrides,
        closed_pr_days=_int(problems, retention, "closed_pr_days", "retention", 14),
        org_store_share_with=share_with,
    )
    if problems:
        raise DeployError([f"deploy file: {line}" for line in problems.lines()])
    return deploy


@dataclass(frozen=True)
class DeployCheck:
    problems: List[str]
    unchecked: List[str]
    model: Optional[str]


def against_deploy(cfg, deploy: Deploy, repo_name: Optional[str]) -> DeployCheck:
    """Whether this deployment can satisfy a repo's effective config.

    Some of what the spec asks cannot be known offline (whether a team
    exists, whether the repo is public); those come back in `unchecked`,
    for the caller to print, never as a pass.
    """
    problems: List[str] = []
    unchecked: List[str] = []
    names = ", ".join(sorted(deploy.profiles))
    model: Optional[str] = None

    if cfg.profile is None:
        problems.append(f"profile: this deployment requires one; its profiles: {names}")
    elif cfg.profile not in deploy.profiles:
        problems.append(f"profile: '{cfg.profile}' is not a profile of this deployment; "
                        f"its profiles: {names}")
    else:
        prof = deploy.profiles[cfg.profile]
        if cfg.model is None:
            model = prof.default_model
        elif cfg.model in prof.models:
            model = cfg.model
        else:
            problems.append(f"model: '{cfg.model}' is not allowed by profile '{prof.name}'; "
                            f"allowed: {', '.join(prof.models)}")

    if cfg.team is not None:
        unchecked.append(f"team '{cfg.team}' exists (checked by the control plane against GitHub)")

    if cfg.kindex_store == "org":
        share = deploy.org_store_share_with
        bare = repo_name.rsplit("/", 1)[-1] if repo_name else None
        if share is None:
            problems.append("kindex.store: org, but this deployment has no org store")
        elif share != "all-private":
            if bare is None:
                problems.append("kindex.store: org needs --repo-name to check the org "
                                "store's share_with list")
            elif bare not in share:
                problems.append(f"kindex.store: org, but the org store is not shared with '{bare}'")
        unchecked.append("the repo is private (checked by the control plane; the org store "
                         "is never shared with a public repo)")

    return DeployCheck(problems, unchecked, model)
