#!/usr/bin/env bash
#
# Launcher tests for claudebox.sh's kindex resolution. No Docker: every case
# runs with --dry-run and asserts on the docker command the launcher prints. A
# `kin` stub stands in for the host's kindex. The launcher runs under
# /bin/bash on purpose, which is 3.2 on macOS, the shell it has to survive.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SCRIPT_DIR/claudebox.sh"
FILTER="${1:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

STUBS="$WORK/stubs"; mkdir -p "$STUBS"
cat >"$STUBS/kin" <<'STUB'
#!/bin/sh
printf '%s|%s\n' "$(pwd)" "$*" >>"$STUB_KIN_LOG"
if [ -n "${STUB_KIN_FAIL:-}" ]; then
  echo "Error: Unknown kindex profile 'hoo3' (from kin); known profiles: personal" >&2
  exit 2
fi
case "$1" in
  --version) echo "kin 9.8.7 (Kindex)" ;;
  config) printf '%s\n' "$STUB_KIN_DIR" ;;
  profile) printf '{"profile": "%s", "source": "%s"}\n' "${STUB_KIN_PROFILE:-personal}" "${STUB_KIN_SOURCE:-default}" ;;
esac
STUB
chmod +x "$STUBS/kin"

REPO="$WORK/repo"; mkdir -p "$REPO/.git"
STORE="$WORK/store"; mkdir -p "$STORE"; : >"$STORE/kindex.db"
SPACED="$WORK/Application Support/kindex"; mkdir -p "$SPACED"; : >"$SPACED/kindex.db"
EMPTY="$WORK/empty-store"; mkdir -p "$EMPTY"
RELSTORE="relstore"; mkdir -p "$REPO/$RELSTORE"; : >"$REPO/$RELSTORE/kindex.db"
HOMESTORE="$WORK/homekindex"; mkdir -p "$HOMESTORE"; : >"$HOMESTORE/kindex.db"
ENVF="$WORK/env"; : >"$ENVF"
BASE_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

PASS=0; FAIL=0; FAILED=""
ok()  { PASS=$((PASS + 1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); FAILED="$FAILED
  - $1"; printf 'FAIL %s\n       %s\n' "$1" "$2"; sed 's/^/       | /' "$WORK/out"; }
selected() { [ -z "$FILTER" ] && return 0; case "$1" in *"$FILTER"*) return 0 ;; esac; return 1; }

# launch LABEL WITH_KIN(1|0) -- VAR=VALUE... -- launcher args...
# Leaves combined output in $WORK/out and the exit status in $RC.
launch() {
  local label="$1" with_kin="$2"; shift 2
  [ "${1:-}" = "--" ] && shift
  local -a envs=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  local path="$BASE_PATH"; [ "$with_kin" = 1 ] && path="$STUBS:$BASE_PATH"
  : >"$WORK/kin.log"
  env -i PATH="$path" HOME="$WORK" STUB_KIN_LOG="$WORK/kin.log" STUB_KIN_DIR="$STORE" \
    ${envs[@]+"${envs[@]}"} /bin/bash "$LAUNCHER" --dry-run "$@" >"$WORK/out" 2>&1
  RC=$?
}

# expect LABEL RC-WANT -- substrings... ; a leading ! negates a substring.
expect() {
  local label="$1" want="$2"; shift 2
  [ "${1:-}" = "--" ] && shift
  local s missing=""
  [ "$RC" = "$want" ] || missing="$missing [exit $RC, wanted $want]"
  for s in "$@"; do
    case "$s" in
      !*) grep -qF -- "${s#!}" "$WORK/out" && missing="$missing [should not have: ${s#!}]" ;;
      *)  grep -qF -- "$s" "$WORK/out" || missing="$missing [missing: $s]" ;;
    esac
  done
  if [ -n "$missing" ]; then bad "$label" "$missing"; else ok "$label"; fi
}

RUN=(run --repo "$REPO" --name cb --env-file "$ENVF" --no-restart)

L="auto: the resolved store is mounted read-only"
if selected "$L"; then
  launch "$L" 1 -- -- "${RUN[@]}"
  expect "$L" 0 -- "$STORE:/kindex-src:ro" "profile personal via default"
fi

L="auto: resolution runs from inside the repo (roots match on cwd)"
if selected "$L"; then
  launch "$L" 1 -- -- "${RUN[@]}"
  if grep -q "^$(cd "$REPO" && pwd)|config get data_dir" "$WORK/kin.log"; then ok "$L"; else bad "$L" "kin config ran from: $(cut -d'|' -f1 "$WORK/kin.log" | head -1)"; fi
