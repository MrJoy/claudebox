#!/usr/bin/env bash
#
# Provider-selection tests for entrypoint.sh. No Docker, no network, no real
# provider: this stubs gh/git/claude onto PATH, runs the entrypoint far enough to
# wire the backend and start one review pass, and then checks what it did — the
# error it refused to start with, or the exact environment it handed `claude`.
#
#   ./test-providers.sh          # run everything
#   ./test-providers.sh vertex   # only cases whose label matches 'vertex'
#
# Scope is deliberately narrow: the "Backend selection" block and the model-tier
# pinning after it. Everything downstream (the real clone, gh, an actual model
# call) is stubbed out, so a green run says the env wiring is right — not that
# any provider accepts it. Test a new provider live with `claudebox.sh test`
# before trusting it unattended.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$SCRIPT_DIR/entrypoint.sh"
FILTER="${1:-}"

# --- A bash that can run the entrypoint ------------------------------------
# entrypoint.sh uses `declare -A`, so it needs bash 4+. macOS ships bash 3.2 as
# /bin/bash, which is fine for this test script itself but can't run the thing
# under test; look for a newer one.
BASH_BIN=""
for candidate in "${BASH:-}" "$(command -v bash || true)" /opt/homebrew/bin/bash /usr/local/bin/bash /bin/bash; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  if [ "$("$candidate" -c 'echo ${BASH_VERSINFO[0]}')" -ge 4 ] 2>/dev/null; then
    BASH_BIN="$candidate"; break
  fi
