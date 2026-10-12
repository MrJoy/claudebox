#!/usr/bin/env python3
"""`claudebox.sh config validate`: check a repo config file, offline.

Run inside the image (founder ruling, 2026-10-09): the launcher mounts the
files read-only into a throwaway, network-less container and calls this.
Without --deploy it checks the schema alone, which anyone can run. With
--deploy it also checks the effective config against the deployment, and
prints what only the control plane can check rather than passing it.
"""

import argparse
import os
import sys
from dataclasses import fields
from typing import Optional, Sequence

import deploy_config
from config_load import printable
import repo_config
from common import ConfigError


def _read(path: str, role: str, stderr) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        say(stderr, f"ERROR: cannot read {role} {path}: {exc.strerror}")
        return None


def say(stream, text: str) -> None:
    """Write one line. Every line can carry text from a file the reviewed
    repo wrote, so nothing unprintable reaches the terminal (PR #17 review)."""
    stream.write(printable(text) + "\n")


def main(argv: Sequence[str], stdout=sys.stdout, stderr=sys.stderr,
         persona_dir: Optional[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="claudebox.sh config")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate")
    v.add_argument("file")
    v.add_argument("--org")
    v.add_argument("--deploy")
    v.add_argument("--repo-name")
    args = parser.parse_args(list(argv))
    persona_dir = persona_dir or os.environ.get("PERSONA_DIR") or "/opt/claudebox/personas"

    repo_data = _read(args.file, "repo file", stderr)
    org_data = _read(args.org, "org file", stderr) if args.org else None
    if repo_data is None or (args.org and org_data is None):
        return 1

    res = repo_config.resolve(org_data, repo_data, persona_dir)
    failed = False
    for line in res.repo_problems + res.org_problems:
        say(stderr, f"ERROR: {line}")
        failed = True
    if res.repo_problems:
        if res.config is None:
            say(stderr, "ERROR: a hosted repo with this file and this org file is not reviewed")
        elif res.source == "org":
            say(stderr, "ERROR: a hosted repo with this file falls back to the org file")
        else:
            say(stderr, "ERROR: a hosted repo with this file falls back to the built-in defaults")

    check = None
    if args.deploy:
        deploy_data = _read(args.deploy, "deploy file", stderr)
        if deploy_data is None:
            return 1
        try:
            deploy = deploy_config.load_deploy(deploy_data)
        except deploy_config.DeployError as exc:
            for line in exc.lines:
                say(stderr, f"ERROR: {line}")
            return 1
        except ConfigError as exc:
            say(stderr, f"ERROR: {exc}")
            return 1
        if res.config is not None and not failed:
            check = deploy_config.against_deploy(res.config, deploy, args.repo_name)
            for line in check.problems:
                say(stderr, f"ERROR: against deploy file: {line}")
                failed = True

    if failed:
        return 1

    scope = "" if check is not None else "; deployment not checked"
    say(stdout, f"OK: config is valid (source: {res.source}{scope})")
    for f in fields(repo_config.RepoConfig):
        value = getattr(res.config, f.name)
        say(stdout, f"  {f.name}: {'-' if value is None else value}")
    if check is not None:
        say(stdout, f"  effective model: {check.model}")
        for item in check.unchecked:
            say(stdout, f"NOT CHECKED HERE: {item}")
    else:
        stdout.write("NOT CHECKED HERE: profile, model, team and kindex.store are checked "
                     "only against a deployment (pass --deploy)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
