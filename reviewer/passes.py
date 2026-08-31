"""Running one review pass.

One `claude` invocation, its stream-json consumed as it arrives, its stderr
captured to a temp file. stdout is the only pipe on purpose: reading two pipes
from one child is where deadlocks live, and the shell version already sent
stderr to a file.
"""

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from common import Pair, log

# Matches provider error text, an upstream surface that can change without
# notice, so the failure mode of a miss matters: a missed match falls through to
# the ordinary path (drop the session, carry on), which is the pre-existing
# behavior. A false positive keeps a session that will fail again next cycle and
# be dropped then. Neither wedges the loop.
#
# `limit reached` and `reached your limit` are here because `limit` on its own is
# only reachable via `rate.?limit` and `usage limit`, so the near-miss wordings
# (`5-hour limit reached`, `you have reached your limit`) matched nothing.
#
# re.MULTILINE is belt-and-braces, not the thing that makes a status code on its
# own line match. The 429/529 arm reads (^|[^0-9])(429|529)([^0-9]|$), and a
# newline is a non-digit, so [^0-9] already covers every line boundary and the
# anchors are only consulted at the very start and end of the string -- where ^
# and $ match with or without the flag. It stays because the shell used grep,
# which is line-oriented, and a future edit that narrows the character classes
# would need it.
USAGE_LIMIT_RE = re.compile(
    r"rate.?limit|usage limit|limit reached|reached your limit|too many requests"
    r"|quota|overloaded|(^|[^0-9])(429|529)([^0-9]|$)",
    re.IGNORECASE | re.MULTILINE,
)

_WHITESPACE = re.compile(r"[\n\t ]+")


def is_usage_limit(text: str) -> bool:
    return bool(USAGE_LIMIT_RE.search(text or ""))


def usage_limit_line(text: str) -> str:
    """The first line that read as a limit, truncated.

    The classifier scans the whole stderr while the log tails only its last few
    lines, so a limit reported early in a long stderr is classified right and
    invisible to whoever reads the log. Only the matched line, and only 400
    characters of it: claude's stderr is not a stream that can be assumed
    credential-free.
    """
    for line in (text or "").splitlines():
        if USAGE_LIMIT_RE.search(line):
            return line[:400]
    return ""


def _alt(value: Any, fallback: Any) -> Any:
    """jq's `//`: falls back only on null or false.

    Python's `or` would also swallow 0 and "", so a numeric result of 0 would
    log as an empty string where the shell logged "0".
    """
    return fallback if value is None or value is False else value


def format_event(event: Dict[str, Any]) -> List[str]:
    """Human-readable lines for one stream-json event, or none."""
    etype = event.get("type")

    if etype == "system" and event.get("subtype") == "init":
        return [f"  ▸ session {event.get('session_id')} started"]

    if etype == "assistant":
        out: List[str] = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    out.extend(text.split("\n"))
            elif block.get("type") == "tool_use":
                # Compact separators, matching jq's tojson: the shell logged
                # the same 200 characters of a tool input, and Python's default
                # ", " / ": " would spend a dozen of them on whitespace.
                payload = json.dumps(block.get("input"), separators=(",", ":"))[:200]
                out.append(f"  → {block.get('name')}: {payload}")
        return out

    if etype == "user":
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, list):
                text = " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
            elif isinstance(content, (dict, list)):
                # jq's tostring JSON-encodes a non-string; Python's str() would
                # render a dict with single quotes, which is not what the shell
                # put in the log.
                text = json.dumps(content)
            else:
                text = "" if content is None else str(content)
            out.append("  ← " + _WHITESPACE.sub(" ", text).strip()[:200])
        return out

    if etype == "result":
        return [
            f"  ✓ result ({_alt(event.get('subtype'), '')}): "
            + str(_alt(event.get("result"), ""))[:800]
        ]

    return []


def build_argv(
    session_id: Optional[str],
    model: str,
    persona_prompt: str,
    mcp_args: List[str],
    prompt: str,
) -> List[str]:
    """The claude invocation.

    --append-system-prompt is passed on EVERY invocation, resumed included: the
    flag does not survive --resume (measured 2026-08-21). It also keeps the
    persona out of the task prompt, which is what makes the verbatim-operator-
    prompt guarantee possible.

    The `--` before the prompt is load-bearing: --mcp-config is variadic, so
    without it the CLI parses the prompt as another config path.
    """
    argv = ["claude", "-p"]
    if session_id:
        argv += ["--resume", session_id]
    argv += [
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model", model,
        "--append-system-prompt", persona_prompt,
    ]
    argv += list(mcp_args)
    argv += ["--", prompt]
    return argv


@dataclass(frozen=True)
class PassResult:
    rc: int
    session_id: Optional[str]
    limited: bool
    limit_line: str


def run_pass(
    pair: Pair,
    prompt: str,
    session_id: Optional[str],
    persona_prompt: str,
    model: str,
    mcp_args: List[str],
    cwd: str,
    popen: Callable[..., Any] = subprocess.Popen,
) -> PassResult:
    argv = build_argv(session_id, model, persona_prompt, mcp_args, prompt)
    recovered = session_id

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errfile:
        proc = popen(
            argv,
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
            # Take the last session_id seen. Recovered here, as the stream
            # arrives, so it is in hand before the exit code is judged.
            if event.get("session_id"):
                recovered = event["session_id"]
            for out in format_event(event):
                log(out, pair=pair)
        proc.stdout.close()
        rc = proc.wait()

        errfile.seek(0)
        stderr = errfile.read()

    if rc != 0:
        limited = is_usage_limit(stderr)
        line = usage_limit_line(stderr) if limited else ""
        log(f"WARN: claude exited {rc}:", pair=pair)
        # Truncated at the same 400 characters as usage_limit_line above and
        # gh._stderr_tail, and for the same reason: claude's stderr is not a
        # stream that can be assumed credential-free, and one very long line
        # is the shape that carries a token past a reader's eye. Blank lines
        # dropped so a stderr padded with them still shows five real ones.
        for tail in [ln for ln in stderr.splitlines() if ln.strip()][-5:]:
            log(f"  {tail[:400]}", pair=pair)
        return PassResult(rc=rc, session_id=recovered, limited=limited, limit_line=line)

    return PassResult(rc=0, session_id=recovered, limited=False, limit_line="")
