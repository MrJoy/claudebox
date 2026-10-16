"""Running one review pass through the Codex CLI.

The AGENT=codex counterpart of passes.run_pass: one `codex exec` invocation,
its JSONL consumed as it arrives, its stderr captured to a temp file, and the
same PassResult handed back, so nothing above the pass runner needs to know
which agent ran. docs/superpowers/specs/2026-10-16-codex-agent-design.md has
the recon every flag here rests on.
"""

import json
import re
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional

import passes
from common import Pair, log

_WHITESPACE = re.compile(r"[\n\t ]+")

# A retry Codex makes on its own, which may still succeed. Only the failure it
# finally gives up with is evidence.
_RECONNECTING = "Reconnecting..."

_SWITCHED = re.compile(r"Automatically switched to (.+?) due to")

_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
                 "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_string(text: str) -> str:
    """A TOML basic string, quotes included.

    `-c key=value` parses the value as TOML and quietly falls back to the raw
    text when that fails, which would hand Codex a different persona than the
    one written. Emitting valid TOML every time keeps the text byte-for-byte.
    """
    out = []
    for ch in text:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def build_argv(
    session_id: Optional[str], model: str, persona_prompt: str, prompt: str
) -> List[str]:
    """The codex invocation, fresh or resumed.

    --ignore-user-config is the counterpart of Claude's --strict-mcp-config: a
    reviewed repo's .codex/config.toml starts MCP servers of its choosing
    without it. --strict-config makes an unknown -c key an error: codex
    otherwise ignores one silently, which once left a "fallback off" setting
    that did not exist looking like a guard. developer_instructions carries
    the persona on every call, the way --append-system-prompt does. `--` comes before the session id and the
    prompt, so a prompt that starts with a dash is never read as a flag.
    """
    argv = ["codex", "exec"]
    if session_id:
        argv.append("resume")
    argv += [
        "--json",
        "--strict-config",
        "--ignore-user-config",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m", model,
        "-c", "developer_instructions=" + toml_string(persona_prompt),
        "-c", "check_for_update_on_startup=false",
        "--",
    ]
    if session_id:
        argv.append(session_id)
    argv.append(prompt)
    return argv


def format_event(event: Dict[str, Any]) -> List[str]:
    """Human-readable lines for one JSONL event, or none."""
    etype = event.get("type")
    if etype == "thread.started":
        return [f"  ▸ session {event.get('thread_id')} started"]
    if etype == "item.completed":
        item = event.get("item") or {}
        itype = item.get("type")
        if itype == "agent_message":
            text = item.get("text") or ""
            return text.split("\n") if text else []
        if itype == "command_execution":
            output = _WHITESPACE.sub(" ", str(item.get("aggregated_output") or "")).strip()
            return ["  → " + str(item.get("command") or "")[:200], "  ← " + output[:200]]
        return []
    if etype == "turn.completed":
        usage = event.get("usage") or {}
        return [f"  ✓ turn complete (in {usage.get('input_tokens', 0)}, "
                f"out {usage.get('output_tokens', 0)} tokens)"]
    return []


def error_text(event: Dict[str, Any]) -> str:
    """The failure message an event carries, or "".

    Never the review's own text: a reviewer writing about usage limits is not
    a limit, which is the same guard passes._API_ERROR_PREFIX gives Claude.
    """
    etype = event.get("type")
    if etype == "turn.failed":
        return str((event.get("error") or {}).get("message") or "")
    if etype == "item.completed":
        item = event.get("item") or {}
        return str(item.get("message") or "") if item.get("type") == "error" else ""
    if etype == "error":
        message = str(event.get("message") or "")
        return "" if message.startswith(_RECONNECTING) else message
    return ""


def _find_models(value: Any) -> Optional[tuple]:
    if isinstance(value, dict):
        if "previous_model" in value and "current_model" in value:
            return value["previous_model"], value["current_model"]
        for child in value.values():
            found = _find_models(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_models(child)
            if found:
                return found
    return None


def switched_model(event: Dict[str, Any]) -> str:
    """The model Codex substituted for the one asked for, or "".

    The only defence: codex has no setting to turn the switch off
    (allow_provider_model_fallback is a model-catalog field the server sends,
    not config). The event shape of a switch was never observed, so this looks
    for the field names and the message the binary carries.
    """
    found = _find_models(event)
    if found and found[0] != found[1] and found[1]:
        return str(found[1])
    match = _SWITCHED.search(json.dumps(event))
    return match.group(1).strip() if match else ""


def run_pass(
    pair: Pair,
    prompt: str,
    session_id: Optional[str],
    persona_prompt: str,
    model: str,
    cwd: str,
    popen: Callable[..., Any] = subprocess.Popen,
) -> passes.PassResult:
    argv = build_argv(session_id, model, persona_prompt, prompt)
    recovered = session_id
    errors: List[str] = []
    turn_failed = False
    switched = ""

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errfile:
        # stdin closed: codex exec reads it to the end before starting, even
        # with a prompt argument, and would otherwise wait on PID 1's.
        proc = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errfile,
            cwd=cwd,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            # Recovered as the stream arrives, so a pass that started a thread
            # and then hit a limit still has a resumable one.
            if event.get("type") == "thread.started" and event.get("thread_id"):
                recovered = event["thread_id"]
            if event.get("type") == "turn.failed":
                turn_failed = True
            text = error_text(event)
            if text:
                errors.append(text)
            if not switched:
                switched = switched_model(event)
            for out in format_event(event):
                log(out, pair=pair)
        proc.stdout.close()
        rc = proc.wait()

        errfile.seek(0)
        stderr = errfile.read()

    if rc != 0 or turn_failed:
        evidence = "\n".join([stderr] + errors)
        limited = passes.is_usage_limit(evidence)
        limit_line = passes.usage_limit_line(evidence) if limited else ""
        log(f"WARN: codex exited {rc}:", pair=pair)
        # Truncated as passes.run_pass truncates, and for the same reason:
        # stderr cannot be assumed credential-free.
        for tail in [ln for ln in (stderr.splitlines() + errors) if ln.strip()][-5:]:
            log(f"  {tail[:400]}", pair=pair)
        return passes.PassResult(rc=rc or 1, session_id=recovered,
                                 limited=limited, limit_line=limit_line)

    if switched:
        log(f"WARN: codex switched from {model} to {switched}; "
            "ending this cycle after the group.", pair=pair)
    return passes.PassResult(rc=0, session_id=recovered, limited=False,
                             limit_line="", switched=bool(switched))
