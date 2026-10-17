# Codex Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `AGENT=codex` runs every claudebox review pass through `codex exec` on a ChatGPT-subscription login, with everything above the pass runner unchanged.

**Architecture:** A new `reviewer/codex_pass.py` mirrors `passes.run_pass` for Codex (argv, JSONL formatting, session-id recovery, limit classification) and returns the same `PassResult`. `Supervisor` picks the runner once from `AGENT`. The entrypoint skips the provider block under Codex, prunes and checks the login volume, and turns off Linear and kindex. The launcher attaches a named volume and gains `codex-login`.

**Tech Stack:** Python 3.11 stdlib (supervisor), bash (entrypoint, bash 4+; launcher, bash 3.2-safe), Docker, `@openai/codex` from npm.

**Spec:** `docs/superpowers/specs/2026-10-16-codex-agent-design.md`

## Global Constraints

- `AGENT` values: `claude` (default) and `codex`. Anything else is a startup `ConfigError`.
- Default `REVIEW_MODEL` under `AGENT=codex`: `gpt-6.1-sol`.
- Every Codex invocation carries `--json --ignore-user-config --dangerously-bypass-approvals-and-sandbox -m <model> -c developer_instructions=<…> -c allow_provider_model_fallback=false -c check_for_update_on_startup=false`, then `--`, then (resume only) the thread id, then the prompt. Verified against codex-cli 0.162.1 on 2026-10-16.
- Never pass `--dangerously-bypass-hook-trust`.
- Codex runs with `stdin=subprocess.DEVNULL`. It blocks on an open stdin.
- Named volume `claudebox-codex`, mounted at `/home/reviewer/.codex`. `CODEX_HOME=/home/reviewer/.codex`.
- At startup under `AGENT=codex`, everything in `CODEX_HOME` except `auth.json` is deleted before any `codex` runs.
- `reviewer/` stays standard-library only.
- `AGENT=claude` must be byte-identical in behavior: the existing suites pass unchanged, except for the `stdin=DEVNULL` addition to `passes.run_pass`.
- `claudebox.sh` must stay bash 3.2-safe (`${arr[@]+"${arr[@]}"}`).
- Pin `CODEX_VERSION=0.162.1` (the version recon ran against).

## Review Focus

