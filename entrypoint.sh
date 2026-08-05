#!/usr/bin/env bash
#
# PR-reviewer loop. Configures auth, prepares a writable working copy of the
# (read-only) seed repo, points Claude Code at the configured model provider
# (Ollama Cloud, Anthropic, or any Anthropic-compatible endpoint), then
# repeatedly runs a non-interactive review pass until the container is stopped.
set -euo pipefail

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- Quoted-value repair ---------------------------------------------------
# `docker run --env-file` does NOT do shell-style quote processing: it takes
# everything after the '=' literally. So a perfectly natural-looking line
#
#   ANTHROPIC_BASE_URL="https://gateway.example/v1/acct/gw/anthropic"
#
# yields a value that literally starts and ends with a double quote, and the
# failure lands far away and late — Claude Code appends /v1/messages to it and
# every request dies on an unparseable URL, long after startup. Values that beg
# to be quoted (a URL, a header with a space and a colon, a search query) are the
# ones most likely to hit this. Strip one matched surrounding pair and say so
# rather than failing: the operator's intent is never "include these quotes".
strip_surrounding_quotes() {
  local name val
  for name in "$@"; do
    val="${!name:-}"
    case "$val" in
      '"'*'"'|"'"*"'") ;;
      *) continue ;;
    esac
    export "$name=${val:1:${#val}-2}"
    log "WARN: $name was wrapped in quotes; stripping them. (docker --env-file keeps quotes literally, so drop them from your env file.)"
  done
}

# Fail at startup on a base URL that plainly isn't one, rather than letting every
# request fail later with an opaque error from the HTTP client.
check_url() {
  case "${2:-}" in
    http://*|https://*) ;;
    *) die "$1 must be an http(s) URL; got '${2:-}'." ;;
  esac
  # Claude Code appends the endpoint path (/v1/messages) to this URL itself, so a
  # base URL that already ends in an endpoint path produces a doubled path and a
  # 404 on every request. Very easy to do when copying a curl example, and the
  # error the provider returns names the URI but not the reason. Note a bare
  # trailing /v1 is fine and required for the Vertex base URL, so only the full
  # endpoint paths are rejected.
  case "${2%/}" in
    */v1/messages|*/v1/chat/completions|*/v1/responses|*/v1/complete)
      die "$1 ends with an endpoint path; it must be the base URL only. Claude Code appends /v1/messages itself, so this becomes a doubled path and every request 404s. Use ${2%/v1/*} instead." ;;
  esac
  return 0
}

# Do this before anything reads these values.
strip_surrounding_quotes \
  PROVIDER GATEWAY_UPSTREAM REVIEW_MODEL \
  ANTHROPIC_BASE_URL ANTHROPIC_BEDROCK_BASE_URL ANTHROPIC_VERTEX_BASE_URL \
  ANTHROPIC_VERTEX_PROJECT_ID CLOUD_ML_REGION ANTHROPIC_CUSTOM_HEADERS \
  ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN OLLAMA_API_KEY \
  CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_API_TOKEN \
  GITHUB_TOKEN GITHUB_REPOSITORY LINEAR_API_KEY \
  PR_ASSIGNEE PR_IDS PR_SEARCH

# --- Required configuration ------------------------------------------------
# Provider-specific credentials are validated in "Backend selection" below.
: "${GITHUB_TOKEN:?set GITHUB_TOKEN (privilege-minimized: read repo/PRs, write PR comments)}"
: "${GITHUB_REPOSITORY:?set GITHUB_REPOSITORY in owner/repo form}"

# --- Hardening checks ------------------------------------------------------
# The loop runs unattended in YOLO mode (--dangerously-skip-permissions), so it
# must not be able to cause damage. Verify the container was launched with the
# security boundaries the README requires and refuse to run if a security-
# critical one is missing. Resource bounds (pids/memory) only cap runaway use,
# not damage, and detecting them reliably varies across cgroup v1/v2, so a
# missing one is just a warning. Set ALLOW_UNHARDENED=1 to downgrade the hard
# failures to warnings (e.g. a non-Docker runtime, or a deliberate test).
ALLOW_UNHARDENED="${ALLOW_UNHARDENED:-0}"
hardening_failed=0

# Record an unmet security requirement. We collect all of them and decide
# whether to abort afterwards, so one run reports every problem at once.
require() {
  if [ "$ALLOW_UNHARDENED" = "1" ]; then
    log "WARN (unhardened, ignored via ALLOW_UNHARDENED): $*"
  else
    log "HARDENING ERROR: $*"
    hardening_failed=1
  fi
}

# Warn if no pids/memory limit is in effect. Best-effort: tries cgroup v2 then
# v1, and stays quiet if it can't tell (better than a false alarm).
check_resource_limit() {
  local name=$1 v2=$2 v1=$3 unlimited=$4 v
  if [ -r "$v2" ]; then v="$(cat "$v2")"
  elif [ -r "$v1" ]; then v="$(cat "$v1")"
  else log "WARN: cannot determine $name limit; consider setting it."; return 0; fi
  case "$v" in
    "$unlimited") log "WARN: no $name limit set; a runaway pass could exhaust host resources." ;;
    # cgroup v1 reports "unlimited" memory as a huge sentinel rather than 'max'.
    ''|*[!0-9]*) ;;
    *) if [ "$v" -ge 9223372036854000000 ]; then
         log "WARN: no $name limit set; a runaway pass could exhaust host resources."
       fi ;;
  esac
  # Always succeed: a "limit is fine" result must not leak a non-zero exit
  # status, or `set -e` would abort the script on a correctly-limited container.
  return 0
}

# --- PR selection ----------------------------------------------------------
# Which PRs to review is chosen by exactly one selector env var. These helpers
# validate that choice and enumerate the candidate PR numbers each cycle.

# True (exit 0) when $1 is a truthy flag value: 1 / true / yes (any case).
pr_truthy() {
  case "$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z')" in
    1|true|yes) return 0 ;;
    *) return 1 ;;
  esac
}

# Split a comma/whitespace-separated list of PR numbers into one-per-line,
# validating each is a positive integer (die otherwise). Word-splitting on the
# unquoted expansion does the comma->space splitting.
parse_pr_ids() {
  local raw="$1" tok
  for tok in $(printf '%s' "$raw" | tr ',' ' '); do
    case "$tok" in
      ''|*[!0-9]*) die "PR_IDS contains a non-numeric value: '$tok' (expected e.g. 12,15,20)" ;;
      *) printf '%s\n' "$tok" ;;
    esac
  done
}

# Determine the active selector; die unless EXACTLY ONE is provided. Sets the
# global PR_SELECTOR to one of: all | assignee | ids | search.
resolve_pr_selection() {
  local n=0
  PR_SELECTOR=""
  if pr_truthy "${PR_ALL:-}";        then n=$((n + 1)); PR_SELECTOR="all"; fi
  if [ -n "${PR_ASSIGNEE:-}" ];      then n=$((n + 1)); PR_SELECTOR="assignee"; fi
  if [ -n "${PR_IDS:-}" ];           then n=$((n + 1)); PR_SELECTOR="ids"; fi
  if [ -n "${PR_SEARCH:-}" ];        then n=$((n + 1)); PR_SELECTOR="search"; fi
  if [ "$n" -eq 0 ]; then
    die "no PR selector set; provide exactly one of PR_ALL, PR_ASSIGNEE, PR_IDS, PR_SEARCH (launcher: --all / --assignee / --prs / --search)."
  fi
  if [ "$n" -gt 1 ]; then
    die "multiple PR selectors set; provide exactly one of PR_ALL, PR_ASSIGNEE, PR_IDS, PR_SEARCH."
  fi
  # Validate the ID list up front so a bad value fails fast, not every cycle.
  [ "$PR_SELECTOR" = "ids" ] && parse_pr_ids "$PR_IDS" >/dev/null
  return 0
}