fi

L="auto: no kin on PATH means no mount and no error"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}"
  expect "$L" 0 -- "!/kindex-src" "!WARN:" "!kindex"
fi

L="--kindex-profile with no kin on PATH is fatal"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --kindex-profile hoo3
  expect "$L" 1 -- "ERROR:" "kin is not on PATH"
fi

L="--kindex-dir with no kin on PATH still mounts that dir"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --kindex-dir "$SPACED"
  expect "$L" 0 -- 'Application\ Support/kindex:/kindex-src:ro' "from --kindex-dir"
fi

L="auto: an unknown profile warns and launches without kindex"
if selected "$L"; then
  launch "$L" 1 -- STUB_KIN_FAIL=1 -- "${RUN[@]}"
  expect "$L" 0 -- "WARN:" "running without kindex" "!/kindex-src"
fi

L="auto: a resolved dir with no kindex.db warns and launches without kindex"
if selected "$L"; then
  launch "$L" 1 -- STUB_KIN_DIR="$EMPTY" -- "${RUN[@]}"
  expect "$L" 0 -- "holds no kindex.db" "!/kindex-src"
fi

L="auto: a store path with a space stays one argument"
if selected "$L"; then
  launch "$L" 1 -- STUB_KIN_DIR="$SPACED" -- "${RUN[@]}"
  # show_and_run prints with printf %q, which escapes the space.
  expect "$L" 0 -- 'Application\ Support/kindex:/kindex-src:ro'
fi

L="auto: a relative data_dir canonicalizes against the repo"
if selected "$L"; then
  launch "$L" 1 -- STUB_KIN_DIR="$RELSTORE" -- "${RUN[@]}"
  expect "$L" 0 -- "$REPO/$RELSTORE:/kindex-src:ro"
fi

L="auto: a ~/-prefixed data_dir expands to HOME"
if selected "$L"; then
  launch "$L" 1 -- STUB_KIN_DIR="~/homekindex" -- "${RUN[@]}"
  expect "$L" 0 -- "$HOMESTORE:/kindex-src:ro"
fi

L="--no-kindex mounts nothing and never asks kin"
if selected "$L"; then
  launch "$L" 1 -- -- "${RUN[@]}" --no-kindex
  expect "$L" 0 -- "!/kindex-src"
  [ ! -s "$WORK/kin.log" ] || bad "$L (kin was called)" "$(cat "$WORK/kin.log")"
fi

L="--kindex-profile passes --profile to both kin calls"
if selected "$L"; then
  launch "$L" 1 -- STUB_KIN_PROFILE=hoo3 STUB_KIN_SOURCE=flag -- "${RUN[@]}" --kindex-profile hoo3
  expect "$L" 0 -- "$STORE:/kindex-src:ro" "profile hoo3 via flag"
  [ "$(grep -c -- '--profile hoo3' "$WORK/kin.log")" = 2 ] || bad "$L (calls)" "$(cat "$WORK/kin.log")"
fi

L="--kindex-profile that kin rejects is fatal"
if selected "$L"; then
  launch "$L" 1 -- STUB_KIN_FAIL=1 -- "${RUN[@]}" --kindex-profile hoo3
  expect "$L" 1 -- "ERROR:"
fi

L="--kindex-dir mounts that dir without asking kin"
if selected "$L"; then
  launch "$L" 1 -- -- "${RUN[@]}" --kindex-dir "$SPACED"
  expect "$L" 0 -- 'Application\ Support/kindex:/kindex-src:ro'
  [ ! -s "$WORK/kin.log" ] || bad "$L (kin was called)" "$(cat "$WORK/kin.log")"
fi

L="--kindex-dir without a kindex.db is fatal"
if selected "$L"; then
  launch "$L" 1 -- -- "${RUN[@]}" --kindex-dir "$EMPTY"
  expect "$L" 1 -- "ERROR:" "holds no kindex.db"
fi

L="--no-kindex with an override is fatal"
if selected "$L"; then
  launch "$L" 1 -- -- "${RUN[@]}" --no-kindex --kindex-dir "$STORE"
  expect "$L" 1 -- "ERROR:"
fi

L="--kindex-profile with --kindex-dir is fatal"
if selected "$L"; then
  launch "$L" 1 -- -- "${RUN[@]}" --kindex-profile hoo3 --kindex-dir "$STORE"
  expect "$L" 1 -- "ERROR:"
