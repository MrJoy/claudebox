"""MCP server flags for AGENT=codex, and the two programs Codex runs for them.

Codex gets kindex and Linear as `-c mcp_servers.*` flags (build_args). The
servers' keys must never sit in Codex's environment or argv, where a pass's
shell could read them (spec measurements 3 to 5), so this same file is also
what Codex executes:

  codex_mcp.py kindex DIR KIN_MCP    the kindex stdio server command. Reads
                                     DIR/voyage.key, then execs kin-mcp with
                                     the key in kin-mcp's environment only.
  codex_mcp.py linear-headers DIR    Linear's http_headers_helper. Prints the
                                     Authorization header JSON from
                                     DIR/linear.key.

The key files are mode 600 and written by the entrypoint. This file runs on
every pass as the reviewer user, so it must stay root-owned like the rest of
reviewer/: a copy the reviewer could write would be run by the next pass.

kindex is required (`required = true`) and Linear is not, because a server that
fails to start fails the pass only when required (spec, "A server that fails
to start"): the graph is promised in the prompt, a ticket lookup is a bonus.

Stdlib plus the small sibling modules it imports; nothing here starts a process
or touches the network at import time.
"""

import json
import os
import sys
from typing import List

import codex_pass
import graph_snapshot
import kindex_tools
from common import ConfigError

LINEAR_URL = "https://mcp.linear.app/mcp"
LINEAR_KEY_FILE = "linear.key"
VOYAGE_KEY_FILE = "voyage.key"


def build_args(mcp_dir: str, kindex: bool, linear: bool, kin_mcp_bin: str,
               python: str = sys.executable,
               helper: str = os.path.abspath(__file__)) -> List[str]:
    """The `-c` flags for whichever servers are on, [] when neither is."""
    pairs: List[str] = []
    if kindex:
        pairs += [
            "mcp_servers.kindex.command=" + codex_pass.toml_string(python),
            "mcp_servers.kindex.args=" + codex_pass.toml_array(
                [helper, "kindex", mcp_dir, kin_mcp_bin]),
            "mcp_servers.kindex.enabled_tools=" + codex_pass.toml_array(
                sorted(kindex_tools.READ_TOOLS)),
            "mcp_servers.kindex.required=true",
        ]
    if linear:
        # Codex splits http_headers_helper on spaces (measured), so a space in
        # any part would run a different program.
        for part in (python, helper, mcp_dir):
            if any(c.isspace() for c in part):
                raise ConfigError(
                    f"cannot build Linear's header helper command: {part!r} contains whitespace")
        pairs += [
            "mcp_servers.linear.url=" + codex_pass.toml_string(LINEAR_URL),
            "mcp_servers.linear.http_headers_helper=" + codex_pass.toml_string(
                f"{python} {helper} linear-headers {mcp_dir}"),
        ]
    args: List[str] = []
    for p in pairs:
        args += ["-c", p]
    return args


def _read_key(mcp_dir: str, name: str) -> str:
    """The key file's text minus at most one trailing newline, or ''."""
    try:
        with open(os.path.join(mcp_dir, name)) as f:
            text = f.read()
    except OSError:
        return ""
    return text[:-1] if text.endswith("\n") else text


def _linear_headers(mcp_dir: str) -> int:
    key = _read_key(mcp_dir, LINEAR_KEY_FILE)
    if not key:
        sys.stderr.write(f"codex_mcp: no key in {os.path.join(mcp_dir, LINEAR_KEY_FILE)}\n")
        return 1
    sys.stdout.write(json.dumps({"Authorization": "Bearer " + key}))
    return 0


def _kindex(mcp_dir: str, kin_mcp: str) -> int:
    env = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
    env["KIN_PROFILE"] = graph_snapshot.CONTAINER_PROFILE
    voyage = _read_key(mcp_dir, VOYAGE_KEY_FILE)
    if voyage:
        env["VOYAGE_API_KEY"] = voyage
    os.execve(kin_mcp, [kin_mcp], env)
    return 1  # execve returns only by raising; here for the type checker


def main(argv: List[str]) -> int:
    if len(argv) == 3 and argv[0] == "kindex":
        return _kindex(argv[1], argv[2])
    if len(argv) == 2 and argv[0] == "linear-headers":
        return _linear_headers(argv[1])
    sys.stderr.write("usage: codex_mcp.py kindex DIR KIN_MCP | linear-headers DIR\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