# Echo candidate PR numbers (one per line) for the active selector.
enumerate_candidate_prs() {
  case "$PR_SELECTOR" in
    all)      gh pr list -R "$GITHUB_REPOSITORY" --state open --limit 100 --json number --jq '.[].number' ;;
    assignee) gh pr list -R "$GITHUB_REPOSITORY" --state open --assignee "$PR_ASSIGNEE" --limit 100 --json number --jq '.[].number' ;;
    search)   gh pr list -R "$GITHUB_REPOSITORY" --search "$PR_SEARCH" --limit 100 --json number --jq '.[].number' ;;
    ids)      parse_pr_ids "$PR_IDS" ;;
  esac
}

# Substitute the {{PR}} token in a prompt template with a PR number.
render_prompt() {
  printf '%s' "${1//\{\{PR\}\}/$2}"
}

# --- Optional Linear context ------------------------------------------------
# LINEAR_API_KEY (optional) lets the reviewer read the Linear ticket a PR claims
# to implement. Linear's MCP server accepts an API key passed straight through as
# `Authorization: Bearer <key>` (https://linear.app/docs/mcp) instead of the
# interactive OAuth flow, so the unattended loop stays headless. Use a READ-ONLY
# key: this loop runs with --dangerously-skip-permissions, so a write-capable key
# would let it mutate your tickets. Same trust model as GITHUB_TOKEN — the key's
# scope can't be checked from in here, so it's on the operator.

# Echo the review-prompt stanza that puts the Linear tools to work, or nothing
# when Linear isn't configured. Leading space: it's appended to a prompt.
linear_stanza() {
  [ -n "${LINEAR_API_KEY:-}" ] || return 0
  printf '%s' " If the PR title, body, or branch name references a Linear ticket, look that ticket up with the Linear MCP tools and read both its description and its comments — comments often carry later feedback, scope changes, and revised requirements that the description doesn't. Judge the change against what the ticket actually asks for, and raise any divergence from its stated requirements or acceptance criteria as a finding like any other. If no ticket is referenced, or you can't resolve the reference, review the code as usual — a missing ticket is not itself a finding."
}

# Write the MCP server config to $1 and return 0, or return 1 when there's
# nothing to configure. The key is passed via env.LINEAR_API_KEY (not --arg)
# so it never appears in the jq argv/`ps` output; jq's JSON string handling
# still does the escaping, so a key containing a quote or backslash can't
# produce a broken file. umask in a subshell makes the file 600 at creation,
# so the key is never briefly world-readable.
write_mcp_config() {
  [ -n "${LINEAR_API_KEY:-}" ] || return 1
  ( umask 077
    LINEAR_API_KEY="$LINEAR_API_KEY" jq -n '{
      mcpServers: {
        linear: {
          type: "http",
          url: "https://mcp.linear.app/mcp",
          headers: { Authorization: ("Bearer " + env.LINEAR_API_KEY) }
        }
      }
    }' >"$1" )
}

# Unprivileged user. Claude Code also refuses --dangerously-skip-permissions as
# root, but check explicitly for a clear message (e.g. if run with --user root).
[ "$(id -u)" != "0" ] || require "running as root; run as an unprivileged user (don't override the image's 'reviewer' user with --user root)."

# no-new-privileges and dropped capabilities both read from /proc/self/status.
if [ -r /proc/self/status ]; then
  nnp="$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)"
  capbnd="$(awk '/^CapBnd:/ {print $2}' /proc/self/status)"
  # Set by --security-opt no-new-privileges; blocks regaining privilege via setuid.
  [ "$nnp" = "1" ] || require "no-new-privileges is not set; run with --security-opt no-new-privileges."
  # --cap-drop ALL empties the bounding set; a default container keeps a non-zero set.
  { [ -z "$capbnd" ] || [ "$capbnd" = "0000000000000000" ]; } || require "Linux capabilities are not all dropped (CapBnd=$capbnd); run with --cap-drop ALL."
else
  log "WARN: cannot read /proc/self/status; skipping no-new-privileges and capability checks."
fi

check_resource_limit pids   /sys/fs/cgroup/pids.max   /sys/fs/cgroup/pids/pids.max                max
check_resource_limit memory /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes max

if [ "$hardening_failed" = "1" ]; then
  die "container is not hardened (see errors above). Re-run with the flags in README 'Run', or set ALLOW_UNHARDENED=1 to override."
fi

# --- Defaults --------------------------------------------------------------
REPO_PATH="${REPO_PATH:-/repo}"
# Working-clone location. Normally a private dir under $HOME. With
# --export-sessions the launcher sets HOST_REPO_PATH to the *host's* repo path
# and bind-mounts ~/.claude/projects/<encoded> from the host; cloning and
# running the review at that same path makes Claude Code encode the session
# project folder identically to the host, so transcripts file under the shared
# folder. mkdir here both creates the path and proves it's writable — if it
# isn't (an exotic host root not pre-created in the image; see Dockerfile), we
# warn and fall back to the default so the loop still runs (sessions just won't
# line up).
if [ -n "${HOST_REPO_PATH:-}" ] && mkdir -p "$HOST_REPO_PATH" 2>/dev/null; then
  WORK_REPO="$HOST_REPO_PATH"
  WORK_DIR="$(dirname "$HOST_REPO_PATH")"
elif [ -n "${HOST_REPO_PATH:-}" ]; then
  log "WARN: HOST_REPO_PATH=$HOST_REPO_PATH is not creatable here; sessions won't line up. Using the default work dir."
  WORK_DIR="${WORK_DIR:-$HOME/work}"
  WORK_REPO="$WORK_DIR/repo"
else
  WORK_DIR="${WORK_DIR:-$HOME/work}"
  WORK_REPO="$WORK_DIR/repo"