done
[ -n "$BASH_BIN" ] || { printf 'ERROR: no bash 4+ found; entrypoint.sh needs one (macOS /bin/bash is 3.2 — `brew install bash`).\n' >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"

# gh and git only need to succeed; nothing here exercises them. (PR enumeration
# is bypassed by handing every case PR_IDS=1, which needs no gh call.)
printf '#!/bin/sh\nexit 0\n' >"$BIN/gh"
printf '#!/bin/sh\nexit 0\n' >"$BIN/git"
# One cycle per case: the review loop ends with `sleep $REVIEW_INTERVAL_SECONDS`
# as its last unprotected command, so a failing sleep makes the entrypoint's own
# `set -e` end the run right after the first pass. Cheaper and quieter than
# signalling the supervisor from outside.
printf '#!/bin/sh\nexit 1\n' >"$BIN/sleep"

# The `claude` stub is the probe: it records the environment the entrypoint built
# and the argv it was called with. The entrypoint sends claude's stderr to a temp
# file it then discards, so the dump goes to a file of our own instead.
cat >"$BIN/claude" <<'STUB'
#!/usr/bin/env bash
{
  echo "ARGV $*"
  for v in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN \
           ANTHROPIC_CUSTOM_HEADERS ANTHROPIC_BEDROCK_BASE_URL \
           ANTHROPIC_VERTEX_BASE_URL ANTHROPIC_VERTEX_PROJECT_ID \
           CLOUD_ML_REGION CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX \
           CLAUDE_CODE_SKIP_BEDROCK_AUTH CLAUDE_CODE_SKIP_VERTEX_AUTH \
           ANTHROPIC_MODEL ANTHROPIC_DEFAULT_FABLE_MODEL \
           ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
           ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_SMALL_FAST_MODEL; do
    if [ -n "${!v+set}" ]; then echo "ENV $v=${!v}"; else echo "ENV $v=<unset>"; fi
  done
} >"$HOME/dump"
exit 42
STUB
chmod +x "$BIN"/*

PASS=0; FAIL=0; SKIP=0
FAILED_LABELS=""

# Run the entrypoint once. $1 = label, rest = VAR=VALUE for its environment.
# Leaves the log in $OUT and the claude-stub dump in $DUMP (absent if the
# entrypoint died before starting a pass).
run_entrypoint() {
  local label="$1"; shift
  HOME_DIR="$WORK/home"; OUT="$WORK/out"; DUMP="$HOME_DIR/dump"
  rm -rf "$HOME_DIR"; mkdir -p "$HOME_DIR/work/repo/.git" "$HOME_DIR/seed"
  # env -i so nothing from the caller's shell leaks in and quietly satisfies a
  # variable the case means to leave unset.
  env -i PATH="$BIN:$PATH" HOME="$HOME_DIR" \
    ALLOW_UNHARDENED=1 \
    GITHUB_TOKEN=x GITHUB_REPOSITORY=owner/repo PR_IDS=1 \
    REPO_PATH="$HOME_DIR/seed" REVIEW_INTERVAL_SECONDS=1 \
    "$@" "$BASH_BIN" "$ENTRYPOINT" >"$OUT" 2>&1
}

ok()   { PASS=$((PASS + 1)); printf 'ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); FAILED_LABELS="$FAILED_LABELS
  - $1"; printf 'FAIL %s\n       %s\n' "$1" "$2"; [ -s "$OUT" ] && sed 's/^/       | /' "$OUT"; }

# Should the case named $1 run at all?
selected() {
  [ -z "$FILTER" ] && return 0
  case "$1" in *"$FILTER"*) return 0 ;; *) SKIP=$((SKIP + 1)); return 1 ;; esac
}

# refuses LABEL EXPECTED-SUBSTRING -- VAR=VALUE...
# The entrypoint must abort with a message containing EXPECTED-SUBSTRING, and
# must never reach a review pass.
refuses() {
  local label="$1" want="$2"; shift 2
  [ "${1:-}" = "--" ] && shift
  selected "$label" || return 0
  run_entrypoint "$label" "$@"
  if [ -e "$DUMP" ]; then
    bad "$label" "expected a startup failure, but a review pass ran"
  elif ! grep -qF "$want" "$OUT"; then
    bad "$label" "expected the error to mention: $want"
  else
    ok "$label"
  fi
}

# wires LABEL 'VAR=VALUE...' -- ENVVAR=EXPECTED...
# The entrypoint must start a pass and hand `claude` exactly these values. Use
# the literal <unset> as EXPECTED to assert a variable is not in claude's env.
wires() {
  local label="$1"; shift
  local -a env_in=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do env_in+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  selected "$label" || return 0
  run_entrypoint "$label" ${env_in[@]+"${env_in[@]}"}
  if [ ! -e "$DUMP" ]; then
    bad "$label" "expected a review pass to start, but the entrypoint never reached one"
    return 0
  fi
  local expect missing=""
  for expect in "$@"; do
    grep -qxF "ENV $expect" "$DUMP" || missing="$missing $expect(was: $(grep "^ENV ${expect%%=*}=" "$DUMP" | sed 's/^ENV //'))"
  done
  if [ -n "$missing" ]; then
    bad "$label" "wrong environment:$missing"
  else
    ok "$label"
  fi
}

printf 'Running provider tests with %s (bash %s)\n\n' "$BASH_BIN" "$("$BASH_BIN" -c 'echo $BASH_VERSION')"

# --- ollama (default) -------------------------------------------------------
wires "ollama: default model, Bearer auth, x-api-key blanked" \
  PROVIDER=ollama OLLAMA_API_KEY=k \
  -- ANTHROPIC_BASE_URL=https://ollama.com ANTHROPIC_AUTH_TOKEN=k ANTHROPIC_API_KEY= \
     ANTHROPIC_MODEL=glm-5.2:cloud ANTHROPIC_SMALL_FAST_MODEL=glm-5.2:cloud
wires "ollama: is the default provider" \
  OLLAMA_API_KEY=k \
  -- ANTHROPIC_BASE_URL=https://ollama.com ANTHROPIC_MODEL=glm-5.2:cloud
refuses "ollama: missing key" "OLLAMA_API_KEY" -- PROVIDER=ollama

# --- anthropic --------------------------------------------------------------
wires "anthropic: API key wins and blanks the Bearer token" \
  PROVIDER=anthropic ANTHROPIC_API_KEY=k ANTHROPIC_AUTH_TOKEN=stray \
  -- ANTHROPIC_API_KEY=k ANTHROPIC_AUTH_TOKEN= ANTHROPIC_BASE_URL='<unset>' \
     ANTHROPIC_MODEL=claude-opus-4-8
# The OAuth-token path must UNSET the key vars, not blank them: an empty
# ANTHROPIC_API_KEY outranks the token in Claude Code and would shadow it.
wires "anthropic: OAuth token path unsets the key vars" \
  PROVIDER=anthropic CLAUDE_CODE_OAUTH_TOKEN=tok \
  -- ANTHROPIC_API_KEY='<unset>' ANTHROPIC_AUTH_TOKEN='<unset>' ANTHROPIC_MODEL=claude-opus-4-8
refuses "anthropic: no credential at all" "needs a credential" -- PROVIDER=anthropic

# --- custom -----------------------------------------------------------------
wires "custom: Bearer auth" \
  PROVIDER=custom ANTHROPIC_BASE_URL=https://example.test/ REVIEW_MODEL=m ANTHROPIC_AUTH_TOKEN=k \
  -- ANTHROPIC_BASE_URL=https://example.test/ ANTHROPIC_AUTH_TOKEN=k ANTHROPIC_API_KEY= ANTHROPIC_MODEL=m
refuses "custom: no base URL" "ANTHROPIC_BASE_URL" -- PROVIDER=custom REVIEW_MODEL=m ANTHROPIC_AUTH_TOKEN=k
refuses "custom: no model" "REVIEW_MODEL" -- PROVIDER=custom ANTHROPIC_BASE_URL=https://example.test/ ANTHROPIC_AUTH_TOKEN=k
refuses "custom: no credential" "needs an auth credential" -- PROVIDER=custom ANTHROPIC_BASE_URL=https://example.test/ REVIEW_MODEL=m

# --- cloudflare: shared requirements ---------------------------------------
CF_HDR='cf-aig-authorization: Bearer cftok'
refuses "cloudflare: no GATEWAY_UPSTREAM" "GATEWAY_UPSTREAM" -- PROVIDER=cloudflare
refuses "cloudflare: unknown GATEWAY_UPSTREAM" "unknown GATEWAY_UPSTREAM" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=azure REVIEW_MODEL=m
refuses "cloudflare: no REVIEW_MODEL (no default for any upstream)" "REVIEW_MODEL" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=bedrock

# --- cloudflare/anthropic ---------------------------------------------------
wires "cloudflare/anthropic: base URL + gateway token as x-api-key" \
  PROVIDER=cloudflare GATEWAY_UPSTREAM=anthropic REVIEW_MODEL=claude-opus-4-8 \
  ANTHROPIC_BASE_URL=https://gw.test/anthropic ANTHROPIC_API_KEY=cftok ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" \
  -- ANTHROPIC_BASE_URL=https://gw.test/anthropic ANTHROPIC_API_KEY=cftok ANTHROPIC_AUTH_TOKEN= \
     "ANTHROPIC_CUSTOM_HEADERS=$CF_HDR" ANTHROPIC_MODEL=claude-opus-4-8 \
     CLAUDE_CODE_USE_BEDROCK='<unset>' CLAUDE_CODE_USE_VERTEX='<unset>'
refuses "cloudflare/anthropic: no base URL" "ANTHROPIC_BASE_URL" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=anthropic REVIEW_MODEL=m ANTHROPIC_API_KEY=k
refuses "cloudflare/anthropic: no credential" "needs a credential" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=anthropic REVIEW_MODEL=m ANTHROPIC_BASE_URL=https://gw.test/anthropic

# --- cloudflare/bedrock -----------------------------------------------------
wires "cloudflare/bedrock: switches set, stale Anthropic vars dropped" \
  PROVIDER=cloudflare GATEWAY_UPSTREAM=bedrock REVIEW_MODEL=us.anthropic.claude-opus-4-5-v1:0 \
  ANTHROPIC_BEDROCK_BASE_URL=https://gw.test/aws-bedrock/bedrock-runtime/us-east-1/ \
  ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" ANTHROPIC_API_KEY=stale ANTHROPIC_BASE_URL=https://stale.test/ \
  -- ANTHROPIC_BEDROCK_BASE_URL=https://gw.test/aws-bedrock/bedrock-runtime/us-east-1/ \
     CLAUDE_CODE_USE_BEDROCK=1 CLAUDE_CODE_SKIP_BEDROCK_AUTH=1 CLAUDE_CODE_USE_VERTEX='<unset>' \
     "ANTHROPIC_CUSTOM_HEADERS=$CF_HDR" \
     ANTHROPIC_BASE_URL='<unset>' ANTHROPIC_API_KEY='<unset>' ANTHROPIC_AUTH_TOKEN='<unset>' \
     ANTHROPIC_MODEL=us.anthropic.claude-opus-4-5-v1:0 \
     ANTHROPIC_DEFAULT_HAIKU_MODEL=us.anthropic.claude-opus-4-5-v1:0
refuses "cloudflare/bedrock: no base URL" "ANTHROPIC_BEDROCK_BASE_URL" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=bedrock REVIEW_MODEL=m ANTHROPIC_CUSTOM_HEADERS="$CF_HDR"
# The gateway header is the ONLY credential here (cloud auth is skipped), so its
# absence has to be a startup error rather than a per-request 403.
refuses "cloudflare/bedrock: no gateway header" "ANTHROPIC_CUSTOM_HEADERS" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=bedrock REVIEW_MODEL=m ANTHROPIC_BEDROCK_BASE_URL=https://gw.test/b/
refuses "cloudflare/bedrock: asked to do its own AWS auth" "gateway-only" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=bedrock REVIEW_MODEL=m ANTHROPIC_BEDROCK_BASE_URL=https://gw.test/b/ \
     ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" CLAUDE_CODE_SKIP_BEDROCK_AUTH=0

# --- cloudflare/vertex ------------------------------------------------------
wires "cloudflare/vertex: switches, project, and region all wired" \
  PROVIDER=cloudflare GATEWAY_UPSTREAM=vertex REVIEW_MODEL=claude-opus-4-5@20251101 \
  ANTHROPIC_VERTEX_BASE_URL=https://gw.test/google-vertex-ai/v1 ANTHROPIC_VERTEX_PROJECT_ID=proj \
  CLOUD_ML_REGION=us-east5 ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" \
  -- ANTHROPIC_VERTEX_BASE_URL=https://gw.test/google-vertex-ai/v1 ANTHROPIC_VERTEX_PROJECT_ID=proj \
     CLOUD_ML_REGION=us-east5 CLAUDE_CODE_USE_VERTEX=1 CLAUDE_CODE_SKIP_VERTEX_AUTH=1 \
     CLAUDE_CODE_USE_BEDROCK='<unset>' ANTHROPIC_API_KEY='<unset>' \
     ANTHROPIC_MODEL=claude-opus-4-5@20251101
refuses "cloudflare/vertex: no project id" "ANTHROPIC_VERTEX_PROJECT_ID" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=vertex REVIEW_MODEL=m \
     ANTHROPIC_VERTEX_BASE_URL=https://gw.test/v1 CLOUD_ML_REGION=us-east5 ANTHROPIC_CUSTOM_HEADERS="$CF_HDR"
refuses "cloudflare/vertex: no region" "CLOUD_ML_REGION" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=vertex REVIEW_MODEL=m \
     ANTHROPIC_VERTEX_BASE_URL=https://gw.test/v1 ANTHROPIC_VERTEX_PROJECT_ID=proj ANTHROPIC_CUSTOM_HEADERS="$CF_HDR"
refuses "cloudflare/vertex: asked to do its own GCP auth" "gateway-only" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=vertex REVIEW_MODEL=m \
     ANTHROPIC_VERTEX_BASE_URL=https://gw.test/v1 ANTHROPIC_VERTEX_PROJECT_ID=proj CLOUD_ML_REGION=us-east5 \
     ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" CLAUDE_CODE_SKIP_VERTEX_AUTH=0

# A CLAUDE_CODE_USE_* switch, not GATEWAY_UPSTREAM, is what Claude Code actually
# reads to pick an API — so a contradicting one must fail loudly, never be
# quietly overridden.
refuses "cloudflare: USE_BEDROCK contradicts upstream=anthropic" "contradicts GATEWAY_UPSTREAM" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=anthropic REVIEW_MODEL=m \
     ANTHROPIC_BASE_URL=https://gw.test/anthropic ANTHROPIC_API_KEY=k CLAUDE_CODE_USE_BEDROCK=1
refuses "cloudflare: USE_VERTEX contradicts upstream=bedrock" "contradicts GATEWAY_UPSTREAM" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=bedrock REVIEW_MODEL=m \
     ANTHROPIC_BEDROCK_BASE_URL=https://gw.test/b/ ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" CLAUDE_CODE_USE_VERTEX=1
refuses "cloudflare: USE_BEDROCK contradicts upstream=vertex" "contradicts GATEWAY_UPSTREAM" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=vertex REVIEW_MODEL=m \
     ANTHROPIC_VERTEX_BASE_URL=https://gw.test/v1 ANTHROPIC_VERTEX_PROJECT_ID=proj CLOUD_ML_REGION=us-east5 \
     ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" CLAUDE_CODE_USE_BEDROCK=1

# --- ANTHROPIC_CUSTOM_HEADERS is provider-agnostic --------------------------
wires "custom headers pass through on a non-gateway provider" \
  PROVIDER=ollama OLLAMA_API_KEY=k ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" \
  -- "ANTHROPIC_CUSTOM_HEADERS=$CF_HDR" ANTHROPIC_BASE_URL=https://ollama.com
refuses "custom headers must look like a header" "ANTHROPIC_CUSTOM_HEADERS" \
  -- PROVIDER=ollama OLLAMA_API_KEY=k ANTHROPIC_CUSTOM_HEADERS=missing-the-colon

# --- unknown provider -------------------------------------------------------
refuses "unknown PROVIDER" "unknown PROVIDER" -- PROVIDER=azure

# --- quoted values ----------------------------------------------------------
# `docker --env-file` keeps quotes literally, so a quoted line in an env file
# arrives with the quotes as part of the value. These assert we repair that
# instead of shipping a nonsense URL/header to the provider.
wires "quoted base URL is unwrapped" \
  PROVIDER=cloudflare GATEWAY_UPSTREAM=anthropic REVIEW_MODEL=m \
  ANTHROPIC_BASE_URL='"https://gw.test/v1/acct/gw/anthropic"' ANTHROPIC_API_KEY='"cftok"' \
  -- ANTHROPIC_BASE_URL=https://gw.test/v1/acct/gw/anthropic ANTHROPIC_API_KEY=cftok
wires "single-quoted values are unwrapped too" \
  PROVIDER=ollama OLLAMA_API_KEY="'k'" ANTHROPIC_CUSTOM_HEADERS="'$CF_HDR'" \
  -- ANTHROPIC_AUTH_TOKEN=k "ANTHROPIC_CUSTOM_HEADERS=$CF_HDR"
# Quotes only come off in matched pairs — a value that merely contains one is
# left alone.
wires "an interior quote is left alone" \
  PROVIDER=custom ANTHROPIC_BASE_URL=https://example.test/ REVIEW_MODEL=m ANTHROPIC_AUTH_TOKEN='ab"cd' \
  -- 'ANTHROPIC_AUTH_TOKEN=ab"cd'
refuses "quoted vertex project id still fails on a bad URL" "must be an http(s) URL" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=vertex REVIEW_MODEL=m \
     ANTHROPIC_VERTEX_BASE_URL=gw.test/v1 ANTHROPIC_VERTEX_PROJECT_ID='"proj"' \
     CLOUD_ML_REGION=us-east5 ANTHROPIC_CUSTOM_HEADERS="$CF_HDR"

# --- base URLs must be URLs -------------------------------------------------
refuses "custom: base URL with no scheme" "must be an http(s) URL" \
  -- PROVIDER=custom ANTHROPIC_BASE_URL=example.test REVIEW_MODEL=m ANTHROPIC_AUTH_TOKEN=k
refuses "cloudflare/bedrock: base URL with no scheme" "must be an http(s) URL" \
  -- PROVIDER=cloudflare GATEWAY_UPSTREAM=bedrock REVIEW_MODEL=m \
     ANTHROPIC_BEDROCK_BASE_URL=gw.test/aws-bedrock/ ANTHROPIC_CUSTOM_HEADERS="$CF_HDR"
refuses "ollama: overridden base URL must still be a URL" "must be an http(s) URL" \
  -- PROVIDER=ollama OLLAMA_API_KEY=k ANTHROPIC_BASE_URL=ollama.com

# --- Result -----------------------------------------------------------------
printf '\n%d passed, %d failed' "$PASS" "$FAIL"
[ "$SKIP" -gt 0 ] && printf ', %d skipped (filter: %s)' "$SKIP" "$FILTER"
printf '\n'
if [ "$FAIL" -gt 0 ]; then
  printf 'Failed:%s\n' "$FAILED_LABELS"
  exit 1
fi
