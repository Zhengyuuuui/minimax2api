"""The three signatures the MiniMax Agent web bundle attaches to every API call.

All three are MD5 digests over strings the frontend already computes, and two of
them are load-bearing beyond the digest itself:

``x-signature`` covers a timestamp, a static salt and the exact request body.
It has no URL in it, so it says nothing about which endpoint or credential a
request is aimed at — only that the three agree.

``yy`` covers the *encoded URL*.  That is what makes the fingerprint part of the
protocol: the query string carries ``uuid`` / ``device_id`` / ``user_id`` and the
screen size, so a token only replays against the session it was issued to.  It
also means the **parameter order is part of the contract** — reordering the query
changes the digest, and the upstream rejects the rewrite without naming a field.
The agent and check-in query builders therefore join pairs by hand rather than
through anything that sorts keys.

The salt is not a secret.  It ships inside the bundle the browser downloads, so
it is public by construction; treat it as a protocol constant.
"""

from __future__ import annotations

import hashlib

# Baked into the web bundle.
SIGNATURE_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"
# Appended to the yy digest input.
SIGNATURE_SUFFIX = "ooui"

# The characters encodeURIComponent leaves alone.  Notably *not* the same set
# urllib.parse.quote uses: JS escapes ``!*'()`` and leaves ``~`` untouched,
# while stdlib's default does the opposite.  One differing character is enough
# to invalidate a signature.
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()"
)


def md5_hex(value: str) -> str:
    """Lowercase 32-character hex MD5 of ``value``."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def encode_uri_component(value: str) -> str:
    """Percent-encode ``value`` the way JavaScript's encodeURIComponent does.

    Non-ASCII goes through as UTF-8, one percent-triplet per byte, which is what
    the browser builtin produces for a non-ASCII input.
    """
    out: list[str] = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if char in _UNRESERVED:
            out.append(char)
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def form_encode(value: str) -> str:
    """Percent-encode the way URLSearchParams does.

    Same character set as ``encode_uri_component`` except that a space becomes
    ``+``.  The check-in endpoints build their query this way; the agent
    endpoints do not, so the two need separate encoders.
    """
    return encode_uri_component(value).replace("%20", "+")


def x_signature(unix_seconds: int, body: str) -> str:
    """``MD5(unix_seconds + salt + raw_body)``."""
    return md5_hex(f"{unix_seconds}{SIGNATURE_SALT}{body}")


def yy(full_url: str, body: str, unix_ms: int) -> str:
    """``MD5(encodeURIComponent(url) + "_" + body + MD5(ms) + "ooui")``."""
    return md5_hex(encode_uri_component(full_url) + "_" + body + md5_hex(str(unix_ms)) + SIGNATURE_SUFFIX)


if __name__ == "__main__":  # pragma: no cover - manual signature check
    import time

    now = time.time()
    millis = int(now * 1000)
    print("x-signature:", x_signature(int(now), '{"content":"hi"}'))
    print("yy:", yy("https://agent.minimax.io/x?a=1", "{}", millis))
    print("encodeURIComponent:", encode_uri_component("a b!~*'()ü中"))
