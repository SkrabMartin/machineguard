"""Small CLI for Phase 1 contract inspection and local file fingerprinting."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from machineguard.core.catalog import DATASET_CONTRACTS
from machineguard.core.provenance import sha256_file


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="machineguard", description="MachineGuard foundation CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    contracts = subcommands.add_parser("contracts", help="Show observed source contracts")
    contracts.add_argument("dataset_id", nargs="?", choices=sorted(DATASET_CONTRACTS))
    fingerprint = subcommands.add_parser("sha256", help="Hash a local source file")
    fingerprint.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "contracts":
        selected = (
            {args.dataset_id: DATASET_CONTRACTS[args.dataset_id]}
            if args.dataset_id
            else DATASET_CONTRACTS
        )
        print(json.dumps({name: asdict(contract) for name, contract in selected.items()}, indent=2))
        return 0
    if args.command == "sha256":
        try:
            print(sha256_file(args.path))
        except OSError as exc:
            parser.exit(2, f"machineguard: cannot read {args.path}: {exc}\n")
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