fi
REVIEW_INTERVAL_SECONDS="${REVIEW_INTERVAL_SECONDS:-300}"
# REVIEW_MODEL's default depends on the provider; it is resolved in the
# "Backend selection" block below.
# Rotate to a fresh session after this many successful passes, to cap the
# growth of a long-lived resumed session's context. 0 = never rotate.
MAX_PASSES_PER_SESSION="${MAX_PASSES_PER_SESSION:-0}"
case "$MAX_PASSES_PER_SESSION" in ''|*[!0-9]*) die "MAX_PASSES_PER_SESSION must be a non-negative integer";; esac
# Prompts are PR-scoped: the harness runs one session per PR and substitutes the
# {{PR}} token with that PR's number. REVIEW_PROMPT starts a PR's session;
# FOLLOWUP_PROMPT is used when resuming it on a later cycle. Custom overrides use
# the same {{PR}} token.
#
# The gh constraints in these prompts are not style advice: they are what the
# privilege-minimized token can actually do. A bare `gh pr view` asks for
# statusCheckRollup, and a fine-grained PAT has no permission that grants it, so
# the whole command fails on a permission error that reads like a token
# misconfiguration. Naming the exact working invocation is cheaper than letting
# each session rediscover it -- and a session that burns its first tool calls on
# 403s tends to start guessing at the diff instead of reading it.
_gh_stanza="Two constraints on the GitHub CLI here, because the token is deliberately privilege-minimized: always pass an explicit --json field list to \`gh pr view\` (a bare \`gh pr view\` also fetches statusCheckRollup, which this token cannot be granted permission for, so it fails outright), and do not use \`gh pr checks\` at all -- it needs that same permission and cannot work. CI status is therefore unavailable to you: review the code on its own merits, and never wait on or refer to check results."
DEFAULT_PROMPT="Perform a thorough review of pull request #{{PR}} in this repository. Inspect it with \`gh pr diff {{PR}}\` and \`gh pr view {{PR}} --json number,title,body,author,url,state,isDraft,headRefName,headRefOid,baseRefName,labels,files,commits,comments,reviews\`, and be sure you're looking at the most recent commit on its branch. $_gh_stanza Pay particular attention to test quality/robustness, security, correctness, and architectural coherence/consistency, and whether the approach the PR takes is prudent and robust in light of the issue it addresses. Post findings as comments on the PR, one comment per finding. Sign your comments with '-claudebox'."
# Prompt used when RESUMING a PR's session (it already holds context from prior
# passes on that PR, so this nudges a re-check rather than re-introducing the task).
# The gh stanza is repeated here rather than relied on from the session's own
# history: a resumed session has been running for hours and its early turns are
# the first thing a context summary drops, so the constraint has to arrive with
# every pass or it silently stops being in effect.
DEFAULT_FOLLOWUP="I've fetched the latest refs. Re-check pull request #{{PR}} for new commits or changes since your last review of it. Apply the same review standard, and only post findings you haven't already raised on this PR. Be sure you're looking at the most recent commit on its branch. $_gh_stanza Sign your comments with '-claudebox'."
# Linear context is added to the DEFAULTS only: an operator who supplied their own
# prompt gets exactly that prompt, unedited. No-op when LINEAR_API_KEY is unset.
_linear_stanza="$(linear_stanza)"
DEFAULT_PROMPT="${DEFAULT_PROMPT}${_linear_stanza}"
DEFAULT_FOLLOWUP="${DEFAULT_FOLLOWUP}${_linear_stanza}"
unset _linear_stanza _gh_stanza
REVIEW_PROMPT="${REVIEW_PROMPT:-$DEFAULT_PROMPT}"
FOLLOWUP_PROMPT="${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}"
# Suffixes append to whichever prompt is now in effect (default or operator
# override) — unlike the Linear stanza above, they apply either way. A single
# space joins them since the prompts above end in '.'.
if [ -n "${REVIEW_PROMPT_SUFFIX:-}" ]; then
  REVIEW_PROMPT="${REVIEW_PROMPT} ${REVIEW_PROMPT_SUFFIX}"
fi
if [ -n "${FOLLOWUP_PROMPT_SUFFIX:-}" ]; then
  FOLLOWUP_PROMPT="${FOLLOWUP_PROMPT} ${FOLLOWUP_PROMPT_SUFFIX}"
fi

# Validate PR selection now (fail fast, before auth/clone), and warn if a prompt
# template won't name the PR.
resolve_pr_selection
case "$REVIEW_PROMPT"   in *'{{PR}}'*) : ;; *) log "WARN: REVIEW_PROMPT has no {{PR}} token; reviews won't name the specific PR." ;; esac
case "$FOLLOWUP_PROMPT" in *'{{PR}}'*) : ;; *) log "WARN: FOLLOWUP_PROMPT has no {{PR}} token; reviews won't name the specific PR." ;; esac

# --- GitHub auth (gh + git) ------------------------------------------------
# gh reads GH_TOKEN from the environment; setup-git makes git reuse it for
# github.com over https, so PR branch fetches are authenticated.
export GH_TOKEN="$GITHUB_TOKEN"
gh auth setup-git
git config --global user.name  "PR Reviewer (bot)"
git config --global user.email "pr-reviewer@localhost"
git config --global --add safe.directory "$WORK_REPO"

# --- Extra request headers -------------------------------------------------
# Claude Code's ANTHROPIC_CUSTOM_HEADERS adds headers to every provider request,
# as "Name: value", and takes SEVERAL headers as a multi-line value. A multi-line
# value is inexpressible in an env file: `docker run --env-file` is strictly one
# KEY=VALUE per line, with no continuation and no escape processing. So accept two
# spellings that each fit on one line and assemble the real multi-line value here:
#
#   ANTHROPIC_CUSTOM_HEADERS=cf-aig-gateway-id: default\ncf-aig-authorization: Bearer t
#   ANTHROPIC_CUSTOM_HEADERS_1=cf-aig-gateway-id: default
#   ANTHROPIC_CUSTOM_HEADERS_2=cf-aig-authorization: Bearer t
#
# Numbered values are appended after the unnumbered one, in index order, so the
# two forms can be mixed. Some secondhand sources say Claude Code also accepts
# comma-separated headers; that is undocumented, and a header value may legitimately
# contain a comma, so we translate to the multi-line form it definitely takes
# rather than passing a comma-joined string through.
CUSTOM_HEADER_MAX=20

# Echo the assembled multi-line header block (empty when none is configured).
build_custom_headers() {
  local out i name val last=0 noncontiguous=0
  # The unnumbered var is already de-quoted with the rest of the config above.
  out="${ANTHROPIC_CUSTOM_HEADERS:-}"
  # Translate ONLY the two-character sequence \n. printf '%b' would be shorter but
  # also eats \t, \\ and \xNN, which could quietly mangle a token in a header value.
  out="${out//\\n/$'\n'}"
  for ((i = 1; i <= CUSTOM_HEADER_MAX; i++)); do
    name="ANTHROPIC_CUSTOM_HEADERS_$i"
    # >&2 because this function's stdout IS the assembled header block: a warning
    # on stdout would be captured into a header value.
    strip_surrounding_quotes "$name" >&2
    val="${!name:-}"
    [ -n "$val" ] || continue
    # A gap (…_1 and _3 set, no _2) is usually a typo. Use every value we found
    # regardless — dropping one silently would be worse — but say something.
    [ "$i" -gt $((last + 1)) ] && noncontiguous=1
    last=$i
    out="${out:+$out$'\n'}${val//\\n/$'\n'}"
  done
  [ "$noncontiguous" = 1 ] && log "WARN: ANTHROPIC_CUSTOM_HEADERS_* indices skip a number; all of them are still being sent, but check for a typo." >&2
  printf '%s' "$out"
}

_custom_headers="$(build_custom_headers)"
if [ -n "$_custom_headers" ]; then
  # Validate each header separately: one bad line among several would otherwise
  # surface as a provider 4xx with no hint which header caused it. Header values
  # are credentials, so failures name the header but never the whole value.
  while IFS= read -r _h; do
    [ -n "$_h" ] || continue
    case "$_h" in
      *:*) ;;
      *) die "ANTHROPIC_CUSTOM_HEADERS entry '${_h%%[[:space:]]*}…' is not 'Name: value' (no ':'). Separate several headers with a literal \\n, or use ANTHROPIC_CUSTOM_HEADERS_1, _2, …" ;;
    esac
  done <<<"$_custom_headers"
  export ANTHROPIC_CUSTOM_HEADERS="$_custom_headers"
