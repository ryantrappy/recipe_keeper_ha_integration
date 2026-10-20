"""Test credential setup without real user inputs or cloud traffic."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from recipe_assistant import __main__ as cli


def test_configure_cloud_writes_private_file_without_echoing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "data/credentials.json"
    monkeypatch.setenv("RECIPE_KEEPER_CREDENTIALS_FILE", str(path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "recipe-assistant",
            "configure-cloud",
            "--env-file",
            str(tmp_path / "missing.env"),
        ],
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        Mock(side_effect=["test@example.invalid", "private-password"]),
    )
    cli.main()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {
        "email": "test@example.invalid",
        "password": "private-password",
    }
    output = capsys.readouterr()
    assert "private-password" not in output.out + output.err
    assert "test@example.invalid" not in output.out + output.err
    monkeypatch.setattr(cli.getpass, "getpass", Mock(side_effect=AssertionError))
    with pytest.raises(SystemExit):
        cli.main()
    assert json.loads(path.read_text())["password"] == "private-password"


def test_configure_cloud_rotates_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "credentials.json"
    path.write_text('{"email":"old@example.invalid","password":"old"}')
    monkeypatch.setenv("RECIPE_KEEPER_CREDENTIALS_FILE", str(path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "recipe-assistant",
            "configure-cloud",
            "--replace-credentials",
            "--env-file",
            str(tmp_path / "missing.env"),
        ],
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        Mock(side_effect=["new@example.invalid", "new-password"]),
    )
    cli.main()
    assert json.loads(path.read_text())["password"] == "new-password"
    assert path.stat().st_mode & 0o777 == 0o600


def test_inspect_cloud_does_not_contact_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECIPE_KEEPER_CACHE", str(tmp_path / "cache.db"))
    monkeypatch.setenv("RECIPE_KEEPER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("RECIPE_KEEPER_PASSWORD", "private-password")
    monkeypatch.setattr(
        "sys.argv",
        [
            "recipe-assistant",
            "inspect",
            "--backend",
            "cloud",
            "--env-file",
            str(tmp_path / "missing.env"),
        ],
    )
    monkeypatch.setattr(cli.CloudSession, "page", Mock(side_effect=AssertionError))
    cli.main()


def test_cli_refuses_foreign_cache_without_modifying_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE Existing (value TEXT)")
    original = path.read_bytes()
    monkeypatch.setenv("RECIPE_KEEPER_CACHE", str(path))
    monkeypatch.setenv("RECIPE_KEEPER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("RECIPE_KEEPER_PASSWORD", "private-password")
    monkeypatch.setattr(
        "sys.argv",
        [
            "recipe-assistant",
            "inspect",
            "--backend",
            "cloud",
            "--env-file",
            str(tmp_path / "missing.env"),
        ],
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert path.read_bytes() == original
