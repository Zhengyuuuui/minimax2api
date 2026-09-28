"""What a JWT can and cannot tell us about an account.

The token is a bearer credential the operator pasted in, so verifying its
signature would tell us nothing — only the upstream can decide whether it is
still live.  What the payload *is* good for is identification: an email or phone
number lets the console say which account was just added instead of showing an
opaque blob, and the phone's country code hints at which deployment the account
lives on.

The hint is only a hint.  Mainland accounts are also created with email
addresses, so the console always lets the region be overridden.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from .records import REGION_CN, REGION_GLOBAL

_PHONE_DIGITS = re.compile(r"\D")


class NotJWT(ValueError):
    """Input that is not a decodable JWT."""


@dataclass
class TokenInfo:
    user_id: str = ""
    email: str = ""
    phone: str = ""
    expires_at: int = 0
    region: str = REGION_GLOBAL

    @property
    def identifier(self) -> str:
        """The best available human-readable label."""
        if self.email:
            return self.email
        if self.phone:
            return self.phone
        return self.user_id


def _decode_segment(segment: str) -> bytes | None:
    padded = segment + "=" * (-len(segment) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(padded)
        except (ValueError, TypeError):
            continue
    return None


def _first_string(claims: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # Numeric ids are common.  Python has no float64 rounding hazard
            # here, but the text form is what the upstream expects back.
            return str(int(value))
    return ""


def infer_region(phone: str) -> str:
    """Guess the deployment from a phone number's country code."""
    digits = _PHONE_DIGITS.sub("", phone.strip())
    if not digits:
        return ""
    if digits.startswith("86"):
        return REGION_CN
    return REGION_GLOBAL


def parse_token(token: str) -> TokenInfo:
    """Decode a JWT payload without verifying it.

    Accepts the ``realUserID+JWT`` pair the mainland site hands out as a single
    string: the id before the last ``+`` is kept as a candidate ``user_id`` and
    is used if nothing better turns up in the claims.
    """
    raw = (token or "").strip()
    if not raw:
        raise NotJWT("empty token")

    manual_id = ""
    if "+" in raw:
        candidate, _, rest = raw.rpartition("+")
        # A JWT has exactly two dots; a payload separator does not.
        if candidate and rest.count(".") >= 2:
            manual_id = candidate.strip()
            raw = rest.strip()

    parts = raw.split(".")
    if len(parts) < 2 or not parts[1]:
        raise NotJWT("not a JWT token")

    payload = _decode_segment(parts[1])
    if payload is None:
        raise NotJWT("not a JWT token")
    try:
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise NotJWT("not a JWT token") from None
    if not isinstance(claims, dict):
        raise NotJWT("not a JWT token")

    info = TokenInfo(
        user_id=_first_string(
            claims, ("user_id", "userId", "uid", "sub", "realUserID", "real_user_id")
        )
        or manual_id,
        email=_first_string(claims, ("email", "mail", "email_address")),
        phone=_first_string(claims, ("phone", "mobile", "phone_number", "phoneNumber")),
    )
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        info.expires_at = int(exp)
    region = infer_region(info.phone)
    info.region = region or REGION_GLOBAL
    return info


def region_for(phone: str, override: str = "") -> str:
    """Resolve the region to store, honouring an explicit override."""
    if override in (REGION_CN, REGION_GLOBAL):
        return override
    return infer_region(phone) or REGION_GLOBAL
