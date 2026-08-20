#!/usr/bin/env python3
"""Low-privilege agent adapter for texpage-bridge.

This wrapper deliberately exposes only the build/request/status plane. It never
loads projects.json, broker state, browser profiles, credentials, or signed
artifact URLs itself; those stay behind texpage_bridge.py and the broker.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


def _project(value: str) -> str:
    if not PROJECT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "project alias must contain only letters, digits, '.', '_' or '-'"
        )
    return value


def _request_id(value: str) -> str:
    if not REQUEST_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid request id")
    return value


def _bounded_int(name: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="texpage-agent",
        description="Low-privilege agent adapter for texpage-bridge",
    )
    parser.add_argument(
        "--bridge-home",
        help="texpage-bridge checkout; defaults to TEXPAGE_BRIDGE_HOME or auto-discovery",
    )
    parser.add_argument("project", type=_project, help="allow-listed project alias")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("build", "submit and wait for the TeXPage build result"),
        ("submit", "submit an asynchronous TeXPage build request"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--timeout",
            type=_bounded_int("timeout", 1, 7200),
            default=240,
            help="compile timeout in seconds (1..7200)",
        )
        command.add_argument(
            "--no-push",
            action="store_true",
            help="compile the currently selected TeXPage version without a Git push",
        )

    request = sub.add_parser("request", help="show one central build request")
    request.add_argument("request_id", type=_request_id)

    requests = sub.add_parser("requests", help="list recent central build requests")
    requests.add_argument(
        "--limit",
        type=_bounded_int("limit", 1, 100),
        default=20,
        help="number of requests to list (1..100)",
    )

    sub.add_parser("status", help="show the last successful local build record")
    return parser


def resolve_bridge_home(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    elif os.environ.get("TEXPAGE_BRIDGE_HOME"):
        candidates.append(Path(os.environ["TEXPAGE_BRIDGE_HOME"]).expanduser())
    else:
        here = Path(__file__).resolve()
        candidates.extend(here.parents)

    for candidate in candidates:
        home = candidate.resolve()
        if (home / "texpage_bridge.py").is_file():
            return home

    source = "--bridge-home/TEXPAGE_BRIDGE_HOME" if explicit or os.environ.get("TEXPAGE_BRIDGE_HOME") else "parent directories"
    raise RuntimeError(
        f"texpage-bridge checkout not found via {source}; set TEXPAGE_BRIDGE_HOME"
    )


def command_for(args: argparse.Namespace, bridge_home: Path) -> list[str]:
    command = [
        sys.executable,
        str(bridge_home / "texpage_bridge.py"),
        args.project,
        args.command,
    ]
    if args.command in {"build", "submit"}:
        command.extend(["--timeout", str(args.timeout)])
        if args.no_push:
            command.append("--no-push")
    elif args.command == "request":
        command.append(args.request_id)
    elif args.command == "requests":
        command.extend(["--limit", str(args.limit)])
    return command


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bridge_home = resolve_bridge_home(args.bridge_home)
    except RuntimeError as exc:
        print(f"TEXPAGE AGENT ERROR: {exc}", file=sys.stderr)
        return 3

    completed = subprocess.run(command_for(args, bridge_home), cwd=bridge_home, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
