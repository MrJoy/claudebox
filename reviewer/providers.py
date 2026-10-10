"""Provider validation: does the environment name a backend Claude Code can use?

Extracted from the `case "$PROVIDER"` block in entrypoint.sh so that one
module decides it for both of the places that need to: local mode, through
`review_loop.py --check --provider` ahead of the entrypoint's auth, clone and
translator startup, and the hosted gateway's profile loader. Validation only.
Wiring the environment Claude Code is handed (the exports, the blanking and
unsetting of the credential vars, the model-tier pinning, the Workers AI
translator) stays in entrypoint.sh, which runs it after this has passed.

Every check reads only what the environment holds before the entrypoint wires
anything, which is what lets it run at the --check seam. The messages are the
ones the shell produced, since an operator reading a refusal should not be
able to tell which side of the move refused them.
"""

import os
from typing import Mapping, Optional

from common import ConfigError

PROVIDERS = ("ollama", "anthropic", "custom", "cloudflare", "workersai")
GATEWAY_UPSTREAMS = ("anthropic", "bedrock", "vertex")
CUSTOM_HEADER_MAX = 20

# Claude Code appends the endpoint path itself, so a base URL that already ends
# in one doubles it and every request 404s. A bare trailing /v1 is fine, and
# required for the Vertex base URL, so only full endpoint paths are refused.
_ENDPOINT_PATHS = ("/v1/messages", "/v1/chat/completions", "/v1/responses", "/v1/complete")

_CF_HEADERS_HINT = "ANTHROPIC_CUSTOM_HEADERS='cf-aig-authorization: Bearer <CF_AIG_TOKEN>'"


def _strip_quotes(value: str) -> str:
    """One matched surrounding pair, the way strip_surrounding_quotes does it."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _require(env: Mapping[str, str], name: str, message: str) -> str:
    """The shell's `: "${NAME:?message}"`: unset and empty both refuse."""
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"{name}: {message}")
    return value


def check_url(name: str, value: str) -> None:
    """Refuse a base URL that plainly isn't one, rather than letting every
    request fail later with an opaque error from the HTTP client."""
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ConfigError(f"{name} must be an http(s) URL; got '{value}'.")
    trimmed = value[:-1] if value.endswith("/") else value
    if trimmed.endswith(_ENDPOINT_PATHS):
        base = value[:value.rfind("/v1/")]
        raise ConfigError(
            f"{name} ends with an endpoint path; it must be the base URL only. "
            "Claude Code appends /v1/messages itself, so this becomes a doubled "
            f"path and every request 404s. Use {base} instead."
        )


def has_custom_headers(env: Mapping[str, str]) -> bool:
    """Whether any extra request header is configured, in either spelling.

    The entrypoint joins ANTHROPIC_CUSTOM_HEADERS and ANTHROPIC_CUSTOM_HEADERS_1
    through _CUSTOM_HEADER_MAX into one value after this runs, and the bedrock
    and vertex arms require that joined value, so the check has to look at
    every input the join would. A numbered value that is only a pair of quotes
    joins as nothing, so it counts as nothing here too.
    """
    if env.get("ANTHROPIC_CUSTOM_HEADERS", ""):
        return True
    return any(
        _strip_quotes(env.get(f"ANTHROPIC_CUSTOM_HEADERS_{i}", ""))
        for i in range(1, CUSTOM_HEADER_MAX + 1)
    )


def _require_headers(env: Mapping[str, str], message: str) -> None:
    if not has_custom_headers(env):
        raise ConfigError(f"ANTHROPIC_CUSTOM_HEADERS: {message}")


def _reject_conflicting_switch(name: str, value: str, upstream: str, chosen: str) -> None:
    """Inside Claude Code the CLAUDE_CODE_USE_* switch, not GATEWAY_UPSTREAM,
    decides the API, so a stale one in an env file would silently win."""
    if value in ("", "0"):
        return
    raise ConfigError(
        f"{name}={value} selects the {upstream} upstream, which contradicts "
        f"GATEWAY_UPSTREAM={chosen}. Don't set {name} — GATEWAY_UPSTREAM picks "
        "the upstream and the entrypoint sets the switch."
    )


