from __future__ import annotations

import hashlib
import hmac
import ipaddress
import socket
from collections.abc import Mapping
from urllib.parse import urlsplit

from app.config import Settings

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
USEFUL_HEADERS = {
    "accept",
    "content-length",
    "content-type",
    "host",
    "user-agent",
    "x-forwarded-for",
    "x-github-event",
    "x-hookrelay-signature",
    "x-request-id",
}


class UnsafeDestinationError(ValueError):
    pass


def verify_signature(raw_body: bytes, secret: str, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    supplied = signature_header.removeprefix("sha256=")
    if len(supplied) != 64:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied.lower())


def sanitize_headers(headers: Mapping[str, str], maximum: int) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in SENSITIVE_HEADERS:
            continue
        if lowered in USEFUL_HEADERS or lowered.startswith("x-webhook-"):
            sanitized[lowered] = value[:512]
        if len(sanitized) >= maximum:
            break
    return sanitized


def _is_unsafe_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    metadata = ipaddress.ip_address("169.254.169.254")
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address == metadata
    )


def validate_destination_url(url: str, settings: Settings, *, resolve_dns: bool = True) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeDestinationError("destination must use http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeDestinationError("destination must have a hostname and no embedded credentials")
    if parsed.fragment:
        raise UnsafeDestinationError("destination fragments are not allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        if not settings.allow_private_destinations_for_dev:
            raise UnsafeDestinationError("localhost destinations are disabled")
        return url

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal and _is_unsafe_ip(literal):
        if not settings.allow_private_destinations_for_dev:
            raise UnsafeDestinationError("private, local, and reserved destinations are disabled")
        return url

    if resolve_dns:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    hostname, parsed.port or 443, type=socket.SOCK_STREAM
                )
            }
        except socket.gaierror as exc:
            raise UnsafeDestinationError("destination hostname could not be resolved") from exc
        if not settings.allow_private_destinations_for_dev and any(
            _is_unsafe_ip(address) for address in addresses
        ):
            raise UnsafeDestinationError("destination resolves to a private or reserved address")
    return url
