"""Headless account registration — create a MiniMax account without a browser.

The manual ways in are both a person: paste a JWT from devtools, or click through
the device-code sign-in.  Neither scales to "fill the pool", and both depend on an
operator being at a screen.  This module does the same two steps a human would,
against the account service directly:

    1. register — obtain a mailbox, ask for an email code, verify it.  Verifying
       is the registration: ``/oauth2/login`` with ``loginType=21`` creates the
       account when the address is new and returns a session cookie either way.
    2. authorise — run the device-code flow the desktop client uses, and approve
       it with the cookie from step 1 instead of a browser.  The token that
       ``/oauth2/token`` then returns is exactly what the sign-in button imports,
       so it is handed to the *same* importer; there is no second account shape.

Two facts make step 1 work without a captcha.  The bundle carries Tencent
captcha code, but the build served on ``account.minimax.io`` has it compiled off
(``h.Xy = false``), so the send-code branch never calls it and the request goes
out with empty ``randStr`` / ``ticket``.  And ``randStr`` / ``ticket`` are the
only captcha-shaped fields, so there is nothing else to satisfy.

Step 2 is the part worth spelling out, because it is where a "register only"
approach stops short: a session cookie is not a gateway credential.  The gateway
authenticates an OAuth ``access_token`` in an ``Authorization`` header, and the
device flow is what mints one.  Approving it server-side, from the cookie, closes
the loop the browser would otherwise close.

The whole thing runs as a background task with a polled status, mirroring
``DeviceLoginService``: a registration takes tens of seconds (a verification mail
has to arrive), which is too long to hold a request open for.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from . import proxy
from . import signing
from .config import SignupSettings
from .records import REGION_CN, REGION_GLOBAL

# The account service, per region.  Same split as device_login: a cookie from one
# deployment is only meaningful against it.
ACCOUNT_ORIGINS = {
    REGION_CN: "https://account.minimax.cn",
    REGION_GLOBAL: "https://account.minimax.io",
}

# The email-code login type.  It is a string in the payload, not a number.
LOGIN_TYPE_EMAIL_CODE = "21"
# How long an emailed code is usable, per the mail body's own "expires in 5
# minutes".  Used as the accept window when reading the mailbox: a code older
# than this is refused by the service anyway, so accepting it only produces a
# confusing failure later.  Four minutes leaves the margin the mail describes.
CODE_VALIDITY_SEC = 240
# Password login.  With ``authToken`` set to an RSA-encrypted password and a
# fresh email ``code`` alongside, the same call is how a password is *created*
# for an account that has none — see ``_set_password``.
LOGIN_TYPE_PASSWORD = "20"

# Fixed query parameters both the browser and the desktop client send.  ``unix``
# is milliseconds here — matching the bundle, which passes ``Date.now()`` straight
# into the parameter *and* into the ``yy`` digest.  Seconds would sign a URL the
# request never is, and the send-code call refuses that with a bare "request
# parameters" error that names no field.
_QUERY_KEYS = (
    ("device_platform", "web"),
    ("biz_id", "3"),
    ("app_id", "3001"),
    ("version_code", "22201"),
    ("os_name", "Mac"),
    ("browser_name", "chrome"),
    ("lang", "en"),
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# Session states.  ``registered`` means the account is in the pool, the same
# standard device_login holds: a flow that produced a credential but failed to
# import it is a failure the console has to show, not a success.
PENDING = "pending"
REGISTERED = "registered"
FAILED = "failed"
CANCELED = "canceled"

_MAX_PENDING = 4
# The verification code is six digits, but so is a value in the tracking pixel
# MiniMax appends to every message ("...&mac=270819&...").  Matching "six digits"
# alone therefore finds a constant that is never the code, which is how a working
# login ends up rejected as "code incorrect".  The real code follows the phrase
# in the template; the fallback (any six digits not sitting after "mac=") exists
# only so a template rewrite degrades to something rather than to nothing.
_MAIL_CODE_RE = __import__("re").compile(r"code is:?\s*\n?\s*(\d{6})", __import__("re").IGNORECASE)
_MAIL_ANY_CODE_RE = __import__("re").compile(r"(?<!mac=)\b(\d{6})\b")


def _fail(status: int, message: str) -> Exception:
    from .admin import AdminError

    return AdminError(status, message)


def origin_for(region: str) -> str:
    return ACCOUNT_ORIGINS.get(region, ACCOUNT_ORIGINS[REGION_GLOBAL])


@dataclass
class _AccountSession:
    """The ``account.minimax.io`` cookie jar plus the fingerprint it signs with.

    ``uuid`` and ``device_id`` are the browser's localStorage values; they are
    part of every signed URL, so they have to be stable for the life of a
    registration and are generated the way ``security`` generates them elsewhere.
    """

    uuid: str
    device_id: str
    cookies: dict[str, str] = field(default_factory=dict)
    # The mailbox's own bearer JWT, kept here because the password step needs a
    # *second* verification code and the mailbox is the only place to read it.
    mail_jwt: str = ""

    @property
    def sid(self) -> str:
        return self.cookies.get("_sid", "")

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def absorb(self, response: httpx.Response) -> None:
        for key, value in response.cookies.items():
            self.cookies[key] = value

    def build_target(self, base: str, path: str, unix_ms: int) -> str:
        """Join the fixed parameters in bundle order; the order is signed.

        Hand-joined rather than through ``urlencode`` so nothing can reorder keys:
        ``yy`` is an MD5 over the encoded URL, and a sort changes the digest.
        """
        pairs = list(_QUERY_KEYS)
        pairs.insert(4, ("unix", str(unix_ms)))
        pairs.append(("uuid", self.uuid))
        pairs.append(("device_id", self.device_id))
        pairs.append(("client", "web"))
        query = "&".join(f"{k}={v}" for k, v in pairs)
        return f"{base}{path}?{query}"


class SignupService:
    """Starts registrations and reports on them as they progress.

    Owns the proxy pool, because the pool exists for this one caller: an account
    registered through a proxy has to be counted against that proxy's exit
    address, and that is only knowable here.  ``db`` is optional so the flow can
    be unit-driven without a store.
    """

    def __init__(
        self,
        client: Any,
        admin: Any,
        settings_fn: Callable[[], Any],
        db: Any = None,
        signin: Any = None,
    ) -> None:
        self._client = client
        self._admin = admin
        self._settings_fn = settings_fn
        self._db = db
        # The first day's credit is only issued by the check-in endpoint, so a
        # freshly registered account sits at zero until it claims.  Wiring the
        # check-in service here lets a registration claim immediately; it is
        # optional so the flow can still be driven without one (tests, scripts).
        self._signin = signin
        self._sessions: dict[str, "SignupSession"] = {}
        self._selector = proxy.Selector("rotate")

    # ------------------------------------------------------------------ public

    async def start(
        self, name: str = "", region: str = "", count: int = 1, password: str = ""
    ) -> dict[str, Any]:
        settings = self._settings_fn().signup
        if not settings.enabled:
            raise _fail(400, "headless registration is disabled")
        if not settings.mail_pass:
            raise _fail(400, "no mail service passkey configured (settings.signup.mail_pass)")
        region = region or settings.region
        if region not in ACCOUNT_ORIGINS:
            region = settings.region

        self._evict()
        pending = sum(1 for s in self._sessions.values() if s.status == PENDING)
        if pending >= _MAX_PENDING:
            raise _fail(429, "too many registrations waiting; finish or cancel one first")

        count = max(1, min(int(count or 1), settings.batch_max))
        session = SignupSession(
            id=secrets.token_hex(6),
            region=region,
            name=name.strip(),
            count=count,
            settings=settings,
            # Per-run override of the account password.  Blank means "use the
            # configured default", which may itself be blank (no password at all).
            password=password.strip(),
        )
        self._sessions[session.id] = session
        session.task = asyncio.create_task(self._run(session), name="minimaxcode2api-signup")
        return session.public()

    def get(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise _fail(404, "registration not found (finished ones are evicted)")
        return session.public()

    async def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise _fail(404, "registration not found")
        if session.task is not None:
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
        if session.status == PENDING:
            session.status = CANCELED
        return session.public()

    # ---------------------------------------------------------------- internals

    def _evict(self) -> None:
        horizon = time.time() - 900
        for session_id in [
            session_id
            for session_id, session in self._sessions.items()
            if session.status != PENDING and session.finished_at < horizon
        ]:
            self._sessions.pop(session_id, None)

    async def _run(self, session: "SignupSession") -> None:
        """Register ``session.count`` accounts, one at a time, through the pool.

        Serial on purpose: parallel registrations from one process share an
        egress address until the pool rotates, which is the exact pattern the
        per-address limit exists to avoid, and the mail service rate-limits
        per-address too.  A gap between accounts keeps both honest.
        """
        try:
            for index in range(session.count):
                session.index = index + 1
                proxy_entry, exhausted = await self._pick_proxy(session)
                if exhausted:
                    # A pool exists and every entry is at its address budget: that
                    # is the one case where registering would mean reusing a burnt
                    # exit address, so the batch stops rather than skipping it.
                    session.error = "no usable proxy with address budget left"
                    break
                # No pool (or no budget needed): fall back to the machine's own
                # egress, which is the operator's configured proxy when there is
                # one and a direct connection when there is not.
                if proxy_entry is not None:
                    proxy_url = proxy_entry.url
                else:
                    proxy_url = (self._settings_fn().upstream.proxy or "").strip()
                session.proxy = proxy_url
                session.step = "starting"
                try:
                    account = await register_account(
                        settings=session.settings,
                        region=session.region,
                        name=session.name,
                        on_step=session.note,
                        proxy_url=proxy_url,
                        import_token=self._admin.import_device_token,
                        password=session.password,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - one failed account is not the batch
                    session.error = _clip(str(err))
                    _log(
                        f"[signup] account {index + 1}/{session.count} failed"
                        f" proxy={proxy.redact(proxy_url) or 'direct'}: {session.error}"
                    )
                    await self._note_proxy(session, proxy_entry, ok=False)
                    if session.registered:
                        continue
                    break

                await self._record_success(session, account, proxy_entry)
                await self._signin_new_account(session, account)
                if index < session.count - 1 and session.settings.gap_seconds:
                    await asyncio.sleep(session.settings.gap_seconds)

            session.status = REGISTERED if session.registered else FAILED
            if not session.registered and not session.error:
                session.error = "no accounts were registered"
        except asyncio.CancelledError:
            session.status = CANCELED
            raise
        finally:
            session.finished_at = time.time()

    async def _signin_new_account(self, session: "SignupSession", account: dict[str, Any]) -> None:
        """Claim the new account's first day of credit, right after it registers.

        The free credit a new account is said to have is issued by the check-in
        endpoint, not by registration: an account that never claims reads as zero
        on every balance surface, which looks exactly like a broken account.  This
        runs the same check-in the console's manual button runs, for this one
        account, and never lets its failure touch the registration — the account
        is already in the pool and worth keeping even if the claim does not land.
        """
        if self._signin is None or self._db is None:
            return
        account_id = str((account or {}).get("id") or "")
        if not account_id:
            return
        try:
            fresh = await self._db.account_by_id(account_id)
            if fresh is None:
                return
            outcome = await self._signin.run_account(fresh)
            session.note(f"check-in: {outcome.status}")
            _log(
                f"[signup] check-in {fresh.name or account_id}:"
                f" {outcome.status} {outcome.reason}".rstrip()
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the account is already usable
            _log(f"[signup] check-in failed for {account_id}: {_clip(str(err))}")

    async def _pick_proxy(self, session: "SignupSession") -> tuple[Any, bool]:
        """Choose a proxy whose exit address is still under the limit.

        Returns ``(entry, exhausted)``.  ``entry`` is ``None`` when there is no
        proxy pool to use at all — the caller then falls back to the machine's own
        egress (``upstream.proxy`` or direct) rather than refusing to register.
        ``exhausted`` is True only when a pool *exists* but every entry is at its
        address budget, which is the one case that is a real refusal.

        The limit is checked against the *counted* address, which is only known
        after a check; an entry that has never been checked has no counted address
        and is therefore allowed — its first use is how it gets one.
        """
        if not session.settings.use_proxies or self._db is None:
            return None, False
        proxies = await self._db.list_proxies()
        usable = [p for p in proxies if p.enabled and p.status != proxy.PROXY_BAD]
        if not usable:
            # An empty pool is not an error: the operator simply has not added
            # proxies, and registration falls back to the machine's own egress.
            return None, False
        # Rotate through usable entries, skipping any whose address is full.
        for _ in range(len(usable)):
            candidate = self._selector.pick(usable)
            if candidate is None:
                return None, True
            if not candidate.exit_ip:
                return candidate, False
            used = await self._db.ip_usage(candidate.exit_ip)
            if used < session.settings.per_ip_limit:
                return candidate, False
            # Full: drop it from this round's candidates and try the next.
            usable = [p for p in usable if p.id != candidate.id]
        return None, True

    async def _record_success(self, session: "SignupSession", account: dict[str, Any], proxy_entry: Any) -> None:
        account_name = str(account.get("name") or account.get("id") or "")
        email = account.get("email") or ""
        exit_ip = ""
        if proxy_entry is not None and self._db is not None:
            result = await proxy.check(proxy_entry.url, geo_url=session.settings.proxy_check_url)
            proxy.apply_result(proxy_entry, result)
            if result.ok:
                exit_ip = result.exit_ip

            def bump(entry: Any) -> None:
                entry.used_count += 1
                entry.last_used_at = time.time()

            await self._db.update_proxy(proxy_entry.id, bump)
            if exit_ip:
                await self._db.record_ip_use(exit_ip)
        session.registered += 1
        session.account_id = str(account.get("id") or "")
        session.account_name = account_name
        session.email = email
        _log(
            f"[signup] registered {account_name or session.account_id}"
            f" ({session.registered}/{session.count}) region={session.region}"
            f" email={email} proxy={proxy.redact(proxy_entry.url) if proxy_entry else 'direct'}"
            f" exit={exit_ip or '-'}"
        )

    async def _note_proxy(self, session: "SignupSession", proxy_entry: Any, *, ok: bool) -> None:
        if proxy_entry is None or self._db is None:
            return
        if not ok:
            proxy_entry.fail_count += 1
            await self._db.update_proxy(
                proxy_entry.id, lambda p: setattr(p, "fail_count", proxy_entry.fail_count)
            )


@dataclass
class SignupSession:
    """One in-flight registration run, and the console's window onto it."""

    id: str
    region: str
    settings: SignupSettings
    name: str = ""
    count: int = 1
    # A per-run password override; empty means "use the configured default".
    password: str = ""
    email: str = ""
    status: str = PENDING
    step: str = "starting"
    error: str = ""
    account_id: str = ""
    account_name: str = ""
    proxy: str = ""
    index: int = 0
    registered: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    task: asyncio.Task | None = field(default=None, repr=False)

    def note(self, step: str) -> None:
        self.step = step

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "region": self.region,
            "email": self.email,
            "step": self.step,
            "error": self.error,
            "accountId": self.account_id,
            "accountName": self.account_name,
            "proxy": proxy.redact(self.proxy) if self.proxy else "",
            "count": self.count,
            "index": self.index,
            "registered": self.registered,
            "elapsedMs": int((time.time() - self.started_at) * 1000),
        }