fi
unset _custom_headers _h

# --- Workers AI translator (LiteLLM) ---------------------------------------
# Cloudflare's Workers AI catalog (glm-5.2, the Kimi models, ...) is reachable
# ONLY over an OpenAI-compatible schema: Cloudflare's REST API docs state plainly
# that its Anthropic-shaped /ai/v1/messages endpoint does not serve @cf/ models,
# and Claude Code speaks nothing but the Anthropic Messages API. So for
# PROVIDER=workersai we run LiteLLM's proxy in-container as a translator —
# Anthropic /v1/messages in, OpenAI /chat/completions out, streaming and tool
# calls included. Tool calling is the whole job of a reviewer, which is why an
# off-the-shelf translator that already gets it right beats one of our own.
#
# The proxy is a local implementation detail: it listens on loopback, holds the
# Cloudflare token only via the environment, and exists only for this provider.
#
# The full chain is: Claude Code -> LiteLLM -> shim -> Cloudflare. The shim is
# workersai-shim.py; see start_shim below for why the extra hop exists.
LITELLM_PORT="${LITELLM_PORT:-4000}"
LITELLM_CONFIG="$HOME/litellm.yaml"
LITELLM_PID=""
SHIM_PORT="${SHIM_PORT:-4001}"
SHIM_BIN="${SHIM_BIN:-/usr/local/bin/workersai-shim.py}"
SHIM_PID=""

# Shut the translators down with us. Without this a `docker stop` (or any die
# below) would leave them running until the container is reaped.
stop_litellm() {
  [ -n "$LITELLM_PID" ] || return 0
  kill "$LITELLM_PID" 2>/dev/null || true
  LITELLM_PID=""
}
stop_shim() {
  [ -n "$SHIM_PID" ] || return 0
  kill "$SHIM_PID" 2>/dev/null || true
  SHIM_PID=""
}
# LiteLLM first: it is the one holding a client connection open, and shutting the
# thing behind it down first would turn a clean stop into a burst of 502s.
trap 'stop_litellm; stop_shim' EXIT

# Write the proxy config. The Cloudflare token is referenced as os.environ/... so
# it is never written to disk; the file still gets mode 600, since the account id
# and model choice are nobody else's business either. Values are emitted through
# jq so a model id full of '@' and '/' (or an account id with something odd in it)
# can't break the YAML — a JSON scalar is a valid YAML scalar.
write_litellm_config() {
  local path=$1 model=$2 api_base=$3
  ( umask 077; : >"$path" )
  {
    printf 'model_list:\n'
    printf '  - model_name: %s\n' "$(jq -rn --arg v "$model" '$v|@json')"
    printf '    litellm_params:\n'
    # openai/ prefix = "talk to an OpenAI-compatible endpoint at api_base",
    # which is what Cloudflare's /ai/v1 surface is.
    printf '      model: %s\n' "$(jq -rn --arg v "openai/$model" '$v|@json')"
    printf '      api_base: %s\n' "$(jq -rn --arg v "$api_base" '$v|@json')"
    printf '      api_key: os.environ/CLOUDFLARE_API_TOKEN\n'
    printf 'general_settings:\n'
    # Without a master key the proxy would accept unauthenticated requests from
    # anything that can reach the port. Loopback-only makes that a small window,
    # but it costs nothing to close it.
    printf '  master_key: os.environ/LITELLM_MASTER_KEY\n'
    printf 'litellm_settings:\n'
    # Claude Code sends Anthropic-specific parameters that have no OpenAI
    # equivalent. Dropping them beats failing the request outright.
    printf '  drop_params: true\n'
    # REQUIRED, not a tuning knob. For the "openai" provider LiteLLM translates an
    # incoming /v1/messages request into the OpenAI *Responses* API by default
    # (`input`/`instructions`/`max_output_tokens`, and flat `{type,name,parameters}`
    # tools). Cloudflare's /ai/v1 surface serves Responses only for a couple of
    # models -- not glm-5.2 -- so every request failed its schema union with a wall
    # of "required properties at '/' are 'messages'" and, once per tool,
    # "required properties at '/tools/N/function' are 'name'". This flag routes
    # through /chat/completions instead, which emits `messages` and nested
    # `{type: function, function: {name, ...}}` tools -- the shape Cloudflare wants.
    printf '  use_chat_completions_url_for_anthropic_messages: true\n'
  } >>"$path"
}

# Start the translator and block until it answers, or die. Starting it lazily on
# the first request is not an option: the first review pass would fail while it
# was still booting, and a review pass that fails is a session thrown away.
start_litellm() {
  local model=$1 api_base=$2 waited=0
  [ -x "${LITELLM_BIN:-}" ] || die "PROVIDER=workersai needs the bundled LiteLLM translator at ${LITELLM_BIN:-<LITELLM_BIN unset>}, which is missing. Rebuild the image (the Dockerfile installs it)."
  # A per-container random key, so it can't be anything an operator has reused.
  export LITELLM_MASTER_KEY="sk-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
  write_litellm_config "$LITELLM_CONFIG" "$model" "$api_base"
  # --host 127.0.0.1 is load-bearing: the proxy defaults to 0.0.0.0, and it is an
  # unauthenticated-by-default gateway holding a Cloudflare token. Nothing outside
  # this container has any business reaching it.
  # --num_workers 1 because the loop reviews one PR at a time; the default is one
  # worker per CPU, which would waste memory and eat into --pids-limit.
  # LITELLM_DEBUG=1 logs the translated request/response bodies, which is the only
  # practical way to see what the translator actually put on the wire when a
  # provider rejects a request. It is off by default because those bodies include
  # the Authorization header, i.e. the Cloudflare token.
  local debug_args=()
  if pr_truthy "${LITELLM_DEBUG:-}"; then
    debug_args=(--detailed_debug)
    log "WARN: LITELLM_DEBUG is on; $HOME/litellm.log will contain full request bodies INCLUDING CREDENTIALS. Turn it off for unattended runs."
  fi
  "$LITELLM_BIN" --config "$LITELLM_CONFIG" \
    --host 127.0.0.1 --port "$LITELLM_PORT" --num_workers 1 \
    ${debug_args[@]+"${debug_args[@]}"} \
    >"$HOME/litellm.log" 2>&1 &
  LITELLM_PID=$!
  log "Starting the LiteLLM translator on 127.0.0.1:$LITELLM_PORT (pid $LITELLM_PID)..."
  # /health/liveliness is the proxy's own unauthenticated liveness probe.
  while [ "$waited" -lt 120 ]; do
    if ! kill -0 "$LITELLM_PID" 2>/dev/null; then
      log "--- last 40 lines of the translator log ---"
      tail -n 40 "$HOME/litellm.log" || true
      die "the LiteLLM translator exited while starting up (log above)."
    fi
    if curl -fsS -o /dev/null "http://127.0.0.1:$LITELLM_PORT/health/liveliness" 2>/dev/null; then
      log "Translator ready."
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  log "--- last 40 lines of the translator log ---"
  tail -n 40 "$HOME/litellm.log" || true
  die "the LiteLLM translator did not become ready within ${waited}s (log above)."
}

