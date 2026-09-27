"""Which kin-mcp tools a reviewer may call.

The reviewer's kindex is a private snapshot (graph_snapshot), so a write can
never reach the host graph. Writes are still denied: a reviewer that "saved" a
note would believe in it after the next refresh erased it, and parallel
personas share the snapshot, so they would read each other's notes and lose the
blindness to one another they are built on.

An allowlist, so it fails closed. The Dockerfile writes every tool the pinned
kindex registers to KINDEX_TOOLS_FILE; everything in it that is not below is
passed to --disallowedTools, including a tool a kindex bump adds. The build
also checks that everything below still exists, so a renamed read tool fails
the image rather than silently vanishing.

Classified by reading each tool body in kindex 0.44's mcp_server.py. Four that
look like reads are out: coord_read advances a read cursor, remind_check fires
reminder notifications, stale_check re-hashes files against the container's cwd
and writes demotion markers, and graph_heal works on the raw connection.
search and context do record ranking pheromone in the copy; that is accepted.
"""

from typing import FrozenSet, List

from common import ConfigError

READ_TOOLS: FrozenSet[str] = frozenset({
    "ask",
    "candidate_list",
    "candidate_show",
    "changelog",
    "context",
    "coord_list",
    "graph_stats",
    "list_nodes",
    "mode_list",
    "mode_show",
    "remind_list",
    "search",
    "show",
    "status",
    "suggest",
    "task_get",
    "task_list",
    "watch_list",
})


def deny_args(tools_file: str) -> List[str]:
    """The --disallowedTools flag for every kindex tool not in READ_TOOLS."""
    try:
        with open(tools_file, encoding="utf-8") as fh:
            names = [line.strip() for line in fh if line.strip()]
    except OSError as exc:
        raise ConfigError(f"cannot read the kindex tool list {tools_file}: {exc}")
    if not names:
        raise ConfigError(f"{tools_file} lists no kindex tools")
    denied = sorted(set(names) - READ_TOOLS)
    if not denied:
        return []
    return ["--disallowedTools", ",".join(f"mcp__kindex__{n}" for n in denied)]
