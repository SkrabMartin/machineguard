"""Provenance tests use explicitly synthetic local bytes."""

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from machineguard.core.provenance import SourceManifest, sha256_file


def _manifest(path: Path) -> SourceManifest:
    return SourceManifest.from_file(
        path,
        dataset_id="synthetic-test-source",
        source_url="https://example.org/test.bin",
        retrieved_at_utc=datetime(2026, 10, 27, tzinfo=UTC),
        license_status="synthetic fixture",
        schema_version="1",
        parser_version="1",
    )


def test_hash_file_streaming_and_manifest_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"synthetic source bytes\x00" * 100)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert sha256_file(path, chunk_size=7) == expected
    manifest = _manifest(path)
    assert manifest.sha256 == expected
    assert manifest.size_bytes == path.stat().st_size
    encoded = manifest.to_json()
    assert SourceManifest.from_json(encoded) == manifest
    assert SourceManifest.from_json(encoded).to_json() == encoded


def test_reject_bad_manifest_data(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"test")
    manifest = _manifest(path)
    with pytest.raises(ValueError, match="sha256"):
        SourceManifest(
            **{
                **json.loads(manifest.to_json()),
                "sha256": "bad",
                "retrieved_at_utc": manifest.retrieved_at_utc,
            }
        )
    with pytest.raises(ValueError, match="HTTPS"):
        SourceManifest(
            **{
                **json.loads(manifest.to_json()),
                "source_url": "http://example.org",
                "retrieved_at_utc": manifest.retrieved_at_utc,
            }
        )
    with pytest.raises(ValueError, match="UTC"):
        SourceManifest(
            **{
                **json.loads(manifest.to_json()),
                "retrieved_at_utc": datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2))),
            }
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        SourceManifest(
            **{**json.loads(manifest.to_json()), "retrieved_at_utc": datetime(2026, 1, 1)}
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        SourceManifest.from_file(
            path,
            dataset_id="synthetic-test-source",
            source_url="https://example.org/test.bin",
            retrieved_at_utc=datetime(2026, 10, 27),
            license_status="synthetic fixture",
            schema_version="1",
            parser_version="1",
        )
    with pytest.raises(ValueError, match="schema"):
        SourceManifest.from_json("{}")
    with pytest.raises(ValueError, match="chunk_size"):
        sha256_file(path, chunk_size=0)
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "missing")