1. A persona or round stanza containing `"`, `\`, newlines, tabs or non-ASCII must reach Codex byte-for-byte. `-c` falls back to a raw string when TOML parsing fails, which would silently change the text. Task 1 pins the encoding.
2. A task prompt beginning with `-` must not be parsed as a flag, on fresh and resumed passes alike. Task 1 pins `--` placement.
3. A successful review whose own text discusses "usage limits" or "429" must not be classified as a limit. Task 1 tests it.
4. A transient `Reconnecting... n/5 (… 429 …)` error followed by a completed turn is a success, not a limit. Task 1 tests it.
5. A container whose login has expired, or that never ran `codex-login`, must fail at startup with the fix named, not fail every pass. Task 4 tests it.

---

### Task 1: The Codex pass runner

**Files:**
- Create: `reviewer/codex_pass.py`
- Modify: `reviewer/passes.py` (`PassResult` gains a field; `run_pass` passes `stdin=subprocess.DEVNULL`)
- Test: `tests/test_codex_pass.py`, `tests/test_passes.py`

**Interfaces:**
- Produces:
  - `passes.PassResult` gains `switched: bool = False` (a default, so every existing constructor call still works).
  - `codex_pass.toml_string(text: str) -> str`: a TOML basic string, quotes included.
  - `codex_pass.build_argv(session_id: Optional[str], model: str, persona_prompt: str, prompt: str) -> List[str]`
  - `codex_pass.format_event(event: Dict[str, Any]) -> List[str]`
  - `codex_pass.error_text(event: Dict[str, Any]) -> str`: the failure message an event carries, or `""`.
  - `codex_pass.switched_model(event: Dict[str, Any]) -> str`: the substituted model's name, or `""`.
  - `codex_pass.run_pass(pair: Pair, prompt: str, session_id: Optional[str], persona_prompt: str, model: str, cwd: str, popen: Callable[..., Any] = subprocess.Popen) -> passes.PassResult`
- Consumes: `passes.is_usage_limit`, `passes.usage_limit_line`, `common.log`, `common.Pair`.

- [ ] **Step 1: Write the failing tests** in `tests/test_codex_pass.py`, styled like `tests/test_passes.py` (unittest, `import _path`).

`ArgvTest`:
- `test_fresh_pass`: `build_argv(None, "gpt-6.1-sol", "P", "review it")` equals `["codex", "exec", "--json", "--ignore-user-config", "--dangerously-bypass-approvals-and-sandbox", "-m", "gpt-6.1-sol", "-c", "developer_instructions=" + toml_string("P"), "-c", "allow_provider_model_fallback=false", "-c", "check_for_update_on_startup=false", "--", "review it"]`.
- `test_resumed_pass`: with `session_id="T1"`, the argv starts `["codex", "exec", "resume", "--json", …]`, and its last three elements are `["--", "T1", "review it"]`.
- `test_resumed_pass_still_carries_the_persona_and_the_lockdown`: the resumed argv contains `"--ignore-user-config"` and `"developer_instructions=" + toml_string("P")`.
- `test_never_bypasses_hook_trust`: `"--dangerously-bypass-hook-trust"` is in neither argv.
- `test_a_dash_leading_prompt_follows_the_separator` (Review Focus 2): for prompt `"--- x"`, the element after `"--"` (fresh) or after the id (resumed) is `"--- x"`.

`TomlStringTest` (Review Focus 1):
- `test_escapes`: `toml_string('a"b\\c\nd\te')` equals `'"a\\"b\\\\c\\nd\\te"'`.
- `test_non_ascii_survives`: `toml_string("é—✓")` round-trips through `tomllib.loads("x = " + s)["x"]`. Decorate it `@unittest.skipUnless(tomllib_available, …)`, because the image has 3.11 and a developer's host may not.
- `test_control_characters_are_escaped`: `toml_string("\x01")` contains `\\u0001`.

`FormatEventTest`:
- `thread.started` gives `["  ▸ session T1 started"]`.
- `item.completed` with an `agent_message` item gives the text split on newlines (empty text gives `[]`).
- `item.completed` with a `command_execution` item gives `["  → " + command[:200], "  ← " + whitespace-squashed aggregated_output[:200]]`.
- `item.started` gives `[]`.
- `turn.completed` gives `["  ✓ turn complete (in N, out M tokens)"]` from `usage.input_tokens`/`usage.output_tokens`.
- An unknown type gives `[]`.

`ErrorTextTest`:
- `turn.failed` gives `event["error"]["message"]`.
- `item.completed` with item type `error` gives `item["message"]`.
- `error` gives `message`, **except** one starting `"Reconnecting..."`, which gives `""`.
- `item.completed` with an `agent_message` whose text is `"you've hit your usage limit"` gives `""` (Review Focus 3).

`SwitchedModelTest`:
- An event carrying, at any nesting depth, `previous_model` and `current_model` with different values gives `current_model`.
- An event whose `message` contains `"Automatically switched to"` gives the text between `"switched to "` and `" due to"`, stripped.
- An ordinary event gives `""`.

`RunPassTest`, driving a real subprocess (a tiny Python script that prints JSONL, as `tests/test_passes.py` does):
- `test_success_recovers_the_thread_id`: `thread.started` T1, `agent_message`, `turn.completed`, exit 0 → `rc == 0`, `session_id == "T1"`, `limited is False`, `switched is False`.
- `test_resume_keeps_the_given_id_when_no_thread_event`: `session_id="T0"` given, the stream has none → `session_id == "T0"`.
- `test_usage_limit_in_turn_failed`: `turn.failed` whose message is `"You've hit your usage limit. Upgrade to Plus to continue using Codex (https://chatgpt.com/explore/plus), or try again at 3:00 PM."`, exit 1 → `rc != 0`, `limited is True`, `limit_line` contains `"usage limit"`, and the session id from `thread.started` is kept.
- `test_turn_failed_with_exit_zero_is_a_failure`: `turn.failed` with an ordinary message, exit 0 → `rc == 1`, `limited is False`.
- `test_reconnecting_429_then_success_is_not_a_limit` (Review Focus 4): an `error` `"Reconnecting... 2/5 (unexpected status 429 Too Many Requests …)"`, then `turn.completed`, exit 0 → `rc == 0`, `limited is False`.
- `test_review_prose_about_limits_is_not_a_limit` (Review Focus 3): an `agent_message` mentioning "usage limit" and "429", then `turn.completed`, exit 0 → `limited is False`.
- `test_limit_on_stderr_only`: exit 1, no `turn.failed`, stderr `"You've hit your usage limit."` → `limited is True`.
- `test_a_model_switch_is_reported`: an event with `previous_model: "gpt-6.1-sol"`, `current_model: "gpt-6-luna"`, then `turn.completed`, exit 0 → `rc == 0`, `switched is True`, and the log (capture `sys.stdout`) contains a WARN naming both models.
- `test_stdin_is_closed`: the script reads `sys.stdin.read()` and prints `turn.completed` only when it returns `""` → `rc == 0`. Have the test's `popen` wrapper assert `kwargs["stdin"] is subprocess.DEVNULL`.

In `tests/test_passes.py` `RunPassTest`, add `test_stdin_is_closed` the same way, for the Claude runner.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `./test-python.sh`
Expected: `tests/test_codex_pass.py` errors on `import codex_pass`, and the new `test_passes` case fails on the missing `stdin` kwarg.

- [ ] **Step 3: Implement `reviewer/codex_pass.py`, and the `passes.py` changes**

- `toml_string`: escape `\`, `"`, `\b \t \n \f \r`, and other control characters below `0x20` plus `0x7f` as `\uXXXX`. Leave everything else literal, non-ASCII included. Do not use `json.dumps`: TOML has no `\/`, and the explicit table keeps that from mattering.
- `run_pass`: the same shape as `passes.run_pass`, with one stdout pipe, stderr to a `TemporaryFile`, `text=True, bufsize=1`, `cwd=cwd` and `stdin=subprocess.DEVNULL`.
  - For each JSON object line: take `thread_id` from `thread.started`; append any `error_text` to a list; note a `turn.failed`; record the first non-empty `switched_model`; and log `format_event` lines with `pair=pair`.
  - Failure is `rc != 0` or a `turn.failed` seen. On failure, the evidence is stderr plus the collected error texts joined with newlines, and `limited = passes.is_usage_limit(evidence)`. Then log `WARN: codex exited {rc}:` and the last 5 non-blank stderr lines truncated to 400 characters, as `passes.run_pass` does. Return `rc` as `rc or 1`.
  - On success: when a switch was seen, log `WARN: codex switched from {model} to {switched}; ending this cycle after the group`, and return `switched=True`.
