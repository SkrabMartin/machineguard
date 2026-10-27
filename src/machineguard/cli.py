"""Small CLI for Phase 1 contract inspection and local file fingerprinting."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from machineguard import __version__
from machineguard.anomaly.multivariate import run_multivariate, verify_multivariate
from machineguard.anomaly.pipeline import run_baselines, verify_baselines
from machineguard.core.catalog import DATASET_CONTRACTS
from machineguard.core.provenance import SourceManifest, sha256_file
from machineguard.features.pipeline import build_features, verify_features
from machineguard.ingestion.pipeline import ingest_metropt
from machineguard.risk.feasibility import verify_risk_audit, write_risk_audit
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
    anomaly = subcommands.add_parser(
        "fit-anomaly-baselines", help="Fit train-only statistical anomaly baselines"
    )
    anomaly.add_argument("feature_version", type=Path)
    anomaly.add_argument("--output-root", type=Path, required=True)
    anomaly.add_argument(
        "--policy", type=Path, default=Path("configs/metropt_anomaly_baseline.json")
    )
    anomaly.add_argument(
        "--report-windows", type=Path, default=Path("configs/metropt_report_windows.json")
    )
    anomaly_inspect = subcommands.add_parser(
        "inspect-anomaly-baselines", help="Verify a statistical baseline artifact"
    )
    anomaly_inspect.add_argument("version_dir", type=Path)
    multivariate = subcommands.add_parser(
        "fit-isolation-forest", help="Fit and compare state-conditional Isolation Forest"
    )
    multivariate.add_argument("feature_version", type=Path)
    multivariate.add_argument("--baseline-version", type=Path, required=True)
    multivariate.add_argument("--output-root", type=Path, required=True)
    multivariate.add_argument(
        "--policy", type=Path, default=Path("configs/metropt_isolation_forest.json")
    )
    multivariate.add_argument(
        "--baseline-policy", type=Path, default=Path("configs/metropt_anomaly_baseline.json")
    )
    multivariate.add_argument(
        "--report-windows", type=Path, default=Path("configs/metropt_report_windows.json")
    )
    multivariate_inspect = subcommands.add_parser(
        "inspect-isolation-forest", help="Verify a multivariate comparison artifact"
    )
    multivariate_inspect.add_argument("version_dir", type=Path)
    risk = subcommands.add_parser(
        "audit-risk-feasibility", help="Audit fault-report evidence without fitting a risk model"
    )
    risk.add_argument("feature_version", type=Path)
    risk.add_argument("--output", type=Path, required=True)
    risk.add_argument("--policy", type=Path, default=Path("configs/metropt_risk_feasibility.json"))
    risk.add_argument(
        "--report-windows", type=Path, default=Path("configs/metropt_report_windows.json")
    )
    risk_inspect = subcommands.add_parser(
        "inspect-risk-audit", help="Recompute and verify a risk feasibility audit"
    )
    risk_inspect.add_argument("path", type=Path)
    risk_inspect.add_argument("--feature-version", type=Path, required=True)
    risk_inspect.add_argument(
        "--policy", type=Path, default=Path("configs/metropt_risk_feasibility.json")
    )
    risk_inspect.add_argument(
        "--report-windows", type=Path, default=Path("configs/metropt_report_windows.json")
    )
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
    if args.command == "fit-anomaly-baselines":
        try:
            path = run_baselines(
                args.feature_version, args.output_root, args.policy, args.report_windows
            )
            result = verify_baselines(path)
            print(json.dumps({"version_dir": str(path), "version_id": result["version_id"]}))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: anomaly baseline failed: {exc}\n")
        return 0
    if args.command == "inspect-anomaly-baselines":
        try:
            result = verify_baselines(args.version_dir)
            print(json.dumps({"version_id": result["version_id"]}))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: inspect baseline failed: {exc}\n")
        return 0
    if args.command == "fit-isolation-forest":
        try:
            path = run_multivariate(
                args.feature_version,
                args.baseline_version,
                args.output_root,
                args.policy,
                args.baseline_policy,
                args.report_windows,
            )
            result = verify_multivariate(path)
            print(json.dumps({"version_dir": str(path), "version_id": result["version_id"]}))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: Isolation Forest failed: {exc}\n")
        return 0
    if args.command == "inspect-isolation-forest":
        try:
            result = verify_multivariate(args.version_dir)
            print(json.dumps({"version_id": result["version_id"]}))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: inspect Isolation Forest failed: {exc}\n")
        return 0
    if args.command == "audit-risk-feasibility":
        try:
            path = write_risk_audit(
                args.feature_version, args.policy, args.report_windows, args.output
            )
            print(json.dumps({"path": str(path), "status": "risk_unavailable"}))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: risk feasibility audit failed: {exc}\n")
        return 0
    if args.command == "inspect-risk-audit":
        try:
            result = verify_risk_audit(
                args.path, args.feature_version, args.policy, args.report_windows
            )
            print(json.dumps({"status": result["status"], "failed_gates": result["failed_gates"]}))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"machineguard: risk audit verification failed: {exc}\n")
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
