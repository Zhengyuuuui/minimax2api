"""Identifiers.

Everything here is randomness, and all of it comes from ``secrets`` — the values
identify records, browser sessions and pins, so the OS CSPRNG is the only
acceptable source.

The fingerprint helpers are here rather than next to the protocol because the
*constraints* on them are local: ``device_id`` has to be eight digits, and the
upstream answers a non-numeric one with a 400 that names no field, which is
exactly the kind of thing worth a comment next to the generator.
"""

from __future__ import annotations

import secrets


def new_id(length: int = 12) -> str:
    """A URL-safe random identifier."""
    return secrets.token_hex(max(1, length // 2))


def random_uuid() -> str:
    """An RFC 4122 v4 UUID.

    ``secrets`` is used rather than ``uuid4`` so the value comes from the OS
    CSPRNG, the same source as everything else here.
    """
    data = bytearray(secrets.token_bytes(16))
    data[6] = (data[6] & 0x0F) | 0x40
    data[8] = (data[8] & 0x3F) | 0x80
    encoded = data.hex()
    return "-".join(
        (encoded[0:8], encoded[8:12], encoded[12:16], encoded[16:20], encoded[20:32])
    )


def random_device_id() -> str:
    """An eight-digit device id.

    It must be digits and nothing else.  The upstream tolerates almost anything in
    ``uuid`` but is strict about this one: a non-numeric value makes every
    ``/minimax-cloud/…`` request fail with a 400 whose message names no field and
    mentions nothing the operator can act on.  The web bundle's own fallback is
    ``1e7 + rand(9e7)``, i.e. eight digits in this same range, which is what is
    reproduced here.
    """
    return str(10_000_000 + secrets.randbelow(90_000_000))