# ------------------------------------------------------------------- the flow


async def register_account(
    *,
    settings: SignupSettings,
    region: str,
    name: str,
    on_step: Callable[[str], None] | None = None,
    proxy_url: str = "",
    import_token: Callable[..., Any] | None = None,
    password: str = "",
) -> dict[str, Any]:
    """Create one account and import it; return the imported account view.

    Split out from the service so it can be driven directly (a script, a test)
    without the status bookkeeping, which is the only part that needs an event
    loop of its own.

    ``proxy_url`` routes both the mailbox calls and the account calls through one
    proxy, so a single registration leaves by exactly one address — the mailbox
    and the account being seen from different IPs is itself a signal, and keeping
    them together is also what makes the exit address attributable afterwards.
    """
    step = on_step or (lambda _s: None)
    account_origin = origin_for(region)

    # trust_env=False so a stray HTTP(S)_PROXY in the environment cannot add a
    # second egress path the pool does not know about.
    kwargs: dict[str, Any] = {"timeout": 30.0, "follow_redirects": False, "trust_env": False}
    if proxy_url:
        kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**kwargs) as account_client:
        return await _register_with(
            account_client, account_origin, settings, region, name, step, import_token, password
        )


async def _register_with(
    client: httpx.AsyncClient,
    account_origin: str,
    settings: SignupSettings,
    region: str,
    name: str,
    step: Callable[[str], None],
    import_token: Callable[..., Any] | None,
    password: str = "",
) -> dict[str, Any]:
    from .security import random_device_id, random_uuid

    session = _AccountSession(uuid=random_uuid(), device_id=random_device_id())

    # 1. mailbox
    step("creating mailbox")
    mailbox = await _mail_create(client, settings, name)
    email = mailbox.get("address") or ""
    if not email:
        raise _fail(502, f"mail service returned no address: {_clip(json.dumps(mailbox))}")
    session.mail_jwt = mailbox.get("jwt") or ""

    # 2. email code (captcha is compiled off on this build; the two fields are empty)
    step("requesting verification code")
    code = await _obtain_code(
        client, account_origin, session, settings, email, label="verification"
    )

    # 3. verify = register; this sets _sid
    step(f"verifying {code}")
    status, payload = await _signed(
        client, account_origin, session, "/oauth2/login",
        {"loginType": LOGIN_TYPE_EMAIL_CODE, "email": email, "code": code,
         "deviceID": session.device_id},
    )
    if payload.get("code") != 0 or not session.sid:
        raise _fail(502, f"registration failed: {_clip(json.dumps(payload))}")
    identity = payload.get("data") or {}
    step("account created")

    # 5. set a password, if one was configured.  Email-code sign-in never sets
    # one, so an account created this way has no password at all until this call:
    # the password-reset endpoint is the only writer of that field.
    #
    # A failure here does not fail the registration: the account exists and works,
    # and it is more useful in the pool without a password than discarded.  The
    # password is then reported as empty rather than as the configured value.
    password_set = ""
    chosen_password = password or settings.password
    if chosen_password:
        step("setting password")
        try:
            await _set_password(
                client, account_origin, session, settings, email,
                used_codes={code}, password=chosen_password,
            )
            password_set = chosen_password
        except Exception as err:  # noqa: BLE001 - the account is already usable
            _log(f"[signup] password step failed, account kept without one: {_clip(str(err))}")

    # 6. mint an OAuth token for the account, server-side
    step("authorising device flow")
    token, refresh_token = await _mint_token_full(client, account_origin, session, settings)
    step("importing to pool")

    if import_token is None:
        raise _fail(500, "no token importer wired")
    # The email and password are handed to the importer so they are stored with
    # the account: they are the login an operator reads off the console, and the
    # upstream's own identifier is a username rather than either.  The refresh
    # token rides in ``remark`` because that is the account's free-text field and
    # the schema has no column for it yet; the re-auth path reads it back there.
    account = await import_token(
        token, name, region=region, email=email, password=password_set,
        refresh_token=refresh_token,
        source="signup",
    )
    account = dict(account or {})
    account["email"] = email
    return account


