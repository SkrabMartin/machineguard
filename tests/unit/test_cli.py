"""CLI smoke tests with synthetic bytes and observed contract metadata."""

import hashlib
import json
from pathlib import Path

import pytest

from machineguard.cli import main


def test_contracts_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["contracts", "metropt-3-uci"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"metropt-3-uci"}
    assert len(payload["metropt-3-uci"]["fields"]) == 17


def test_sha256_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "synthetic.txt"
    path.write_text("synthetic test data", encoding="utf-8")
    assert main(["sha256", str(path)]) == 0
    assert capsys.readouterr().out.strip() == hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit) as error:
        main(["sha256", str(tmp_path / "missing")])
    assert error.value.code == 2
