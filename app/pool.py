"""The account pool.

The scheduler is the one place that decides *which* token answers a request, so
the interesting choices live here rather than in any handler:

- **Failure is not the same thing as death.**  A rate-limited account is sick,
  not dead: it recovers on its own, so it is cooled down for a while and put
  back in rotation.  Only a credential the upstream explicitly rejected is taken
  out of the pool, because conflating the two strands capacity that would have
  come back by itself.
- **Load is open streams, not completed requests.**  A turn holds an account for
  as long as the stream lives, so the least-loaded account is the one with the
  fewest streams open right now.  Counting turns would send the next request to
  whichever account finished its last one soonest, which is unrelated to whether
  it can take another.
- **Failover excludes, rather than re-asks.**  The caller asks for an account it
  has not tried yet; the pool never has to guess which account "the next one" was.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import upstream
from .records import (
    KIND_OAUTH,
    STATUS_ACTIVE,
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_INVALID,
    Account,
)
from .security import random_uuid

# A request waiting for capacity polls at this interval.  Capacity arrives when
# another request finishes, so there is nothing to react to faster than this.
_WAIT_INTERVAL = 0.1

# How long the exponential backoff doubles per consecutive failure.  Six steps
# covers the default 60s base to the 900s ceiling.
_BACKOFF_STEPS = 6


class NoAvailableAccount(Exception):
    """Every account is unavailable.

    The reason is carried deliberately: the caller has to distinguish *no account
    was ever configured* (a setup problem) from *the pool is momentarily
    exhausted* (a condition that resolves itself), and it is the only party that
    knows how to explain that difference to the user.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SkipAccount(Exception):
    """This account cannot serve the request, but it is not at fault.

    An account whose agent id has not been discovered yet cannot open a session,
    and a handler that fails the request for it would turn a one-time gap into a
    permanent refusal.  The gateway catches this and routes the next attempt to a
    different account, which can only be decided by the caller: the pool does not
    know why the request was made.
    """


@dataclass
class Lease:
    """One account taken out of the pool for the lifetime of one request."""

    account: Account
    token: str
    id: str = field(default_factory=random_uuid)
    taken_at: float = field(default_factory=time.monotonic)

    @property
    def name(self) -> str:
        return self.account.name or self.account.id


@dataclass
class _Slot:
    """Live per-account state that only exists in memory."""

    inflight: int = 0