async def _send_code(
    client: httpx.AsyncClient,
    account_origin: str,
    session: _AccountSession,
    email: str,
    *,
    label: str = "verification",
) -> bool:
    """Ask for an email code.  Returns whether the server confirmed the send.

    Code 32 ("the previous code was not confirmed as sent") is the endpoint's
    spacing signal, and — confirmed against a live mailbox — a send that answers
    it still delivers mail: the throttle is on *confirming*, not on delivery.  So
    it is reported rather than raised, and the caller decides by looking in the
    mailbox, which is the only honest signal.  A different refusal is raised: it
    will not fix itself by waiting.
    """
    _status, payload = await _signed(
        client, account_origin, session, "/v1/api/user/login/sms/send",
        {"email": email, "phone": "", "randStr": "", "ticket": ""},
    )
    code = _envelope_code(payload)
    if code == 0:
        return True
    if code == 32:
        _log(f"[signup] {label} send throttled (code 32); waiting for the mail anyway")
        return False
    raise _fail(502, f"{label} code request failed: {_clip(json.dumps(payload))}")


async def _obtain_code(
    client: httpx.AsyncClient,
    account_origin: str,
    session: _AccountSession,
    settings: SignupSettings,
    email: str,
    *,
    label: str = "verification",
    used: set[str] | None = None,
    attempts: int = 3,
) -> str:
    """Get a *new* code: ask, wait for mail, and ask again if none comes.

    The accept window is the code's own lifetime, not "after the send": the send
    endpoint throttles with code 32, and a throttled send means the mail was
    already sent — often seconds before the request that reports the throttle.  A
    send-relative window would exclude exactly that still-valid mail, which is
    the failure this replaces.  Codes already used are excluded separately by
    ``used``, so a retired code cannot be picked up just because it is recent.
    """
    seen = set(used or set())
    for attempt in range(attempts):
        await _send_code(client, account_origin, session, email, label=label)
        code = await _mail_wait_code(
            client, settings, session.mail_jwt,
            exclude=seen,
            since=time.time() - CODE_VALIDITY_SEC,
        )
        if code:
            return code
        seen |= await _mail_codes(client, settings, session.mail_jwt)
        if attempt < attempts - 1:
            _log(f"[signup] no {label} mail yet; retrying send ({attempt + 2}/{attempts})")
            await asyncio.sleep(20)
    raise _fail(504, f"no {label} email arrived in time")


