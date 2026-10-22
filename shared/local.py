"""Local-only runtime destination policy, independent of HTTP libraries.

Configured single-label and .local names are operator-owned LAN names. This
function performs no DNS or network access. HTTP callers must disable automatic
redirects and environment proxies, so data is sent only to the selected endpoint.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_LAN = tuple(ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))


def require_local_url(url: str) -> str:
    """Validate a configured LAN destination before transmitting personal data."""
    if not isinstance(url, str) or "\\" in url:
        raise ValueError("runtime URL must identify a local HTTP(S) endpoint")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
        raise ValueError("local endpoint requires HTTP(S) without embedded credentials")
    # Validate malformed/out-of-range ports without treating them as DNS names.
    parsed.port
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Only conventional LAN names; reject alternative numeric-IP encodings.
        valid_name = bool(re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*", host))
        local = valid_name and (host == "localhost" or host.endswith(".local") or "." not in host)
        if host.startswith("0x") or (host.replace(".", "").isdigit()):
            local = False
    else:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        local = any(address.version == network.version and address in network for network in _LAN)
    if not local:
        raise ValueError("runtime endpoint must be loopback, a private LAN IP, or an operator-owned LAN hostname")
    return url
