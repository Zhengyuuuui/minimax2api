"""Device-code sign-in, so importing an account is one click.

The manual path is opening devtools and copying a JWT off ``localStorage``: a step
that requires knowing where the value lives, goes stale within hours, and has to be
repeated per account.  MiniMax's desktop client signs in through an OAuth2
device-code flow — it is the ``/unified-login?...user_code=...`` URL a browser gets
redirected to when something asks to "log in to the app" — and the flow hands the
token to whoever polled for it.  So the bridge can be the device and skip the
copy-paste entirely.

The three endpoints, verified against account.minimax.io:

    POST /oauth2/device/code
        json {"client_id":"mcode-public",
              "code_challenge":"<base64url sha256(verifier)>",
              "code_challenge_method":"S256"}
        -> {"device_code","user_code","verification_uri_complete",
            "expires_in":300,"interval":3}

    (in the operator's browser) /oauth-authorize?user_code=XXXX-XXXX
        the page calls GET /oauth2/device/authorize?user_code=...&app_id=3001
        with the browser's own cookie — that is the whole "approval"

    POST /oauth2/token
        form client_id, grant_type=urn:ietf:params:oauth:grant-type:device_code,
             device_code, code_verifier
        -> 400 authorization_pending | 400 slow_down | 200 with the token

**No callback, no port, no cookie to sniff.**  The token is delivered to the holder
of the ``code_verifier``, which is the point of PKCE and the reason nothing listens.

Two error messages that are unforgiving about the request shape: a ``scope`` field
that is absent or empty is accepted while any real scope answers ``invalid_scope``,
and ``app_id`` belongs to the browser's authorize call only — put it on the token
endpoint and it is an ``invalid_request``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .records import REGION_CN, REGION_GLOBAL
from .security import new_id

CLIENT_ID = "mcode-public"
# The scope and audience the desktop client's OAuth contract declares.  Sending
# none of them is also accepted, but sending a *different* scope is refused with
# invalid_scope, so the declared pair is the safe value to send.
OAUTH_SCOPE = "agent.default"
OAUTH_AUDIENCE = "agent-backend"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# The account service is per-region, and the token it issues is only usable
# against that region's agent deployment.  From the desktop client's own origin
# table: cn -> account.minimax.cn, en -> account.minimax.io.
ACCOUNT_ORIGINS = {
    REGION_CN: "https://account.minimax.cn",
    REGION_GLOBAL: "https://account.minimax.io",
}


def origin_for(region: str) -> str:
    return ACCOUNT_ORIGINS.get(region, ACCOUNT_ORIGINS[REGION_GLOBAL])


def device_code_url(region: str) -> str:
    return f"{origin_for(region)}/oauth2/device/code"


def token_url(region: str) -> str:
    return f"{origin_for(region)}/oauth2/token"

# Session states.  ``authorized`` means the token arrived *and* the account landed
# in the pool: a token that fails to import is a failure, not a success the console
# would have to explain.
PENDING = "pending"
AUTHORIZED = "authorized"
DENIED = "denied"
EXPIRED = "expired"
CANCELED = "canceled"
FAILED = "failed"

# A login link is a click target, not a queue: cap what an open console can start
# before old codes have expired out on their own.
_MAX_PENDING = 8

# Poll spacing is the server's ``interval``; a network error backs off on top of it
# so a flaky line turns into fewer requests, not a hot loop.  The floor is a
# courtesy to the server, not a protocol fact; the tests lower it to exercise the
# transitions without spending real seconds on them.
_MIN_INTERVAL = 1.0
_DEFAULT_INTERVAL = 3.0
_TRANSIENT_STEP = 2.0
_MAX_INTERVAL = 15.0
_MAX_TRANSIENT = 4

# The spec name is trusted anywhere in the envelope; the loose names only when the
# value looks like a JWT, so a session id in the same response is not mistaken for
# a credential.
_STRICT_TOKEN_KEYS = ("access_token",)
_LOOSE_TOKEN_KEYS = ("token", "id_token")


def pkce_pair() -> tuple[str, str]:
    """A fresh verifier and its S256 challenge.

    32 random bytes base64url-no-pad is 43 characters, the minimum length RFC 7636
    allows and the shape this server issued codes for.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def extract_token(payload: Any) -> str:
    """Find the access token wherever the server chose to put it."""
    return find_token(payload)[0]