async def _set_password(
    client: httpx.AsyncClient,
    account_origin: str,
    session: _AccountSession,
    settings: SignupSettings,
    email: str,
    *,
    used_codes: set[str] | None = None,
    password: str = "",
) -> None:
    """Give a freshly registered account a password.

    Registration by email code creates an account *without* a password, and the
    web client's "create a password" screen is a **password login** carrying the
    new password as its ``authToken``:

        POST /oauth2/login { loginType: "20" (PASSWORD), email, authToken: RSA(new), code, deviceID }

    ``/v1/api/user/change_password`` is the *other* operation — changing a
    password that already exists — and answers 1200019 (USER_NO_PHONE_OR_EMAIL)
    for an account that has none.  So the create path is a login, and the code it
    carries is a fresh emailed one.

    A failure here is not fatal to the registration: the account still works, it
    simply has no password to record, and the caller keeps the email so a login
    can be done by code later.  So it is raised as its own error and the caller
    decides — here, by leaving the password empty rather than pretending.
    """
    # A second code, because the registration code was consumed by the login.
    # Codes already in the mailbox are excluded: it keeps every message, so a
    # poll racing the new mail would otherwise return a consumed code.
    code = await _obtain_code(
        client, account_origin, session, settings, email,
        label="password", used=used_codes,
    )
    await _set_password_with_code(
        client, account_origin, session, settings, email, code,
        password=password or settings.password,
    )