class Pool:
    """Holds every account and hands them out according to the routing settings."""

    def __init__(self, db: Any, settings_fn: Callable[[], Any]) -> None:
        self._db = db
        self._settings_fn = settings_fn
        self._accounts: dict[str, Account] = {}
        self._slots: dict[str, _Slot] = {}
        self._rotation = itertools.count()
        self._lock = asyncio.Lock()
        # Set by server.py: a callable that renews one account's token on demand.
        # Late-bound because the keeper needs the pool loaded to see accounts.
        self.renewer: Any = None

    # -------------------------------------------------------------------- load

    async def load(self) -> None:
        """Reload every account from the store.

        Called at boot and after any account change.  Never called on the request
        path: the pool is a cache with its own in-memory counters, and reloading
        one from under a request would drop a lease's account out of the pool and
        count a turn against an account nobody is using.
        """
        accounts = await self._db.list_accounts()
        async with self._lock:
            self._accounts = {account.id: account for account in accounts}
            self._slots = {account_id: self._slots.get(account_id, _Slot()) for account_id in self._accounts}
            self._rotation = itertools.count()

    def accounts(self) -> list[Account]:
        return list(self._accounts.values())

    def account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def snapshot(self) -> list[dict[str, Any]]:
        """Accounts plus live counters, for the console.

        The counters are merged into a copy of the projection rather than into
        the stored record: the pool is a cache, the database is the record, and a
        view is never a write.
        """
        from .db import account_view

        out = []
        for account in self.accounts():
            slot = self._slots.get(account.id) or _Slot()
            out.append({**account_view(account, 0).to_json(), "inflight": slot.inflight})
        return out

    def usable_count(self) -> int:
        now = time.time()
        return sum(
            1
            for account in self._accounts.values()
            if account.enabled
            and account.status != STATUS_INVALID
            and account.status != STATUS_DISABLED
            and not (account.status == STATUS_COOLDOWN and account.cooldown_until > now)
        )

    # --------------------------------------------------------------- selection

    async def acquire(
        self, *, exclude: set[str] | None = None, allow: set[str] | None = None
    ) -> Lease:
        """Take one usable account, waiting for capacity if need be."""
        exclude = exclude or set()
        deadline = time.monotonic() + max(0.0, self._settings_fn().routing.capacity_wait_sec)
        while True:
            async with self._lock:
                lease = self._take(exclude, allow)
                if lease is not None:
                    return lease
                reason = self._no_account_reason(exclude, allow)
            if time.monotonic() >= deadline:
                raise NoAvailableAccount(reason)
            await asyncio.sleep(_WAIT_INTERVAL)

    def _take(self, exclude: set[str], allow: set[str] | None) -> Lease | None:
        for account in self._ranked(exclude, allow):
            slot = self._slots.setdefault(account.id, _Slot())
            if slot.inflight >= max(1, account.max_concurrent):
                continue
            slot.inflight += 1
            return Lease(account=account, token=account.token)
        return None

    def _no_account_reason(self, exclude: set[str], allow: set[str] | None) -> str:
        """Why nothing was available, in the terms the user would think in.

        The pool holds the only copy of several facts the request would want to
        explain itself — how many accounts never got a token, which ones are sick
        — so this is reported here rather than reconstructed from outside.
        """
        accounts = list(self._accounts.values())
        if not accounts:
            return "no accounts configured; add one in the console"
        enabled = [account for account in accounts if account.enabled]
        if not enabled:
            return f"all {len(accounts)} accounts are disabled"
        fresh = [
            account
            for account in enabled
            if account.id not in exclude and (allow is None or account.id in allow)
        ]
        if not fresh:
            return f"all {len(enabled)} usable accounts were already tried"
        now = time.time()
        cooling = {
            account.name or account.id
            for account in fresh
            if account.status == STATUS_COOLDOWN and account.cooldown_until > now
        }
        if len(cooling) == len(fresh):
            return f"all accounts cooling down: {_names(cooling)}"
        invalid = {
            account.name or account.id
            for account in fresh
            if account.status in (STATUS_INVALID, STATUS_DISABLED)
        }
        if len(invalid) == len(fresh):
            return f"accounts unusable: {_names(invalid)}"
        busy = [
            account
            for account in fresh
            if account.status not in (STATUS_INVALID, STATUS_DISABLED)
            and (self._slots.get(account.id) or _Slot()).inflight
            >= max(1, account.max_concurrent)
        ]
        if len(busy) == len(fresh):
            return f"all accounts at max_concurrent={busy[0].max_concurrent}"
        return "no account available"

    def _ranked(self, exclude: set[str], allow: set[str] | None) -> list[Account]:
        """Accounts worth trying, in the order the routing settings prefer.

        Filtering happens before ranking: a disabled or rate-limited account
        should not influence the choice, only the error if nothing is left.
        """
        now = time.time()
        candidates = []
        for account in self._accounts.values():
            if not account.enabled or account.id in exclude:
                continue
            if allow is not None and account.id not in allow:
                continue
            if account.status in (STATUS_INVALID, STATUS_DISABLED):
                continue
            if account.status == STATUS_COOLDOWN:
                if account.cooldown_until > now:
                    continue
                # The cooldown ran out, so the account is usable again.  The mark
                # is cleared here instead of being left to expire a second time.
                account.status = STATUS_ACTIVE
            candidates.append(account)
        return self._order(candidates)

    def _order(self, candidates: list[Account]) -> list[Account]:
        routing = self._settings_fn().routing

        # prefer_idle is a filter, not an order: it keeps only the accounts nobody
        # is using, when any exist.  Applied first, so the chosen strategy ranks
        # an already-reduced set.
        if routing.prefer_idle:
            idle = [
                account
                for account in candidates
                if (self._slots.get(account.id) or _Slot()).inflight == 0
            ]
            if idle:
                candidates = idle

        if routing.strategy == "priority":
            return sorted(candidates, key=lambda account: -account.priority)
        if routing.strategy == "round_robin":
            return sorted(candidates, key=lambda account: account.priority, reverse=True)
        if routing.strategy == "random":
            return list(candidates)
        # least_inflight: open streams are a better measure of load than a
        # lifetime counter, because a turn occupies an account for as long as the
        # response is still arriving.
        return sorted(
            candidates,
            key=lambda account: (
                (self._slots.get(account.id) or _Slot()).inflight,
                -account.priority,
                self._accounts.get(account.id).name if self._accounts.get(account.id) else "",
            ),
        )

    # ----------------------------------------------------------------- release

    async def release(self, lease: Lease, *, success: bool, error: BaseException | None = None) -> None:
        """Give a lease back and record what happened to its account.

        ``error=None`` with ``success=False`` means the account could not serve
        the request but its health was never in question — no agent id yet.
        Nothing is recorded for it, because cooldown is for accounts that are
        being rate limited and a temporary gap is not one.
        """
        async with self._lock:
            slot = self._slots.get(lease.account.id)
            if slot is not None and slot.inflight > 0:
                slot.inflight -= 1
        # Bookkeeping runs outside the lock: _record_failure may renew the
        # credential, and the renewer reloads the pool, which takes the same
        # lock — re-entering it here would deadlock the request.
        if success:
            await self._record_success(lease)
        elif error is not None:
            await self._record_failure(lease, error)

    async def _record_success(self, lease: Lease) -> None:
        def apply(account: Account) -> None:
            account.success_count += 1
            account.fail_count = 0
            account.last_error = ""
            account.status = STATUS_ACTIVE
            account.cooldown_until = 0.0
            account.last_used_at = time.time()

        await self._mutate(lease.account.id, apply)

    async def _record_failure(self, lease: Lease, error: BaseException | None) -> None:
        if isinstance(error, upstream.InvalidCredential):
            # A refused OAuth credential is usually just an expired one-hour
            # token, so renewal gets a chance before retirement: an account that
            # can be refreshed must not be retired, because retirement is manual
            # and a pool that empties itself every hour is the failure this
            # prevents.  Only if renewal fails is the credential treated as dead.
            if lease.account.kind == KIND_OAUTH and self.renewer is not None:
                try:
                    renewed = await self.renewer(lease.account.id)
                except Exception:  # noqa: BLE001 - a failed renewal falls through
                    renewed = None
                if renewed is not None and getattr(renewed, "ok", False):
                    # The account row was rewritten by the renewer; pick it up so
                    # the next routing decision sees the fresh token.
                    await self.load()
                    return
                if renewed is not None and getattr(renewed, "transient", False):
                    # The renewal could not even reach the network.  That says
                    # nothing about the credential, so the account is cooled like
                    # a rate-limited one instead of retired: a proxy outage must
                    # not empty the pool.
                    def cool_transient(account: Account) -> None:
                        routing = self._settings_fn().routing
                        steps = min(account.fail_count, _BACKOFF_STEPS)
                        seconds = min(
                            float(routing.cooldown_max_sec),
                            float(routing.cooldown_base_sec) * (2**steps),
                        )
                        account.fail_count += 1
                        account.status = STATUS_COOLDOWN
                        account.cooldown_until = time.time() + seconds
                        account.last_error = _short(error)
                        account.updated_at = time.time()

                    await self._mutate(lease.account.id, cool_transient)
                    return

            # A credential the upstream refused is the one failure that does not
            # recover, so the account is retired rather than cooled.
            def retire(account: Account) -> None:
                account.status = STATUS_INVALID
                account.fail_count += 1
                account.last_error = _short(error)
                account.cooldown_until = 0.0
                account.updated_at = time.time()

            await self._mutate(lease.account.id, retire)
            return

        # Anything else is treated as rate limiting until proven otherwise: the
        # account stays in the pool and is given room, with the backoff growing
        # per consecutive failure so a persistently sick one is visited rarely.
        def cool(account: Account) -> None:
            routing = self._settings_fn().routing
            steps = min(account.fail_count, _BACKOFF_STEPS)
            seconds = min(
                float(routing.cooldown_max_sec),
                float(routing.cooldown_base_sec) * (2**steps),
            )
            account.fail_count += 1
            account.status = STATUS_COOLDOWN
            account.cooldown_until = time.time() + seconds
            account.last_error = _short(error)
            account.updated_at = time.time()

        await self._mutate(lease.account.id, cool)

    async def _mutate(self, account_id: str, apply: Callable[[Account], None]) -> Account | None:
        """Apply a change to one account in the database, then refresh the cache.

        The write goes through ``save_account_state`` rather than a full update:
        every change the request path makes is a status change or a counter, and a
        full read-modify-write would also bump ``updated_at`` on a tenant that has
        not been touched otherwise.
        """
        account = await self._db.save_account_state(account_id, apply)
        if account is not None:
            self._accounts[account.id] = account
        return account

    # -------------------------------------------------------- explicit changes

    async def note_saved(self, account: Account) -> None:
        """Adopt an account another component just wrote."""
        self._accounts[account.id] = account

    async def forget(self, account_id: str) -> None:
        async with self._lock:
            self._accounts.pop(account_id, None)
            self._slots.pop(account_id, None)

    async def reset_account(self, account_id: str) -> Account | None:
        """Return an invalid account to the pool for another attempt.

        Reachable only from the console: an automatic version of this would be a
        retry loop by another name.
        """

        def apply(account: Account) -> None:
            account.status = STATUS_ACTIVE
            account.fail_count = 0
            account.last_error = ""
            account.cooldown_until = 0.0

        return await self._mutate(account_id, apply)

    async def save(self, account: Account) -> None:
        """Persist an account modified outside the request path."""
        stored = await self._db.upsert_account(account)
        self._accounts[stored.id] = stored


def _names(items: set[str] | list[str]) -> str:
    ordered = sorted(items)
    if len(ordered) <= 3:
        return ", ".join(ordered)
    return f"{', '.join(ordered[:3])} +{len(ordered) - 3} more"


def _short(error: BaseException | None) -> str:
    if error is None:
        return ""
    return str(error)[:500]