# Start the normalizer that sits between LiteLLM and Cloudflare.
#
# It exists for one thing LiteLLM cannot be configured out of: on an assistant
# message that carries only tool_calls, LiteLLM omits the `content` key entirely.
# That is legal OpenAI, and Cloudflare's glm-5.2 accepts it, but the Kimi models
# reject it outright:
#
#   Model execution failed (User Input Error):
#   Invalid value at messages[N].content: Invalid input
#
# Confirmed by sending Cloudflare two otherwise byte-identical bodies: without
# the key, 400; with `content: ""`, 200. Claude Code emits a tool-only assistant
# turn on every single tool call, so for those models it is not an edge case --
# essentially every review pass dies on the second turn.
#
# It has to be a separate hop because the defect is in LiteLLM's *output*, after
# the Anthropic->OpenAI translation, which its own proxy hooks run before and so
# cannot reach. Two provider prefixes that do normalize content were ruled out
# for doing much more than that: `deepseek/` drops the tool-role message from the
# conversation, and `mistral/` rewrites tool_choice "required" to "any".
#
# Unconditional for this provider rather than per-model: `content: ""` is valid
# OpenAI on its own terms, so there is one code path here, and it is the one that
# gets exercised. SHIM_NORMALIZE=0 takes the hop out if it ever proves otherwise.
start_shim() {
  local upstream=$1 waited=0
  [ -f "$SHIM_BIN" ] || die "PROVIDER=workersai needs the normalizer at $SHIM_BIN, which is missing. Rebuild the image (the Dockerfile installs it)."
  [ "$SHIM_PORT" != "$LITELLM_PORT" ] || die "SHIM_PORT and LITELLM_PORT are both $SHIM_PORT; they are two separate local listeners and need two separate ports."
  SHIM_UPSTREAM_URL="$upstream" SHIM_PORT="$SHIM_PORT" \
    python3 "$SHIM_BIN" >"$HOME/shim.log" 2>&1 &
  SHIM_PID=$!
  log "Starting the Workers AI normalizer on 127.0.0.1:$SHIM_PORT (pid $SHIM_PID)..."
  # Block until it answers: LiteLLM starts next and must not be handed requests
  # for a port nothing is listening on. A GET is the normalizer's own probe --
  # any HTTP answer at all proves the socket is up, hence no curl -f.
  while [ "$waited" -lt 30 ]; do
    if ! kill -0 "$SHIM_PID" 2>/dev/null; then
      log "--- last 20 lines of the normalizer log ---"
      tail -n 20 "$HOME/shim.log" || true
      die "the Workers AI normalizer exited while starting up (log above)."
    fi
    if curl -sS -o /dev/null "http://127.0.0.1:$SHIM_PORT/" 2>/dev/null; then
      log "Normalizer ready."
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  log "--- last 20 lines of the normalizer log ---"
  tail -n 20 "$HOME/shim.log" || true
  die "the Workers AI normalizer did not become ready within ${waited}s (log above)."
}

# Are the translators still up? Called each cycle so a dead one is a loud, fatal
# error rather than every review pass failing for an unexplained reason.
check_litellm() {
  if [ -n "$SHIM_PID" ] && ! kill -0 "$SHIM_PID" 2>/dev/null; then
    log "--- last 20 lines of the normalizer log ---"
    tail -n 20 "$HOME/shim.log" || true
    die "the Workers AI normalizer died (log above). Restarting the container will bring it back."
  fi
  [ -n "$LITELLM_PID" ] || return 0
  kill -0 "$LITELLM_PID" 2>/dev/null && return 0
  log "--- last 40 lines of the translator log ---"
  tail -n 40 "$HOME/litellm.log" || true
  die "the LiteLLM translator died (log above). Restarting the container will bring it back."
}

# --- Backend selection (Claude Code -> model provider) ---------------------
# Claude Code speaks the Anthropic API. PROVIDER picks where those requests go:
#   ollama     - Ollama Cloud's native Anthropic-compatible API (default).
#   anthropic  - Anthropic's own API.
#   custom     - any other Anthropic-compatible endpoint you point us at.
#   cloudflare - a Cloudflare AI Gateway fronting Anthropic, Bedrock, or Vertex
#                (GATEWAY_UPSTREAM picks which).
#   workersai  - a Cloudflare Workers AI model, through the bundled translator.
# Whichever backend is chosen, we pin EVERY model tier to the single
# $REVIEW_MODEL. A non-Anthropic backend has no Opus/Sonnet/Haiku models, so if
# a subagent or alias requests an un-overridden tier, Claude Code errors out on
# an unknown model; mapping them all to $REVIEW_MODEL keeps any request valid.
# (On Anthropic this also guarantees one model does every bit of the work.)
PROVIDER="${PROVIDER:-ollama}"
# What we report the backend as once it's wired. PROVIDER=cloudflare refines this
# to name the upstream too, since that's the part that decides model ID shape.
PROVIDER_LABEL="$PROVIDER"

echo
echo
echo "Using provider: ${PROVIDER}"
echo
echo

