"""Shared vocabulary for the review loop.

`Pair` replaces the "$pr:$mode:$persona" string key the bash loop used, along
with the ${key%%:*} / ${_rest#*:} unpacking that went with it. As a frozen
dataclass it is hashable, so it is the session map's key directly, and a
persona basename containing a colon can no longer corrupt a key.
"""

import sys
import threading
import time
from dataclasses import dataclass
from typing import NoReturn, Optional, TextIO


class ConfigError(Exception):
    """Startup misconfiguration. Caught at the top level and turned into die()."""


@dataclass(frozen=True, order=True)
class Pair:
    pr: int
    mode: str
    persona: str

    def __str__(self) -> str:
        return f"#{self.pr} {self.mode}/{self.persona}"


# Serializes writes so that concurrent passes (phase B) cannot interleave
# mid-line. Held here rather than in the caller so every log path gets it.
_lock = threading.Lock()


def _stamp() -> str:
    # UTC, matching entrypoint.sh's `date -u +%H:%M:%S`.
    return time.strftime("%H:%M:%S", time.gmtime())


def log(msg: str, pair: Optional[Pair] = None, stream: Optional[TextIO] = None) -> None:
    out = stream if stream is not None else sys.stdout
    prefix = f" [{pair}]" if pair is not None else ""
    with _lock:
        out.write(f"[{_stamp()}]{prefix} {msg}\n")
        out.flush()


def die(msg: str) -> NoReturn:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.stderr.flush()
    raise SystemExit(1)