async def _set_password_with_code(
    client: httpx.AsyncClient,
    account_origin: str,
    session: _AccountSession,
    settings: SignupSettings,
    email: str,
    code: str,
    *,
    password: str = "",
) -> None:
    """Apply the password, given the emailed code that authorises it.

    Split from ``_set_password`` so a caller that wants to control the pacing —
    the backfill script, which must not look like a machine — can obtain the code
    itself and submit it a beat later, instead of the two happening back to back.
    """
    chosen = password or settings.password
    _status, payload = await _signed(
        client, account_origin, session, "/oauth2/login",
        {
            "loginType": LOGIN_TYPE_PASSWORD,
            "email": email,
            "authToken": rsa_encrypt(chosen),
            "code": code,
            "deviceID": session.device_id,
        },
    )
    if payload.get("code") != 0:
        raise _fail(502, f"create-password failed: {_clip(json.dumps(payload))}")


# The key the bundle encrypts new passwords with.  A public key, so it is a
# protocol constant rather than a secret, and it is the same one the reset form
# in the browser uses.
LOGIN_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDF5ndG2/UB4L5tbvQaNLHSoBTW\n"
    "DKbrNBuOmUIP23eCmC2ELMx3kppEikxTp5cV8NxUZl6ii+KLwKugioAXApzypHXb\n"
    "gXbq13kTKA7OCA1xtAoMdH9cltjBiFAUJlgmVjr0MuJCknhVAjWLjCVRHege+Atl\n"
    "gkUBUeGa9O+cWcPEwQIDAQAB\n"
    "-----END PUBLIC KEY-----"
)


