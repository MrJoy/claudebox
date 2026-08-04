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

# PROVIDER=workersai starts the bundled LiteLLM translator and blocks until it
# answers its liveness probe. Both halves are stubbed: `litellm` just has to stay
# alive so the entrypoint's kill -0 check passes (`tail -f`, not `sleep`, since
# sleep is the stub that ends the loop), and `curl` reports it ready at once so
# the readiness wait never reaches that failing sleep.
printf '#!/bin/sh\nprintf "%%s" "$*" >"$HOME/litellm-argv"\nexec tail -f /dev/null\n' >"$BIN/litellm"
printf '#!/bin/sh\nexit 0\n' >"$BIN/curl"

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
    # Escape newlines back to a literal \n: ANTHROPIC_CUSTOM_HEADERS is genuinely
    # multi-line, and one dump line per variable keeps the assertions greppable.
    # Backslashes are escaped FIRST, so a real newline (dumped as \n) can't be
    # confused with a literal backslash-n that was never translated (dumped \\n).
    if [ -n "${!v+set}" ]; then
      val="${!v}"; val="${val//\\/\\\\}"; echo "ENV $v=${val//$'\n'/\\n}"
    else echo "ENV $v=<unset>"; fi
  done
  # The generated translator config, so its YAML quoting can be asserted.
  if [ -f "$HOME/litellm.yaml" ]; then sed 's/^/CFG /' "$HOME/litellm.yaml"; fi
  # And the argv the translator was launched with — --host 127.0.0.1 is a
  # security boundary, not a preference, so it gets asserted.
  if [ -f "$HOME/litellm-argv" ]; then echo "PROXY $(cat "$HOME/litellm-argv")"; fi
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
  # variable the case means to leave unset. Case-supplied vars come last, so a
  # case can override any default below (LITELLM_BIN, to test an image missing
  # the translator).
  env -i PATH="$BIN:$PATH" HOME="$HOME_DIR" \
    ALLOW_UNHARDENED=1 \
    GITHUB_TOKEN=x GITHUB_REPOSITORY=owner/repo PR_IDS=1 \
    REPO_PATH="$HOME_DIR/seed" REVIEW_INTERVAL_SECONDS=1 \
    LITELLM_BIN="$BIN/litellm" \
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
    # A LOG:<substring> expectation checks the run's log instead of the env dump.
    case "$expect" in
      LOG:*) grep -qF "${expect#LOG:}" "$OUT" || missing="$missing [log missing: ${expect#LOG:}]"; continue ;;
      # A CFG:<line> expectation checks a line of the generated litellm.yaml.
      CFG:*) grep -qxF "CFG ${expect#CFG:}" "$DUMP" || missing="$missing [config missing: ${expect#CFG:}]"; continue ;;
      # A NOT:VAR=value expectation asserts claude did NOT get that exact value —
      # for a credential that must stay behind the translator, where the value it
      # legitimately holds instead is random and can't be matched positively.
      NOT:*) grep -qxF "ENV ${expect#NOT:}" "$DUMP" && missing="$missing [should not have: ${expect#NOT:}]"; continue ;;
      # A PROXY:<substring> expectation checks the translator's launch argv.
      PROXY:*) grep -q -- "PROXY .*${expect#PROXY:}" "$DUMP" || missing="$missing [proxy argv missing: ${expect#PROXY:}]"; continue ;;
      # NOPROXY:<substring> — the translator must NOT have been given that flag.
      NOPROXY:*) grep -q -- "PROXY .*${expect#NOPROXY:}" "$DUMP" && missing="$missing [proxy argv should not have: ${expect#NOPROXY:}]"; continue ;;
    esac
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
# GATEWAY_UPSTREAM is optional and defaults to the upstream that needs no
# switches; bedrock/vertex must be named because they change the wire protocol.
wires "cloudflare: GATEWAY_UPSTREAM defaults to anthropic" \
  PROVIDER=cloudflare REVIEW_MODEL=m ANTHROPIC_BASE_URL=https://gw.test/anthropic \
  ANTHROPIC_API_KEY=k \
  -- ANTHROPIC_BASE_URL=https://gw.test/anthropic CLAUDE_CODE_USE_BEDROCK='<unset>' \
     CLAUDE_CODE_USE_VERTEX='<unset>' LOG:provider=cloudflare/anthropic
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
# Anthropic authenticates API keys with x-api-key only, so on this upstream the
# two auth styles are NOT interchangeable: x-api-key wins, and a lone Bearer
# earns a warning naming the error it otherwise produces at request time.
wires "cloudflare/anthropic: x-api-key wins over a stray Bearer" \
  PROVIDER=cloudflare GATEWAY_UPSTREAM=anthropic REVIEW_MODEL=m \
  ANTHROPIC_BASE_URL=https://gw.test/anthropic ANTHROPIC_API_KEY=realkey ANTHROPIC_AUTH_TOKEN=stray \
  -- ANTHROPIC_API_KEY=realkey ANTHROPIC_AUTH_TOKEN=
