"""Content fingerprints and source manifests; no network access or file mutation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from urllib.parse import urlparse

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SourceManifest:
    dataset_id: str
    source_url: str
    filename: str
    sha256: str
    size_bytes: int
    retrieved_at_utc: datetime
    license_status: str
    schema_version: str
    parser_version: str

    def __post_init__(self) -> None:
        if not self.dataset_id or not self.schema_version or not self.parser_version:
            raise ValueError("dataset_id, schema_version, and parser_version are required")
        url = urlparse(self.source_url)
        if url.scheme != "https" or not url.netloc:
            raise ValueError("source_url must be an absolute HTTPS URL")
        if not self.filename or Path(self.filename).name != self.filename:
            raise ValueError("filename must be a basename")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase 64-character digest")
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        offset = self.retrieved_at_utc.utcoffset()
        if self.retrieved_at_utc.tzinfo is None or offset is None:
            raise ValueError("retrieved_at_utc must be timezone-aware")
        if offset.total_seconds() != 0:
            raise ValueError("retrieved_at_utc must be UTC")
        if not self.license_status.strip():
            raise ValueError("license_status is required")

    def to_json(self) -> str:
        data = asdict(self)
        data["retrieved_at_utc"] = self.retrieved_at_utc.isoformat()
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Self:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("manifest must be a JSON object")
        expected = set(cls.__dataclass_fields__)
        if set(data) != expected:
            raise ValueError("manifest fields do not match the schema")
        try:
            data["retrieved_at_utc"] = datetime.fromisoformat(data["retrieved_at_utc"])
            return cls(**data)
        except (TypeError, KeyError) as exc:
            raise ValueError("invalid manifest field type") from exc

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        dataset_id: str,
        source_url: str,
        retrieved_at_utc: datetime,
        license_status: str,
        schema_version: str,
        parser_version: str,
    ) -> Self:
        if retrieved_at_utc.tzinfo is None or retrieved_at_utc.utcoffset() is None:
            raise ValueError("retrieved_at_utc must be timezone-aware")
        return cls(
            dataset_id=dataset_id,
            source_url=source_url,
            filename=path.name,
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
            retrieved_at_utc=retrieved_at_utc.astimezone(UTC),
            license_status=license_status,
            schema_version=schema_version,
            parser_version=parser_version,
        )
