"""Daily check-in runs as one background task.

**The order of the opening sequence is the whole point.**  ``/config`` creates the
account's record on the agent side, and a check-in claimed *before* that record
exists is registered and never paid out — running the sequence afterwards does not
recover the day.  So ``prepare()`` is called before the claim, every time, rather
than only when an agent id is discovered and discarded.

**Only the international region checks in.**  The mainland deployment has no
check-in service to talk to, so a mainland account is passed over silently — the
skip is not an error, it simply is not a thing that account does.

**Failure is per account, never global.**  One dead token must not stop the
accounts behind it, and a burst must be paced: check-in is risk-control sensitive
endpoint and a run of parallel calls from one address is a pattern that gets
accounts flagged.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import upstream
from .records import (
    Account,
    REGION_CN,
    SigninDay,
    SIGNIN_ALREADY,
    SIGNIN_FAILED,
    SIGNIN_OK,
    SIGNIN_SKIPPED,
    SIGNIN_UNPAID,
    Credit,
    SigninPanel,
)
from .security import random_uuid


def _panel_of(upstream_panel: upstream.SigninPanel | None) -> SigninPanel | None:
    """Copy an upstream panel into the stored one.

    Two classes for the same thing on purpose: the upstream's shape is whatever
    the API answered last week, and the stored one is what the console renders.
    Converting at this boundary means an upstream field rename cannot make the
    console stop drawing the board.
    """
    if upstream_panel is None:
        return None
    return SigninPanel(
        scene=upstream_panel.scene,
        days=[
            SigninDay(
                day_no=day.day_no,
                points=day.points,
                status=day.status,
                is_today=day.is_today,
            )
            for day in upstream_panel.days
        ],
    )

# How recently a claim's grant must have arrived to be counted as proof the points
# landed.  The check-in timestamp is in whole days, so a wider window would admit
# yesterday's grant as today's.
_GRANT_WINDOW_SEC = 3600


@dataclass
class Outcome:
    """What one account's run decided."""

    account_id: str
    account_name: str
    status: str = SIGNIN_SKIPPED
    reason: str = ""
    points: int = 0
    total: float = 0.0
    day_no: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "accountName": self.account_name,
            "status": self.status,
            "reason": self.reason,
            "points": self.points,
            "total": self.total,
            "dayNo": self.day_no,
        }


@dataclass
class _Run:
    """Book-keeping for one pass over the pool."""

    started_at: float = field(default_factory=time.time)
    outcomes: list[Outcome] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        counts = {
            SIGNIN_OK: 0,
            SIGNIN_ALREADY: 0,
            SIGNIN_FAILED: 0,
            SIGNIN_SKIPPED: 0,
            SIGNIN_UNPAID: 0,
        }
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return {
            "id": random_uuid(),
            "startedAt": int(self.started_at),
            "durationMs": int((time.time() - self.started_at) * 1000),
            "counts": counts,
            "outcomes": [outcome.to_json() for outcome in self.outcomes],
        }