wires "cloudflare/anthropic: Bearer-only warns about x-api-key" \
  PROVIDER=cloudflare GATEWAY_UPSTREAM=anthropic REVIEW_MODEL=m \
  ANTHROPIC_BASE_URL=https://gw.test/anthropic ANTHROPIC_AUTH_TOKEN=tok \
  -- ANTHROPIC_AUTH_TOKEN=tok ANTHROPIC_API_KEY= "LOG:x-api-key header is required"

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

# --- several custom headers -------------------------------------------------
# An env file can't hold a multi-line value, so both one-line spellings have to
# arrive at claude as the real multi-line value Claude Code expects. The dump
# re-escapes newlines to \n, which is also how they're written on input.
wires "several headers: literal \\n in one value becomes a newline" \
  PROVIDER=ollama OLLAMA_API_KEY=k \
  'ANTHROPIC_CUSTOM_HEADERS=cf-aig-gateway-id: gw\ncf-aig-authorization: Bearer t' \
  -- 'ANTHROPIC_CUSTOM_HEADERS=cf-aig-gateway-id: gw\ncf-aig-authorization: Bearer t'
wires "several headers: numbered vars are joined in index order" \
  PROVIDER=ollama OLLAMA_API_KEY=k \
  ANTHROPIC_CUSTOM_HEADERS_2='cf-aig-authorization: Bearer t' \
  ANTHROPIC_CUSTOM_HEADERS_1='cf-aig-gateway-id: gw' \
  -- 'ANTHROPIC_CUSTOM_HEADERS=cf-aig-gateway-id: gw\ncf-aig-authorization: Bearer t'
wires "several headers: the two spellings mix, unnumbered first" \
  PROVIDER=ollama OLLAMA_API_KEY=k \
  'ANTHROPIC_CUSTOM_HEADERS=a: 1\nb: 2' ANTHROPIC_CUSTOM_HEADERS_1='c: 3' \
  -- 'ANTHROPIC_CUSTOM_HEADERS=a: 1\nb: 2\nc: 3'
wires "several headers: a skipped index warns but sends everything" \
  PROVIDER=ollama OLLAMA_API_KEY=k \
  ANTHROPIC_CUSTOM_HEADERS_1='a: 1' ANTHROPIC_CUSTOM_HEADERS_3='c: 3' \
  -- 'ANTHROPIC_CUSTOM_HEADERS=a: 1\nc: 3' 'LOG:indices skip a number'
wires "several headers: quotes are stripped from a numbered value too" \
  PROVIDER=ollama OLLAMA_API_KEY=k ANTHROPIC_CUSTOM_HEADERS_1='"a: 1"' \
  -- 'ANTHROPIC_CUSTOM_HEADERS=a: 1' 'LOG:ANTHROPIC_CUSTOM_HEADERS_1 was wrapped in quotes'
refuses "several headers: one bad entry among several is rejected" "ANTHROPIC_CUSTOM_HEADERS" \
  -- PROVIDER=ollama OLLAMA_API_KEY=k \
     'ANTHROPIC_CUSTOM_HEADERS=a: 1\nmissing-the-colon'
# On bedrock/vertex the header block IS the only credential, so a numbered-only
# spelling has to satisfy the requirement the same as the unnumbered one.
wires "several headers: numbered vars satisfy the bedrock credential check" \
  PROVIDER=cloudflare GATEWAY_UPSTREAM=bedrock REVIEW_MODEL=m \
  ANTHROPIC_BEDROCK_BASE_URL=https://gw.example/bedrock \
  ANTHROPIC_CUSTOM_HEADERS_1='cf-aig-authorization: Bearer t' \
  -- 'ANTHROPIC_CUSTOM_HEADERS=cf-aig-authorization: Bearer t' CLAUDE_CODE_USE_BEDROCK=1

# --- workersai (Cloudflare Workers AI via the bundled translator) ------------
# Only the wiring is tested; the stubbed litellm never translates anything.
wires "workersai: defaults to glm-5.2 behind the local translator" \
  PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct CLOUDFLARE_API_TOKEN=cftok \
  -- ANTHROPIC_BASE_URL=http://127.0.0.1:4000 ANTHROPIC_API_KEY= \
     ANTHROPIC_MODEL=@cf/zai-org/glm-5.2 ANTHROPIC_SMALL_FAST_MODEL=@cf/zai-org/glm-5.2 \
     LOG:'Translator ready.'
