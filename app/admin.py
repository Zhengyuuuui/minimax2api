"""The console's API.

Every route here is a maintenance action rather than a serving path, and each one
exists for one of three reasons:

- **An account needs a part of itself read off the internet.**  ``realUserID`` is
  not in the JWT and an agent id is a number the upstream keeps to itself, so
  import discovers both by calling the upstream rather than asking the operator
  for values they cannot look up.
- **An operator needs out of a state the server got itself into.**  A dead
  credential is retired by the pool; there is a route to put it back, because it
  is the one state the operator cannot leave without one.
- **A handler needs its own settings and logs.**  Everything tunable lives in one
  JSON document, and everything that happened lives in the audit table.

The store's writes are read-modify-write through a mutator rather than an UPDATE
statement built here, so every write goes through one row-loading path and a
column added later cannot be silently dropped by a route that predates it.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

from . import upstream
from . import proxy as proxy_mod
from .records import (
    STATUS_ACTIVE,
    STATUS_INVALID,
    SOURCE_DEVICE,
    SOURCE_PASSWORD,
    SOURCE_SIGNUP,
    SOURCE_TOKEN,
    Account,
    KIND_GUEST,
    KIND_OAUTH,
    KIND_TOKEN,
    Quota,
    REGION_CN,
    REGION_GLOBAL,
    Credit,
)
from .security import new_id, random_device_id, random_uuid


class AdminError(Exception):
    """A route's own failure, paired with the status it should be reported with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------- import


@dataclass
class ImportResult:
    """What importing one entry did."""

    ok: bool = False
    id: str = ""
    name: str = ""
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "id": self.id, "name": self.name, "reason": self.reason}


