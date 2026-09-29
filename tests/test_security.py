from __future__ import annotations

import hashlib
import hmac
import socket

import pytest

from app.config import Settings
from app.security import (
    UnsafeDestinationError,
    sanitize_headers,
    validate_destination_url,
    verify_signature,
)


def test_hmac_signature_valid_and_invalid() -> None:
    body = b'{"ok":true}'
    secret = "a-very-long-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, secret, f"sha256={digest}") is True
    assert verify_signature(body + b" ", secret, f"sha256={digest}") is False
    assert verify_signature(body, secret, None) is False
    assert verify_signature(body, secret, "md5=bad") is False
    assert verify_signature(body, secret, "sha256=abc") is False


def test_signature_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def compare(left: str, right: str) -> bool:
        nonlocal called
        called = True
        return left == right

    monkeypatch.setattr(hmac, "compare_digest", compare)
    verify_signature(b"body", "long-enough-secret", "sha256=" + "0" * 64)
    assert called


def test_header_sanitization_redacts_and_limits() -> None:
    headers = {
        "Authorization": "Bearer secret",
        "Cookie": "session=secret",
        "X-Api-Key": "secret",
        "Content-Type": "application/json",
        "User-Agent": "test",
        "X-Webhook-Topic": "x" * 600,
        "Unrelated": "drop",
    }
    result = sanitize_headers(headers, 2)
    assert result == {"content-type": "application/json", "user-agent": "test"}
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/a",
        "http://localhost/hook",
        "http://service.localhost/hook",
        "http://127.0.0.1/hook",
        "http://10.1.2.3/hook",
        "http://172.16.0.1/hook",
        "http://192.168.1.1/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/hook",
        "http://user:pass@example.com/hook",
        "http:///missing-host",
        "https://example.com/path#fragment",
    ],
)
def test_unsafe_destination_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeDestinationError):
        validate_destination_url(url, Settings(), resolve_dns=False)


def test_public_literal_is_accepted() -> None:
    assert (
        validate_destination_url("https://93.184.216.34/hook", Settings(), resolve_dns=False)
        == "https://93.184.216.34/hook"
    )


def test_private_destination_development_escape_hatch() -> None:
    settings = Settings(allow_private_destinations_for_dev=True)
    assert validate_destination_url("http://localhost:9000/hook", settings) == (
        "http://localhost:9000/hook"
    )
    assert validate_destination_url("http://127.0.0.1/hook", settings) == ("http://127.0.0.1/hook")


def test_dns_resolution_blocks_private_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))],
    )
    with pytest.raises(UnsafeDestinationError, match="resolves"):
        validate_destination_url("https://example.test/hook", Settings())


def test_dns_failure_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise socket.gaierror("no host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(UnsafeDestinationError, match="could not be resolved"):
        validate_destination_url("https://missing.test/hook", Settings())
