#!/usr/bin/env python3
"""Import advocate's persona prompts into claudebox persona definition files.

Run once, commit the output. Re-run when advocate's prompts change:

    ./tools/import-advocate-personas.py ~/wander/advocate/src/advocate/personas.py personas

advocate's personas.py is parsed with `ast` rather than imported, because
importing it pulls in pydantic. Every SYSTEM_PROMPTS entry is an f-string whose
only interpolation is _COMMON_OUTPUT_FORMAT, so dropping every interpolation is
exactly the transformation we want: it removes advocate's JSON output contract
(claudebox posts gh comments, not JSON) and keeps the persona identity. The
assertion below fails loudly if that stops being true upstream.
"""
import ast
import pathlib
import sys

src = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
tree = ast.parse(src.read_text())


def literal_text(node):
    """The literal parts of a string or f-string, interpolations dropped."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    raise TypeError(ast.dump(node)[:120])


def enum_id(node):
    assert isinstance(node, ast.Attribute), ast.dump(node)[:120]
    return node.attr


meta, prompts = {}, {}
for stmt in tree.body:
    if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
        continue
    if stmt.target.id == "PERSONA_META":
        for k, v in zip(stmt.value.keys, stmt.value.values):
            fields = {kk.value: vv for kk, vv in zip(v.keys, v.values)}
            meta[enum_id(k)] = {
                "label": literal_text(fields["name"]),
                "success": literal_text(fields["success"]),
            }
    elif stmt.target.id == "SYSTEM_PROMPTS":
        for k, v in zip(stmt.value.keys, stmt.value.values):
            prompts[enum_id(k)] = literal_text(v).strip()

assert set(meta) == set(prompts), (sorted(meta), sorted(prompts))
out.mkdir(parents=True, exist_ok=True)
for pid in sorted(meta):
    body = prompts[pid]
    assert "JSON" not in body, f"{pid}: advocate's output contract survived extraction"
    (out / f"{pid}.md").write_text(
        "---\n"
        f"label: {meta[pid]['label']}\n"
        f"success: {meta[pid]['success']}\n"
        "---\n"
        f"{body}\n"
    )
    print(f"wrote {pid}.md  label={meta[pid]['label']!r}  {len(body)} chars")