fi

L="--no-repo skips automatic kindex resolution and never asks kin"
if selected "$L"; then
  launch "$L" 1 -- -- run --no-repo --name cb --env-file "$ENVF" --no-restart
  expect "$L" 0 -- "!/kindex-src" "kindex is not resolved without a mounted repo"
  [ ! -s "$WORK/kin.log" ] || bad "$L (kin was called)" "$(cat "$WORK/kin.log")"
fi

L="--no-repo --kindex-dir still mounts that dir"
if selected "$L"; then
  launch "$L" 1 -- -- run --no-repo --name cb --env-file "$ENVF" --no-restart --kindex-dir "$STORE"
  expect "$L" 0 -- "$STORE:/kindex-src:ro"
fi

L="--no-repo --kindex-profile still resolves, from PWD"
if selected "$L"; then
  : >"$WORK/kin.log"
  ( cd "$REPO" && env -i PWD="$REPO" PATH="$STUBS:$BASE_PATH" HOME="$WORK" STUB_KIN_LOG="$WORK/kin.log" STUB_KIN_DIR="$STORE" \
      STUB_KIN_PROFILE=hoo3 STUB_KIN_SOURCE=flag \
      /bin/bash "$LAUNCHER" --dry-run run --no-repo --name cb --env-file "$ENVF" --no-restart --kindex-profile hoo3 \
      >"$WORK/out" 2>&1 )
  RC=$?
  expect "$L" 0 -- "$STORE:/kindex-src:ro" "profile hoo3 via flag"
  if grep -q "^$(cd "$REPO" && pwd)|config get data_dir --profile hoo3" "$WORK/kin.log"; then ok "$L (cwd)"; else bad "$L (cwd)" "kin config ran from: $(cut -d'|' -f1 "$WORK/kin.log" | head -1)"; fi
fi

L="test: mounts the store the same way"
if selected "$L"; then
  launch "$L" 1 -- -- test --repo "$REPO" --env-file "$ENVF"
  expect "$L" 0 -- "$STORE:/kindex-src:ro"
fi

L="build: pins the image's kindex to the host's version"
if selected "$L"; then
  launch "$L" 1 -- -- build
  expect "$L" 0 -- "KINDEX_VERSION=9.8.7"
fi

L="build: no kin means the Dockerfile default"
if selected "$L"; then
  launch "$L" 0 -- -- build
  expect "$L" 0 -- "!KINDEX_VERSION"
fi

L="build: a kin whose --version fails falls back to the Dockerfile default"
if selected "$L"; then
  launch "$L" 1 -- STUB_KIN_FAIL=1 -- build
  expect "$L" 0 -- "docker build" "!KINDEX_VERSION"
fi

L="build: a passthrough --build-arg KINDEX_VERSION lands after the launcher's own"
if selected "$L"; then
  # docker gives the LAST --build-arg for a key the win, so the passthrough
  # coming after the launcher's own on the printed command line already
  # overrides it; this pins that ordering against a regression.
  launch "$L" 1 -- -- build -- --build-arg KINDEX_VERSION=1.2.3
  line="$(grep -- '+ docker build' "$WORK/out")"
  second="${line#*KINDEX_VERSION=9.8.7}"
  if [ "$RC" = 0 ] && [ "$line" != "$second" ]; then
    case "$second" in
      *"KINDEX_VERSION=1.2.3"*) ok "$L" ;;
      *) bad "$L" "passthrough --build-arg did not land after the launcher's own: $line" ;;
    esac
  else
    bad "$L" "unexpected output: $line"
  fi
fi

L="selectors: --new alone is accepted"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --new
  expect "$L" 0 -- "PR_NEW=1" "!ERROR:"
fi

L="selectors: --new combines with --assignee"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --new --assignee alice
  expect "$L" 0 -- "PR_ASSIGNEE=alice" "PR_NEW=1"
fi

L="selectors: --author passes PR_AUTHOR"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --author alice
  expect "$L" 0 -- "PR_AUTHOR=alice" "!PR_NEW=1" "!ERROR:"
fi

L="selectors: --new combines with --author"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --author alice --new
  expect "$L" 0 -- "PR_AUTHOR=alice" "PR_NEW=1"
fi

L="selectors: --author with --assignee is refused"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --author alice --assignee bob --new
  expect "$L" 1 -- "ERROR:" "multiple PR selector flags"
fi

L="selectors: --new with any other selector is refused"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --all --new
  expect "$L" 1 -- "ERROR:" "--new cannot be combined with --all"