case "$PROVIDER" in
  ollama)
    # Ollama serves a native Anthropic-compatible API; auth MUST go through
    # ANTHROPIC_AUTH_TOKEN (a Bearer token), not ANTHROPIC_API_KEY.
    : "${OLLAMA_API_KEY:?set OLLAMA_API_KEY (from https://ollama.com/settings/keys), or choose a different PROVIDER}"
    export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://ollama.com}"
    check_url ANTHROPIC_BASE_URL "$ANTHROPIC_BASE_URL"
    export ANTHROPIC_AUTH_TOKEN="$OLLAMA_API_KEY"
    export ANTHROPIC_API_KEY=""
    REVIEW_MODEL="${REVIEW_MODEL:-glm-5.2:cloud}"
    ;;
  anthropic)
    # Anthropic's own API, using its default endpoint (base URL left unset).
    # Credential resolution, in precedence order — we take the first available so
    # you don't have to paste an API key if `claude` already has a login:
    #   1. ANTHROPIC_API_KEY        - explicit console key (x-api-key).
    #   2. CLAUDE_CODE_OAUTH_TOKEN  - long-lived subscription token; mint one on
    #                                 the host with `claude setup-token`. This is
    #                                 the portable, cross-platform headless path.
    #   3. a mounted credentials file - the SAME creds `claude` uses outside the
    #                                 container. Mount the host's ~/.claude into
    #                                 the reviewer's home; mount it READ-WRITE so
    #                                 Claude Code can refresh the token (a :ro
    #                                 mount fails on refresh), and note a macOS
    #                                 host keeps these in the Keychain, not a file.
    # An EMPTY ANTHROPIC_API_KEY outranks the OAuth token in Claude Code's
    # precedence, so for paths 2/3 we `unset` (not blank) the header vars to make
    # sure nothing shadows the credential we actually want used.
    creds_file="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
      export ANTHROPIC_AUTH_TOKEN=""   # explicit key wins; drop any stray Bearer
      log "PROVIDER=anthropic: authenticating with ANTHROPIC_API_KEY."
    elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
      unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
      log "PROVIDER=anthropic: authenticating with CLAUDE_CODE_OAUTH_TOKEN."
    elif [ -r "$creds_file" ]; then
      unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
      log "PROVIDER=anthropic: authenticating with mounted credentials at $creds_file."
    else
      die "PROVIDER=anthropic needs a credential. Provide one of: ANTHROPIC_API_KEY (https://console.anthropic.com/); CLAUDE_CODE_OAUTH_TOKEN (run 'claude setup-token' on your host); or mount your host ~/.claude (read-write) so $creds_file exists."
    fi
    REVIEW_MODEL="${REVIEW_MODEL:-claude-opus-4-8}"
    ;;
  custom)
    # Any other Anthropic-compatible endpoint. The caller supplies the URL and a
    # model the endpoint serves; there is no sensible default for either.
    : "${ANTHROPIC_BASE_URL:?set ANTHROPIC_BASE_URL to your endpoint for PROVIDER=custom}"
    : "${REVIEW_MODEL:?set REVIEW_MODEL to a model your endpoint serves for PROVIDER=custom}"
    check_url ANTHROPIC_BASE_URL "$ANTHROPIC_BASE_URL"
    export ANTHROPIC_BASE_URL
    # Accept whichever auth style the endpoint expects: ANTHROPIC_AUTH_TOKEN
    # sends "Authorization: Bearer" (most gateways/compatible services),
    # ANTHROPIC_API_KEY sends Anthropic's native "x-api-key". Require one.
    if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
      export ANTHROPIC_API_KEY=""
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
      export ANTHROPIC_AUTH_TOKEN=""
    else
      die "PROVIDER=custom needs an auth credential: set ANTHROPIC_AUTH_TOKEN (Bearer) or ANTHROPIC_API_KEY (x-api-key)."
    fi
    ;;
  cloudflare)
    # A Cloudflare AI Gateway (see the "Claude Code" page under AI Gateway ->
    # Integrations -> Coding agents). The gateway fronts one of three upstreams,
    # and Claude Code talks to each of them differently, so GATEWAY_UPSTREAM says
    # which: the Anthropic API, Amazon Bedrock, or Google Vertex AI.
    #
    # GATEWAY-ONLY, deliberately: the gateway holds the cloud credentials and
    # Claude Code skips its own AWS/GCP auth. This container has no AWS or GCP
    # credentials and mounts none, so there is no direct-to-Bedrock/Vertex path
    # to support. The CLAUDE_CODE_USE_* / CLAUDE_CODE_SKIP_*_AUTH switches are
    # therefore ours to set, not the operator's; we only validate that nothing in
    # the environment contradicts the upstream they picked.
    #
    # Optional, defaulting to anthropic: that arm is the one where GATEWAY_UPSTREAM
    # changes nothing (a gateway URL in ANTHROPIC_BASE_URL, an Anthropic key, no
    # switches to flip), so requiring it there is a question with only one sensible
    # answer. bedrock and vertex do have to be asked for by name — each reads a
    # different base-URL variable and sets switches that change the wire protocol.
    GATEWAY_UPSTREAM="${GATEWAY_UPSTREAM:-anthropic}"
    # Each upstream names models its own way (Anthropic IDs vs. Bedrock's
    # us.anthropic.*-v1:0 vs. Vertex's claude-*@date), so no default is right
    # more than a third of the time.
    : "${REVIEW_MODEL:?set REVIEW_MODEL to a model ID your GATEWAY_UPSTREAM serves for PROVIDER=cloudflare}"
    # The gateway token travels as a cf-aig-authorization header. For bedrock and
    # vertex it is the ONLY credential there is (Claude Code's own cloud auth is
    # skipped), so those arms require it.
    cf_headers_hint="ANTHROPIC_CUSTOM_HEADERS='cf-aig-authorization: Bearer <CF_AIG_TOKEN>'"

    # Reject a CLAUDE_CODE_USE_* switch that selects an upstream other than the
    # one GATEWAY_UPSTREAM names: inside Claude Code that switch, not our
    # GATEWAY_UPSTREAM, decides the API — so a stale one in an env file would
    # silently win. Fail instead of quietly picking a side.
    reject_conflicting_switch() {
      case "${2:-}" in
        ''|0) return 0 ;;
        *) die "$1=$2 selects the $3 upstream, which contradicts GATEWAY_UPSTREAM=$GATEWAY_UPSTREAM. Don't set $1 — GATEWAY_UPSTREAM picks the upstream and the entrypoint sets the switch." ;;
      esac
    }
    # Likewise refuse a request for Claude Code to do its own cloud auth: nothing
    # in here can satisfy it, and it would fail per-request instead of at startup.
    reject_cloud_auth() {
      case "${2:-1}" in
        1) return 0 ;;
        *) die "PROVIDER=cloudflare is gateway-only, but $1=$2 asks Claude Code to authenticate to $3 itself — this container holds no $3 credentials. Leave $1 unset (the entrypoint sets it to 1) and let the gateway hold the credentials." ;;
      esac
    }

    case "$GATEWAY_UPSTREAM" in
      anthropic)
        # The gateway's Anthropic endpoint speaks the plain Anthropic API, so this
        # is the ordinary base-URL-plus-credential shape — with one sharp edge.
        # Anthropic authenticates API keys via x-api-key ONLY; Authorization:
        # Bearer is accepted there just for OAuth subscription tokens. So unlike
        # PROVIDER=custom, the two auth styles are NOT interchangeable here: a
        # console key placed in ANTHROPIC_AUTH_TOKEN starts up fine, then every
        # request comes back {"type":"authentication_error","message":"x-api-key
        # header is required"} — which Claude Code reports as "Invalid API key",
        # pointing at the key's value rather than the variable it's sitting in.
        # Bearer stays permitted (an OAuth token is legitimate), but say so.
        : "${ANTHROPIC_BASE_URL:?set ANTHROPIC_BASE_URL to the anthropic endpoint of your gateway (https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY_ID>/anthropic) for GATEWAY_UPSTREAM=anthropic}"
        reject_conflicting_switch CLAUDE_CODE_USE_BEDROCK "${CLAUDE_CODE_USE_BEDROCK:-}" bedrock
        reject_conflicting_switch CLAUDE_CODE_USE_VERTEX  "${CLAUDE_CODE_USE_VERTEX:-}"  vertex
        check_url ANTHROPIC_BASE_URL "$ANTHROPIC_BASE_URL"
        export ANTHROPIC_BASE_URL
        if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
          export ANTHROPIC_AUTH_TOKEN=""
        elif [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
          export ANTHROPIC_API_KEY=""
          log "WARN: GATEWAY_UPSTREAM=anthropic is authenticating with ANTHROPIC_AUTH_TOKEN (Authorization: Bearer). Anthropic accepts Bearer only for OAuth subscription tokens; a console API key MUST go in ANTHROPIC_API_KEY instead, or every request fails with 'x-api-key header is required' (surfaced as 'Invalid API key')."
        else
          die "GATEWAY_UPSTREAM=anthropic needs a credential: set ANTHROPIC_API_KEY to an Anthropic API key (sent as x-api-key — this is the upstream credential, NOT your gateway token, which belongs in ANTHROPIC_CUSTOM_HEADERS). ANTHROPIC_AUTH_TOKEN (Bearer) works only for an OAuth subscription token. If your gateway is authenticated, also set $cf_headers_hint."
        fi
        ;;
      bedrock)
        : "${ANTHROPIC_BEDROCK_BASE_URL:?set ANTHROPIC_BEDROCK_BASE_URL to the bedrock endpoint of your gateway (https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY_ID>/aws-bedrock/bedrock-runtime/<AWS_REGION>/) for GATEWAY_UPSTREAM=bedrock}"
        : "${ANTHROPIC_CUSTOM_HEADERS:?GATEWAY_UPSTREAM=bedrock authenticates to the gateway with a header and nothing else (Claude Code skips its own AWS auth): set $cf_headers_hint}"
        reject_conflicting_switch CLAUDE_CODE_USE_VERTEX "${CLAUDE_CODE_USE_VERTEX:-}" vertex
        reject_cloud_auth CLAUDE_CODE_SKIP_BEDROCK_AUTH "${CLAUDE_CODE_SKIP_BEDROCK_AUTH:-}" AWS
        # In Bedrock mode the Anthropic-API vars are dead weight at best; drop them
        # so a leftover key can't muddy which endpoint is really in use.
        check_url ANTHROPIC_BEDROCK_BASE_URL "$ANTHROPIC_BEDROCK_BASE_URL"
        unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
        export ANTHROPIC_BEDROCK_BASE_URL
        export CLAUDE_CODE_USE_BEDROCK=1
        export CLAUDE_CODE_SKIP_BEDROCK_AUTH=1
        ;;
      vertex)
        : "${ANTHROPIC_VERTEX_BASE_URL:?set ANTHROPIC_VERTEX_BASE_URL to the vertex endpoint of your gateway (https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY_ID>/google-vertex-ai/v1) for GATEWAY_UPSTREAM=vertex}"
        : "${ANTHROPIC_VERTEX_PROJECT_ID:?set ANTHROPIC_VERTEX_PROJECT_ID to your GCP project id for GATEWAY_UPSTREAM=vertex}"
        : "${CLOUD_ML_REGION:?set CLOUD_ML_REGION to the Vertex region serving your model (e.g. us-east5) for GATEWAY_UPSTREAM=vertex}"
        : "${ANTHROPIC_CUSTOM_HEADERS:?GATEWAY_UPSTREAM=vertex authenticates to the gateway with a header and nothing else (Claude Code skips its own Vertex auth): set $cf_headers_hint}"
        reject_conflicting_switch CLAUDE_CODE_USE_BEDROCK "${CLAUDE_CODE_USE_BEDROCK:-}" bedrock
        reject_cloud_auth CLAUDE_CODE_SKIP_VERTEX_AUTH "${CLAUDE_CODE_SKIP_VERTEX_AUTH:-}" GCP
        check_url ANTHROPIC_VERTEX_BASE_URL "$ANTHROPIC_VERTEX_BASE_URL"
        unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
        export ANTHROPIC_VERTEX_BASE_URL ANTHROPIC_VERTEX_PROJECT_ID CLOUD_ML_REGION
        export CLAUDE_CODE_USE_VERTEX=1
        export CLAUDE_CODE_SKIP_VERTEX_AUTH=1
        ;;
      *)
        die "unknown GATEWAY_UPSTREAM='$GATEWAY_UPSTREAM'; use one of: anthropic, bedrock, vertex."
        ;;
    esac
    unset cf_headers_hint
    PROVIDER_LABEL="cloudflare/$GATEWAY_UPSTREAM"
    ;;
  workersai)
    # A model from Cloudflare's Workers AI catalog, reached through the bundled
    # LiteLLM translator (see "Workers AI translator" above for why one is needed).
    # Only two things to configure, because the endpoint is derivable: the account
    # the models are billed to, and a token that can invoke them.
    : "${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID (Cloudflare dashboard -> Workers and Pages -> Overview, or the account id in your dashboard URL)}"
    : "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN to a Cloudflare API token with the Workers AI Read permission (dash.cloudflare.com/profile/api-tokens). A token, not the Global API Key.}"
    export CLOUDFLARE_API_TOKEN
    # glm-5.2 is the same model the default ollama provider uses, so the reviewer
    # behaves the same way on either backend. Other options in the catalog:
    # @cf/moonshotai/kimi-k2.7-code, @cf/moonshotai/kimi-k2.6, @cf/zai-org/glm-4.7-flash.
    REVIEW_MODEL="${REVIEW_MODEL:-@cf/zai-org/glm-5.2}"
    # Cloudflare's OpenAI-compatible surface. LiteLLM appends /chat/completions.
    workersai_base="https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/ai/v1"
    check_url CLOUDFLARE_WORKERS_AI_URL "$workersai_base"
    # Normalizer in front of Cloudflare (see start_shim), then LiteLLM in front of
    # that. Started innermost-first so each one is already answering before the
    # thing that talks to it comes up. SHIM_NORMALIZE=0 collapses the chain back to
    # LiteLLM talking to Cloudflare directly.
    if pr_truthy "${SHIM_NORMALIZE:-1}"; then
      start_shim "$workersai_base"
      workersai_upstream="http://127.0.0.1:$SHIM_PORT"
    else
      log "WARN: SHIM_NORMALIZE is off; models that require an explicit assistant content field (the Kimi models) will fail."
      workersai_upstream="$workersai_base"
    fi
    start_litellm "$REVIEW_MODEL" "$workersai_upstream"
    unset workersai_base workersai_upstream
    # Point Claude Code at the translator, not at Cloudflare. Auth is the
    # translator's own per-container key; the Cloudflare token stays behind it, so
    # a prompt-injected review can't read a credential out of Claude Code's env.
    export ANTHROPIC_BASE_URL="http://127.0.0.1:$LITELLM_PORT"
    export ANTHROPIC_AUTH_TOKEN="$LITELLM_MASTER_KEY"
    export ANTHROPIC_API_KEY=""
    PROVIDER_LABEL="workersai (via the bundled LiteLLM translator)"
    ;;
  *)
    die "unknown PROVIDER='$PROVIDER'; use one of: ollama, anthropic, custom, cloudflare, workersai."
    ;;