def rsa_encrypt(value: str) -> str:
    """Encrypt a password the way JSEncrypt does: PKCS#1 v1.5, base64 out.

    The bundle's ``setPublicKey`` + ``encrypt`` pair is JSEncrypt, whose default
    is RSAES-PKCS1-v1_5 over a 1024-bit key with the ciphertext base64-encoded —
    which is what ``pycryptodome``'s ``PKCS1_v1_5`` produces.  Where JSEncrypt
    and many libraries differ is the digest padding: using OAEP here would
    produce a ciphertext the server rejects without saying why.
    """
    from base64 import b64encode

    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA

    key = RSA.import_key(LOGIN_PUBLIC_KEY)
    cipher = PKCS1_v1_5.new(key)
    return b64encode(cipher.encrypt(value.encode("utf-8"))).decode("ascii")


async def _mint_token(
    client: httpx.AsyncClient,
    account_origin: str,
    session: _AccountSession,
    settings: SignupSettings,
) -> str:
    """Run the device flow and return the access token (see ``_mint_token_full``)."""
    token, _refresh = await _mint_token_full(client, account_origin, session, settings)
    return token


async def _mint_token_full(
    client: httpx.AsyncClient,
    account_origin: str,
    session: _AccountSession,
    settings: SignupSettings,
) -> tuple[str, str]:
    """Run the device flow and return ``(access_token, refresh_token)``.

    ``device/code`` is a plain JSON endpoint; the approve call is the signed one,
    because it is on the same account surface as everything else the bundle does.
    The group id is required to approve and only the GET reports it, so the GET
    always precedes the POST.

    The refresh token is returned alongside the access token because the access
    token lives one hour and the refresh token is what makes the credential
    renewable at all — dropping it is what leaves a pool full of accounts that
    each need a fresh browser sign-in every hour.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    response = await client.post(
        f"{account_origin}/oauth2/device/code",
        json={
            "client_id": "mcode-public",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": settings.oauth_scope,
            "audience": settings.oauth_audience,
        },
        timeout=20.0,
    )
    device = _json_or_text(response)
    device_code = device.get("device_code") if isinstance(device, dict) else ""
    user_code = device.get("user_code") if isinstance(device, dict) else ""
    if not device_code or not user_code:
        raise _fail(502, f"device/code failed: {_clip(json.dumps(device))}")

    # GET reports the groups; the default one that can authorise is the target.
    _, info = await _signed(
        client, account_origin, session,
        f"/oauth2/device/authorize?user_code={user_code}&app_id=3001",
        None, method="GET",
    )
    group_id = ""
    if isinstance(info, dict):
        for group in info.get("groups") or []:
            if isinstance(group, dict) and group.get("can_authorize") and group.get("group_id"):
                group_id = str(group["group_id"])
                if group.get("is_default"):
                    break
    if not group_id:
        raise _fail(502, "no authorisable group on the new account")

    _, approved = await _signed(
        client, account_origin, session, "/oauth2/device/authorize",
        {"user_code": user_code, "decision": "approve", "group_id": group_id},
    )
    if not isinstance(approved, dict) or approved.get("status") != "approved":
        raise _fail(502, f"device approval failed: {_clip(json.dumps(approved))}")

    interval = float(device.get("interval") or 3)
    deadline = time.time() + float(device.get("expires_in") or 300)
    while time.time() < deadline:
        response = await client.post(
            f"{account_origin}/oauth2/token",
            data={
                "client_id": "mcode-public",
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "code_verifier": verifier,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=20.0,
        )
        payload = _json_or_text(response)
        if isinstance(payload, dict) and payload.get("access_token"):
            _log(
                "[signup] device token granted "
                f"expires_in={payload.get('expires_in')} keys={sorted(payload)}"
            )
            return str(payload["access_token"]), str(payload.get("refresh_token") or "")
        error = payload.get("error") if isinstance(payload, dict) else ""
        if error == "authorization_pending":
            await asyncio.sleep(interval)
            continue
        if error == "slow_down":
            interval = min(interval + 2, 15)
            await asyncio.sleep(interval)
            continue
        raise _fail(502, f"token poll failed: {_clip(json.dumps(payload))}")
    raise _fail(504, "device code expired before the token was issued")


# --------------------------------------------------------------- signed request


async def _signed(
    client: httpx.AsyncClient,
    account_origin: str,
    session: _AccountSession,
    path: str,
    payload: dict[str, Any] | None,
    *,
    method: str = "POST",
) -> tuple[int, Any]:
    """One signed call to the account service, carrying the session cookie.

    The signature recipe is the project's own (``signing``): ``yy`` over the
    encoded URL, ``x-signature`` over the body.  The one account-specific detail
    is that ``unix`` in the query is milliseconds, not seconds.
    """
    unix_ms = int(time.time() * 1000)
    url = session.build_target(account_origin, path, unix_ms)
    body = json.dumps(payload) if payload is not None else ""
    path_with_query = url[len(account_origin):]
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "origin": account_origin,
        "referer": account_origin + "/unified-login",
        "user-agent": _UA,
        "yy": signing.yy(path_with_query, body, unix_ms),
        "x-timestamp": str(unix_ms // 1000),
        "x-signature": signing.x_signature(unix_ms // 1000, body),
    }
    cookie = session.cookie_header()
    if cookie:
        headers["cookie"] = cookie

    response = await client.request(
        method, url, headers=headers, content=body.encode() if method == "POST" else None, timeout=20.0
    )
    session.absorb(response)
    payload_out = _json_or_text(response)
    return response.status_code, payload_out


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return {"text": _clip(response.text)}


def _envelope_code(payload: Any) -> int | None:
    """The account service's own status code, where it puts one."""
    if isinstance(payload, dict):
        info = payload.get("statusInfo")
        if isinstance(info, dict) and "code" in info:
            return int(info.get("code") or 0)
        if "code" in payload:
            return int(payload.get("code") or 0)
    return None