# The Cloudflare token must NOT reach Claude Code: it stays behind the translator,
# so a prompt-injected review cannot read it out of the environment.
wires "workersai: the Cloudflare token is not handed to claude" \
  PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct CLOUDFLARE_API_TOKEN=cftok \
  -- NOT:ANTHROPIC_AUTH_TOKEN=cftok NOT:ANTHROPIC_API_KEY=cftok \
     CFG:'      api_key: os.environ/CLOUDFLARE_API_TOKEN'
# Load-bearing, not a tuning knob: without it LiteLLM translates /v1/messages into
# the OpenAI *Responses* API, which Cloudflare does not serve for these models, and
# every request fails its schema union.
wires "workersai: the translator is forced onto chat/completions" \
  PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct CLOUDFLARE_API_TOKEN=cftok \
  -- CFG:'  use_chat_completions_url_for_anthropic_messages: true'
wires "workersai: LITELLM_DEBUG adds detailed logging and warns about credentials" \
  PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct CLOUDFLARE_API_TOKEN=cftok \
  LITELLM_DEBUG=1 \
  -- PROXY:'--detailed_debug' LOG:'INCLUDING CREDENTIALS'
wires "workersai: no detailed logging by default" \
  PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct CLOUDFLARE_API_TOKEN=cftok \
  -- NOPROXY:'--detailed_debug'
# The proxy is unauthenticated by default and holds a Cloudflare token, and its
# own default host is 0.0.0.0. Loopback-only is a boundary, so assert it.
wires "workersai: the translator is bound to loopback only" \
  PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct CLOUDFLARE_API_TOKEN=cftok \
  -- PROXY:'--host 127.0.0.1' PROXY:'--num_workers 1'
wires "workersai: LITELLM_PORT moves the local endpoint" \
  PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct CLOUDFLARE_API_TOKEN=cftok \
  LITELLM_PORT=4123 \
  -- ANTHROPIC_BASE_URL=http://127.0.0.1:4123
# The config is generated through jq, so a model id full of '@' and '/' -- and an
# account id with a YAML-hostile character in it -- come out as quoted scalars.
wires "workersai: model and account id are emitted as quoted YAML scalars" \
  PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID='acct: "odd"' CLOUDFLARE_API_TOKEN=cftok \
  REVIEW_MODEL=@cf/moonshotai/kimi-k2.7-code \
  -- CFG:'  - model_name: "@cf/moonshotai/kimi-k2.7-code"' \
     CFG:'      model: "openai/@cf/moonshotai/kimi-k2.7-code"' \
     CFG:'      api_base: "https://api.cloudflare.com/client/v4/accounts/acct: \"odd\"/ai/v1"'
refuses "workersai: no account id" "CLOUDFLARE_ACCOUNT_ID" \
  -- PROVIDER=workersai CLOUDFLARE_API_TOKEN=cftok
refuses "workersai: no API token" "CLOUDFLARE_API_TOKEN" \
  -- PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct
refuses "workersai: translator missing from the image" "LiteLLM translator" \
  -- PROVIDER=workersai CLOUDFLARE_ACCOUNT_ID=acct CLOUDFLARE_API_TOKEN=cftok \
     LITELLM_BIN=/nonexistent/litellm

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
# Claude Code appends /v1/messages to the base URL, so one that already ends in
# an endpoint path 404s every request -- an easy copy-from-a-curl-example mistake.
refuses "custom: base URL ending in an endpoint path" "ends with an endpoint path" \
  -- PROVIDER=custom REVIEW_MODEL=m ANTHROPIC_AUTH_TOKEN=t \
     ANTHROPIC_BASE_URL=https://api.example/accounts/a/ai/v1/chat/completions
refuses "custom: base URL ending in /v1/messages" "ends with an endpoint path" \
  -- PROVIDER=custom REVIEW_MODEL=m ANTHROPIC_AUTH_TOKEN=t \
     ANTHROPIC_BASE_URL=https://api.example/ai/v1/messages/
# A bare trailing /v1 is legitimate -- the Vertex base URL requires one.
wires "vertex: a bare trailing /v1 base URL is fine" \
  PROVIDER=cloudflare GATEWAY_UPSTREAM=vertex REVIEW_MODEL=m \
  ANTHROPIC_VERTEX_BASE_URL=https://gw.test/google-vertex-ai/v1 \
  ANTHROPIC_VERTEX_PROJECT_ID=p CLOUD_ML_REGION=us-east5 \
  ANTHROPIC_CUSTOM_HEADERS="$CF_HDR" \
  -- ANTHROPIC_VERTEX_BASE_URL=https://gw.test/google-vertex-ai/v1 CLAUDE_CODE_USE_VERTEX=1
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