class SigninService:
    """Runs check-ins, and keeps balances fresh while it is about it."""

    def __init__(self, db: Any, pool: Any, client: Any, settings_fn: Any) -> None:
        self._db = db
        self._pool = pool
        self._client = client
        self._settings_fn = settings_fn
        self._task: asyncio.Task[None] | None = None
        self._credit_task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._running = False
        self._last_run: dict[str, Any] | None = None
        self._next_run_at: float = 0.0
        self._last_credit_run: dict[str, Any] | None = None

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="minimaxcode2api-signin")
        self._schedule_next()
        if self._credit_task is None:
            self._credit_task = asyncio.create_task(
                self._credit_loop(), name="minimaxcode2api-credits"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        credit_task, self._credit_task = self._credit_task, None
        if credit_task is not None:
            credit_task.cancel()
            try:
                await credit_task
            except asyncio.CancelledError:
                pass

    def _schedule_next(self) -> None:
        settings = self._settings_fn().signin
        now = datetime.now()
        due = now.replace(hour=settings.hour, minute=settings.minute, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        self._next_run_at = due.timestamp()

    def next_run_at(self) -> str:
        if not self._next_run_at:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._next_run_at))

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._settings_fn().signin.enabled,
            "running": self._running,
            "nextRunAt": self.next_run_at(),
            "lastRun": self._last_run,
        }

    async def _loop(self) -> None:
        """Sleep until the due time, then run.  Never raises out of the loop."""
        while True:
            try:
                # The event is what a manual "run now" sets, so a manual run does
                # not have to wait for the clock.
                wait = max(0.0, self._next_run_at - time.time())
                if wait > 0:
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
                self._wakeup.clear()
                if not self._settings_fn().signin.enabled:
                    self._schedule_next()
                    continue
                self._last_run = (await self.run_daily()).to_json()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop must survive
                self._last_run = {
                    "startedAt": int(time.time()),
                    "error": f"{type(err).__name__}: {err}",
                }
            finally:
                self._schedule_next()

    async def run_now(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        """Run a pass immediately, outside the schedule."""
        run = await self.run_daily(account_ids)
        self._last_run = run.to_json()
        return self._last_run

    # ------------------------------------------------------------- balance refresh

    async def _credit_loop(self) -> None:
        """Re-read stale balances on a short timer, forever.

        Separate from the check-in schedule on purpose: check-in is a once-a-day,
        risk-control-sensitive claim, while a balance is a cheap read that the
        console shows as live.  Tying the two together is what leaves a signed-in
        account displaying the zero it had at registration.
        """
        while True:
            try:
                interval = max(1, int(self._settings_fn().signin.credit_refresh_min)) * 60
                await asyncio.sleep(interval)
                self._last_credit_run = await self.refresh_credits()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop must survive
                self._last_credit_run = {
                    "at": int(time.time()),
                    "error": f"{type(err).__name__}: {err}",
                }

    async def refresh_credits(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        """Refresh every balance whose snapshot has gone stale.

        Only stale accounts are read: a pool of twenty polled every five minutes
        is twenty requests a cycle for numbers that mostly have not moved, and the
        endpoints are the same risk-control surface the check-in uses.  A caller
        that names accounts gets exactly those, freshness ignored — that is the
        "refresh now" the console button needs.
        """
        settings = self._settings_fn().signin
        horizon = max(1, int(settings.credit_refresh_min)) * 60
        now = time.time()
        wanted = {account_id for account_id in account_ids or []} if account_ids else None
        accounts = await self._db.list_accounts()

        refreshed, failed, skipped = 0, 0, 0
        for account in accounts:
            # Disabled accounts are refreshed too: an operator turns an account off
            # to park it, not to make its balance lie, and the number on the
            # console is what decides whether it is worth turning back on.
            if account.region == REGION_CN:
                skipped += 1
                continue
            if wanted is not None and account.id not in wanted:
                continue
            # A named account is always read, even if its snapshot is fresh: the
            # caller asked for a live number, not a cached one.
            if wanted is None:
                fresh = account.credit is not None and now - account.credit.synced_at < horizon
                if fresh:
                    skipped += 1
                    continue
            try:
                credit = await self._fetch_credit(
                    upstream.credential_of(account), float(settings.timeout_sec)
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one account only
                credit = None
            if credit is None:
                failed += 1
            else:
                await self._save(account, credit=credit)
                refreshed += 1
            # The same spacing as check-in: a burst of reads from one address is
            # its own pattern, and there is no urgency behind a balance.
            if settings.gap_seconds > 0:
                await asyncio.sleep(settings.gap_seconds)

        return {
            "at": int(now),
            "refreshed": refreshed,
            "failed": failed,
            "skipped": skipped,
        }

    def credit_status(self) -> dict[str, Any]:
        return {
            "intervalMin": max(1, int(self._settings_fn().signin.credit_refresh_min)),
            "lastRun": self._last_credit_run,
        }

    # ------------------------------------------------------------------- the run

    async def run_daily(self, account_ids: list[str] | None = None) -> _Run:
        run = _Run()
        self._running = True
        try:
            accounts = await self._db.list_accounts()
            settings = self._settings_fn().signin
            wanted = {account_id for account_id in account_ids or []} if account_ids else None

            for account in accounts:
                if not account.enabled:
                    continue
                if wanted is not None and account.id not in wanted:
                    continue
                try:
                    outcome = await self.run_account(account)
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - one account only
                    outcome = Outcome(
                        account_id=account.id,
                        account_name=account.name or account.id,
                        status=SIGNIN_FAILED,
                        reason=f"{type(err).__name__}: {err}"[:300],
                    )
                run.outcomes.append(outcome)
                # Paced on purpose: a burst of parallel check-ins from one address
                # is exactly the pattern that gets accounts flagged.
                if settings.gap_seconds > 0:
                    await asyncio.sleep(settings.gap_seconds)
        finally:
            self._running = False
        return run

    async def run_account(self, account: Account) -> Outcome:
        outcome = Outcome(account_id=account.id, account_name=account.name or account.id)
        if account.region == REGION_CN:
            outcome.reason = "mainland region, no check-in service"
            return await _record_outcome(self._db, self._pool, account, outcome)

        settings = self._settings_fn().signin
        if settings.skip_zero_credit and account.credit is not None:
            stale = time.time() - account.credit.synced_at > settings.credit_fresh_min * 60
            if not stale and account.credit.total <= 0:
                outcome.reason = "zero balance"
                return await _record_outcome(self._db, self._pool, account, outcome)

        credential = upstream.credential_of(account)
        deadline = float(settings.timeout_sec)

        # A fresh balance is read first when the stored one is old: an account
        # that was never probed would otherwise be judged on a zero that was
        # never read from anywhere.
        credit = account.credit
        if credit is None or time.time() - credit.synced_at > settings.credit_fresh_min * 60:
            credit = await self._fetch_credit(credential, deadline)
            if credit is not None:
                await self._save(account, credit=credit)
            outcome.total = credit.total if credit is not None else 0

        # This has to happen before the claim, not before the status.  /config is
        # what creates the account's record on the agent side, and a claim made
        # before it exists is lost for the day.
        await self._prepare(credential, deadline, account)

        panel = await asyncio.wait_for(self._client.signin_status(credential), timeout=deadline)
        if panel.claimed_today:
            outcome.status = SIGNIN_ALREADY
            outcome.reason = "already checked in today"
            outcome.day_no = panel.today.day_no if panel.today else 0
            outcome.points = panel.today.points if panel.today else 0
            # Read the balance again even on an already-claimed day.  The console's
            # check-in button is pressed precisely because the number looks wrong,
            # and answering "already checked in" while still showing the stale
            # figure is the same complaint again.  The points may have landed since
            # the stored reading, or the reading may simply be old.
            credit = await self._fetch_credit(credential, deadline)
            if credit is not None:
                await self._save(account, credit=credit)
                outcome.total = credit.total
            return await _record_outcome(self._db, self._pool, account, outcome, panel=_panel_of(panel))

        claim = await asyncio.wait_for(self._client.signin_claim(credential), timeout=deadline)
        outcome.day_no = claim.day_no or (claim.panel.today.day_no if claim.panel and claim.panel.today else 0)
        outcome.points = claim.points

        if claim.is_duplicate():
            outcome.status = SIGNIN_ALREADY
            outcome.reason = "already claimed today"
        elif claim.points and await self._grant_missing(credential, deadline):
            # The claim endpoint answers success whether or not the points were
            # ever issued, so a claim is only proof when a grant can be found for
            # it.  A second state, rather than a failure: something went wrong
            # that the request cannot fix and the operator should see.
            outcome.status = SIGNIN_UNPAID
            outcome.reason = "claim succeeded but no credit grant arrived"
        else:
            outcome.status = SIGNIN_OK

        # The balance was read before the claim, so without this the console keeps
        # showing the number from before the points landed — the exact "I checked
        # in and it still says the old figure" the operator notices.  A real claim
        # or a duplicate both mean the membership may have moved, so both re-read.
        if outcome.status in (SIGNIN_OK, SIGNIN_ALREADY):
            credit = await self._fetch_credit(credential, deadline)
            if credit is not None:
                await self._save(account, credit=credit)
                outcome.total = credit.total

        return await _record_outcome(self._db, self._pool, account, outcome, panel=_panel_of(claim.panel))

    # --------------------------------------------------------------- sub-steps

    async def _prepare(self, credential: upstream.Credential, timeout: float, account: Account) -> None:
        """Run the opening sequence, and adopt any agent id it discovers."""
        try:
            prepared = await asyncio.wait_for(self._client.prepare(credential), timeout=timeout * 3)
        except asyncio.CancelledError:
            raise
        except upstream.UpstreamError:
            # A failure here is recorded as the day's outcome by the caller's
            # handler: the account simply does not check in today.
            raise
        agent_id, changed = prepared.resolve_agent_id(account.agent_id)
        if changed:
            await self._db.update_account(
                account.id, lambda item: setattr(item, "agent_id", agent_id)
            )
            fresh = await self._db.account_by_id(account.id)
            if fresh is not None:
                await self._pool.note_saved(fresh)

    async def _fetch_credit(
        self, credential: upstream.Credential, timeout: float
    ) -> Credit | None:
        try:
            info = await asyncio.wait_for(self._client.credit(credential), timeout=timeout)
        except (upstream.UpstreamError, asyncio.TimeoutError):
            return None
        return Credit(
            total=info.total,
            free=info.free,
            purchased=info.purchased,
            plan_name=info.plan_name,
            plan_type=info.plan_type,
            synced_at=time.time(),
        )

    async def _grant_missing(self, credential: upstream.Credential, timeout: float) -> bool:
        """Whether a just-claimed credit grant can be found.  True means: not.

        An unreadable breakdown is treated as present rather than missing: a
        network failure here must not turn a successful check-in into an unpaid
        one on the console.
        """
        try:
            grants = await asyncio.wait_for(self._client.credit_grants(credential), timeout=timeout)
        except (upstream.UpstreamError, asyncio.TimeoutError):
            return False
        now = time.time()
        for grant in grants:
            if now - (grant.granted_at / 1000.0) > _GRANT_WINDOW_SEC:
                continue
            if grant.remaining > 0:
                return False
        return True

    async def _save(self, account: Account, *, credit: Credit | None = None) -> None:
        """Persist a balance, and let the pool's cache see it."""

        def apply(item: Account) -> None:
            if credit is not None:
                item.credit = credit

        saved = await self._db.save_account_state(account.id, apply)
        if saved is not None:
            await self._pool.note_saved(saved)


async def _record_outcome(
    db: Any,
    pool: Any,
    account: Account,
    outcome: Outcome,
    *,
    panel: SigninPanel | None = None,
) -> Outcome:
    """Persist what a run found, whether it claimed or not.

    Both paths land here, because the board and the streak are worth just as
    much on a day nothing was claimed: a seven-day panel that only fills in on the
    day a claim lands loses the reason it was looked at.
    """

    def apply(item: Account) -> None:
        item.signin_at = time.time()
        item.signin_status = outcome.status
        item.signin_error = outcome.reason[:500]
        item.signin_points = outcome.points
        if outcome.total:
            item.signin_total = outcome.total
        if panel is not None and panel.today is not None:
            # The board numbers the days of its own seven-day cycle, which is the
            # only "streak" this service counts.
            item.signin_streak = panel.today.day_no
        if panel is not None:
            item.signin_panel = panel

    saved = await db.save_account_state(account.id, apply)
    if saved is not None:
        await pool.note_saved(saved)
    return outcome