# ------------------------------------------------------------------- mailbox

# The temporary-mail worker's contract.  Kept in one place because it is the only
# non-MiniMax dependency here, and the one most likely to be swapped.
MAIL_CREATE_PATH = "/admin/new_address"
MAIL_LIST_PATH = "/api/parsed_mails"


async def _mail_create(
    client: httpx.AsyncClient, settings: SignupSettings, name: str
) -> dict[str, Any]:
    base = settings.mail_base.rstrip("/")
    address_name = f"{name or settings.name_prefix}{secrets.token_hex(3)}"
    response = await client.post(
        base + MAIL_CREATE_PATH,
        headers={"content-type": "application/json", "x-admin-auth": settings.mail_pass},
        json={"name": address_name, "domain": settings.mail_domain},
        timeout=20.0,
    )
    payload = _json_or_text(response)
    if not isinstance(payload, dict):
        raise _fail(502, f"mail service error: {_clip(str(payload))}")
    # The worker answers a bad admin passkey with this code.  Surfaced by name so
    # an operator is told to re-enter the passkey instead of chasing a network or
    # domain problem: the two look identical in a raw "register failed" message,
    # and only one of them is fixed in Settings.
    code = str(payload.get("code") or "")
    if code == "AUTH_ADMIN_CREDENTIAL_INVALID" or (
        response.status_code in (401, 403) and "ADMIN" in code.upper()
    ):
        raise _fail(
            502,
            "邮箱服务拒绝了管理密钥（AUTH_ADMIN_CREDENTIAL_INVALID）："
            "请在「设置」或「号池 → 邮箱配置」重新填写 mail_pass，"
            "确认与邮箱 worker 的 admin 密码一致",
        )
    return payload