esac

# Pin every model tier to the one review model (see the note above).
export ANTHROPIC_MODEL="$REVIEW_MODEL"
export ANTHROPIC_DEFAULT_FABLE_MODEL="$REVIEW_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$REVIEW_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$REVIEW_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$REVIEW_MODEL"
# Deprecated alias for the small/fast model; set too for older code paths.
export ANTHROPIC_SMALL_FAST_MODEL="$REVIEW_MODEL"

# --- MCP servers -----------------------------------------------------------
# --strict-mcp-config is passed ALWAYS: /repo is untrusted input, and without it
# a repo under review that ships its own .mcp.json could get MCP servers of its
# choosing loaded into a --dangerously-skip-permissions session. Strict mode
# means the reviewer loads only what we generate here, or nothing at all.
CLAUDE_MCP_ARGS=(--strict-mcp-config)
MCP_CONFIG_FILE="$HOME/mcp.json"
rm -f "$MCP_CONFIG_FILE"
if write_mcp_config "$MCP_CONFIG_FILE"; then
  CLAUDE_MCP_ARGS+=(--mcp-config "$MCP_CONFIG_FILE")
  log "Linear MCP enabled (expects a READ-ONLY Linear API key)."
fi

# --- Prepare a writable working copy ---------------------------------------
# REPO_PATH is the user's primary repo, mounted read-only. We make a cheap LOCAL
# clone of it: git copies the local object store rather than pulling over the
# network. --no-hardlinks is required because a bind mount is a different device
# than the container fs, so hardlinking (git clone --local's default) fails with
# "Invalid cross-device link". The clone is our own writable repo; the mount is
# never touched. If no usable repo is mounted, fall back to a network clone.
git config --global --add safe.directory "$REPO_PATH"
mkdir -p "$WORK_DIR"
if [ ! -d "$WORK_REPO/.git" ]; then
  if git -C "$REPO_PATH" rev-parse --git-dir >/dev/null 2>&1; then
    log "Local-cloning seed repo from $REPO_PATH (no network) -> $WORK_REPO"
    git clone --local --no-hardlinks "$REPO_PATH" "$WORK_REPO"
  else
    log "No usable git repo at $REPO_PATH; cloning $GITHUB_REPOSITORY over the network"
    gh repo clone "$GITHUB_REPOSITORY" "$WORK_REPO"
  fi