fi

CFG="$WORK/claudebox.json"; printf '{}\n' >"$CFG"
DEP="$WORK/claudebox.deploy.json"; printf '{}\n' >"$DEP"

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Drop --network none or the capability/no-new-privileges hardening flags.
L="config validate: runs the validator in a hardened, network-less throwaway container"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG"
  expect "$L" 0 -- "docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges" \
    "$CFG:/config/claudebox.json:ro" "--entrypoint python3 claudebox /opt/claudebox/reviewer/config_cli.py validate /config/claudebox.json" \
    '!--deploy' '!--env-file'
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Drop the deployment bind mount or the repo-name argument.
L="config validate: --deploy and --repo-name reach the validator"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" --deploy "$DEP" --repo-name acme/api
  expect "$L" 0 -- "$DEP:/config/claudebox.deploy.json:ro" "--deploy /config/claudebox.deploy.json" "--repo-name acme/api"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Drop the org bind mount or pass its host path inside the container.
L="config validate: --org is mounted and passed"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" --org "$DEP"
  expect "$L" 0 -- "$DEP:/config/org.json:ro" "--org /config/org.json"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Leave a relative config path relative on the Docker host.
L="config validate: a relative FILE is made absolute"
if selected "$L"; then
  (cd "$WORK" && env -i PATH="$BASE_PATH" HOME="$WORK" /bin/bash "$LAUNCHER" --dry-run config validate claudebox.json >"$WORK/out" 2>&1); RC=$?
  expect "$L" 0 -- "$WORK/claudebox.json:/config/claudebox.json:ro"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Name Docker before rejecting a missing config file.
L="config validate: a missing FILE dies before docker"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$WORK/nope.json"
  expect "$L" 1 -- "FILE not found: $WORK/nope.json" '!docker run'
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Skip existence validation for --deploy.
L="config validate: a missing --deploy dies"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" --deploy "$WORK/nope.json"
  expect "$L" 1 -- "--deploy not found"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Accept config without a subcommand.
L="config: no subcommand dies"
if selected "$L"; then
  launch "$L" 0 -- -- config
  expect "$L" 1 -- "config needs a subcommand"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Accept config check as an alias for validate.
L="config: an unknown subcommand dies"
if selected "$L"; then
  launch "$L" 0 -- -- config check "$CFG"
  expect "$L" 1 -- "unknown config subcommand 'check'"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Accept config validate without a file.
L="config validate: no FILE dies"
if selected "$L"; then
  launch "$L" 0 -- -- config validate
  expect "$L" 1 -- "config validate needs a FILE"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Ignore a second config file argument.
L="config validate: two FILEs die"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" "$DEP"
  expect "$L" 1 -- "config validate takes one FILE"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Allow deployment config flags on run.
L="config flags with another command die"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --deploy "$DEP"
  expect "$L" 1 -- "only apply to config validate"
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Reparse a FILE named run as another command.
L="config validate: a FILE named like a command is still a FILE"
if selected "$L"; then
  printf '{}\n' >"$WORK/run"
  (cd "$WORK" && env -i PATH="$BASE_PATH" HOME="$WORK" /bin/bash "$LAUNCHER" --dry-run config validate run >"$WORK/out" 2>&1); RC=$?
  expect "$L" 0 -- "$WORK/run:/config/claudebox.json:ro" '!more than one command'
fi

# Oracle: tester-brief.md Task 8 Interfaces (command, mounts and rejection rules).
# Mutation: Require an env file or repo for config validation.
L="config validate: needs no env file and no repo"
if selected "$L"; then
  (cd "$WORK" && env -i PATH="$BASE_PATH" HOME="$WORK" /bin/bash "$LAUNCHER" --dry-run --no-repo config validate "$CFG" >"$WORK/out" 2>&1); RC=$?
  expect "$L" 0 -- "config_cli.py validate"
fi


# Oracle: Task 8 Interfaces, FILE/--org/--deploy must be regular files before docker.
# Mutation: check existence only, allowing directories through as config mounts.
L="config validate: directories are not config files"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$WORK"
  expect "$L" 1 -- "FILE not found: $WORK" '!docker run'
  launch "$L" 0 -- -- config validate "$CFG" --org "$WORK"
  expect "$L (org)" 1 -- "--org not found: $WORK" '!docker run'
  launch "$L" 0 -- -- config validate "$CFG" --deploy "$WORK"
  expect "$L (deploy)" 1 -- "--deploy not found: $WORK" '!docker run'