- `passes.py`: add the `switched: bool = False` field and `stdin=subprocess.DEVNULL` in `run_pass`'s `popen` call. Update the module docstring's first line to name both agents.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `./test-python.sh` then `python3 -m py_compile reviewer/*.py`
Expected: `OK`, and no output from `py_compile`.

- [ ] **Step 5: Commit**

```bash
git add reviewer/codex_pass.py reviewer/passes.py tests/test_codex_pass.py tests/test_passes.py
git commit -m "codex_pass: run one review pass through codex exec"
```

---

### Task 2: Supervisor selects the agent, and a model switch ends the cycle

**Files:**
- Modify: `reviewer/review_loop.py` (new `AGENTS`/`parse_agent`; `preflight`; `main`; `Supervisor.__init__`/`_run_one`; `run_cycle`)
- Test: `tests/test_review_loop.py`

**Interfaces:**
- Consumes: `codex_pass.run_pass` and `PassResult.switched` from Task 1.
- Produces:
  - `review_loop.AGENTS = ("claude", "codex")`
  - `review_loop.parse_agent(env: Mapping[str, str]) -> str`: unset or empty means `"claude"`; otherwise one of `AGENTS`, else `ConfigError("unknown AGENT='x'; use one of: claude, codex.")`.
  - `Supervisor(..., agent: str = "claude")`.

- [ ] **Step 1: Write the failing tests** in `tests/test_review_loop.py`

