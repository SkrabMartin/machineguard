"""Small CLI for Phase 1 contract inspection and local file fingerprinting."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from machineguard import __version__
from machineguard.core.catalog import DATASET_CONTRACTS
from machineguard.core.provenance import SourceManifest, sha256_file
from machineguard.features.pipeline import build_features, verify_features
from machineguard.ingestion.pipeline import ingest_metropt
from machineguard.storage.parquet import verify_dataset


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="machineguard", description="MachineGuard foundation CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    contracts = subcommands.add_parser("contracts", help="Show observed source contracts")
    contracts.add_argument("dataset_id", nargs="?", choices=sorted(DATASET_CONTRACTS))
    fingerprint = subcommands.add_parser("sha256", help="Hash a local source file")
    fingerprint.add_argument("path", type=Path)
    source = subcommands.add_parser(
        "source-manifest", help="Record a local file's source provenance"
    )
    source.add_argument("path", type=Path)
    source.add_argument("--source-url", required=True)
    source.add_argument("--license-status", required=True)
    source.add_argument("--retrieved-at-utc", required=True)
    ingest = subcommands.add_parser(
        "ingest-metropt", help="Validate and version MetroPT CSV/Parquet"
    )
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--source-manifest", required=True, type=Path)
    ingest.add_argument("--output-root", required=True, type=Path)
    inspect = subcommands.add_parser(
        "inspect-dataset", help="Verify and summarize a dataset version"
    )
    inspect.add_argument("version_dir", type=Path)
    features = subcommands.add_parser("build-features", help="Build causal MetroPT features")
    features.add_argument("input_version", type=Path)
    features.add_argument("--output-root", type=Path, required=True)
    features.add_argument("--spec", type=Path, default=Path("configs/metropt_feature_spec.json"))
    features.add_argument("--split", type=Path, default=Path("configs/metropt_anomaly_split.json"))
    features.add_argument(
        "--report-windows", type=Path, default=Path("configs/metropt_report_windows.json")
    )
    feature_inspect = subcommands.add_parser("inspect-features", help="Verify a feature version")
    feature_inspect.add_argument("version_dir", type=Path)
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
    if args.command == "source-manifest":
        try:
            retrieved_at = datetime.fromisoformat(args.retrieved_at_utc)
            manifest = SourceManifest.from_file(
                args.path,
                dataset_id="metropt-3-uci",
                source_url=args.source_url,
                retrieved_at_utc=retrieved_at,
                license_status=args.license_status,
                schema_version="1",
                parser_version=__version__,
            )
            print(manifest.to_json())
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: source manifest failed: {exc}\n")
        return 0
    if args.command == "ingest-metropt":
        try:
            manifest = SourceManifest.from_json(args.source_manifest.read_text(encoding="utf-8"))
            version_dir = ingest_metropt(args.path, args.output_root, manifest)
            result = verify_dataset(version_dir)
            print(json.dumps({"version_dir": str(version_dir), "quality": result["quality"]}))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: ingest failed: {exc}\n")
        return 0
    if args.command == "inspect-dataset":
        try:
            result = verify_dataset(args.version_dir)
            print(json.dumps({"version_id": result["version_id"], "quality": result["quality"]}))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: inspect failed: {exc}\n")
        return 0
    if args.command == "build-features":
        try:
            path = build_features(
                args.input_version, args.output_root, args.spec, args.split, args.report_windows
            )
            result = verify_features(path)
            print(
                json.dumps(
                    {
                        "version_dir": str(path),
                        "rows": result["rows"],
                        "split_counts": result["split_counts"],
                    }
                )
            )
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: feature build failed: {exc}\n")
        return 0
    if args.command == "inspect-features":
        try:
            result = verify_features(args.version_dir)
            print(
                json.dumps(
                    {
                        "version_id": result["version_id"],
                        "rows": result["rows"],
                        "split_counts": result["split_counts"],
                    }
                )
            )
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: inspect features failed: {exc}\n")
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
