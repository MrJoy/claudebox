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

# kindex, for read-only access to the host's knowledge graph (see CLAUDE.md,
# "Read-only kindex access"). Its own venv, root-owned like /opt/litellm and
# for the same reason: the reviewer runs this code and must not be able to edit
# it. `claudebox.sh build` passes the host's `kin --version` here, so the image
# opens the schema the host writes; this default is for builds with no host kin.
#
# tools.txt is every MCP tool this version registers. reviewer/kindex_tools.py
# denies all of them except a hand-classified read set, so a tool a bump adds
# is denied until someone classifies it. The dump runs with a throwaway HOME:
# importing the server must not touch a real store, and there is none here.
ARG KINDEX_VERSION=0.44.0
RUN python3 -m venv /opt/kindex \
 && /opt/kindex/bin/pip install --no-cache-dir "kindex[mcp]==${KINDEX_VERSION}" \
 && HOME=/tmp/kindex-build /opt/kindex/bin/python -c \
      'import asyncio; from kindex.mcp_server import mcp; print("\n".join(sorted(t.name for t in asyncio.run(mcp.list_tools()))))' \
      >/opt/kindex/tools.txt \
 && test -s /opt/kindex/tools.txt \
 && rm -rf /tmp/kindex-build \
 && find /opt/kindex -name '__pycache__' -type d -prune -exec rm -rf {} +
ENV KINDEX_PYTHON=/opt/kindex/bin/python \
    KINDEX_MCP_BIN=/opt/kindex/bin/kin-mcp \
    KINDEX_TOOLS_FILE=/opt/kindex/tools.txt

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
# Persona definitions for the adversarial review set: one file per persona
# (frontmatter label/success, body = system prompt) plus _shared.md, which every
# persona body gets appended to. Read at runtime from PERSONA_DIR, which an
# operator can point at a read-only mount to supply their own set. Imported from
# advocate by tools/import-advocate-personas.py; see CLAUDE.md.
COPY --chown=reviewer:reviewer personas/ /opt/claudebox/personas/
# The review supervisor: entrypoint.sh execs reviewer/review_loop.py once it has
# finished setting up the environment and the working clone. Stdlib-only, like
# the normalizer below, so it runs on the python3 installed above.
# Root-owned, like LITELLM_BIN above and for the same reason: the reviewer needs
# to read this code, never to write it. The loop reviews untrusted repos under
# --dangerously-skip-permissions, and under --restart unless-stopped a writable
# supervisor would be a persistence vector across restarts.
COPY reviewer/ /opt/claudebox/reviewer/
# Every tool the allowlist names must still exist in the pinned kindex. A read
# tool renamed by a bump would otherwise be denied by omission, silently.
RUN python3 -c 'import sys; sys.path.insert(0, "/opt/claudebox/reviewer"); import kindex_tools; have = set(open("/opt/kindex/tools.txt").read().split()); gone = sorted(kindex_tools.READ_TOOLS - have); sys.exit("kindex_tools.READ_TOOLS names tools this kindex lacks: " + ", ".join(gone) if gone else 0)'
# The Workers AI normalizer, which entrypoint.sh runs between LiteLLM and
# Cloudflare for PROVIDER=workersai. Stdlib-only, so it runs on the python3
# installed above with no venv of its own. See the file header for why it exists.
COPY --chown=reviewer:reviewer workersai-shim.py /usr/local/bin/workersai-shim.py
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/workersai-shim.py

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
