"""Strict JSON reading for claudebox's config files, and the problem list.

The repo, org and deployment files are JSON for now (founder ruling,
2026-10-09; the design names YAML). JSON's own parser is lax in exactly the
ways a strict loader cannot afford: it keeps the last of two duplicate keys,
accepts NaN and Infinity, reads `true` back as something Python treats as
the integer 1, and raises RecursionError, which is not a ValueError, on deep
nesting well inside any byte cap. This module closes each of those, and everything it refuses
comes back as one ConfigError that names the file.
"""

import difflib
import json
from typing import Iterable, List, Tuple

from common import ConfigError


def join(path: str, key: str) -> str:
    return key if not path else f"{path}.{key}"


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_str(value) -> bool:
    return isinstance(value, str)


def is_obj(value) -> bool:
    return isinstance(value, dict)


def printable(text: str) -> str:
    """`text` with every character str.isprintable() refuses written as an escape.

    For anything a config file supplies that is then printed: a control
    sequence in a key or a value could otherwise move the cursor, conceal
    lines or forge a verdict on the operator's terminal.
    """
    return "".join(c if c.isprintable() else
                   (f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{ord(c):04x}")
                   for c in text)


class Problems:
    """Every problem in a file, so one run reports all of them at once."""

    def __init__(self) -> None:
        self._items: List[Tuple[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self._items.append((path, message))

    def extend(self, other: "Problems") -> None:
        self._items.extend(other._items)

    def unknown(self, path: str, key: str, allowed: Iterable[str]) -> None:
        close = difflib.get_close_matches(key, list(allowed), n=1)
        hint = f" (did you mean '{close[0]}'?)" if close else ""
        self.add(join(path, key), f"unknown key{hint}")

    def lines(self) -> List[str]:
        # A key name or a value from the file can reach these lines, and the
        # file is the reviewed repo's, so nothing unprintable may reach a
        # terminal through them (PR #17 review).
        return [printable(f"{p}: {m}" if p else m) for p, m in self._items]

    def __bool__(self) -> bool:
        return bool(self._items)


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key '{key}'")
        out[key] = value
    return out


def _no_constants(name):
    raise ValueError(f"{name} is not a number this file accepts")


def read_json(data: bytes, cap: int, label: str) -> dict:
    if len(data) > cap:
        raise ConfigError(f"{label}: is {len(data)} bytes; the cap is {cap} bytes.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{label}: is not UTF-8 ({exc.reason} at byte {exc.start}).")
    try:
        value = json.loads(
            text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{label}: is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}."
        )
    except ValueError as exc:
        raise ConfigError(f"{label}: {exc}.")
    except RecursionError:
        # json's decoder recurses once per nesting level, and RecursionError
        # is not a ValueError. A byte cap does not bound depth: 1 KB of "["
        # is enough, and in phase 2a this file comes from a repo's default
        # branch and the reader is the control plane.
        raise ConfigError(f"{label}: is nested too deeply to read.")
    if not is_obj(value):
        raise ConfigError(f"{label}: must hold a JSON object at the top level.")
    try:
        # A `\ud800` escape is valid JSON and decodes to a lone surrogate, which
        # no UTF-8 output can carry: refused here, so nothing downstream meets it.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigError(f"{label}: holds a lone surrogate escape (\\ud800 to \\udfff), "
                          "which is not a character.")
    return value
