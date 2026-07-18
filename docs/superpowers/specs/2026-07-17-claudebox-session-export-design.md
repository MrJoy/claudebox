# claudebox — export & path-align in-container Claude sessions

**Date:** 2026-07-17
**Status:** Approved design, pending implementation plan

## Problem

claudebox runs an unattended Claude Code review loop *inside* a container. Claude
Code writes each session transcript to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`,
where `<encoded-cwd>` is the working directory with every non-alphanumeric character
replaced by `-`. Two facts keep those in-container sessions from lining up with the
host's own sessions for the same repo (e.g. when comparing them in a tool like clauditor):

1. **Not exported.** The transcripts live on the container's ephemeral filesystem
   (`/home/reviewer/.claude/projects/...`) and vanish when the container is removed.
2. **Wrong project folder.** The review clone runs at `/home/reviewer/work/repo`, so it
   encodes to `-home-reviewer-work-repo` — a container-only folder, not the host's
   `-Users-<you>-...-<repo>` folder.

We want in-container review sessions to land on the host, filed under the *same*
project folder the host uses for that repo.

## Encoding rule (verified)

Claude Code's project-folder name is the absolute cwd with each non-alphanumeric
character mapped one-for-one to `-`, with no collapsing of runs. Verified against the
host's existing folders:

- `/Users/jonathonfrisby/mrjoy/MrJoy.com/MrJoy.com` → `-Users-jonathonfrisby-mrjoy-MrJoy-com-MrJoy-com` (`.` → `-`)
- `/Users/jonathonfrisby/Unity-Games/3DTDF2P/.claude/worktrees/1982` → `...-3DTDF2P--claude-worktrees-1982` (`/` then `.` → two dashes, no collapse)

Reproducible in shell as: `printf '%s' "$repo_abs" | sed 's/[^a-zA-Z0-9]/-/g'`.

## Design

One opt-in launcher flag, `--export-sessions`, that does two **coupled** things: export
the transcripts and align the in-container path. They are coupled deliberately — with a
narrow per-repo mount, export only lands if the in-container cwd encodes to the same
folder we mounted, so alignment is what makes the mount work, not a separate nicety.

Chosen safety posture (from brainstorming):
- **Narrowest blast radius:** mount only *this repo's* project folder, not all of `~/.claude/projects`.
- **Opt-in:** off by default; preserves current behavior and safety posture.

### 1. Launcher (`claudebox.sh`)

- Add `--export-sessions` flag (default off).
- It requires a mounted repo. If combined with `--no-repo`, `die` — there is no host
  path to line up to.
- When enabled, in `build_run_flags`:
  - Compute `repo_abs` (already done for the `/repo` mount) and
    `encoded="$(printf '%s' "$repo_abs" | sed 's/[^a-zA-Z0-9]/-/g')"`.
  - `mkdir -p "$HOME/.claude/projects/$encoded"` on the host **before** `docker run`, so
    Docker binds an existing, user-owned source dir rather than creating a root-owned one.
  - Add `-e HOST_REPO_PATH="$repo_abs"`.
  - Add `-v "$HOME/.claude/projects/$encoded:/home/reviewer/.claude/projects/$encoded"`,
    **unless** `--mount-claude` is also set — then all of `~/.claude` is already mounted
    read-write and the narrow mount would be redundant, so skip it (still pass
    `HOST_REPO_PATH` for alignment).
- Applies to both `run` and `test` (they share `build_run_flags`).
- Document in `usage()`.

### 2. Entrypoint (`entrypoint.sh`)

- Where `WORK_DIR` / `WORK_REPO` are defined: if `HOST_REPO_PATH` is set and non-empty,
  use it as the working-clone location:
  - `WORK_REPO="$HOST_REPO_PATH"`, `WORK_DIR="$(dirname "$HOST_REPO_PATH")"`.
  - Otherwise keep today's `WORK_DIR="${WORK_DIR:-$HOME/work}"`, `WORK_REPO="$WORK_DIR/repo"`.
- Everything downstream is unchanged: `mkdir -p "$WORK_DIR"`, the local
  `git clone --local --no-hardlinks "$REPO_PATH" "$WORK_REPO"` (or network fallback),
  `safe.directory`, `cd "$WORK_REPO"`, and the review loop. The clone simply happens at
  the aligned path, so `claude`'s cwd encodes to the mounted folder and transcripts
  stream directly onto the host.
- Graceful degradation: if `mkdir -p "$WORK_DIR"` fails at the aligned path (an exotic
  host root not pre-created in the image — see §3), log a warning and fall back to the
  default `$HOME/work/repo` location so the loop still runs (export just won't line up).

### 3. Dockerfile

The container runs as the unprivileged `reviewer` user and cannot `mkdir` a top-level
path component under `/` (`/` is root-owned and not writable by `reviewer`), and it
cannot start as root and drop privileges because `--cap-drop ALL` removes `CAP_SETUID`.
Only the **first** path component needs to pre-exist and be writable; `mkdir -p` creates
everything below it.

- Pre-create `/Users` and `/home`, owned by `reviewer`, in the image
  (`/Users` covers macOS hosts, `/home` covers Linux hosts).
- `/home/reviewer` already exists; making `/home` itself `reviewer`-owned only lets the
  clone path be created under a differently-named home if the host repo lives there.

### 4. Docs

- `README.md`: a short subsection on `--export-sessions` — what it does, that it aligns
  the in-container path with the host so sessions group under the same project, and the
  safety note below.
- `.env.example`: note that `HOST_REPO_PATH` exists but is **launcher-managed**, not a
  user-set variable.
- Launcher `--help` (`usage()`): document `--export-sessions`.

## Safety

Even narrowed, `--export-sessions` bind-mounts one host session folder **read-write**
into a container running in YOLO mode (`--dangerously-skip-permissions`). The container
can read and rewrite that repo's transcripts. That is the accepted cost of exporting;
the narrow, per-repo mount keeps every *other* project's transcripts untouched. This
trade-off is called out in the README. The three existing safety boundaries (unprivileged
user, read-only seed repo, privilege-minimized GitHub token) are unchanged.

## Alternatives considered

- **Mount all of `~/.claude/projects`** — robust (Claude computes the folder name, we
  don't reproduce the encoding), but the container could read/delete every project's
  transcripts. Rejected in favor of the narrow mount.
- **Bind-mount a host scratch dir at `repo_abs`** for the writable clone instead of
  pre-creating roots in the image — works for any host root without a Dockerfile change,
  but adds host-side scratch management, an extra mount, and a clone that persists on the
  host across runs. Rejected for more moving parts; the two-line Dockerfile change is
  simpler.
- **Symlink the host path to `$HOME/work/repo`** — rejected: Claude resolves the physical
  cwd (realpath), so the encoding would use the symlink target, not the host path.
- **Always-on (no flag)** — rejected: changes default behavior and always mounts host
  session files into a YOLO container.

## Out of scope

- Exporting anything beyond session transcripts (settings, todos, shell snapshots).
- Reconciling or de-duplicating in-container vs host sessions; they simply coexist in the
  same project folder as distinct session ids.
- Any change to the review cadence, provider selection, or session-rotation logic.
