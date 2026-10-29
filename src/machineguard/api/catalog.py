"""Small SQLite index of verified local artifacts, never a raw-path HTTP interface."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from machineguard.anomaly.pipeline import verify_baselines
from machineguard.core.provenance import sha256_file
from machineguard.prognostics.benchmark import verify_benchmark
from machineguard.storage.parquet import verify_dataset

KINDS = ("dataset", "baseline", "rul")


class CatalogError(ValueError):
    """An artifact or catalog failed a trust/compatibility check."""


class Catalog:
    def __init__(self, root: Path, database: Path) -> None:
        self.root = root.resolve()
        self.database = database.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='artifacts'"
                ).fetchone():
                    raise CatalogError("unversioned artifact table cannot be migrated implicitly")
                connection.execute(
                    "CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                    "relative_path TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, "
                    "summary_json TEXT NOT NULL, UNIQUE(kind, relative_path))"
                )
                connection.execute("PRAGMA user_version = 1")
            elif version != 1:
                raise CatalogError(f"unsupported catalog schema version {version}")
            else:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
                }
                if columns != {
                    "artifact_id",
                    "kind",
                    "relative_path",
                    "manifest_sha256",
                    "summary_json",
                }:
                    raise CatalogError("catalog schema does not match version 1")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=5)

    def _path(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root) or candidate == self.root:
            raise CatalogError("artifact path escapes configured root")
        return candidate

    @staticmethod
    def _no_symlinks(path: Path) -> None:
        if any(item.is_symlink() for item in path.rglob("*")):
            raise CatalogError("artifact contains a symlink")

    def _verify(self, kind: str, path: Path) -> dict[str, Any]:
        if kind == "dataset":
            return verify_dataset(path)
        if kind == "baseline":
            manifest = verify_baselines(path)
            evaluation = json.loads((path / "evaluation.json").read_text(encoding="utf-8"))
            if not isinstance(evaluation, dict) or not all(
                key in evaluation for key in ("metrics", "reported_windows", "metric_caveat")
            ):
                raise CatalogError("baseline evaluation is incomplete")
            return manifest
        if kind == "rul":
            report = verify_benchmark(path)
            if not isinstance(report.get("subsets"), dict) or any(
                not isinstance(item, dict)
                or "validation" not in item
                or "publisher_test" not in item
                for item in report["subsets"].values()
            ):
                raise CatalogError("RUL report is incomplete")
            return report
        raise CatalogError("unsupported artifact kind")

    def register(self, kind: str, path: Path) -> dict[str, Any]:
        if kind not in KINDS:
            raise CatalogError("unsupported artifact kind")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise CatalogError("artifact must be under configured root")
        try:
            self._no_symlinks(resolved)
            verified = self._verify(kind, resolved)
            digest = sha256_file(resolved / "manifest.json")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CatalogError(f"artifact verification failed: {exc}") from exc
        relative = resolved.relative_to(self.root).as_posix()
        identity = hashlib.sha256(f"{kind}:{relative}:{digest}".encode()).hexdigest()[:24]
        if kind == "dataset":
            summary = {
                "dataset_id": verified["source"]["dataset_id"],
                "source_sha256": verified["source"]["sha256"],
                "rows": verified["quality"]["rows_written"],
                "timezone_policy": "source_naive_unresolved",
            }
        elif kind == "baseline":
            summary = {
                "version_id": verified["version_id"],
                "feature_manifest_sha256": verified["identity"]["feature_manifest_sha256"],
                "evidence_status": "descriptive_anomaly_only",
            }
        else:
            summary = {
                "dataset_id": "nasa-cmapss-classic",
                "archive_sha256": verified["archive_sha256"],
                "evidence_status": "simulated_engine_cycle_rul",
            }
        record = {
            "artifact_id": identity,
            "kind": kind,
            "manifest_sha256": digest,
            "summary": summary,
        }
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT artifact_id FROM artifacts WHERE kind=? AND relative_path=?",
                    (kind, relative),
                ).fetchone()
                if existing and existing[0] != identity:
                    raise CatalogError("artifact path changed after registration")
                connection.execute(
                    "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?)",
                    (identity, kind, relative, digest, json.dumps(summary, sort_keys=True)),
                )
        except sqlite3.Error as exc:
            raise CatalogError(f"catalog write failed: {exc}") from exc
        return record

    def list(self, kind: str | None, offset: int, limit: int) -> dict[str, Any]:
        if kind is not None and kind not in KINDS:
            raise CatalogError("unsupported artifact kind")
        if offset < 0 or not 1 <= limit <= 100:
            raise CatalogError("invalid pagination")
        clause = " WHERE kind=?" if kind else ""
        parameters: tuple[str, ...] = (kind,) if kind else ()
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM artifacts" + clause, parameters
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT artifact_id, kind, manifest_sha256, summary_json FROM artifacts"
                + clause
                + " ORDER BY kind, artifact_id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    "artifact_id": row[0],
                    "kind": row[1],
                    "manifest_sha256": row[2],
                    "summary": json.loads(row[3]),
                }
                for row in rows
            ],
            "offset": offset,
            "limit": limit,
            "total": count,
        }

    def get(self, kind: str, artifact_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        if (
            kind not in KINDS
            or len(artifact_id) != 24
            or any(character not in "0123456789abcdef" for character in artifact_id)
        ):
            raise CatalogError("unknown artifact")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT relative_path, manifest_sha256, summary_json FROM artifacts "
                "WHERE kind=? AND artifact_id=?",
                (kind, artifact_id),
            ).fetchone()
        if row is None:
            raise CatalogError("unknown artifact")
        path = self._path(row[0])
        try:
            self._no_symlinks(path)
            if sha256_file(path / "manifest.json") != row[1]:
                raise CatalogError("registered manifest changed")
            verified = self._verify(kind, path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CatalogError(f"registered artifact failed verification: {exc}") from exc
        return (
            path,
            verified,
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "manifest_sha256": row[1],
                "summary": json.loads(row[2]),
            },
        )