- `ParseAgentTest`: unset → `"claude"`; `""` → `"claude"`; `"codex"` → `"codex"`; `"gpt"` raises `ConfigError` whose message contains `claude, codex`.
- `test_check_rejects_an_unknown_agent`: `main(["--check"])` with `AGENT=gpt` exits 1. Follow the existing `--check` tests' env setup.
- `test_check_provider_skips_provider_validation_under_codex`: `main(["--check", "--provider"])` with `AGENT=codex` and **no** provider credential in env returns 0. A twin with `AGENT` unset and no `OLLAMA_API_KEY` still exits 1.
- `test_codex_agent_dispatches_to_codex_pass`: build a `Supervisor(agent="codex", …)` the way the existing `_run_one` tests do, patch `codex_pass.run_pass` and `passes.run_pass` with recorders, and run one pair. Only `codex_pass.run_pass` is called, with `persona_prompt == supervisor.system_prompt_for(pair)` and no `mcp_args` keyword.
- `test_spawn_failure_names_the_agent`: under `agent="codex"`, a `run_pass` that raises `OSError` returns `rc=1`, and the log line contains `could not run codex`.
- `test_a_switched_pass_counts_as_success_and_ends_the_cycle`: two groups. The first group's only pair returns `PassResult(rc=0, session_id="T1", limited=False, limit_line="", switched=True)`. `run_cycle` returns `CYCLE_LIMITED`. That pair is in `reviewed` (recorded as a success), is **not** in `owed`, and the second group did not run.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `./test-python.sh`
Expected: the new tests fail, on `parse_agent` and the `agent` keyword not existing.

- [ ] **Step 3: Implement**

