"""Fresh authentication tests use synthetic credentials and mocked HTTPS."""

import io
import json
import warnings
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest

from recipe_assistant import cloud_probe
from recipe_assistant.cloud_probe import Authentication, ProbeError, fresh_login

EMAIL = "test@example.invalid"
PASSWORD = "synthetic password+with&symbols"
TOKEN = "synthetic-private-access-token"
TOKEN_RESPONSE = {
    "access_token": TOKEN,
    "token_type": "bearer",
    "expires_in": 3600,
    ".issued": "Mon, 19 Oct 2026 20:00:00 GMT",
    ".expires": "Mon, 19 Oct 2026 21:00:00 GMT",
}


def mock_login_response(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> MagicMock:
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = raw
    monkeypatch.setattr(cloud_probe, "build_opener", Mock(return_value=opener))
    return opener


def test_fresh_login_uses_entered_credentials_and_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = mock_login_response(monkeypatch, json.dumps(TOKEN_RESPONSE).encode())
    result = fresh_login(EMAIL, PASSWORD)
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://recipekeeper.azurewebsites.net/token"
    assert parse_qs(request.data.decode()) == {
        "username": [EMAIL],
        "password": [PASSWORD],
        "grant_type": ["password"],
    }
    assert request.get_header("Authorization") is None
    assert result.token == TOKEN
    assert result.summary() == {
        "fresh_login": "succeeded",
        "access_token_received": True,
        "expires_in_seconds": 3600,
        "issued_at_utc": "2026-10-19T20:00:00+00:00",
        "expires_at_utc": "2026-10-19T21:00:00+00:00",
        "refresh_token_returned": False,
        "credentials_saved": False,
    }
    assert TOKEN not in repr(result)
    assert TOKEN not in json.dumps(result.summary())
    assert PASSWORD not in json.dumps(result.summary())


@pytest.mark.parametrize("status", [400, 401, 429, 302, 500])
def test_login_errors_omit_server_body_and_do_not_retry(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    opener = mock_login_response(monkeypatch, b"")
    opener.open.side_effect = HTTPError(
        cloud_probe.LOGIN_URL,
        status,
        "error",
        {},
        io.BytesIO(PASSWORD.encode()),
    )
    with pytest.raises(ProbeError, match=f"HTTP {status}") as caught:
        fresh_login(EMAIL, PASSWORD)
    assert PASSWORD not in str(caught.value)
    assert opener.open.call_count == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"not JSON",
        b"[]",
        b"{}",
        b'{"access_token":"bad\\ntoken"}',
    ],
)
def test_unusable_login_responses(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    mock_login_response(monkeypatch, raw)
    with pytest.raises(ProbeError):
        fresh_login(EMAIL, PASSWORD)


def test_optional_expiry_and_refresh_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_login_response(
        monkeypatch,
        json.dumps(
            {
                "access_token": TOKEN,
                "expires_in": "7200",
                "refresh_token": "private-refresh",
                ".expires": "2026-10-19T22:00:00Z",
                ".issued": "unknown",
            }
        ).encode(),
    )
    result = fresh_login(EMAIL, PASSWORD)
    assert result.expires_in_seconds == 7200
    assert result.expires_at_utc == "2026-10-19T22:00:00+00:00"
    assert result.issued_at_utc is None
    assert result.refresh_token_returned is True
    assert "private-refresh" not in str(result)


def test_login_command_does_not_reuse_cached_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["cloud_probe", "--login"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setenv("RECIPE_KEEPER_CLOUD_TOKEN", "existing-environment-token")
    monkeypatch.setattr(
        cloud_probe.getpass, "getpass", Mock(side_effect=[EMAIL, PASSWORD])
    )
    login = Mock(return_value=Authentication(TOKEN, 3600, None, None, False))
    monkeypatch.setattr(cloud_probe, "fresh_login", login)
    monkeypatch.setattr(
        cloud_probe, "read_mac_session", Mock(side_effect=AssertionError)
    )
    summary = Mock(return_value={"uploaded_entities": 0})
    monkeypatch.setattr(cloud_probe, "summarize", summary)
    cloud_probe.main()
    login.assert_called_once_with(EMAIL, PASSWORD)
    assert summary.call_args.args[0]._token == TOKEN
    output = capsys.readouterr()
    assert json.loads(output.out)["authentication"]["fresh_login"] == "succeeded"
    assert EMAIL not in output.out + output.err
    assert PASSWORD not in output.out + output.err
    assert TOKEN not in output.out + output.err


def test_login_requires_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["cloud_probe", "--login"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    login = Mock()
    monkeypatch.setattr(cloud_probe, "fresh_login", login)
    with pytest.raises(SystemExit) as caught:
        cloud_probe.main()
    assert caught.value.code == 1
    login.assert_not_called()


def test_login_refuses_echo_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["cloud_probe", "--login"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def cannot_hide_input(prompt: str) -> str:
        warnings.warn(
            "cannot hide input", cloud_probe.getpass.GetPassWarning, stacklevel=2
        )
        raise AssertionError("Credential input must not fall back to echo")

    monkeypatch.setattr(cloud_probe.getpass, "getpass", cannot_hide_input)
    with pytest.raises(SystemExit) as caught:
        cloud_probe.main()
    assert caught.value.code == 1
