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

import time
from dataclasses import dataclass
from typing import Any, Iterable

from . import upstream
from .records import (
    STATUS_ACTIVE,
    STATUS_INVALID,
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
            agent_id=self.agent_id,
            device_id=self.device_id or random_device_id(),
            uuid=self.uuid or random_uuid(),
            screen_width=self.screen_width,
            screen_height=self.screen_height,
            base_url=self.base_url,
            group=self.group,
            remark=self.remark,
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

    # ----------------------------------------------------------------- accounts

    async def list_accounts(self) -> list[dict[str, Any]]:
        return self.pool.snapshot()

    async def account(self, account_id: str) -> dict[str, Any]:
        from .db import account_view

        account = await self.db.account_by_id(account_id)
        if account is None:
            raise AdminError(404, "account not found")
        return account_view(account, 0).to_json()

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

    async def import_device_token(self, token: str, name: str = "") -> dict[str, Any]:
        """Turn a freshly signed-in access token into a pool account.

        The account is stored as ``kind="oauth"``, which is what keeps the token
        out of the ``token`` query parameter and in an ``Authorization`` header.
        Storing it as an ordinary web token instead is the mistake that makes a
        working sign-in import look like an expired credential.
        """
        item = AccountInput(
            token=token.strip(), name=name.strip(), region=REGION_GLOBAL, kind=KIND_OAUTH
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
        if action not in ("enable", "disable", "reset", "probe", "credit", "signin"):
            raise AdminError(400, f"unknown action {action!r}")
        account = await self.db.account_by_id(account_id)
        if account is None:
            raise AdminError(404, "account not found")

        if action == "probe":
            return await self.probe_account(account_id)
        if action == "credit":
            return await self.credit_account(account_id)
        if action == "signin":
            if self.signin is None:
                raise AdminError(400, "check-in is not available")
            run = await self.signin.run_now([account_id])
            outcomes = run.get("outcomes") or [{}]
            return outcomes[0]

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

        return config_mod.to_dict(self.settings_fn())

    async def update_settings(self, payload: Any) -> dict[str, Any]:
        from . import config as config_mod

        settings = await self.db.update_settings(payload if isinstance(payload, dict) else {})
        return config_mod.to_dict(settings)

    # ------------------------------------------------------------------- signin

    async def signin_status(self) -> dict[str, Any]:
        if self.signin is None:
            return {"enabled": False, "running": False, "nextRunAt": "", "lastRun": None}
        return self.signin.status()

    async def signin_run_now(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        if self.signin is None:
            raise AdminError(400, "check-in is not enabled")
        return await self.signin.run_now(account_ids)


def _short(err: BaseException) -> str:
    return str(err)[:500]