@dataclass
class AccountInput:
    """One account as a caller describes it.

    Every fingerprint field is optional, because the real one belongs to the
    browser that issued the token and cannot be read from outside.  A missing
    value is generated rather than guessed at: ``yy`` is an MD5 over the query
    string built from these, so the pair that is stored has to be the pair that
    is sent.
    """

    token: str = ""
    region: str = REGION_GLOBAL
    name: str = ""
    user_id: str = ""
    agent_id: str = ""
    device_id: str = ""
    uuid: str = ""
    screen_width: int = 0
    screen_height: int = 0
    base_url: str = ""
    group: str = ""
    remark: str = ""
    identifier: str = ""
    email: str = ""
    password: str = ""
    refresh_token: str = ""
    token_expires_at: float = 0.0
    source: str = SOURCE_TOKEN
    kind: str = KIND_TOKEN

    @classmethod
    def from_payload(cls, node: Any) -> "AccountInput | None":
        """Read one entry, accepting both the console's spelling and a bare token."""
        if isinstance(node, str):
            # A bare string is a token: pasting one in is the most common way an
            # account arrives, and parsing a JWT out of one is not needed here.
            return cls(token=node.strip())
        if not isinstance(node, dict):
            return None

        def text(*names: str) -> str:
            for name in names:
                value = node.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        token = text("token", "jwt")
        # A whole header gets pasted often enough to be worth unwrapping.
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            return None

        region = text("region", "zone") or REGION_GLOBAL
        if region not in (REGION_CN, REGION_GLOBAL):
            region = REGION_GLOBAL
        kind = text("kind")
        return cls(
            token=token,
            region=region,
            kind=KIND_GUEST if kind == KIND_GUEST else KIND_TOKEN,
            name=text("name"),
            user_id=text("userId", "user_id", "realUserID", "real_user_id"),
            agent_id=text("agentId", "agent_id", "agentID"),
            device_id=text("deviceId", "device_id", "deviceID"),
            uuid=text("uuid"),
            screen_width=_as_int(node.get("screenWidth", node.get("screen_width"))),
            screen_height=_as_int(node.get("screenHeight", node.get("screen_height"))),
            base_url=text("baseUrl", "base_url"),
            group=text("group", "grp"),
            remark=text("remark", "note"),
            identifier=text("identifier", "email", "phone", "account"),
        )

    def account(self) -> Account:
        now = time.time()
        return Account(
            id=new_id(),
            name=self.name,
            kind=self.kind,
            region=self.region,
            token=self.token,
            user_id=self.user_id,
            identifier=self.identifier,
            email=self.email,
            password=self.password,
            refresh_token=self.refresh_token,
            token_expires_at=self.token_expires_at,
            agent_id=self.agent_id,
            device_id=self.device_id or random_device_id(),
            uuid=self.uuid or random_uuid(),
            screen_width=self.screen_width,
            screen_height=self.screen_height,
            base_url=self.base_url,
            group=self.group,
            remark=self.remark,
            source=self.source,
            enabled=True,
            priority=0,
            max_concurrent=1,
            status=STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
        )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_bool(value: Any, current: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return current


def _urls_from(payload: Any) -> list[str]:
    """Read proxy URLs from a string, a list, or an object with ``url``/``urls``.

    All three arrive: the console sends ``{url}`` for one and ``{urls:[...]}`` for
    a paste, and a script may POST a bare string.  Blank lines are dropped so a
    trailing newline in a pasted block is not an error.
    """
    raw: Any = payload
    if isinstance(payload, dict):
        raw = payload.get("urls", payload.get("url", ""))
    if isinstance(raw, str):
        items = raw.replace("\r", "\n").split("\n")
    elif isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        items = []
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _password_lines(payload: Any) -> list[tuple[str, str]]:
    """Read ``email password`` pairs from a pasted block.

    One pair per line, split on the first ``----``, tab, comma or run of spaces,
    so both ``a@b.com secret`` and ``a@b.com----secret`` work.  The password is
    taken whole after the first separator: it may contain spaces, and truncating
    it on the next one would silently store the wrong password.
    """
    raw: Any = payload
    if isinstance(payload, dict):
        raw = payload.get("accounts", payload.get("lines", payload.get("text", "")))
    if isinstance(raw, list):
        items = [str(item) for item in raw]
    elif isinstance(raw, str):
        items = raw.replace("\r", "\n").split("\n")
    else:
        items = []

    out: list[tuple[str, str]] = []
    for line in items:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        email, _, password = text.partition("----")
        if not _:
            for sep in ("\t", ","):
                if sep in text:
                    email, _, password = text.partition(sep)
                    break
            else:
                email, _, password = text.partition(" ")
        email = email.strip()
        password = password.strip()
        if email and password:
            out.append((email, password))
    return out


# ---------------------------------------------------------------------- service


@dataclass
class AdminService:
    """Route handlers.  ``server.py`` is the FastAPI layer that calls these."""

    db: Any
    pool: Any
    client: Any
    media: Any
    signin: Any
    settings_fn: Any
    # Set by server.py after construction: the keeper needs the admin (for token
    # import) and the admin needs the keeper (for renewal), and one of the two
    # references has to be late-bound to avoid a cycle.
    keepalive: Any = None

    # ----------------------------------------------------------------- accounts

    async def list_accounts(self) -> list[dict[str, Any]]:
        return self.pool.snapshot()

    async def account(self, account_id: str) -> dict[str, Any]:
        from .db import account_view

        account = await self.db.account_by_id(account_id)
        if account is None:
            raise AdminError(404, "account not found")
        return account_view(account, 0).to_json()

    async def account_password(self, account_id: str) -> dict[str, Any]:
        """Return an account's stored password, for the console's reveal action.

        Deliberately a separate call rather than a field on the account list: the
        list is fetched on every render and by anyone who can reach the port, and
        the password is only needed when an operator asks to see it.
        """
        account = await self.db.account_by_id(account_id)
        if account is None:
            raise AdminError(404, "account not found")
        return {"id": account.id, "password": account.password or ""}

    async def import_accounts(self, payload: Any, *, discover: bool = True) -> dict[str, Any]:
        """Add accounts, discovering whatever cannot be pasted in.

        Each entry is handled on its own terms: one that cannot be reached is
        recorded as failed while the rest still land, because a batch of ten
        tokens should not all be refused over one expired one.
        """
        if isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
            nodes = list(payload["accounts"])
        elif isinstance(payload, list):
            nodes = list(payload)
        else:
            nodes = [payload]

        results = [await self._import_one(AccountInput.from_payload(node), discover) for node in nodes]
        await self.pool.load()
        return {"results": [item.to_json() for item in results], "imported": sum(1 for r in results if r.ok)}

    async def import_password_accounts(self, payload: Any) -> dict[str, Any]:
        """Import accounts from email+password pairs, signing each one in.

        A password entry has no token to paste, so the token is minted by logging
        in — the same password grant the keeper's fallback uses.  One entry that
        cannot sign in is reported on its own and does not stop the rest, exactly
        like a token batch.  A successful sign-in stores the access token, the
        refresh token *and* the password, so this account is the kind that can
        always be recovered from the console alone.
        """
        lines = _password_lines(payload)
        if not lines:
            raise AdminError(400, "no email/password entries given")

        settings = self.settings_fn()
        proxy = (settings.upstream.proxy or "").strip() or None
        results = []
        for email, password in lines:
            name = email.split("@")[0]
            try:
                import httpx

                from . import keepalive
                from .security import random_device_id, random_uuid

                account = type("A", (), {})()
                account.email = email
                account.password = password
                account.region = REGION_GLOBAL
                account.uuid = random_uuid()
                account.device_id = random_device_id()
                async with httpx.AsyncClient(
                    timeout=30.0, follow_redirects=False, trust_env=False, proxy=proxy
                ) as client:
                    renewed = await keepalive.renew_with_password(
                        client, account, settings.signup
                    )
                if not renewed.ok:
                    results.append({"email": email, "ok": False, "reason": renewed.error})
                    continue

                item = AccountInput(
                    token=renewed.token,
                    name=name,
                    region=REGION_GLOBAL,
                    kind=KIND_OAUTH,
                    email=email,
                    password=password,
                    refresh_token=renewed.refresh_token,
                    token_expires_at=renewed.expires_at,
                    source=SOURCE_PASSWORD,
                )
                imported = await self._import_one(item, discover=True)
                results.append(
                    {
                        "email": email,
                        "ok": imported.ok,
                        "reason": imported.reason,
                        "id": imported.id,
                    }
                )
            except Exception as err:  # noqa: BLE001 - one entry cannot kill the batch
                results.append({"email": email, "ok": False, "reason": _short(err)})

        await self.pool.load()
        return {
            "results": results,
            "imported": sum(1 for item in results if item["ok"]),
            "total": len(results),
        }

    async def _import_one(self, item: AccountInput | None, discover: bool) -> ImportResult:
        if item is None:
            return ImportResult(reason="no token in the entry")

        account = item.account()
        duplicate = await self.db.list_accounts()
        if any(entry.token == account.token for entry in duplicate):
            return ImportResult(reason="an account with this token already exists")

        credential = upstream.credential_of(account)
        if discover:
            try:
                # One call for both credential kinds: the credential knows which
                # header it authenticates with, and this endpoint is the only
                # source of realUserID either way — it is in neither token.
                identity = await self.client.fetch_user_info(credential)
            except Exception as err:  # noqa: BLE001 - an unreachable token is a result, not a crash
                return ImportResult(reason=_short(err))

            account.user_id = identity.get("real_user_id") or ""
            if not account.user_id:
                return ImportResult(reason="the token carries no realUserID")
            if not account.identifier:
                account.identifier = (
                    identity.get("email") or identity.get("phone") or identity.get("name") or ""
                )

        agent_id, _ = await self._discover_agent_id(credential, account.agent_id)
        if agent_id:
            account.agent_id = agent_id

        stored = await self.db.upsert_account(account)
        await self.pool.note_saved(stored)
        return ImportResult(ok=True, id=stored.id, name=stored.name or stored.id)

    async def import_device_token(
        self,
        token: str,
        name: str = "",
        region: str = REGION_GLOBAL,
        email: str = "",
        password: str = "",
        remark: str = "",
        refresh_token: str = "",
        expires_in: float = 0.0,
        source: str = SOURCE_DEVICE,
    ) -> dict[str, Any]:
        """Turn a freshly signed-in access token into a pool account.

        The account is stored as ``kind="oauth"``, which is what keeps the token
        out of the ``token`` query parameter and in an ``Authorization`` header.
        Storing it as an ordinary web token instead is the mistake that makes a
        working sign-in import look like an expired credential.

        ``region`` is the region whose account service issued the token, and it
        has to be stored with the account: the two deployments have separate
        account databases, so a token is accepted by exactly one of them.

        ``email``/``password`` are recorded when the caller knows them — the
        headless signup path does, the browser sign-in does not — because they
        are the login an operator reads off the console, and the upstream's
        ``identifier`` is a username or phone rather than either.
        """
        if region not in (REGION_CN, REGION_GLOBAL):
            region = REGION_GLOBAL
        item = AccountInput(
            token=token.strip(),
            name=name.strip(),
            region=region,
            kind=KIND_OAUTH,
            email=email.strip(),
            password=password,
            remark=remark,
            refresh_token=refresh_token.strip(),
            # The device-flow response carries expires_in; recording it lets the
            # keeper renew this account before it dies instead of only after a 401.
            token_expires_at=(time.time() + float(expires_in)) if expires_in else 0.0,
            source=source,
        )
        if not item.token:
            raise AdminError(502, "the sign-in returned no token")
        result = await self._import_one(item, discover=True)
        if not result.ok:
            # A duplicate is a real outcome to report, not a bug: signing in twice
            # from one browser is what happens when an operator is unsure the first
            # click worked.
            raise AdminError(400, result.reason or "import failed")
        await self.pool.load()
        return {"id": result.id, "name": result.name}

    async def _discover_agent_id(self, credential: upstream.Credential, current: str) -> tuple[str, bool]:
        """Read the account's agent list and resolve the id it should use.

        A failure is not propagated: an account whose agent list cannot be read is
        still importable, it simply cannot open a session until it is probed, and
        the pool reports that distinctly rather than as an unhealthy account.
        """
        try:
            prepared = await self.client.prepare(credential)
        except Exception:  # noqa: BLE001 - see above
            return current, False
        return prepared.resolve_agent_id(current)

    async def update_account(self, account_id: str, payload: Any) -> dict[str, Any]:
        """Edit an account's own settings, never its token.

        A token is deliberately not editable here: replacing one is a new
        import, because the identity that goes with it has to be re-read too.
        """
        if not isinstance(payload, dict):
            raise AdminError(400, "expected a JSON object")

        def apply(account: Account) -> None:
            for key in ("name", "group", "remark", "region", "agent_id", "user_id", "base_url", "device_id", "uuid"):
                if key in payload:
                    setattr(account, key, str(payload.get(key) or "").strip())
            if "enabled" in payload:
                account.enabled = _as_bool(payload["enabled"], account.enabled)
            if "priority" in payload:
                account.priority = max(-100, min(100, _as_int(payload.get("priority"))))
            if "maxConcurrent" in payload:
                account.max_concurrent = max(1, min(50, _as_int(payload.get("maxConcurrent"))))
            for key in ("screenWidth", "screen_width"):
                if key in payload:
                    account.screen_width = max(0, _as_int(payload.get(key)))
            for key in ("screenHeight", "screen_height"):
                if key in payload:
                    account.screen_height = max(0, _as_int(payload.get(key)))
            # Any edit clears the retired mark: touching an account is the
            # operator saying it should be tried again.
            if account.status == STATUS_INVALID:
                account.status = STATUS_ACTIVE

        account = await self.db.update_account(account_id, apply)
        if account is None:
            raise AdminError(404, "account not found")
        await self.pool.note_saved(account)
        from .db import account_view

        return account_view(account, 0).to_json()

    async def delete_accounts(self, account_ids: Iterable[str]) -> dict[str, int]:
        ids = [account_id for account_id in account_ids if account_id]
        if not ids:
            return {"deleted": 0}
        removed = await self.db.delete_accounts(ids)
        for account_id in ids:
            await self.pool.forget(account_id)
        return {"deleted": removed}

    async def account_action(self, account_id: str, action: str) -> dict[str, Any]:
        """One of the per-account maintenance actions.

        An unknown action is refused rather than ignored: a silently dropped
        action reads to the operator as one that worked.
        """
        if action not in ("enable", "disable", "reset", "probe", "credit", "signin", "renew"):
            raise AdminError(400, f"unknown action {action!r}")
        account = await self.db.account_by_id(account_id)
        if account is None:
            raise AdminError(404, "account not found")

        if action == "probe":
            return await self.probe_account(account_id)
        if action == "credit":
            return await self.credit_account(account_id)
        if action == "renew":
            return await self.keepalive_renew(account_id)
        if action == "signin":
            if self.signin is None:
                raise AdminError(400, "check-in is not available")
            # Run this one account directly rather than through a full pass: a
            # disabled account is skipped by the daily run, but the operator
            # pressing the button means this account, now.  The claim endpoint is
            # idempotent, so pressing it again on a day already claimed answers
            # `already` instead of double-crediting.
            outcome = await self.signin.run_account(account)
            return outcome.to_json()

        def apply(item: Account) -> None:
            if action == "enable":
                item.enabled = True
                item.status = STATUS_ACTIVE
                item.cooldown_until = 0.0
            elif action == "disable":
                item.enabled = False
            elif action == "reset":
                # The only way out of retired: the operator has re-fetched the
                # token, and that is evidence a timer cannot supply.
                item.status = STATUS_ACTIVE
                item.fail_count = 0
                item.last_error = ""
                item.cooldown_until = 0.0
                item.enabled = True

        saved = await self.db.update_account(account_id, apply)
        if saved is None:
            raise AdminError(404, "account not found")
        await self.pool.note_saved(saved)
        from .db import account_view

        return account_view(saved, 0).to_json()

    async def probe_account(self, account_id: str) -> dict[str, Any]:
        """Open a session purely to see whether the account still works.

        Creating a session is the cheapest authenticated call there is: it proves
        the token and fingerprint pair is accepted without spending a turn.
        """
        account = await self.db.account_by_id(account_id)
        if account is None:
            raise AdminError(404, "account not found")
        credential = upstream.credential_of(account)
        started = time.monotonic()
        try:
            latency = await self.client.probe(credential)
        except Exception as err:  # noqa: BLE001 - the console shows the reason
            return {
                "ok": False,
                "error": _short(err),
                "latencyMs": int((time.monotonic() - started) * 1000),
            }

        def apply(item: Account) -> None:
            item.quota = Quota(
                synced_at=time.time(),
                available=True,
                latency_ms=latency,
                plan=(item.credit.plan_name if item.credit else ""),
                note="probe ok",
            )

        saved = await self.db.save_account_state(account_id, apply)
        if saved is not None:
            await self.pool.note_saved(saved)
        return {"ok": True, "latencyMs": latency, "accountId": account_id}

    async def probe_all(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        """Probe accounts one after another and report each result.

        Sequential rather than concurrent: every probe opens a session against the
        same upstream, and a burst of them from one address is the pattern the
        routing settings already pace check-in to avoid.  One account failing does
        not stop the rest — a pool is inspected precisely when something is wrong,
        and the working accounts are the ones that prove it is one account.
        """
        if account_ids:
            wanted = set(account_ids)
            accounts = [a for a in await self.db.list_accounts() if a.id in wanted]
        else:
            accounts = await self.db.list_accounts()

        results = []
        for account in accounts:
            try:
                result = await self.probe_account(account.id)
            except AdminError as err:
                result = {"ok": False, "error": err.message}
            except Exception as err:  # noqa: BLE001 - one account cannot kill the pass
                result = {"ok": False, "error": _short(err)}
            results.append(
                {
                    "accountId": account.id,
                    "accountName": account.name or account.id,
                    **result,
                }
            )
            await asyncio.sleep(1.0)

        return {
            "ok": all(item.get("ok") for item in results) if results else True,
            "total": len(results),
            "okCount": sum(1 for item in results if item.get("ok")),
            "results": results,
        }

    async def probe_credit(self, account_id: str) -> dict[str, Any]:
        """Read the balance from every source that reports one, without writing.

        A balance is reported by more than one endpoint and they do not always
        agree: the membership endpoint carries an `op_credit_summary` for migrated
        accounts and a flat `total_remains_credit` for the ones that predate it,
        while the credit-details endpoint lists grants.  A zero from the flat field
        on a migrated account is not an empty balance, it is the wrong field.

        Nothing is persisted: this is what to run when the console's number and the
        website's number disagree, and a probe that repaired state would hide the
        disagreement it exists to expose.
        """
        account = await self.db.account_by_id(account_id)
        if account is None:
            raise AdminError(404, "account not found")
        credential = upstream.credential_of(account)
        sources: dict[str, Any] = {}
        chosen: dict[str, Any] | None = None

        # The endpoint the bridge actually uses, through the normal parser.
        try:
            info = await self.client.credit(credential)
            sources["membership"] = {
                "ok": True,
                "total": info.total,
                "free": info.free,
                "purchased": info.purchased,
                "planName": info.plan_name,
                "planType": info.plan_type,
            }
            chosen = sources["membership"]
        except Exception as err:  # noqa: BLE001 - the failure is a reading too
            sources["membership"] = {"ok": False, "error": _short(err)}

        # The raw envelopes, so a field the parser does not read is still visible.
        for name, method, path, body in (
            ("membershipRaw", "POST", "/matrix/api/v1/commerce/get_membership_info", "{}"),
            ("grants", "GET", "/minimax-cloud/api/v1/credit/details", ""),
        ):
            try:
                raw = await self.client.fetch_raw(credential, method, path, body)
                payload = json.loads(raw.body) if raw.body.strip().startswith("{") else None
                sources[name] = {
                    "ok": raw.status == 200,
                    "status": raw.status,
                    "fields": _credit_fields(payload) if payload else raw.body[:400],
                }
            except Exception as err:  # noqa: BLE001
                sources[name] = {"ok": False, "error": _short(err)}

        return {
            "ok": bool(chosen and chosen.get("ok")),
            "accountId": account_id,
            "accountName": account.name or account_id,
            "kind": account.kind,
            "stored": account.credit.to_json() if account.credit else None,
            "live": chosen,
            "sources": sources,
        }

    async def credit_account(self, account_id: str) -> dict[str, Any]:
        """Read and store the live balance.

        Reported as its own action because a stale zero is the difference between
        an account that is never used and one that is never noticed.
        """
        account = await self.db.account_by_id(account_id)
        if account is None:
            raise AdminError(404, "account not found")
        credential = upstream.credential_of(account)
        try:
            info = await self.client.credit(credential)
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": _short(err)}

        credit = Credit(
            total=info.total,
            free=info.free,
            purchased=info.purchased,
            plan_name=info.plan_name,
            plan_type=info.plan_type,
            synced_at=time.time(),
        )

        def apply(item: Account) -> None:
            item.credit = credit

        saved = await self.db.save_account_state(account_id, apply)
        if saved is not None:
            await self.pool.note_saved(saved)
        return {"ok": True, "accountId": account_id, **credit.to_json()}

    async def groups(self) -> dict[str, list[str]]:
        return {"groups": await self.db.account_groups()}

    # ------------------------------------------------------------------ proxies

    async def list_proxies(self) -> dict[str, Any]:
        proxies = await self.db.list_proxies()
        return {
            "proxies": [proxy.to_json() for proxy in proxies],
            "ipUsage": [usage.to_json() for usage in await self.db.list_ip_usage()],
        }

    async def add_proxies(self, payload: Any) -> dict[str, Any]:
        """Add one pasted proxy or a newline-separated batch of them.

        Every entry is reported individually: a batch of twenty is normal, and
        one malformed line should not refuse the other nineteen.
        """
        urls = _urls_from(payload)
        if not urls:
            raise AdminError(400, "no proxy URLs given")
        results = []
        for url in urls:
            if not proxy_mod.is_valid(url):
                results.append({"url": proxy_mod.redact(url), "ok": False,
                                "reason": "need socks5://, socks5h://, http:// or https:// with a host"})
                continue
            entry = await self.db.add_proxy(proxy_mod.make_proxy(url))
            results.append({"url": proxy_mod.redact(entry.url), "ok": True, "id": entry.id})
        return {"results": results, "added": sum(1 for r in results if r["ok"])}

    async def update_proxy(self, proxy_id: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AdminError(400, "expected a JSON object")

        def apply(entry: Any) -> None:
            if "enabled" in payload:
                entry.enabled = _as_bool(payload["enabled"], entry.enabled)

        entry = await self.db.update_proxy(proxy_id, apply)
        if entry is None:
            raise AdminError(404, "proxy not found")
        return entry.to_json()

    async def delete_proxy(self, proxy_id: str) -> dict[str, int]:
        return {"deleted": await self.db.delete_proxy(proxy_id)}

    async def check_proxy(self, proxy_id: str) -> dict[str, Any]:
        """Dial one proxy and store what it resolves to."""
        entry = await self.db.proxy_by_id(proxy_id)
        if entry is None:
            raise AdminError(404, "proxy not found")
        settings = self.settings_fn()
        result = await proxy_mod.check(entry.url, geo_url=settings.signup.proxy_check_url)

        def apply(item: Any) -> None:
            proxy_mod.apply_result(item, result)

        saved = await self.db.update_proxy(proxy_id, apply)
        return {"proxy": saved.to_json() if saved else None, **result.to_json()}

    async def check_all_proxies(self) -> dict[str, Any]:
        """Check every enabled proxy, sequentially.

        Sequential on purpose: a batch of checks is the same burst-of-egress
        pattern that gets an address noticed, and these are slow calls that would
        otherwise compete for the same sockets.
        """
        settings = self.settings_fn()
        checked = []
        for entry in await self.db.list_proxies():
            if not entry.enabled:
                continue
            result = await proxy_mod.check(entry.url, geo_url=settings.signup.proxy_check_url)

            def apply(item: Any, _r: Any = result) -> None:
                proxy_mod.apply_result(item, _r)

            saved = await self.db.update_proxy(entry.id, apply)
            checked.append({"url": proxy_mod.redact(entry.url), **result.to_json()})
        return {"checked": checked, "ok": sum(1 for c in checked if c["ok"])}

    # ------------------------------------------------------------------- models

    async def list_models(self) -> list[dict[str, Any]]:
        return [model.to_json() for model in await self.db.list_models()]

    async def update_model(self, model_id: str, payload: Any) -> dict[str, Any]:
        """Edit a catalogue entry's display and dispatch.

        Only the fields the store can persist are accepted: ``upstream`` and
        ``upstream_model`` are compiled-in dispatch hints, and accepting them here
        would produce a value that reads as saved and is silently dropped.
        """
        if not isinstance(payload, dict):
            raise AdminError(400, "expected a JSON object")

        def apply(model: Any) -> None:
            if "enabled" in payload:
                model.enabled = _as_bool(payload["enabled"], model.enabled)
            if "description" in payload:
                model.description = str(payload.get("description") or "")

        model = await self.db.update_model(model_id, apply)
        if model is None:
            raise AdminError(404, "model not found")
        return model.to_json()

    # -------------------------------------------------------------------- audit

    async def list_audits(
        self, limit: int = 50, offset: int = 0, model: str = "", outcome: str = ""
    ) -> dict[str, Any]:
        limit = min(500, max(1, limit))
        offset = max(0, offset)
        audits, total = await self.db.list_audits(
            limit=limit, offset=offset, model=model.strip(), outcome=outcome.strip()
        )
        return {"total": total, "items": [audit.to_json() for audit in audits]}

    async def audit(self, audit_id: str) -> dict[str, Any]:
        audit = await self.db.audit_by_id(audit_id)
        if audit is None:
            raise AdminError(404, "audit not found")
        return audit.to_json()

    async def clear_audits(self) -> None:
        await self.db.clear_audits()

    async def stats(self) -> dict[str, Any]:
        return await self.db.dashboard_stats()

    # -------------------------------------------------------------------- media

    async def list_media(self) -> list[dict[str, Any]]:
        return [item.to_json() for item in await self.db.list_media()]

    async def delete_media(self, media_id: str) -> dict[str, bool]:
        return {"deleted": await self.media.delete(media_id)}

    async def media_size(self) -> dict[str, int]:
        return {"bytes": self.media.total_size()}

    # ----------------------------------------------------------------- settings

    async def get_settings(self) -> dict[str, Any]:
        from . import config as config_mod
        from . import env as env_mod

        # Secrets are masked and env-controlled fields are flagged, both here:
        # this is the only surface that returns settings, and doing it at the
        # source means no future caller can forget.
        return config_mod.to_dict_masked(self.settings_fn(), env_mod.locked_fields())

    async def update_settings(self, payload: Any) -> dict[str, Any]:
        from . import config as config_mod
        from . import env as env_mod

        settings = await self.db.update_settings(payload if isinstance(payload, dict) else {})
        return config_mod.to_dict_masked(settings, env_mod.locked_fields())

    # ---------------------------------------------------------------- keepalive

    async def keepalive_status(self) -> dict[str, Any]:
        if self.keepalive is None:
            return {"running": False, "intervalSec": 0, "lastRun": {}}
        return self.keepalive.status()

    async def keepalive_renew(self, account_id: str) -> dict[str, Any]:
        """Renew one account's token now, on demand."""
        if self.keepalive is None:
            raise AdminError(400, "keep-alive is not available")
        result = await self.keepalive.renew_account(account_id)
        await self.pool.load()
        return result.to_json()

    async def keepalive_sweep(self) -> dict[str, Any]:
        """Renew every account that is due, now."""
        if self.keepalive is None:
            raise AdminError(400, "keep-alive is not available")
        summary = await self.keepalive.sweep()
        await self.pool.load()
        return summary

    # ------------------------------------------------------------------- signin

    async def signin_status(self) -> dict[str, Any]:
        if self.signin is None:
            return {"enabled": False, "running": False, "nextRunAt": "", "lastRun": None}
        return self.signin.status()

    async def signin_run_now(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        if self.signin is None:
            raise AdminError(400, "check-in is not enabled")
        return await self.signin.run_now(account_ids)

    # ------------------------------------------------------------ balance refresh

    async def credit_status(self) -> dict[str, Any]:
        if self.signin is None:
            return {"intervalMin": 0, "lastRun": None}
        return self.signin.credit_status()

    async def credit_refresh(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        """Read balances now: every stale account, or exactly the named ones.

        This is the console's "refresh" — a named account is always read, so a
        click answers with a live number rather than whatever the last pass
        stored.
        """
        if self.signin is None:
            raise AdminError(400, "balance refresh is not available")
        return await self.signin.refresh_credits(account_ids)


def _credit_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """The balance-bearing fields of a raw credit envelope.

    Deliberately a shallow, named subset: the point of the probe is to compare
    numbers across endpoints, not to obscure them inside a full dump, and a
    balance endpoint's envelope carries no credential.
    """
    fields: dict[str, Any] = {}
    summary = payload.get("op_credit_summary")
    if isinstance(summary, dict):
        fields["op_credit_summary"] = summary
    for key in ("total_remains_credit", "opcredit_balance", "op_credit_balance",
                "is_migrated_to_op", "plan_name", "plan_type", "has_token_plan"):
        if key in payload:
            fields[key] = payload[key]
    base = payload.get("base_resp")
    if isinstance(base, dict):
        fields["base_resp"] = base
    grants = payload.get("details")
    if isinstance(grants, list):
        fields["grants"] = [
            {
                "remaining": item.get("remaining_amount"),
                "granted": item.get("granted_amount"),
                "expireAt": item.get("expire_at_ms"),
            }
            for item in grants[:10]
            if isinstance(item, dict)
        ]
        total = 0.0
        for item in grants:
            if not isinstance(item, dict):
                continue
            try:
                total += float(item.get("remaining_amount") or 0)
            except (TypeError, ValueError):
                continue
        fields["grantsTotal"] = round(total, 3)
    return fields


def _short(err: BaseException) -> str:
    return str(err)[:500]
