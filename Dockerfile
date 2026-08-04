# syntax=docker/dockerfile:1
#
# PR-reviewer image: Claude Code CLI + GitHub CLI.
#
# At runtime Claude Code talks to the configured model provider (Ollama Cloud,
# Anthropic, or any Anthropic-compatible endpoint — no proxy needed) and runs in
# non-interactive "YOLO" mode against a read-only copy of a repo, posting review
# comments via a privilege-minimized GitHub token. See README.md for usage.

FROM node:22-bookworm-slim

# --- OS packages -----------------------------------------------------------
# git + ca-certificates: clone/fetch over https; jq/curl: scripting; gnupg:
# verifying the GitHub CLI apt repo key.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      gnupg \
      jq \
 && rm -rf /var/lib/apt/lists/*

# --- GitHub CLI (official apt repo) ----------------------------------------
RUN mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# --- Claude Code CLI -------------------------------------------------------
RUN npm install -g @anthropic-ai/claude-code

# --- Anthropic -> OpenAI translator (LiteLLM) ------------------------------
# For PROVIDER=workersai. Cloudflare's Workers AI models (@cf/...) are reachable
# only over an OpenAI-compatible schema — Cloudflare's own docs say the
# Anthropic-shaped /ai/v1/messages endpoint explicitly does NOT serve them — and
# Claude Code speaks nothing but the Anthropic Messages API. LiteLLM's proxy
# bridges the two: it exposes /v1/messages and translates to /chat/completions,
# streaming and tool calls included, which is the part that has to be right for a
# reviewer to work at all. entrypoint.sh starts it on 127.0.0.1 only, and only
# for that provider; every other provider runs with no extra process.
#
# Pinned deliberately: this pulls a large dependency tree into the image at build
# time, so the version that gets audited is the version that ships. Bump it on
# purpose, then re-verify a live review with `claudebox.sh test`.
ARG LITELLM_VERSION=1.95.0
# fastapi is constrained because litellm[proxy] does not constrain it enough.
# fastapi dropped fastapi.dependencies.utils.get_flat_dependant in 0.140.7 — a
# PATCH release — and litellm 1.95.0 imports it, so an unconstrained resolve
# installs a fastapi whose proxy cannot even be imported. The boundary was found
# by bisection: <=0.140.6 imports, >=0.140.7 does not.
#
# It fails in a maximally confusing way — litellm's CLI catches the real
# ImportError and retries a relative `from proxy_server import ...`, so the only
# thing you see is "ModuleNotFoundError: No module named 'proxy_server'". Hence
# the explicit import check below, which turns that into a build failure instead
# of a container that starts and never serves. Re-bisect when bumping
# LITELLM_VERSION; don't just widen the range.
ARG FASTAPI_CONSTRAINT="fastapi<0.140.7"
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/litellm \
 && /opt/litellm/bin/pip install --no-cache-dir \
      "litellm[proxy]==${LITELLM_VERSION}" "${FASTAPI_CONSTRAINT}" \
 && /opt/litellm/bin/python -c "import litellm.proxy.proxy_server" \
 && find /opt/litellm -name '__pycache__' -type d -prune -exec rm -rf {} +
# Root-owned and world-executable: the unprivileged reviewer runs it but cannot
# modify it. entrypoint.sh refuses PROVIDER=workersai if this is missing.
ENV LITELLM_BIN=/opt/litellm/bin/litellm \
    LITELLM_LOCAL_MODEL_COST_MAP=True

# --- Non-root user ---------------------------------------------------------
# Claude Code refuses --dangerously-skip-permissions when running as root, so
# the loop must run unprivileged. This is also a defense-in-depth boundary.
RUN useradd --create-home --shell /bin/bash reviewer

# Pre-create the top-level roots that host repo paths live under, owned by
# `reviewer`, so `--export-sessions` can clone the working copy at the *host*
# path (session-folder alignment). The unprivileged user can't create a new
# top-level dir under `/`, and can't drop from root under --cap-drop ALL, so
# only the first path component must pre-exist and be writable — `mkdir -p`
# creates the rest. /Users covers macOS hosts, /home covers Linux hosts.
RUN mkdir -p /Users /home && chown reviewer:reviewer /Users /home

# Keep the auto-updater quiet/offline; the pinned version is what we ship.
# REVIEW_MODEL is intentionally NOT baked here: its default is provider-specific
# and resolved by entrypoint.sh, so leaving it unset lets each provider's
# default apply.
ENV DISABLE_AUTOUPDATER=1 \
    REPO_PATH=/repo \
    WORK_DIR=/home/reviewer/work \
    REVIEW_INTERVAL_SECONDS=300

COPY --chown=reviewer:reviewer entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER reviewer
WORKDIR /home/reviewer

# Pre-accept onboarding so headless runs never block on a first-run prompt, and
# pre-create ~/.claude/projects owned by `reviewer`. The latter matters for
# --export-sessions: it bind-mounts a single host folder at
# ~/.claude/projects/<encoded>, and if that parent didn't already exist Docker
# would create it root-owned, blocking Claude Code's other writes under ~/.claude.
RUN mkdir -p /home/reviewer/.claude/projects \
 && printf '{"hasCompletedOnboarding": true, "bypassPermissionsModeAccepted": true}\n' \
      > /home/reviewer/.claude.json

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