def find_token(payload: Any, depth: int = 0, path: str = "") -> tuple[str, str]:
    """Locate the token, and report the field it was found in.

    The response shape of a *successful* poll was never observed without a real
    approval, and one should not be fabricated for it; this walks the envelope
    instead, so the first live login works whatever nesting the server uses.  It
    also returns the winning path, which is how the shape gets recorded from a
    real login rather than guessed at a second time.
    """
    if depth > 4:
        return "", ""
    if isinstance(payload, dict):
        for key in _STRICT_TOKEN_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value, _join(path, key)
        for key in _LOOSE_TOKEN_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.count(".") >= 2:
                return value, _join(path, key)
        for key, value in payload.items():
            found, where = find_token(value, depth + 1, _join(path, str(key)))
            if found:
                return found, where
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found, where = find_token(item, depth + 1, f"{path}[{index}]")
            if found:
                return found, where
    return "", ""


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


@dataclass
class DeviceSession:
    """One in-flight browser sign-in."""

    id: str
    device_code: str
    verifier: str
    user_code: str
    verify_url: str
    expires_at: float
    interval: float
    region: str = REGION_GLOBAL
    name: str = ""
    status: str = PENDING
    error: str = ""
    account_id: str = ""
    account_name: str = ""
    polls: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        """The console's view.  Never the device code or the verifier."""
        return {
            "id": self.id,
            "status": self.status,
            "userCode": self.user_code,
            "region": self.region,
            "verifyUrl": self.verify_url,
            "expiresIn": max(0, int(self.expires_at - time.time())),
            "polls": self.polls,
            "accountId": self.account_id,
            "accountName": self.account_name,
            "error": self.error,
        }


