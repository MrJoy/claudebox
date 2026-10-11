#!/usr/bin/env python3
"""Write schema/claudebox.schema.json from the loader and the persona tree.

The schema is for editors: completion and red squiggles while writing
.github/claudebox.json. The loader in reviewer/repo_config.py is what
decides validity; this is generated from its constants so the two cannot
drift, and tests/test_config_schema.py fails when the committed file is not
what this would write. Run it after adding a persona or a key.

    python3 tools/gen-config-schema.py           # rewrite the file
    python3 tools/gen-config-schema.py --check   # exit 1 if it is stale
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "reviewer"))

import personas  # noqa: E402
import repo_config  # noqa: E402

OUT = os.path.join(ROOT, "schema", "claudebox.schema.json")
NAME = {"type": "string", "pattern": "^[A-Za-z0-9._-]+$"}


def _personas(mode, names):
    # A description, not a pattern: personas.resolve is the one validator, and
    # a hand-written grammar would disagree with it in both directions.
    return {"type": "string",
            "description": f"{mode.capitalize()}-mode personas: a comma or space separated "
                           f"list, or all. Shipped: {', '.join(sorted(names))}."}


def _obj(properties):
    return {"type": "object", "additionalProperties": False, "properties": properties}


def build(persona_dir: str) -> dict:
    code = personas._available(os.path.join(persona_dir, "code"))
    plan = personas._available(os.path.join(persona_dir, "plan"))
    schema = _obj({
        "$schema": {"type": "string", "description": "This schema's URL, for editors. Ignored."},
        "version": {"const": 1, "description": "Config format version. Omitted means 1."},
        "trigger": {"enum": list(repo_config.TRIGGERS),
                    "description": "auto reviews every PR; requested only PRs that ask."},
        "team": dict(NAME, description="Team slug whose review request counts as an ask."),
        "profile": dict(NAME, description="A provider profile the deployment defines."),
        "model": {"type": "string", "pattern": "^\\S+$",
                  "description": "A model the profile allows; omit for its default."},
        "plan": _obj({
            "label": {"type": "string", "pattern": "^\\S(?:.*\\S)?$",
                      "description": "The label that makes a PR a plan review."},
            "personas": _personas("plan", plan),
        }),
        "code": _obj({
            "personas": _personas("code", code),
        }),
        "max_concurrent_personas": {"type": "integer", "minimum": 0,
                                    "description": "A PR's personas at once; 0 is all."},
        "rounds": _obj({
            "cap": {"type": "integer", "minimum": 0,
                    "description": "Automatic rounds before an explicit ask is needed."},
        }),
        "kindex": _obj({
            "store": {"enum": list(repo_config.KINDEX_STORES),
                      "description": "Which kindex store reviewers read."},
        }),
    })
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              "title": "claudebox repo config", **schema}
    return schema


def main(argv) -> int:
    text = json.dumps(build(os.path.join(ROOT, "personas")), indent=2) + "\n"
    if "--check" in argv:
        try:
            with open(OUT, encoding="utf-8") as fh:
                return 0 if fh.read() == text else 1
        except OSError:
            return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
