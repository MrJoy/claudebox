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