fi

# Make sure 'origin' points at GitHub (the seed copy may have a local/other
# remote) so both git fetch and gh resolve to the right repository.
git -C "$WORK_REPO" remote set-url origin "https://github.com/${GITHUB_REPOSITORY}.git" \
  || git -C "$WORK_REPO" remote add origin "https://github.com/${GITHUB_REPOSITORY}.git"

log "Reviewer ready. repo=$GITHUB_REPOSITORY provider=$PROVIDER_LABEL model=$REVIEW_MODEL interval=${REVIEW_INTERVAL_SECONDS}s"

# --- Review loop -----------------------------------------------------------
# One Claude session PER PR. Each cycle the supervisor fetches refs, enumerates
# the candidate PRs (per the active selector), and reviews each in its own
# session: a new session for a PR not seen yet, or --resume of that PR's session
# (tracked in the in-memory PR_SESSION map) so Claude won't re-raise findings on
# it. /loop can't be used here because it needs a live interactive session,
# which headless `-p` mode isn't. Headless + all permissions skipped is safe:
# unprivileged user, minimized token, read-only seed.
cd "$WORK_REPO"
# Per-PR state: session id and successful-pass count, keyed by PR number. These
# are in-memory only, so a container restart re-reviews each PR once (may
# re-comment once) — the same trade-off the old single-session design had.
declare -A PR_SESSION=()
declare -A PR_PASSES=()

# Pretty-print Claude's stream-json (one JSON event per line) into readable,
# live log lines: assistant text, tool calls, tool results, and the final
# result. fromjson? tolerates any non-JSON line instead of aborting the stream.
format_stream() {
  jq -j --unbuffered -R '
    (fromjson? // empty) as $e | $e
    | if .type == "system" and .subtype == "init" then
        "  ▸ session \(.session_id) started\n"
      elif .type == "assistant" then
        ( .message.content[]?
          | if .type == "text" then (.text | select(length > 0) | "\(.)\n")
            elif .type == "tool_use" then "  → \(.name): \(.input | tojson | .[0:200])\n"
            else empty end )
      elif .type == "user" then
        ( .message.content[]?
          | if .type == "tool_result" then
              ( (.content | if type == "array" then (map(.text? // "") | join(" ")) else tostring end))
              | gsub("[\\n\\t ]+"; " ") | "  ← \(.[0:200])\n"
            else empty end )
      elif .type == "result" then
        "  ✓ result (\(.subtype // "")): \((.result // "") | .[0:800])\n"
      else empty end
  '
}

# Run one review pass for $1=prompt, resuming $2=session id when non-empty.
# On success sets RUN_PASS_SESSION_ID to the recovered id (falling back to the
# passed id) and returns 0; returns claude's exit code on failure.
RUN_PASS_SESSION_ID=""
run_pass() {
  local prompt="$1" sid="$2" rc errfile rawfile got
  RUN_PASS_SESSION_ID="$sid"
  errfile="$(mktemp)"; rawfile="$(mktemp)"
  # set +e around the pipeline so a formatter hiccup can't abort the script and
  # so we can read Claude's own exit code via PIPESTATUS[0] (not tee's/jq's).
  set +e
  if [ -n "$sid" ]; then
    claude -p --resume "$sid" --output-format stream-json --verbose \
      --dangerously-skip-permissions --model "$REVIEW_MODEL" \
      "${CLAUDE_MCP_ARGS[@]}" -- "$prompt" \
      2>"$errfile" | stdbuf -oL tee "$rawfile" | format_stream
  else
    claude -p --output-format stream-json --verbose \
      --dangerously-skip-permissions --model "$REVIEW_MODEL" \
      "${CLAUDE_MCP_ARGS[@]}" -- "$prompt" \
      2>"$errfile" | stdbuf -oL tee "$rawfile" | format_stream
  fi
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" -ne 0 ]; then
    log "WARN: claude exited $rc:"; tail -n 5 "$errfile" >&2
    rm -f "$errfile" "$rawfile"; return "$rc"
  fi
  # session_id appears in the init and result events; take the last one seen.
  got="$(jq -r -R '(fromjson? // empty) | select(.session_id) | .session_id' "$rawfile" 2>/dev/null | tail -n 1 || true)"
  [ -n "$got" ] && RUN_PASS_SESSION_ID="$got"
  rm -f "$errfile" "$rawfile"
  return 0
}

while true; do
  # Only does anything for PROVIDER=workersai; a dead translator means every pass
  # this cycle would fail on connection refused, so fail loudly here instead.
  check_litellm
  log "Fetching latest refs..."
  git fetch --all --prune --quiet || log "WARN: git fetch failed; continuing"

  # Re-enumerate every cycle so newly-matching PRs get picked up (PR_IDS is a
  # fixed set). Read the numbers into an array.
  prs=()
  while IFS= read -r _n; do [ -n "$_n" ] && prs+=("$_n"); done < <(enumerate_candidate_prs || true)

  if [ "${#prs[@]}" -eq 0 ]; then
    log "No candidate PRs for selector '$PR_SELECTOR'."
  else
    log "Candidate PRs ($PR_SELECTOR): ${prs[*]}"
  fi

  # Review each PR in its own session, sequentially (they share the one clone).
  for pr in ${prs[@]+"${prs[@]}"}; do
    sid="${PR_SESSION[$pr]:-}"
    if [ -z "$sid" ]; then
      log "Reviewing PR #$pr (new session)..."
      prompt="$(render_prompt "$REVIEW_PROMPT" "$pr")"
    else
      log "Reviewing PR #$pr (resuming session $sid)..."
      prompt="$(render_prompt "$FOLLOWUP_PROMPT" "$pr")"
    fi

    if run_pass "$prompt" "$sid"; then
      PR_SESSION[$pr]="$RUN_PASS_SESSION_ID"
      PR_PASSES[$pr]=$(( ${PR_PASSES[$pr]:-0} + 1 ))
      log "PR #$pr review complete (session ${PR_SESSION[$pr]}, pass ${PR_PASSES[$pr]})."
      # Rotate this PR's session once its cap is hit, to bound context growth.
      if [ "$MAX_PASSES_PER_SESSION" -gt 0 ] && [ "${PR_PASSES[$pr]}" -ge "$MAX_PASSES_PER_SESSION" ]; then
        log "PR #$pr reached MAX_PASSES_PER_SESSION=$MAX_PASSES_PER_SESSION; rotating its session next cycle."
        unset 'PR_SESSION[$pr]'
        PR_PASSES[$pr]=0
      fi
    else
      log "WARN: PR #$pr review failed; starting a fresh session for it next cycle."
      unset 'PR_SESSION[$pr]'
      PR_PASSES[$pr]=0
    fi
  done

  log "Sleeping ${REVIEW_INTERVAL_SECONDS}s..."
  sleep "$REVIEW_INTERVAL_SECONDS"
done
