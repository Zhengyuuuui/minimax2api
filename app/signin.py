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


# Reasons that mean "the network failed", not "this account is bad".  Kept as
# substrings because the reasons are assembled from several exception texts; the
# match is deliberately loose since a false positive only triggers one extra log
# line, while a miss keeps the operator staring at a broken egress.
_NETWORK_MARKERS = (
    "timeout",
    "timed out",
    "connect",
    "connection",
    "disconnect",
    "network",
    "transport",
    "unreachable",
    "reset",
    "proxy",
    "502",
    "503",
    "504",
)


def _is_network_failure(outcome: "Outcome") -> bool:
    """Whether an account's failure was the network's fault, not the account's."""
    if outcome.status not in (SIGNIN_FAILED,):
        return False
    reason = (outcome.reason or "").lower()
    return any(marker in reason for marker in _NETWORK_MARKERS)


# How long a *failed* catch-up probe is trusted before the network is tested
# again.  A long outage then costs one request an hour instead of one per tick,
# which matters because the probe is a real check-in attempt.
_CATCHUP_PROBE_MEMO_SEC = 3600.0


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
        self._catchup_task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._running = False
        self._last_run: dict[str, Any] | None = None
        self._next_run_at: float = 0.0
        self._last_credit_run: dict[str, Any] | None = None
        # One pass at a time: the scheduler, the console's "run now" and the
        # catch-up loop all drive `run_daily`, and the pool must not be walked
        # twice concurrently.
        self._run_lock = asyncio.Lock()
        # The last check-in pass on record, when it happened, and whether a
        # catch-up pass is currently running.  Kept for ``status()`` so the
        # console can explain "scheduled run was missed, catching up".
        self._last_catchup: dict[str, Any] | None = None
        # Live progress of the current pass, for the console's progress bar.  A
        # run walks the pool serially and can take minutes; without a counter the
        # console can only say "running", which is indistinguishable from stuck.
        self._progress: dict[str, Any] = {
            "total": 0, "done": 0, "current": "", "ok": 0, "already": 0, "failed": 0,
            "recent": [],
        }
        # Set by server.py to the keepalive Keeper's renew_account.  Late-bound for
        # the same reason the pool's is: the keeper is built after this service.
        self.renewer: Any = None

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
        if self._catchup_task is None:
            self._catchup_task = asyncio.create_task(
                self._catchup_loop(), name="minimaxcode2api-signin-catchup"
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
        catchup_task, self._catchup_task = self._catchup_task, None
        if catchup_task is not None:
            catchup_task.cancel()
            try:
                await catchup_task
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
            "progress": dict(self._progress),
            "catchupEnabled": bool(getattr(self._settings_fn().signin, "catchup_enabled", True)),
            "lastCatchup": self._last_catchup,
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

    # --------------------------------------------------------------- catch-up

    async def _catchup_loop(self) -> None:
        """Recover a check-in that the clock missed, once the network is back.

        A scheduled pass is missed whenever the machine is asleep at the hour,
        or the egress is down (dead proxy, dropped tunnel) — the run then fails
        every account and books nothing.  This loop looks for exactly that case
        and repairs it:
          * it only acts when *no* account has a check-in recorded for today, so
            a normal day costs nothing but a cheap DB read;
          * it probes **one** active account first — a single request that tells
            a recovered network apart from one that is still down;
          * only if the probe succeeds does it sign the whole pool in, reusing
            the ordinary pass (``already`` outcomes keep it idempotent).
        A *failed* probe is remembered for an hour so a still-broken network is
        not re-tested on every tick.
        """
        while True:
            settings = self._settings_fn().signin
            interval = max(1, int(getattr(settings, "catchup_interval_min", 15))) * 60
            try:
                await asyncio.sleep(interval)
                if not self._settings_fn().signin.enabled:
                    continue
                if not getattr(self._settings_fn().signin, "catchup_enabled", True):
                    continue
                if self._running:
                    continue
                if await self._today_covered():
                    continue
                if self._probe_recently_failed():
                    continue
                await self._catchup_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop must survive
                print(f"[signin] catch-up failed: {type(err).__name__}: {err}", flush=True)

    def _probe_recently_failed(self) -> bool:
        """Whether a failed probe is recent enough to still be trusted.

        The network being down is not a per-tick question: re-testing it every
        ``catchup_interval_min`` turns an outage into a stream of check-in
        attempts on a live account.  A *successful* probe is deliberately not
        remembered — that path signs the whole pool in straight away, and that
        pass's own bookkeeping decides whether anything is still owed.
        """
        last = self._last_catchup
        if not last or last.get("ok"):
            return False
        try:
            at = float(last.get("at") or 0)
        except (TypeError, ValueError):
            return False
        return (time.time() - at) < _CATCHUP_PROBE_MEMO_SEC

    async def _today_covered(self) -> bool:
        """True if any account already checked in today (ok/already).

        That is the signal that the scheduled pass ran: a pass over the pool
        marks every account it touched, so one success is enough to say the day
        is handled.  A pool where everything failed books nothing and does not
        count, which is what makes a missed, network-blocked run detectable.
        """
        accounts = await self._db.list_accounts()
        today = datetime.now().date()
        for account in accounts:
            if not account.signin_at:
                continue
            if account.signin_status not in (SIGNIN_OK, SIGNIN_ALREADY):
                continue
            try:
                day = datetime.fromtimestamp(float(account.signin_at)).date()
            except (TypeError, ValueError, OSError):
                continue
            if day == today:
                return True
        return False

    async def _catchup_once(self) -> None:
        """Probe one account, then sign the whole pool in if the network is up."""
        accounts = await self._db.list_accounts()
        probes = [a for a in accounts if a.enabled and a.status == "active"]
        if not probes:
            probes = [a for a in accounts if a.enabled]
        if not probes:
            return
        probe = probes[0]
        print(
            f"[signin] no check-in recorded today; probing {probe.name or probe.id} "
            f"before a full catch-up",
            flush=True,
        )
        try:
            outcome = await asyncio.wait_for(
                self.run_account(probe), timeout=self._account_budget(self._settings_fn().signin)
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - one probe only
            self._last_catchup = {
                "at": int(time.time()),
                "probe": probe.id,
                "ok": False,
                "reason": f"{type(err).__name__}: {err}"[:200],
            }
            print(f"[signin] catch-up probe failed: {type(err).__name__}: {err}", flush=True)
            return
        ok = outcome.status in (SIGNIN_OK, SIGNIN_ALREADY)
        self._last_catchup = {
            "at": int(time.time()),
            "probe": probe.id,
            "ok": ok,
            "reason": outcome.reason,
        }
        if not ok:
            # Network is still down (or the probe account is the broken one).
            # Do not hammer the pool; the next tick tries again.
            print(
                f"[signin] catch-up probe not usable ({outcome.status}: {outcome.reason}); "
                f"will retry next interval",
                flush=True,
            )
            return
        print("[signin] probe ok — running the full catch-up check-in", flush=True)
        run = await self.run_daily()
        self._last_run = run.to_json()
        counts = self._last_run.get("counts") or {}
        print(
            f"[signin] catch-up done: ok={counts.get(SIGNIN_OK, 0)} "
            f"already={counts.get(SIGNIN_ALREADY, 0)} failed={counts.get(SIGNIN_FAILED, 0)}",
            flush=True,
        )

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
            #
            # A mainland account is read like any other: its commerce endpoint
            # answers the same `total_remains_credit` on the cn host, so skipping
            # it only left the console showing a stale or absent balance.
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
        # Serialized.  A scheduled pass, a manual "run now" and a catch-up pass
        # all walk the same pool, and two at once would claim twice from the
        # same account — the one pattern the pacing below exists to avoid —
        # while overwriting each other's progress counters and clearing
        # ``_running`` while the other is still going.
        await self._run_lock.acquire()
        run = _Run()
        self._running = True
        network_streak = 0
        try:
            accounts = await self._db.list_accounts()
            settings = self._settings_fn().signin
            wanted = {account_id for account_id in account_ids or []} if account_ids else None
            targets = [a for a in accounts if wanted is None or a.id in wanted]
            # Progress is over the accounts this pass will actually walk, so the
            # bar's denominator matches what the loop does.
            self._progress = {
                "total": len(targets), "done": 0, "current": "",
                "ok": 0, "already": 0, "failed": 0,
                "recent": [],
            }

            for account in accounts:
                # A disabled account is still checked in.  Disabling only removes
                # an account from *serving traffic*; its daily credit is free and
                # expiring, so skipping it silently left the operator with a pool
                # of accounts that looked dead purely because nobody signed them
                # in.  Check-in is a one-request claim, not a request to route.
                if wanted is not None and account.id not in wanted:
                    continue
                self._progress["current"] = account.name or account.id
                # A hard per-account ceiling.  Every network call inside has its
                # own timeout, but a bug in any one of them (a hung renewer, a
                # stuck DB write) would otherwise freeze the *whole* run forever
                # — `_running` never resets, the next day's run never starts, and
                # one bad account silently retires check-in until a restart.
                # This is the backstop that makes "one bad account" cost one
                # account, not the feature.
                account_budget = self._account_budget(settings)
                try:
                    outcome = await asyncio.wait_for(
                        self.run_account(account), timeout=account_budget
                    )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    outcome = Outcome(
                        account_id=account.id,
                        account_name=account.name or account.id,
                        status=SIGNIN_FAILED,
                        reason=f"account exceeded {account_budget:.0f}s budget (stuck); skipped",
                    )
                except Exception as err:  # noqa: BLE001 - one account only
                    outcome = Outcome(
                        account_id=account.id,
                        account_name=account.name or account.id,
                        status=SIGNIN_FAILED,
                        reason=f"{type(err).__name__}: {err}"[:300],
                    )
                run.outcomes.append(outcome)
                # Advance the live counter.  Done is bumped for every account the
                # loop finishes, whether it claimed, was already signed, or
                # failed, so the bar always reaches total.
                self._progress["done"] = self._progress.get("done", 0) + 1
                bucket = {SIGNIN_OK: "ok", SIGNIN_ALREADY: "already"}.get(outcome.status)
                if bucket:
                    self._progress[bucket] = self._progress.get(bucket, 0) + 1
                elif outcome.status == SIGNIN_FAILED:
                    self._progress["failed"] = self._progress.get("failed", 0) + 1
                # Feed the home page's live ticker: one line per finished account,
                # newest last, capped so a huge pool does not grow without bound.
                recent = self._progress.setdefault("recent", [])
                recent.append({
                    "name": outcome.account_name or outcome.account_id,
                    "status": outcome.status,
                    "points": outcome.points,
                    "total": outcome.total,
                })
                del recent[:-40]

                # A run of network failures (as opposed to credential or business
                # failures) is the signal that the egress itself is wrong — a dead
                # proxy, a dropped tunnel — and every remaining account will fail
                # the same way.  It is called out loudly instead of scrolling past
                # as one more line, because the fix is the network, not the pool.
                if _is_network_failure(outcome):
                    network_streak += 1
                    if network_streak == 5:
                        print(
                            f"[signin] WARNING: {network_streak} consecutive network failures "
                            f"(latest: {account.name or account.id}: {outcome.reason}). "
                            f"Egress looks broken — check the proxy / network before "
                            f"the next run.",
                            flush=True,
                        )
                else:
                    network_streak = 0

                # Paced on purpose: a burst of parallel check-ins from one address
                # is exactly the pattern that gets accounts flagged.
                if settings.gap_seconds > 0:
                    await asyncio.sleep(settings.gap_seconds)
        finally:
            self._running = False
            self._run_lock.release()
        return run

    @staticmethod
    def _account_budget(settings: Any) -> float:
        """Hard ceiling for one account's check-in, in seconds.

        Generous relative to a healthy account (a few seconds), because the
        useful failures here are slow networks, not fast bugs — but bounded, so
        a genuinely stuck account is skipped instead of freezing the run.  Its
        own method so a test can pin it without waiting out the real budget.
        """
        try:
            base = float(settings.timeout_sec)
        except (TypeError, ValueError):
            base = 30.0
        return max(60.0, base * 6)

    async def run_account(self, account: Account) -> Outcome:
        """Check one account in, renewing once if the credential is refused.

        The OAuth access token lives an hour, so the token in hand is routinely
        already expired when a scheduled or manual check-in runs.  A 401 during the
        opening sequence used to end the run as a failure the console could still
        report as "triggered"; one renewal-and-retry turns that ordinary expiry
        into a normal success.  Only a credential refused *after* a renewal is
        reported as a real failure.
        """
        preflight = Outcome(account_id=account.id, account_name=account.name or account.id)
        # The mainland deployment answers the same status/claim endpoints as the
        # international one, so a cn account checks in like any other.  The gate
        # stays because an operator may know their mainland accounts cannot, and
        # then the skip is a deliberate choice rather than a silent one.
        if account.region == REGION_CN and not self._settings_fn().signin.check_in_cn:
            preflight.reason = "mainland check-in disabled (signin.check_in_cn)"
            return await _record_outcome(self._db, self._pool, account, preflight)

        try:
            return await self._attempt(account)
        except upstream.InvalidCredential as err:
            refused = str(err)
        except asyncio.TimeoutError as err:
            # A slow or unreachable network must fail *this account*, not the
            # whole run and not the HTTP request that triggered it.  `_prepare`
            # normalises its own timeout to an UpstreamError, but the status and
            # claim calls use a bare `wait_for` and would otherwise escape here.
            timeout_outcome = Outcome(account_id=account.id, account_name=account.name or account.id)
            timeout_outcome.status = SIGNIN_FAILED
            timeout_outcome.reason = f"network timeout during check-in: {err or 'no response'}"[:300]
            return await _record_outcome(self._db, self._pool, account, timeout_outcome)

        renewal = await self._renew(account)
        renewed_ok = bool(renewal is not None and getattr(renewal, "ok", False))
        if not renewed_ok:
            # A transport failure is not a verdict on the credential.  When the
            # renewal could not even reach the network, the account is left as it
            # was and reported as skipped, not failed: the next run (or the
            # keepalive sweep) gets another chance, and a temporary outage must
            # not look like a dead account.  `None` means no renewer is wired at
            # all (a setup gap, still not the credential's fault).
            transient = bool(renewal is not None and getattr(renewal, "transient", False))
            if transient or renewal is None:
                preflight.status = SIGNIN_SKIPPED
                preflight.reason = (
                    f"renewal could not reach the network ({refused}); will retry"
                    if transient else f"credential refused and no renewer is wired ({refused})"
                )
                return await _record_outcome(self._db, self._pool, account, preflight)
            preflight.status = SIGNIN_FAILED
            preflight.reason = f"credential refused, not renewable: {refused}"[:300]
            return await _record_outcome(self._db, self._pool, account, preflight)

        fresh = await self._db.account_by_id(account.id) or account
        # Put the renewed credential into the pool's cache without reloading: a
        # reload would rebuild the account objects under the leases the rest of
        # this run is holding, and leaving the stale token cached is what makes
        # the next request fail all over again.
        await self._pool.note_saved(fresh)
        try:
            return await self._attempt(fresh)
        except upstream.InvalidCredential as err:
            retry = Outcome(account_id=fresh.id, account_name=fresh.name or fresh.id)
            retry.status = SIGNIN_FAILED
            retry.reason = f"credential refused even after renewal: {err}"[:300]
            return await _record_outcome(self._db, self._pool, fresh, retry)

    async def _renew(self, account: Account) -> Any:
        """Renew one account's credential; return the renewal result.

        Returns the ``RenewResult`` (or ``None`` when there is nothing to renew
        with) rather than a bare bool, because the caller has to tell two
        outcomes apart: a credential the upstream *refused* is dead, but a
        transport failure (a blinking proxy, a dropped tunnel) says nothing about
        the credential at all.  Collapsing both into "not renewable" is what
        marked healthy accounts failed when the network hiccuped mid-renewal.

        Deliberately does not reload the pool: a daily run holds leases, and a
        reload would rebuild the cache under them.  The database is the record —
        the caller re-reads the account from it and keeps going; the pool's copy
        converges on the next write or reload.  A token that only ever lives in
        the pool cache but not in the row is the bug that lets the next request
        use a dead one again.
        """
        if self.renewer is None:
            return None
        try:
            # Bounded: a renewal that hangs must not stall the run.  The renewer
            # does its own network with a 30s client timeout, so 90s is generous
            # headroom against a slow proxy while still guaranteeing this returns.
            return await asyncio.wait_for(self.renewer(account.id), timeout=90.0)
        except asyncio.TimeoutError:
            return None
        except Exception:  # noqa: BLE001 - a renewer that raises is "no renewal"
            return None

    async def _attempt(self, account: Account) -> Outcome:
        """One check-in pass against a credential presumed usable.

        May raise ``InvalidCredential``; ``run_account`` decides whether that is
        worth renewing for.  Split out so the retry runs the whole opening sequence
        again with the fresh token rather than resuming half way through it.
        """
        outcome = Outcome(account_id=account.id, account_name=account.name or account.id)
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
        except asyncio.TimeoutError as err:
            # `wait_for` raises the *builtin* TimeoutError, not an UpstreamError,
            # so it used to escape every handler and surface as a raw 500 in the
            # console.  A slow network is an ordinary per-account outcome, not a
            # crash: report it as this account's failure and let the run continue.
            raise upstream.UpstreamError(
                f"prepare timed out after {timeout * 3:.0f}s (network slow or unreachable)"
            ) from err
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