class DeviceLoginService:
    """Starts device codes and waits for the browser to approve one."""

    def __init__(self, client: Any, admin: Any) -> None:
        self._client = client
        self._admin = admin
        self._sessions: dict[str, DeviceSession] = {}

    # ------------------------------------------------------------------ public

    async def start(self, name: str = "", region: str = REGION_GLOBAL) -> dict[str, Any]:
        """Ask the requested region's account service for a code pair.

        The region is chosen per login rather than set globally: the two
        deployments have separate account databases, so the region decides both
        which origin issues the token and which agent deployment will accept it.
        """
        if region not in ACCOUNT_ORIGINS:
            region = REGION_GLOBAL
        self._evict()
        pending = sum(1 for session in self._sessions.values() if session.status == PENDING)
        if pending >= _MAX_PENDING:
            raise _fail(429, "too many logins waiting; finish or cancel one first")

        verifier, challenge = pkce_pair()
        payload = await self._post(
            device_code_url(region),
            json={
                "client_id": CLIENT_ID,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": OAUTH_SCOPE,
                "audience": OAUTH_AUDIENCE,
            },
        )
        device_code = str(payload.get("device_code") or "")
        if not device_code:
            raise _fail(502, _describe(payload))

        now = time.time()
        session = DeviceSession(
            id=new_id(),
            device_code=device_code,
            verifier=verifier,
            user_code=str(payload.get("user_code") or ""),
            # The server hands back the complete link; building our own query here
            # is how the extra UI parameters of a pasted URL (client_surface and
            # friends) would creep in as if they were load-bearing.  They are not.
            verify_url=str(payload.get("verification_uri_complete") or payload.get("verification_uri") or ""),
            expires_at=now + float(payload.get("expires_in") or 300),
            region=region,
            # A server-issued 0 is a request to poll immediately, and the floor
            # below is a courtesy to the server; the ``or`` default would replace
            # the zero with our own number instead of clamping it.
            interval=_clamp_interval(payload.get("interval")),
            name=name.strip(),
        )
        self._sessions[session.id] = session
        _log(
            f"[device-login] started user_code={session.user_code}"
            f" interval={session.interval}s expires_in={int(session.expires_at - now)}s"
        )
        session.task = asyncio.create_task(self._poll(session), name="minimaxcode2api-device-login")
        return session.public()

    def get(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise _fail(404, "login session not found (it expires with its code)")
        return session.public()

    async def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise _fail(404, "login session not found")
        if session.task is not None:
            session.task.cancel()
            # Reaped here rather than left to the loop: a cancelled task that is
            # never awaited surfaces as a warning on GC, and this one is created
            # with the operator's console as its only observer.
            await asyncio.gather(session.task, return_exceptions=True)
        if session.status == PENDING:
            session.status = CANCELED
        return session.public()

    # ---------------------------------------------------------------- internals

    def _evict(self) -> None:
        """Drop finished sessions whose codes are long expired.

        A terminal session is worth keeping briefly so a slow console poll can read
        its result, and not worth keeping after that.
        """
        horizon = time.time() - 600
        for session_id in [
            session_id
            for session_id, session in self._sessions.items()
            if session.status != PENDING and session.expires_at < horizon
        ]:
            self._sessions.pop(session_id, None)

    async def _post(self, url: str, **kwargs: Any) -> dict[str, Any]:
        client = self._client.public_client(urlsplit(url).hostname or "")
        try:
            response = await client.post(url, timeout=20.0, **kwargs)
        except Exception as err:  # noqa: BLE001 - a statusless upstream is one error
            raise _transport_error(err) from err
        try:
            payload = response.json() if response.content else {}
        except ValueError as err:
            # An HTML body from these endpoints is an error page or a proxy
            # interposing; either way it is not a grant response and saying so is
            # worth more than a JSON decode trace.
            raise _transport_error(err) from err
        if not isinstance(payload, dict):
            raise _transport_error(ValueError("expected a JSON object"))
        return payload

    async def _poll(self, session: DeviceSession) -> None:
        interval = session.interval
        misses = 0
        while time.time() < session.expires_at:
            await asyncio.sleep(interval)
            session.polls += 1
            try:
                payload = await self._post(
                    token_url(session.region),
                    data={
                        "client_id": CLIENT_ID,
                        "grant_type": DEVICE_GRANT_TYPE,
                        "device_code": session.device_code,
                        "code_verifier": session.verifier,
                    },
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
            except Exception as err:  # noqa: BLE001 - transient, up to a point
                session.error = _clip(str(err))
                misses += 1
                if misses >= _MAX_TRANSIENT:
                    # Four in a row is not a flaky line.  The usual cause is a
                    # missing proxy for a machine that needs one, and telling the
                    # operator that beats letting the code expire pending.
                    session.status = FAILED
                    session.error = (
                        f"{_MAX_TRANSIENT} consecutive network failures: "
                        f"{_clip(str(err))}"
                    )
                    return
                interval = min(interval + _TRANSIENT_STEP, _MAX_INTERVAL)
                continue
            misses = 0

            error = str(payload.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                # The server asked for more spacing; honour it rather than retry
                # at the old rate, which is what keeps a slow_down from becoming
                # a permanent one.
                interval = min(interval + _TRANSIENT_STEP, _MAX_INTERVAL)
                continue
            if error == "expired_token":
                session.status = EXPIRED
                session.error = "the code timed out before it was approved"
                _log(f"[device-login] expired user_code={session.user_code}")
                return
            if error == "access_denied":
                session.status = DENIED
                session.error = "the approval was refused in the browser"
                _log(f"[device-login] denied user_code={session.user_code}")
                return
            if error:
                session.status = FAILED
                session.error = _describe(payload)
                _log(f"[device-login] failed user_code={session.user_code}: {session.error}")
                return

            token, where = find_token(payload)
            if token:
                # Keys only: the token is one step from becoming a live session
                # and has no business in a log line.  The shape is what is worth
                # recording, because a field rename upstream is silent otherwise.
                _log(
                    f"[device-login] approved: token at {where!r}"
                    f" envelope={sorted(payload)}"
                    f" token_len={len(token)} user_code={session.user_code}"
                )
            if not token:
                # A 200 with nothing recognisable in it.  Not retried: the same
                # poll cannot produce a different shape.
                session.status = FAILED
                session.error = "the server approved the code but sent no token"
                _log(
                    f"[device-login] approved but no token;"
                    f" envelope={sorted(payload)} user_code={session.user_code}"
                )
                return

            try:
                account = await self._admin.import_device_token(
                    token, session.name, region=session.region
                )
            except Exception as err:  # noqa: BLE001 - the reason belongs in the console
                session.status = FAILED
                session.error = _clip(str(err))
                _log(
                    f"[device-login] import failed user_code={session.user_code}:"
                    f" {session.error}"
                )
                return

            session.status = AUTHORIZED
            session.account_id = str(account.get("id") or "")
            session.account_name = str(account.get("name") or account.get("id") or "")
            _log(
                f"[device-login] imported {session.account_name or session.account_id}"
                f" user_code={session.user_code} polls={session.polls}"
            )
            return

        session.status = EXPIRED
        session.error = "the code timed out before it was approved"


def _clamp_interval(value: Any) -> float:
    """Honour the server's spacing, within a floor the server is asked to respect."""
    try:
        requested = float(value)
    except (TypeError, ValueError):
        requested = _DEFAULT_INTERVAL
    return max(_MIN_INTERVAL, min(requested, _MAX_INTERVAL))


def _log(message: str) -> None:
    """One line per login event, on stdout with the server's own output."""
    print(message, flush=True)


def _describe(payload: dict[str, Any]) -> str:
    """The server's own words, for the console."""
    for key in ("error_description", "error", "message", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _clip(value)
    return _clip(str(payload))


def _clip(value: str) -> str:
    return value[:300]


def _transport_error(err: BaseException) -> Exception:
    """A failure that says the *request* broke, not that the grant was refused."""
    return _fail(502, f"{type(err).__name__}: {err}")


def _fail(status: int, message: str) -> Exception:
    # Imported lazily: admin owns the error type, and this service is built by
    # server.py with the admin passed in, so a module import here would close a
    # cycle around the only class that can already report a status.
    from .admin import AdminError

    return AdminError(status, message)