fi

# Oracle: Task 8 Interfaces, missing --org is refused before docker.
# Mutation: omit --org file validation while validating --deploy only.
L="config validate: missing org file dies before docker"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" --org "$WORK/no-org.json"
  expect "$L" 1 -- "--org not found: $WORK/no-org.json" '!docker run'
fi

# Oracle: Task 8 Interfaces, all three flags belong only to config validate.
# Mutation: scope-check --deploy alone and accept --org or --repo-name with run.
L="config flags: org and repo-name are refused with run"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --org "$DEP"
  expect "$L (org)" 1 -- "only apply to config validate" '!docker run'
  launch "$L" 0 -- -- "${RUN[@]}" --repo-name acme/api
  expect "$L (repo-name)" 1 -- "only apply to config validate" '!docker run'
fi

# Oracle: Task 8 exact docker contract; CLAUDE.md resource flags 512 and 4g.
# Mutation: config branch omits --pids-limit or --memory.
L="config validate: resource limits survive the config branch"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG"
  expect "$L" 0 -- '--pids-limit 512' '--memory 4g'
fi

# Oracle: Validator DISPATCH 2: base 27dd46a claudebox.sh argument-loop
# final *) arm rejects stray bare words; Task 8 admits positionals only for config.
# Guard: green-now compatibility guard; Validator owns base/head observations.
# Mutation: accumulate or ignore bare positionals for run as well as config.
L="arguments: a stray bare word after run is unknown"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" stray-word
  expect "$L" 1 -- "unknown argument: stray-word (see --help)." '!docker run'
fi

# Oracle: Validator DISPATCH 2: base 27dd46a claudebox.sh argument-loop
# final *) arm rejects stray bare words; Task 8 admits positionals only for config.
# Guard: green-now compatibility guard; Validator owns base/head observations.
# Mutation: collect a bare word before command selection as a deferred positional.
L="arguments: a stray bare word before any command is unknown"
if selected "$L"; then
  launch "$L" 0 -- -- stray-word "${RUN[@]}"
  expect "$L" 1 -- "unknown argument: stray-word (see --help)." '!docker run'
fi

# Oracle: spec-amendment-1.md observable contract (ratified Task 8): CDPATH
# must not redirect the source away from the relative file the operator named.
# Mutation: use plain cd on dirname without neutralizing CDPATH or prefixing ./.
# Reachability: both cwd and CDPATH contain shared/input.json; only cwd is right.
L="config validate: CDPATH cannot redirect a relative FILE mount"
if selected "$L"; then
  mkdir -p "$WORK/path-operator/shared" "$WORK/path-shadow/shared"
  printf '{}\n' >"$WORK/path-operator/shared/input.json"
  printf '{"trigger":"requested"}\n' >"$WORK/path-shadow/shared/input.json"
  (CDPATH= cd "$WORK/path-operator" && env -i PATH="$BASE_PATH" HOME="$WORK" \
    CDPATH="$WORK/path-shadow" /bin/bash "$LAUNCHER" --dry-run \
    config validate shared/input.json >"$WORK/out" 2>&1); RC=$?
  expect "$L" 0 -- "$WORK/path-operator/shared/input.json:/config/claudebox.json:ro" \
    "!$WORK/path-shadow/shared"
fi

# Oracle: spec-amendment-1.md observable contract (ratified Task 8): a named
# file under a directory literally '-' remains that file, including for --org.
# Mutation: feed '-' to cd as directory operand, following OLDPWD instead.
# Reachability: '-/o.json' exists and OLDPWD names a distinct existing directory.
L="config validate: org path under dash directory mounts the named file"
if selected "$L"; then
  mkdir -p "$WORK/dash-operator/-" "$WORK/dash-oldpwd"
  printf '{}\n' >"$WORK/dash-operator/-/o.json"
  (CDPATH= cd "$WORK/dash-operator" && env -i PATH="$BASE_PATH" HOME="$WORK" \
    OLDPWD="$WORK/dash-oldpwd" /bin/bash "$LAUNCHER" --dry-run \
    config validate "$CFG" --org -/o.json >"$WORK/out" 2>&1); RC=$?
  expect "$L" 0 -- "$WORK/dash-operator/-/o.json:/config/org.json:ro" \
    '--org /config/org.json' "!$WORK/dash-oldpwd:/config/org.json:ro" \
    "!$WORK/dash-operator/-:/config/org.json:ro"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || { printf 'Failed:%s\n' "$FAILED"; exit 1; }
