#!/usr/bin/env python3
"""CLI locale de préparation, sans publication."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.alert_fix import core, prepare  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="Préparer un correctif local")
    prepare_parser.add_argument("--ticket", type=Path, required=True)
    prepare_parser.add_argument("--repo", type=Path, required=True)
    prepare_parser.add_argument(
        "--state-dir", type=Path, default=core.default_state_dir()
    )
    prepare_parser.add_argument("--confirm-prepare", action="store_true")
    prepare_parser.add_argument("--trusted-recorded", action="store_true")
    prepare_parser.add_argument("--confirm-ticket-hash", default="")
    prepare_parser.add_argument("--allow-file", action="append", default=[])
    prepare_parser.add_argument(
        "--proposal-source", choices=("recorded", "llm"), default="recorded"
    )
    prepare_parser.add_argument("--recorded-proposals", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "prepare":
        return 2
    if args.proposal_source == "recorded" and args.recorded_proposals is None:
        print("--recorded-proposals est requis pour la source recorded", file=sys.stderr)
        return 2
    return prepare.run_prepare(
        ticket_path=args.ticket,
        repo_path=args.repo,
        state_dir=args.state_dir,
        confirm_prepare=args.confirm_prepare,
        confirm_ticket_hash=args.confirm_ticket_hash,
        allow_files=tuple(args.allow_file),
        proposal_source=args.proposal_source,
        recorded_proposals=args.recorded_proposals,
        trusted_recorded=args.trusted_recorded,
    )


if __name__ == "__main__":
    sys.exit(main())
