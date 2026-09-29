"""Proxy pool: parsing, live checking, and round-robin selection.

Registration is the one operation risk control watches per *address*, so the pool
exists to make each account leave by a different one.  Three pieces:

- **parse** a pasted URL into the scheme a client needs;
- **check** a proxy by actually dialling it and asking a geo service what it sees,
  because the only useful fact about a proxy is its exit IP and that is not in the
  URL;
- **pick** the next proxy to use, honouring the per-address registration limit.

Checking is a real request, not a TCP connect: a proxy that accepts connections
and cannot reach the internet is the common failure, and only a fetch tells the
two apart.  ``httpx`` speaks both schemes (socks5 through ``socksio``), so the
same client that will carry the registration carries the check — a proxy that
passes here is the one that will be used.

The geo endpoint is configurable because it is the one third-party dependency
here and the one most likely to change; ``https://ipwho.is/`` is the default and
answers ``{ip, country, city, connection}`` without a key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

from .records import (
    PROXY_BAD,
    PROXY_OK,
    PROXY_STRATEGIES,
    PROXY_UNKNOWN,
    Proxy,
)

# The schemes an operator may paste.  ``socks5h`` is normalised to ``socks5``:
# httpx resolves the host through the proxy either way, which is what the "h"
# would ask for, so the distinction does not survive into the client.
ALLOWED_SCHEMES = ("socks5", "socks5h", "http", "https")


def scheme_of(url: str) -> str:
    """The scheme a pasted proxy URL declares, normalised, or ``""`` if unusable."""
    scheme = (urlsplit((url or "").strip()).scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return ""
    return "socks5" if scheme == "socks5h" else scheme


def is_valid(url: str) -> bool:
    """Whether a pasted string is a proxy URL this client can dial.

    Checked by construction rather than by regex: a URL is valid exactly when it
    parses, declares an allowed scheme and names a host.  The regex alternative
    is where ports go missing and IPv6 hosts get rejected.
    """
    if not scheme_of(url):
        return False
    parts = urlsplit((url or "").strip())
    return bool(parts.hostname)


def make_proxy(url: str) -> Proxy:
    """Build a fresh, unchecked pool entry from a pasted URL."""
    from .security import new_id

    normalised = (url or "").strip()
    scheme = scheme_of(normalised)
    # Re-spell socks5h as socks5 so the stored URL is the one the client is given.
    if scheme == "socks5" and normalised.lower().startswith("socks5h://"):
        normalised = "socks5://" + normalised[len("socks5h://"):]
    return Proxy(
        id=new_id(),
        url=normalised,
        scheme=scheme,
        status=PROXY_UNKNOWN,
        created_at=time.time(),
    )


@dataclass
class CheckResult:
    ok: bool = False
    exit_ip: str = ""
    country: str = ""
    city: str = ""
    isp: str = ""
    latency_ms: int = 0
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exitIp": self.exit_ip,
            "country": self.country,
            "city": self.city,
            "isp": self.isp,
            "latencyMs": self.latency_ms,
            "error": self.error,
        }


async def check(
    url: str,
    *,
    geo_url: str = "https://ipwho.is/",
    timeout: float = 15.0,
) -> CheckResult:
    """Dial a proxy and report what the world sees through it.

    A transport failure and a bad status are both "not usable", but they are kept
    apart in the message: "cannot connect" and "connected, answered 500" send an
    operator to different places.
    """
    scheme = scheme_of(url)
    if not scheme:
        return CheckResult(error="unsupported proxy scheme (use socks5/http/https)")

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(proxy=url, timeout=timeout, trust_env=False, follow_redirects=True) as client:
            response = await client.get(geo_url)
    except Exception as err:  # noqa: BLE001 - any dial failure is one outcome
        return CheckResult(
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(err).__name__}: {err}"[:200],
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    if response.status_code >= 400:
        return CheckResult(latency_ms=latency_ms, error=f"geo endpoint HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError:
        return CheckResult(latency_ms=latency_ms, error="geo endpoint did not answer JSON")

    exit_ip = str(data.get("ip") or "")
    if not exit_ip:
        return CheckResult(latency_ms=latency_ms, error="geo endpoint reported no exit IP")

    connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    return CheckResult(
        ok=True,
        exit_ip=exit_ip,
        country=str(data.get("country") or ""),
        city=str(data.get("city") or ""),
        isp=str(connection.get("isp") or connection.get("org") or ""),
        latency_ms=latency_ms,
    )


def apply_result(proxy: Proxy, result: CheckResult) -> None:
    """Fold a check into a proxy record, keeping the fail streak honest."""
    proxy.checked_at = time.time()
    proxy.latency_ms = result.latency_ms
    if result.ok:
        proxy.status = PROXY_OK
        proxy.fail_count = 0
        proxy.exit_ip = result.exit_ip
        proxy.country = result.country
        proxy.city = result.city
        proxy.isp = result.isp
    else:
        proxy.status = PROXY_BAD
        proxy.fail_count += 1


def usable(proxies: Iterable[Proxy]) -> list[Proxy]:
    """Entries a registration may be sent through.

    An unchecked proxy counts as usable: requiring a check first would make the
    pool useless until every entry has been dialled, and a bad one fails its
    registration visibly rather than silently.  An entry that has *failed* a
    check, or been disabled, does not.
    """
    return [p for p in proxies if p.enabled and p.status != PROXY_BAD]


class Selector:
    """Chooses the next proxy, spreading registrations across addresses.

    The cursor lives on the instance and advances per pick, which is what makes
    ``rotate`` rotate; a stateless "least used" would stampede the same entry when
    several registrations start at once **and** its ordering would change under
    them, so the cursor is deliberate state, not a cache.
    """

    def __init__(self, strategy: str = "rotate") -> None:
        self.strategy = strategy if strategy in PROXY_STRATEGIES else "rotate"
        self._cursor = 0

    def pick(self, proxies: list[Proxy]) -> Proxy | None:
        pool = usable(proxies)
        if not pool:
            return None
        if self.strategy == "random":
            import secrets

            return pool[secrets.randbelow(len(pool))]
        if self.strategy == "single":
            return pool[0]
        proxy = pool[self._cursor % len(pool)]
        self._cursor = (self._cursor + 1) % len(pool)
        return proxy


def redact(url: str) -> str:
    """Hide the password in a proxy URL, which is the one secret it carries."""
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    return f"{parts.scheme}://{user}:***@{host}"
