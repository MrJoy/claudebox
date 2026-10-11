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
import repo_config
from common import ConfigError


def _read(path: str, role: str, stderr) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        stderr.write(f"ERROR: cannot read {role} {path}: {exc.strerror}\n")
        return None


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
        stderr.write(f"ERROR: {line}\n")
        failed = True
    if res.repo_problems:
        if res.config is None:
            stderr.write("ERROR: a hosted repo with this file and this org file is not reviewed\n")
        elif res.source == "org":
            stderr.write("ERROR: a hosted repo with this file falls back to the org file\n")
        else:
            stderr.write("ERROR: a hosted repo with this file falls back to the built-in defaults\n")

    check = None
    if args.deploy:
        deploy_data = _read(args.deploy, "deploy file", stderr)
        if deploy_data is None:
            return 1
        try:
            deploy = deploy_config.load_deploy(deploy_data)
        except deploy_config.DeployError as exc:
            for line in exc.lines:
                stderr.write(f"ERROR: {line}\n")
            return 1
        except ConfigError as exc:
            stderr.write(f"ERROR: {exc}\n")
            return 1
        if res.config is not None and not failed:
            check = deploy_config.against_deploy(res.config, deploy, args.repo_name)
            for line in check.problems:
                stderr.write(f"ERROR: against deploy file: {line}\n")
                failed = True

    if failed:
        return 1

    scope = "" if check is not None else "; deployment not checked"
    stdout.write(f"OK: config is valid (source: {res.source}{scope})\n")
    for f in fields(repo_config.RepoConfig):
        value = getattr(res.config, f.name)
        stdout.write(f"  {f.name}: {'-' if value is None else value}\n")
    if check is not None:
        stdout.write(f"  effective model: {check.model}\n")
        for item in check.unchecked:
            stdout.write(f"NOT CHECKED HERE: {item}\n")
    else:
        stdout.write("NOT CHECKED HERE: profile, model, team and kindex.store are checked "
                     "only against a deployment (pass --deploy)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
