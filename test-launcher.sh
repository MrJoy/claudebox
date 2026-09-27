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
  expect "$L" 0 -- "!/kindex-src"
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

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || { printf 'Failed:%s\n' "$FAILED"; exit 1; }