- `parse_agent` beside `parse_max_concurrent`. Call it in `preflight` so `--check` validates it. In `main`, only call `providers.validate(env)` under `--provider` when `parse_agent(env) == "claude"`.
- `main` passes `agent=parse_agent(env)` to `Supervisor` and logs `Review agent: {agent}` once. Under `codex` it still builds `mcp_args` (it's harmless), but `_run_one` does not pass them.
- `Supervisor._run_one`: under `codex`, call `codex_pass.run_pass(pair=…, prompt=…, session_id=…, persona_prompt=self.system_prompt_for(pair), model=self.model, cwd=self.cwd)`; otherwise the call as it is today. The `OSError` WARN reads `could not run {self.agent}`.
- `run_cycle`: in the per-pair loop, collect `switched_here` from successes with `result.switched`. Widen the cut condition to `if limited_here or unstarted or switched_here:`, with `outcome = CYCLE_LIMITED if (limited_here or switched_here) else CYCLE_OK`. `cut_owes` stays `limited_here | still_owed`: a switched pair succeeded, so it owes nothing.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `./test-python.sh && ./test-personas.sh && ./test-providers.sh`
Expected: all green. The bash suites prove `AGENT=claude` is unchanged.

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py tests/test_review_loop.py
git commit -m "review_loop: AGENT picks the pass runner; a model switch ends the cycle"
```

---

### Task 3: Install Codex in the image

**Files:**
- Modify: `Dockerfile`

**Interfaces:**
- Produces: `codex` on `PATH` in the image, root-owned; `/home/reviewer/.codex` existing and owned by `reviewer`. Docker copies an image directory's ownership into a new named volume mounted over it, which is what makes the volume writable by `reviewer`.

- [ ] **Step 1: Edit the Dockerfile**

- Below the Claude Code install: `ARG CODEX_VERSION=0.162.1` and `RUN npm install -g @openai/codex@${CODEX_VERSION} && codex --version`, with a comment block in the file's style. It's for `AGENT=codex`, pinned because recon ran against this version, and root-owned like every other tool the reviewer runs but must not modify.
- In the `USER reviewer` block's `RUN`, add `/home/reviewer/.codex` to the `mkdir -p`, with one comment line on why it must exist in the image.

- [ ] **Step 2: Build and check**

Run: `./claudebox.sh build && docker run --rm --entrypoint sh claudebox -c 'codex --version; stat -c %U /home/reviewer/.codex; stat -c %U "$(readlink -f "$(command -v codex)")"'`
Expected: `codex-cli 0.162.1`, `reviewer`, `root`.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "image: install the Codex CLI for AGENT=codex"
```

---

### Task 4: Entrypoint under AGENT=codex

**Files:**
- Modify: `entrypoint.sh`
- Test: `test-personas.sh`

**Interfaces:**
- Consumes: `review_loop.parse_agent` via `--check` (Task 2); `codex` in the image (Task 3).
- Produces: under `AGENT=codex`, an exec'd supervisor whose environment has `AGENT=codex`, `CODEX_HOME=$HOME/.codex`, `REVIEW_MODEL` (default `gpt-6.1-sol`), no `LINEAR_API_KEY`, no `KINDEX_ENABLED`, and no `$HOME/mcp.json`.

- [ ] **Step 1: Write the failing cases in `test-personas.sh`**

Make the existing stub agent-aware rather than writing a second one, so the counting and lock logic stays single. Install it as both `$BIN/claude` and `$BIN/codex`. At the top of the stub:
- When invoked as `codex` with first argument `login`: for `login status`, print `Logged in using ChatGPT` and exit 0, unless `STUB_CODEX_LOGGED_OUT` is set, in which case print `Not logged in` and exit 1. Exit before the invocation counter, so login checks are never counted as passes.
- When invoked as `codex`: success emits `{"type":"thread.started","thread_id":"S<n>"}` and `{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}`. Failure emits `thread.started` (unless `STUB_NO_SESSION`) and `{"type":"turn.failed","error":{"message":"<text>"}}`, and exits 1, where `<text>` for `STUB_FAIL_MODE=limit` is the captured wording `You've hit your usage limit. Upgrade to Plus to continue using Codex (https://chatgpt.com/explore/plus), or try again at 3:00 PM.`
- Record `CODEX_HOME` in each dump's ENV lines.

Add `CODEX="AGENT=codex"` cases. The baseline's `PROVIDER=ollama OLLAMA_API_KEY=k` stays, which is exactly the "PROVIDER set alongside codex" shape:
- `codex: passes go to codex, never claude` → `CALLS:8` over two cycles, and every dump's argv starts `exec`.
- `codex: a resumed pass still carries its persona and the lockdown` (the central assertion) → the second-cycle invocations contain `resume`, `--ignore-user-config` and `developer_instructions=`, with each code persona's identity text once, mirroring the existing `parallel: a resumed pass still carries its own persona` case.
- `codex: model defaults to gpt-6.1-sol` → argv contains `-m gpt-6.1-sol`; with `REVIEW_MODEL=gpt-6-astra`, `-m gpt-6-astra`.
- `codex: PROVIDER is ignored with a warning` → the log contains `PROVIDER=ollama is ignored under AGENT=codex`, and `ANTHROPIC_MODEL` is `<unset>` in the dump.
- `codex: Linear is off` → with `LINEAR_API_KEY=lin`: the log contains `Linear is not available under AGENT=codex`, and no argv contains `Linear`. (The kindex skip is a two-line branch beside it. `test-personas.sh` pins `KINDEX_SRC` to a missing path, so kindex has no case here.)
- `codex: a usage limit keeps the session and backs off` → `STUB_FAIL_ON=1`, so the second cycle's first codex invocation resumes `S1`.
- `codex: CODEX_HOME is pruned to auth.json at startup` → pre-seed `$HOME_DIR/.codex/auth.json`, `AGENTS.md` and `hooks.json` before the run, and add a `PRESEED_CODEX=1` hook to `run_entrypoint`. Afterwards `auth.json` remains and the other two are gone.
- `refuses: codex without a login names codex-login` → `STUB_CODEX_LOGGED_OUT=1`; the startup error contains `claudebox.sh codex-login`, and zero passes ran. Use the suite's `refuses_before_work` helper.
- `refuses: an unknown AGENT` → `AGENT=gpt` dies with `unknown AGENT`.

- [ ] **Step 2: Run the cases and watch them fail**

Run: `./test-personas.sh codex`
Expected: every codex case fails. Passes still go to `claude`.

- [ ] **Step 3: Implement in `entrypoint.sh`**

- Add `AGENT` to the `strip_surrounding_quotes` list. Set `AGENT="${AGENT:-claude}"` and export it before the `--check` call.
- After GitHub auth, under `AGENT=codex`:
  1. `export CODEX_HOME="$HOME/.codex"`.
  2. Prune: delete every entry in `CODEX_HOME` other than `auth.json`, using `find "$CODEX_HOME" -mindepth 1 -maxdepth 1 ! -name auth.json -exec rm -rf {} +`. The comment cites spec recon 11.
  3. `codex login status`, capturing its output; die unless it exits 0 and mentions `ChatGPT`, with the message `AGENT=codex needs a ChatGPT login in the claudebox-codex volume. Run: claudebox.sh codex-login`.
  4. If `PROVIDER` is non-empty, log `WARN: PROVIDER=$PROVIDER is ignored under AGENT=codex.`
  5. `REVIEW_MODEL="${REVIEW_MODEL:-gpt-6.1-sol}"`.
  6. If `LINEAR_API_KEY` is set, log `Linear is not available under AGENT=codex yet; ignoring LINEAR_API_KEY.` and `unset LINEAR_API_KEY`.
- Wrap the "Backend selection" banner, the provider `case` and the model-tier pinning in `if [ "$AGENT" = claude ]; then … fi`.
- kindex: under `codex`, skip setting `KINDEX_ENABLED`. When a store is mounted, log `kindex is not available under AGENT=codex yet; the mounted store is unused.`
- MCP: under `codex`, skip `write_mcp_config`. The `rm -f "$MCP_CONFIG_FILE"` still runs first.
- `--check` already runs before all of this, and validates `AGENT` (Task 2).

- [ ] **Step 4: Run the suites and watch them pass**

Run: `bash -n entrypoint.sh && ./test-personas.sh && ./test-providers.sh`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add entrypoint.sh test-personas.sh
git commit -m "entrypoint: AGENT=codex prunes and checks the login, skips the provider block"
```

---

### Task 5: Launcher support

**Files:**
- Modify: `claudebox.sh`
- Test: `test-launcher.sh`

**Interfaces:**
- Consumes: the image from Task 3; the entrypoint from Task 4.
- Produces:
  - `--agent NAME` (`claude`|`codex`), passed through as `-e AGENT=NAME`. Without the flag, the agent is read from the env file's `AGENT=` line with the same `sed` pattern `derive_name` uses for `GITHUB_REPOSITORY`.
  - Under codex, `run`/`test` add `-v claudebox-codex:/home/reviewer/.codex`.
  - New command `codex-login`, plus `codex-login --status`.

- [ ] **Step 1: Write the failing `test-launcher.sh` cases** (dry-run, matching the file's `launch`/`expect` helpers)

- `run --agent codex` → output contains `-e AGENT=codex` and `-v claudebox-codex:/home/reviewer/.codex`.
- An env file containing `AGENT=codex` with no flag → the volume mount appears, and no `-e AGENT=` appears (the env file already carries it).
- `run` with no agent anywhere → no `claudebox-codex` in the output.
- `codex-login` → `docker run --rm -it -v claudebox-codex:/home/reviewer/.codex --cap-drop ALL --security-opt no-new-privileges --entrypoint codex claudebox login --device-auth`.
- `codex-login --status` → the same with `login status` in place of `login --device-auth`.
- `run --agent gpt` → dies with `--agent must be claude or codex`.
- `--help` mentions `codex-login` and `--agent`.

- [ ] **Step 2: Run them and watch them fail**

Run: `./test-launcher.sh codex`
Expected: every case fails.

- [ ] **Step 3: Implement in `claudebox.sh`**

- Parse `--agent` and the `codex-login` command (accept `--status` only after it). Validate the name.
- `codex-login` needs no env file, repo or name. Add it beside `build|config` in the resolution `case`.
- In `build_run_flags`, under codex: append the volume mount. Unless `DRY_RUN=1`, refuse when `docker ps -q --filter volume=claudebox-codex` prints anything, with `another container is already using the claudebox-codex login (…names…); two containers sharing one login sign each other out. Stop it first.` Skip the check under dry-run, with a comment saying so, since dry-run must not call docker.
- Usage text: document `--agent`, `codex-login` and `--status`, and add one example line.
- Keep it 3.2-safe.

- [ ] **Step 4: Run and pass**

Run: `bash -n claudebox.sh && ./test-launcher.sh`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add claudebox.sh test-launcher.sh
git commit -m "launcher: --agent codex, the claudebox-codex volume, and codex-login"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md`, `.env.example`, `CLAUDE.md`, `HISTORY.md`, `.constrain/trust_policy.yaml`

- [ ] **Step 1: Write the docs**

- `README.md`: a "Running on Codex (ChatGPT subscription)" section covering:
  - why you'd use it;
  - `claudebox.sh codex-login` once, then `run --agent codex` (or `AGENT=codex` in the env file);
  - the default model, `gpt-6.1-sol`;
  - one container per host, and why;
  - kindex and Linear being off for now;
  - what happens at a usage limit: the loop backs off, and after you spend a reset the next poll carries on.
- `.env.example`: an `AGENT` block, no quotes.
- `CLAUDE.md`:
  - Add `codex_pass.py` to the module list in "`reviewer/` is the supervisor".
  - Add a "Codex as the agent" subsection under Architecture that summarizes the spec's recon items 4, 8, 10, 11 and 12, and points to the spec.
  - Add to Configuration: `AGENT`.
  - Add these Gotchas:
    - `--ignore-user-config` on every Codex pass is the counterpart of `--strict-mcp-config`;
    - never `--dangerously-bypass-hook-trust`;
    - the startup prune keeps only `auth.json`;
    - Codex needs `stdin=DEVNULL`;
    - `developer_instructions` goes through `toml_string`, never the raw-string fallback;
    - the volume is single-container because refresh tokens are single-use.
- `HISTORY.md`: one Unreleased bullet.
- `.constrain/trust_policy.yaml`: an entry for the `claudebox-codex` volume's `auth.json`. It's an AUTH tier, held in a reviewer-writable volume, and a whole ChatGPT account credential, so it carries more than a scoped key; it says that.

- [ ] **Step 2: Check**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh && ./test-shim.sh && ./test-launcher.sh`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add README.md .env.example CLAUDE.md HISTORY.md .constrain/trust_policy.yaml
git commit -m "docs: AGENT=codex"
```

---

### Task 7: Verification in the built image (needs the operator)

**Files:** none changed unless a check fails. A failure goes back to the owning task.

This is the spec's "Verification before it runs unattended". Steps marked **operator** need the human, because device-code login is interactive.

- [ ] **Step 1 (operator):** `./claudebox.sh build && ./claudebox.sh codex-login`. Complete the device code in a browser. Then `./claudebox.sh codex-login --status` prints `Logged in using ChatGPT`.
- [ ] **Step 2: Repo MCP lockdown (spec check 1).** Make a throwaway repo whose `.codex/config.toml` declares an MCP server that touches `/tmp/MCP_STARTED`. Run one pass against it inside the image (via `claudebox.sh shell` on a running container, or a `docker run` with the volume) using `codex_pass.build_argv`'s exact flags. Expected: no `/tmp/MCP_STARTED`. Control: the same run with the server given as `-c mcp_servers.x.command=…` creates it.
- [ ] **Step 3: Repo hooks (spec check 2).** First find a hook spelling that fires when passed with `-c` (the control). Then show that the same hook in the repo's `.codex/config.toml` and in `.codex/hooks.json` does not fire under the pass flags. If no control can be made to fire, stop and report it to the operator. Do not ship past an unproven hook boundary.
- [ ] **Step 4: Flags accepted (spec check 3).** One pass with the full flag set plus `--strict-config`. Expected: no config error.
- [ ] **Step 5 (operator watches): Real review (spec check 4).** `./claudebox.sh test --agent codex --prs <a small open PR> --repo <path>` with `MAX_CYCLES=2` in the env file. Expected: signed comments posted, and the second cycle's log shows `(resumed session …)` or the gate holding it as unchanged. To force a resume, push a trivial commit between cycles, or check with `REVIEW_ON_CHANGE=0`.
- [ ] **Step 6: Volume prune (spec check 6).** Write `AGENTS.md` and `hooks.json` into the volume through `docker run --rm -v claudebox-codex:/v alpine sh -c 'touch /v/AGENTS.md /v/hooks.json'`, start the container, and list the volume. Expected: only `auth.json` (plus whatever Codex created since).
- [ ] **Step 7: Concurrent refresh (spec check 7).** Backdate `last_refresh` in the volume's `auth.json` by 30 days through a one-off container using `jq`, then run one cycle with 4 personas in parallel. Expected: all four succeed, and `codex-login --status` still reports a login afterwards. If not, set `MAX_CONCURRENT_PASSES` to default to `1` under `AGENT=codex` in `review_loop.parse_max_concurrent`, with a test and a CLAUDE.md note, per the spec.
- [ ] **Step 8 (operator): Host login intact (spec check 5).** After step 7, run `codex exec "say ok"` on the Mac. Expected: it works without re-login.
- [ ] **Step 9:** Record the results (versions, date, pass/fail per check) in the spec's recon section, and commit.

```bash
git add docs/superpowers/specs/2026-10-16-codex-agent-design.md
git commit -m "spec: record the in-image verification of AGENT=codex"
```