async def _mail_codes(
    client: httpx.AsyncClient, settings: SignupSettings, jwt: str
) -> set[str]:
    """Every six-digit run already in the mailbox, read without waiting."""
    base = settings.mail_base.rstrip("/")
    try:
        response = await client.get(
            base + MAIL_LIST_PATH + "?limit=10&offset=0",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=20.0,
        )
        data = response.json()
    except Exception:  # noqa: BLE001 - an unreadable snapshot just means no exclusions
        return set()
    codes: set[str] = set()
    for message in data.get("results") or []:
        if not isinstance(message, dict):
            continue
        blob = " ".join(str(message.get(key) or "") for key in ("subject", "text", "html"))
        codes.update(_MAIL_CODE_RE.findall(blob) or _MAIL_ANY_CODE_RE.findall(blob))
    return codes


async def _mail_wait_code(
    client: httpx.AsyncClient,
    settings: SignupSettings,
    jwt: str,
    *,
    exclude: set[str] | None = None,
    since: float = 0.0,
) -> str:
    """Read a six-digit code out of the mailbox.

    ``exclude`` is what makes the second read possible: a mailbox keeps every
    message it ever received, so the password step would otherwise find the
    registration's *consumed* code and replay it.  Excluding the codes already
    used means "the newest code that is not one of these".

    ``since`` bounds the age of an acceptable message.  It is passed as
    ``now - CODE_VALIDITY_SEC`` rather than as the send time, because a throttled
    send (code 32) has usually already delivered: the mail predates the response
    that reports the throttle, and a send-relative window would reject exactly
    the code that is waiting to be read.
    """
    base = settings.mail_base.rstrip("/")
    skip = exclude or set()
    deadline = time.time() + settings.mail_timeout_sec
    while time.time() < deadline:
        try:
            response = await client.get(
                base + MAIL_LIST_PATH + "?limit=10&offset=0",
                headers={"Authorization": f"Bearer {jwt}"},
                timeout=20.0,
            )
            data = response.json()
        except Exception:  # noqa: BLE001 - a flaky poll is retried, not fatal
            await asyncio.sleep(settings.mail_poll_sec)
            continue
        for message in data.get("results") or []:
            if not isinstance(message, dict):
                continue
            if since and _mail_time(message.get("created_at")) < since:
                continue
            blob = " ".join(str(message.get(key) or "") for key in ("subject", "text", "html"))
            # The template phrase first; the loose scan only when it is absent,
            # which keeps the tracking pixel's constant out of the answer.
            candidates = _MAIL_CODE_RE.findall(blob) or _MAIL_ANY_CODE_RE.findall(blob)
            for code in candidates:
                if code not in skip:
                    return code
        await asyncio.sleep(settings.mail_poll_sec)
    return ""


def _mail_time(value: Any) -> float:
    """Parse the mail worker's ``YYYY-MM-DD HH:MM:SS`` stamp as UTC epoch.

    The worker records in UTC (its stamps line up with the account service's own
    ``serviceTime``), and comparing against a local-time parse would shift the
    window by the host's offset — which is exactly the bug that lets an expired
    code through on a machine that is not UTC.
    """
    import calendar

    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S")))
    except (ValueError, TypeError):
        return 0.0


def _log(message: str) -> None:
    print(message, flush=True)


def _clip(value: str) -> str:
    return value[:300]