def _reject_cloud_auth(name: str, value: Optional[str], cloud: str) -> None:
    """Nothing in the container can satisfy a request for Claude Code to do its
    own cloud auth, and it would fail per request instead of at startup."""
    if value is None or value == "1":
        return
    raise ConfigError(
        f"PROVIDER=cloudflare is gateway-only, but {name}={value} asks Claude Code "
        f"to authenticate to {cloud} itself — this container holds no {cloud} "
        f"credentials. Leave {name} unset (the entrypoint sets it to 1) and let "
        "the gateway hold the credentials."
    )


def _anthropic(env: Mapping[str, str]) -> None:
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    config_dir = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME", ""), ".claude")
    creds_file = f"{config_dir}/.credentials.json"
    if os.access(creds_file, os.R_OK):
        return
    raise ConfigError(
        "PROVIDER=anthropic needs a credential. Provide one of: ANTHROPIC_API_KEY "
        "(https://console.anthropic.com/); CLAUDE_CODE_OAUTH_TOKEN (run 'claude "
        "setup-token' on your host); or mount your host ~/.claude (read-write) so "
        f"{creds_file} exists."
    )


def _custom(env: Mapping[str, str]) -> None:
    base = _require(env, "ANTHROPIC_BASE_URL",
                    "set ANTHROPIC_BASE_URL to your endpoint for PROVIDER=custom")
    _require(env, "REVIEW_MODEL",
             "set REVIEW_MODEL to a model your endpoint serves for PROVIDER=custom")
    check_url("ANTHROPIC_BASE_URL", base)
    if not (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")):
        raise ConfigError(
            "PROVIDER=custom needs an auth credential: set ANTHROPIC_AUTH_TOKEN "
            "(Bearer) or ANTHROPIC_API_KEY (x-api-key)."
        )


def _cloudflare(env: Mapping[str, str]) -> None:
    upstream = env.get("GATEWAY_UPSTREAM") or "anthropic"
    _require(env, "REVIEW_MODEL",
             "set REVIEW_MODEL to a model ID your GATEWAY_UPSTREAM serves for "
             "PROVIDER=cloudflare")
    use_bedrock = env.get("CLAUDE_CODE_USE_BEDROCK", "")
    use_vertex = env.get("CLAUDE_CODE_USE_VERTEX", "")

    if upstream == "anthropic":
        base = _require(
            env, "ANTHROPIC_BASE_URL",
            "set ANTHROPIC_BASE_URL to the anthropic endpoint of your gateway "
            "(https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY_ID>/anthropic) "
            "for GATEWAY_UPSTREAM=anthropic")
        _reject_conflicting_switch("CLAUDE_CODE_USE_BEDROCK", use_bedrock, "bedrock", upstream)
        _reject_conflicting_switch("CLAUDE_CODE_USE_VERTEX", use_vertex, "vertex", upstream)
        check_url("ANTHROPIC_BASE_URL", base)
        if not (env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")):
            raise ConfigError(
                "GATEWAY_UPSTREAM=anthropic needs a credential: set ANTHROPIC_API_KEY "
                "to an Anthropic API key (sent as x-api-key — this is the upstream "
                "credential, NOT your gateway token, which belongs in "
                "ANTHROPIC_CUSTOM_HEADERS). ANTHROPIC_AUTH_TOKEN (Bearer) works only "
                "for an OAuth subscription token. If your gateway is authenticated, "
                f"also set {_CF_HEADERS_HINT}."
            )
    elif upstream == "bedrock":
        base = _require(
            env, "ANTHROPIC_BEDROCK_BASE_URL",
            "set ANTHROPIC_BEDROCK_BASE_URL to the bedrock endpoint of your gateway "
            "(https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY_ID>/aws-bedrock/"
            "bedrock-runtime/<AWS_REGION>/) for GATEWAY_UPSTREAM=bedrock")
        _require_headers(
            env,
            "GATEWAY_UPSTREAM=bedrock authenticates to the gateway with a header and "
            "nothing else (Claude Code skips its own AWS auth): set "
            f"{_CF_HEADERS_HINT}")
        _reject_conflicting_switch("CLAUDE_CODE_USE_VERTEX", use_vertex, "vertex", upstream)
        _reject_cloud_auth("CLAUDE_CODE_SKIP_BEDROCK_AUTH",
                           env.get("CLAUDE_CODE_SKIP_BEDROCK_AUTH") or None, "AWS")
        check_url("ANTHROPIC_BEDROCK_BASE_URL", base)
    elif upstream == "vertex":
        base = _require(
            env, "ANTHROPIC_VERTEX_BASE_URL",
            "set ANTHROPIC_VERTEX_BASE_URL to the vertex endpoint of your gateway "
            "(https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY_ID>/"
            "google-vertex-ai/v1) for GATEWAY_UPSTREAM=vertex")
        _require(env, "ANTHROPIC_VERTEX_PROJECT_ID",
                 "set ANTHROPIC_VERTEX_PROJECT_ID to your GCP project id for "
                 "GATEWAY_UPSTREAM=vertex")
        _require(env, "CLOUD_ML_REGION",
                 "set CLOUD_ML_REGION to the Vertex region serving your model "
                 "(e.g. us-east5) for GATEWAY_UPSTREAM=vertex")
        _require_headers(
            env,
            "GATEWAY_UPSTREAM=vertex authenticates to the gateway with a header and "
            "nothing else (Claude Code skips its own Vertex auth): set "
            f"{_CF_HEADERS_HINT}")
        _reject_conflicting_switch("CLAUDE_CODE_USE_BEDROCK", use_bedrock, "bedrock", upstream)
        _reject_cloud_auth("CLAUDE_CODE_SKIP_VERTEX_AUTH",
                           env.get("CLAUDE_CODE_SKIP_VERTEX_AUTH") or None, "GCP")
        check_url("ANTHROPIC_VERTEX_BASE_URL", base)
    else:
        raise ConfigError(
            f"unknown GATEWAY_UPSTREAM='{upstream}'; use one of: "
            + ", ".join(GATEWAY_UPSTREAMS) + "."
        )


def _workersai(env: Mapping[str, str]) -> None:
    account = _require(
        env, "CLOUDFLARE_ACCOUNT_ID",
        "set CLOUDFLARE_ACCOUNT_ID (Cloudflare dashboard -> Workers and Pages -> "
        "Overview, or the account id in your dashboard URL)")
    _require(
        env, "CLOUDFLARE_API_TOKEN",
        "set CLOUDFLARE_API_TOKEN to a Cloudflare API token with the Workers AI Read "
        "permission (dash.cloudflare.com/profile/api-tokens). A token, not the "
        "Global API Key.")
    check_url("CLOUDFLARE_WORKERS_AI_URL",
              f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1")


def validate(env: Mapping[str, str]) -> str:
    """Refuse a provider configuration Claude Code cannot use. Returns the provider.

    Raises ConfigError with the message the operator should read.
    """
    provider = env.get("PROVIDER") or "ollama"
    if provider == "ollama":
        _require(env, "OLLAMA_API_KEY",
                 "set OLLAMA_API_KEY (from https://ollama.com/settings/keys), or "
                 "choose a different PROVIDER")
        check_url("ANTHROPIC_BASE_URL", env.get("ANTHROPIC_BASE_URL") or "https://ollama.com")
    elif provider == "anthropic":
        _anthropic(env)
    elif provider == "custom":
        _custom(env)
    elif provider == "cloudflare":
        _cloudflare(env)
    elif provider == "workersai":
        _workersai(env)
    else:
        raise ConfigError(
            f"unknown PROVIDER='{provider}'; use one of: " + ", ".join(PROVIDERS) + "."
        )
    return provider
