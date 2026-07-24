import ipaddress
import socket
from html import escape

from fastapi import HTTPException, status


PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home", ".corp")


def escape_html(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def validate_public_network_host(host: str, *, field_name: str = "host") -> str:
    normalized = host.strip().rstrip(".").lower()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required.",
        )

    if normalized in {"localhost", "ip6-localhost", "ip6-loopback"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot point to localhost or private networks.",
        )
    if normalized.endswith(PRIVATE_HOST_SUFFIXES):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot point to private network hostnames.",
        )

    try:
        ip_addresses = [ipaddress.ip_address(normalized)]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(normalized, None)
        except socket.gaierror:
            return normalized
        ip_addresses = [ipaddress.ip_address(row[4][0]) for row in resolved]

    if any(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        for ip in ip_addresses
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot resolve to a private or non-routable address.",
        )

    return normalized
