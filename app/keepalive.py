"""Credential keep-alive.

The device-flow access token lives one hour (``expires_in=3600``).  A pool that
does not renew it is a pool that works for an hour and then answers 401 to
everything, which is exactly what happened to the first accounts registered here:
they were fine at 13:25 and dead by 14:30.

Two renewal paths, tried in order:

1. **Refresh grant** — ``POST /oauth2/token`` with ``grant_type=refresh_token``.
   Needs no mailbox and no password, and the response carries a *new* refresh
   token, so one successful refresh extends the credential indefinitely.  This is
   the path that matters.
2. **Password sign-in** — the same create-password/login call the registration
   uses, then a fresh device grant.  This is the fallback for an account whose
   refresh token was never stored or has been refused; it needs the email and
   password columns to be populated.

Neither path needs a browser, and neither consumes an emailed code.

The keeper runs on a timer rather than reacting to failures: refreshing a token
that is about to expire costs one request, while discovering the expiry from a
401 costs a failed user request, a cooldown, and a window where the account looks
broken.  The request path still renews on demand as a safety net (see ``pool``),
because a token can expire between two ticks.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from . import signup
from .records import REGION_GLOBAL

# Refresh this long before the token dies.  The access token lives one hour and
# the keeper sweeps every half hour, so the margin has to exceed the sweep
# interval: a token that is "not yet due" on one pass must not be expired by the
# next.  Forty minutes leaves the sweep a window in which every token it will
# need to renew is already due, while still renewing each one only about once an
# hour.
REFRESH_MARGIN_SEC = 2400.0

# How long to wait after a pass that renewed nothing but had accounts due (a
# failure or a transport blip), so the retry is minutes away rather than a full
# interval.
_RETRY_INTERVAL_SEC = 120.0

# A token minted with no expires_in is treated as this; the endpoint always
# sends one, so this is only a guard against a malformed response.
DEFAULT_TOKEN_TTL_SEC = 3600.0


@dataclass
class RenewResult:
    ok: bool = False
    method: str = ""       # "refresh" | "password"
    token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    error: str = ""
    # A transport failure is not a verdict on the credential: the proxy blinked,
    # the network dropped.  The caller retries these later instead of treating
    # the account as un-renewable, which is what a blank error message hides.
    transient: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "method": self.method,
            "expiresAt": self.expires_at,
            "refreshRotated": bool(self.refresh_token),
            "transient": self.transient,
            "error": self.error,
        }


def _is_transient(err: BaseException) -> bool:
    """Whether a failure is the network rather than the credential."""
    return isinstance(err, (httpx.TransportError, httpx.TimeoutException))


async def refresh_with_token(
    client: httpx.AsyncClient,
    account_origin: str,
    refresh_token: str,
) -> RenewResult:
    """Exchange a refresh token for a new access token.

    Form-encoded, and ``app_id`` must not ride along — it belongs to the
    browser's authorize call; on the token endpoint it is an ``invalid_request``.
    """
    if not refresh_token:
        return RenewResult(error="no refresh token")
    try:
        response = await client.post(
            f"{account_origin}/oauth2/token",
            data={
                "client_id": "mcode-public",
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=20.0,
        )
        payload = response.json()
    except Exception as err:  # noqa: BLE001 - any transport failure is one outcome
        return RenewResult(
            error=f"{type(err).__name__}: {err}"[:200],
            transient=_is_transient(err),
        )

    token = str(payload.get("access_token") or "")
    if not token:
        # A refused refresh is a real answer ("invalid_grant"), not a blip, and
        # the caller falls through to the password path.
        return RenewResult(error=str(payload.get("error_description") or payload.get("error") or payload)[:200])
    ttl = float(payload.get("expires_in") or DEFAULT_TOKEN_TTL_SEC)
    return RenewResult(
        ok=True,
        method="refresh",
        token=token,
        # The server rotates the refresh token; keeping the old one works until
        # it does not, so the new one is stored whenever it is offered.
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_at=time.time() + ttl,
    )


async def renew_with_password(
    client: httpx.AsyncClient,
    account: Any,
    settings: Any,
) -> RenewResult:
    """Renew by signing in with the stored password, then a device grant.

    Runs entirely against the account service: no mailbox, no emailed code.  It
    is the fallback for an account whose refresh token is missing or dead, and it
    is also how a brand-new registration's first token is produced.
    """
    if not account.email or not account.password:
        return RenewResult(error="no email/password stored for password renewal")

    origin = signup.origin_for(account.region or REGION_GLOBAL)
    session = signup._AccountSession(uuid=account.uuid, device_id=account.device_id)
    try:
        _status, payload = await signup._signed(
            client, origin, session, "/oauth2/login",
            {
                "loginType": signup.LOGIN_TYPE_PASSWORD,
                "email": account.email,
                "authToken": signup.rsa_encrypt(account.password),
                "deviceID": session.device_id,
            },
        )
        if not isinstance(payload, dict) or payload.get("code") != 0 or not session.sid:
            info = payload.get("statusInfo") if isinstance(payload, dict) else {}
            return RenewResult(error=f"password login: {str((info or {}).get('message') or payload)[:180]}")

        token, refresh = await signup._mint_token_full(client, origin, session, settings)
    except Exception as err:  # noqa: BLE001
        return RenewResult(
            error=f"{type(err).__name__}: {err}"[:200],
            transient=_is_transient(err),
        )

    return RenewResult(
        ok=True,
        method="password",
        token=token,
        refresh_token=refresh,
        expires_at=time.time() + DEFAULT_TOKEN_TTL_SEC,
    )


class Keeper:
    """Renews accounts before their tokens expire, and reports what it did."""

    def __init__(
        self,
        db: Any,
        client: Any,
        settings_fn: Callable[[], Any],
        interval_sec: float | None = None,
    ) -> None:
        self._db = db
        self._client = client
        self._settings_fn = settings_fn
        # None means "read the interval from settings on every pass", so a change
        # made in the console takes effect without a restart.  An explicit number
        # is kept for tests, which have no settings object to consult.
        self._fixed_interval = None if interval_sec is None else max(60.0, float(interval_sec))
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_run: dict[str, Any] = {}

    def _interval(self) -> float:
        if self._fixed_interval is not None:
            return self._fixed_interval
        return max(60.0, float(self._settings_fn().keepalive.interval_sec))

    def _enabled(self) -> bool:
        return bool(self._settings_fn().keepalive.enabled)

    # ------------------------------------------------------------------ public

    async def renew_account(self, account_id: str) -> RenewResult:
        """Renew one account and persist the result."""
        account = await self._db.account_by_id(account_id)
        if account is None:
            return RenewResult(error="account not found")
        if account.kind != "oauth":
            return RenewResult(error="not an oauth account")

        settings = self._settings_fn()
        proxy = (settings.upstream.proxy or "").strip() or None
        origin = signup.origin_for(account.region or REGION_GLOBAL)

        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=False, trust_env=False, proxy=proxy
        ) as client:
            # Refresh first: it is one request and needs no password.  When it is
            # missing or refused, the password path re-logs in and mints a *fresh*
            # refresh token as well, so the account is renewable again rather than
            # merely kept alive once.  Neither path is skipped for want of the
            # other: a stored password is what makes an account keepable even if
            # its refresh token was never captured.
            result = await refresh_with_token(client, origin, account.refresh_token)
            if not result.ok:
                first = result.error
                result = await renew_with_password(client, account, settings.signup)
                if not result.ok:
                    result.error = f"refresh: {first} | password: {result.error}"

        if result.ok:
            def apply(item: Any) -> None:
                item.token = result.token
                if result.refresh_token:
                    item.refresh_token = result.refresh_token
                item.token_expires_at = result.expires_at
                # A renewal is proof the credential works: clear whatever state
                # the last failure left, so a recovered account is scheduled again.
                item.status = "active"
                item.last_error = ""
                item.fail_count = 0
                item.cooldown_until = 0.0

            await self._db.update_account(account_id, apply)
            print(
                f"[keepalive] renewed {account.name or account_id} via {result.method}"
                f" until {time.strftime('%H:%M:%S', time.localtime(result.expires_at))}",
                flush=True,
            )
        elif result.transient:
            # The credential was never judged — the network failed.  Leave the
            # account and its stored refresh token exactly as they were; the next
            # sweep will try again.  Writing a failure here is what makes a flaky
            # proxy look like a dead account.
            print(
                f"[keepalive] {account.name or account_id}: transport failure,"
                f" will retry ({result.error})",
                flush=True,
            )
        else:
            def mark(item: Any) -> None:
                item.last_error = f"renew failed: {result.error}"[:500]

            await self._db.update_account(account_id, mark)
            print(
                f"[keepalive] renew FAILED {account.name or account_id}: {result.error}",
                flush=True,
            )
        return result

    async def sweep(self) -> dict[str, Any]:
        """Renew every enabled oauth account whose token is at or near expiry.

        Only enabled accounts: an account parked by the operator is not spending
        tokens, so renewing it is a request spent on a credential nobody is using,
        and it delays the renewals that matter.  An account that cannot be renewed
        at all — no refresh token and no stored password — is skipped rather than
        counted as a failure: it is not broken, it is simply not renewable, and
        reporting it every pass would bury the accounts that need attention.
        """
        now = time.time()
        due, skipped = [], 0
        for account in await self._db.list_accounts():
            if account.kind != "oauth" or not account.enabled:
                continue
            if account.token_expires_at and account.token_expires_at - REFRESH_MARGIN_SEC > now:
                continue
            if not account.refresh_token and not (account.email and account.password):
                skipped += 1
                continue
            due.append(account)

        renewed, failed, transient = 0, 0, 0
        for account in due:
            try:
                result = await self.renew_account(account.id)
            except Exception as err:  # noqa: BLE001 - one account cannot kill the sweep
                failed += 1
                print(f"[keepalive] {account.name or account.id}: {err}", flush=True)
                continue
            if result.ok:
                renewed += 1
            elif result.transient:
                transient += 1
            else:
                failed += 1
            # Space the renewals: a burst of token grants from one address is its
            # own pattern, and there is no urgency — each account has minutes.
            await asyncio.sleep(2.0)

        self._last_run = {
            "at": now,
            "due": len(due),
            "renewed": renewed,
            "failed": failed,
            "transient": transient,
            "skipped": skipped,
        }
        return self._last_run

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "enabled": self._enabled(),
            "intervalSec": int(self._interval()),
            "lastRun": self._last_run,
        }

    # --------------------------------------------------------------- background

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._loop(), name="minimaxcode2api-keepalive")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        # First sweep waits one interval: on a fresh boot the accounts were just
        # loaded and a burst of grants at startup is the pattern to avoid.
        while self._running:
            try:
                await asyncio.sleep(self._interval())
                if not self._running:
                    return
                if not self._enabled():
                    continue
                summary = await self.sweep()
                # Something was due and did not get renewed: come back soon rather
                # than in a full interval.  The margin is minutes, but a retry that
                # waits ten of them is a retry that can lose a credential.
                if (summary.get("failed") or summary.get("transient")) and self._running:
                    await asyncio.sleep(_RETRY_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop outlives one bad sweep
                print(f"[keepalive] sweep error: {err}", flush=True)
