"""Video generation: a console-only feature, off the public API surface.

The upstream has no video endpoint.  A turn that mentions `@video-creater`
routes to the account's video plugin, the plugin's agent submits the job
through its own Connector tool, and the finished file lands in the account's
cloud drive.  The conversation stream carries the agent's prose and never the
mp4, so the answer has to be picked up from the drive afterwards.

That makes a video asynchronous by nature, so it is modelled as a job:

    submit -> stream the turn -> poll the drive -> download -> done

The service owns the polling loop, not the request that submitted the job.  A
POST never waits on the render; the job row does.  This also means a stream
that dies mid-turn (a proxy reset, a closed tab) does not lose the work: the
probe rounds showed the upstream keeps going regardless — it opens the session,
runs the tools, bills the credits, and files the video — so the poller must
treat a broken stream as an *inconclusive turn*, not a failed job, and go look
in the drive anyway.  That single rule is the difference between the first
probe round's two "failures" and the two videos they actually produced.

Why the console and not `/v1`: the public bridge is a token-metered,
OpenAI-compatible surface whose routing and accounting are built around chat
turns.  A video turn bills *account credits* rather than tokens, runs for
minutes, and produces a file rather than a stream.  Keeping it behind
`/admin/api/videos` also keeps the account choice explicit: the operator picks
which account burns credit, instead of a public caller discovering it.

Finished videos go to ``video.videos_dir`` (``data/videos`` by default), a
directory of its own.  Chat images are cache under a byte ceiling and get
pruned; a video the operator asked for is theirs, and is never pruned under
them.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import upstream
from .records import (
    VIDEO_DONE,
    VIDEO_DOWNLOADING,
    VIDEO_FAILED,
    VIDEO_RENDERING,
    VIDEO_RUNNING,
    VideoJob,
    now_ts,
)
from .security import new_id

log = logging.getLogger("video")

# A generated clip is far larger than a generated image, so the ceiling is
# higher than the media store's 32MB — but a ceiling still has to exist, or one
# bad response decides how much memory the process uses.  Half a gigabyte
# comfortably covers the durations and resolutions the plugin accepts.
_MAX_VIDEO_BYTES = 512 * 1024 * 1024

# What the console may name as a video model.  Deliberately *not* the shared
# model catalogue: a video entry in `models` would surface in `/v1/models` and
# invite public callers to spend account credits through the token-metered
# surface, which is exactly the contamination this split exists to avoid.
VIDEO_MODELS: dict[str, dict[str, str]] = {
    "minimax-h3-max": {
        "name": "MiniMax H3 Max",
        "upstream": "MiniMax-H3-Max",
        "note": "约 20 秒出片，消耗账号积分",
    },
    "minimax-h3": {
        "name": "MiniMax H3.0",
        "upstream": "MiniMax-H3",
        "note": "质量优先，15-30 分钟，消耗账号积分",
    },
    "minimax-hailuo-2-3": {
        "name": "MiniMax Hailuo 2.3",
        "upstream": "MiniMax-Hailuo-2.3",
        "note": "低成本，可用 Token Plan，输出无声视频",
    },
}


def model_catalog() -> list[dict[str, Any]]:
    return [
        {"id": key, "name": item["name"], "upstreamModel": item["upstream"], "note": item["note"]}
        for key, item in VIDEO_MODELS.items()
    ]


class VideoError(Exception):
    """A job that cannot proceed, with a message the console can show."""


class VideoService:
    """Owns video jobs: submission, drive polling, downloads, and restart recovery.

    One service instance per process, handed the pool and the upstream client
    but never the gateway: nothing in here may be reachable from `/v1`.
    """

    def __init__(
        self,
        db: Any,
        pool: Any,
        client: upstream.MiniMaxClient,
        settings_fn: Callable[[], Any],
    ) -> None:
        self._db = db
        self._pool = pool
        self._client = client
        self._settings_fn = settings_fn
        self._tasks: dict[str, asyncio.Task] = {}
        self._slots: asyncio.Semaphore | None = None
        self._slot_size = 0

    # ------------------------------------------------------------------ public

    async def submit_json(
        self,
        *,
        prompt: str,
        model_id: str,
        account_id: str,
        duration: int = 0,
        ratio: str = "",
        resolution: str = "",
    ) -> dict[str, Any]:
        job = await self.submit(
            prompt=prompt,
            model_id=model_id,
            account_id=account_id,
            duration=duration,
            ratio=ratio,
            resolution=resolution,
        )
        return job.to_json()

    async def get_json(self, job_id: str) -> dict[str, Any]:
        job = await self._db.video_job(job_id)
        if job is None:
            raise VideoError(f"job {job_id!r} not found")
        return job.to_json()

    async def list_json(self, limit: int = 100) -> list[dict[str, Any]]:
        return [job.to_json() for job in await self._db.list_video_jobs(limit)]

    async def submit(
        self,
        *,
        prompt: str,
        model_id: str,
        account_id: str,
        duration: int = 0,
        ratio: str = "",
        resolution: str = "",
    ) -> VideoJob:
        """Record the job and start its task.  Returns immediately.

        Validation is here rather than in the route so a console submit and any
        future caller enforce the same contract, and so an unknown model is
        refused before an account is leased.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise VideoError("prompt is required")
        entry = VIDEO_MODELS.get((model_id or "").strip())
        if entry is None:
            known = ", ".join(VIDEO_MODELS)
            raise VideoError(f"unknown video model {model_id!r}; known: {known}")
        account = await self._db.account_by_id((account_id or "").strip())
        if account is None:
            raise VideoError(f"account {account_id!r} not found")
        if not account.agent_id:
            raise VideoError(f"account {account.name or account.id} has no agent id; prepare it first")

        settings = self._settings_fn()
        job = VideoJob(
            id="video_" + new_id(12),
            prompt=prompt[:4000],
            model=entry["upstream"],
            duration=int(duration) if duration else settings.video.default_duration,
            ratio=(ratio or "").strip() or settings.video.default_ratio,
            resolution=(resolution or "").strip() or settings.video.default_resolution,
            account_id=account.id,
            created_at=now_ts(),
            updated_at=now_ts(),
        )
        await self._db.add_video_job(job)
        self._start(job.id)
        return job

    async def cancel(self, job_id: str) -> bool:
        """Stop a running job's task.  A submitted turn upstream is not undone.

        There is no upstream cancel for a render already accepted by the
        plugin; the credits are spent whether or not we keep watching for the
        file.  What cancel does stop is *this process* spending further requests
        on a job the operator no longer wants.
        """
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        job = await self._db.video_job(job_id)
        if job is not None and job.status not in (VIDEO_DONE, VIDEO_FAILED):
            await self._fail(job_id, "cancelled")
            return True
        return False

    async def delete(self, job_id: str) -> bool:
        """Drop the job's row and its file.  Refuses an unfinished job.

        The file goes only with the row: a record deleted while its video still
        sits on disk would strand an orphan nobody can attribute.
        """
        job = await self._db.video_job(job_id)
        if job is None:
            return False
        if job.status not in (VIDEO_DONE, VIDEO_FAILED):
            raise VideoError("job is still running; cancel it first")
        task = self._tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
        await self._db.delete_video_job(job_id)
        if job.media_name:
            path = self._file_path(job.media_name)
            if path is not None:
                try:
                    path.unlink()
                except OSError as err:
                    log.warning("video %s: file remove failed: %s", job_id, err)
        return True

    async def resume_pending(self) -> int:
        """Restart the watcher for every unfinished job.  Called on boot.

        The whole point of storing the session id is that a render outlives the
        process that asked for it: the upstream kept working through a restart
        of the bridge, the only thing lost was our watching.  Resume is a free
        drive read, never a new turn — no session is created, no message is
        sent, and nothing is billed twice.
        """
        count = 0
        for job in await self._db.active_video_jobs():
            self._start(job.id)
            count += 1
        if count:
            log.info("resumed %d unfinished video job(s)", count)
        return count

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def file_for(self, job_id: str) -> tuple[Path, VideoJob] | None:
        job = await self._db.video_job(job_id)
        if job is None or job.status != VIDEO_DONE or not job.media_name:
            return None
        path = self._file_path(job.media_name)
        if path is None or not path.is_file():
            return None
        return path, job

    # ------------------------------------------------------------------ paths

    def videos_dir(self) -> Path:
        settings = self._settings_fn()
        return Path(settings.video.videos_dir or "./videos")

    def _file_path(self, media_name: str) -> Path | None:
        """A stored name, never a stored path.

        The name is only ever produced here (`<job id>.mp4`), and the shape is
        re-checked on the way back out so a hand-edited row cannot point at
        anything outside the directory.
        """
        if not media_name or "/" in media_name or "\\" in media_name or ".." in media_name:
            return None
        candidate = self.videos_dir() / media_name
        base = self.videos_dir().resolve()
        resolved = candidate.resolve()
        if not str(resolved).startswith(str(base)):
            return None
        return resolved

    # ------------------------------------------------------------------ tasks

    def _start(self, job_id: str) -> None:
        if job_id in self._tasks and not self._tasks[job_id].done():
            return
        self._tasks[job_id] = asyncio.create_task(
            self._guarded(job_id), name=f"video-job-{job_id}"
        )

    def _slot_limit(self) -> asyncio.Semaphore:
        """The in-flight ceiling, rebuilt only when the setting is raised.

        Lowering the setting takes effect on the next restart rather than
        yanking a slot out from under a running job — a video turn holds an
        account for minutes, and cancelling mid-render spends the credit for
        nothing.
        """
        limit = max(1, int(self._settings_fn().video.max_concurrent))
        if self._slots is None or self._slot_size < limit:
            self._slots = asyncio.Semaphore(limit)
            self._slot_size = limit
        return self._slots

    async def _guarded(self, job_id: str) -> None:
        try:
            await self._run(job_id)
        except asyncio.CancelledError:
            await self._fail(job_id, "cancelled")
            raise
        except VideoError as err:
            await self._fail(job_id, str(err))
        except Exception as err:  # noqa: BLE001 - a lost task is worse than a failed job
            log.exception("video job %s crashed", job_id)
            await self._fail(job_id, f"{type(err).__name__}: {err}"[:500])
        finally:
            self._tasks.pop(job_id, None)

    async def _run(self, job_id: str) -> None:
        job = await self._db.video_job(job_id)
        if job is None:
            return
        # A job that reached a terminal state is finished for good.  Resume only
        # hands over unfinished rows, but `_start` can be reached from other
        # places, and re-entering the turn or a harvest on a done job would
        # either spend a second turn or overwrite a file that already landed.
        if job.status in (VIDEO_DONE, VIDEO_FAILED):
            return
        settings = self._settings_fn()
        slot = self._slot_limit()
        async with slot:
            if not job.session_id:
                await self._turn(job_id)
            await self._watch(job_id, settings)

    # -------------------------------------------------------------------- turn

    async def _turn(self, job_id: str) -> None:
        """One video turn: enable, session, message.  The stream is best-effort.

        The order is load-bearing.  The session id is stored the moment
        create_session answers, *before* the message goes out, because a stream
        that resets after that point still describes a turn the upstream
        accepted — and without the id, the file it later produces has no way
        back to this job.  The probe rounds proved both halves of that.

        A broken stream is deliberately not treated as an account failure.  The
        pool would cool the account, yet the probe showed the upstream kept
        running and filed the video anyway — cooling a healthy account because
        *our* read died would penalise the wrong thing and strand a good account.
        Only a refused credential (`InvalidCredential`) counts against the
        account; everything else releases the lease with its health untouched,
        and the drive poll decides whether the job actually produced anything.
        """
        job = await self._db.video_job(job_id)
        if job is None:
            return
        settings = self._settings_fn()
        account = await self._db.account_by_id(job.account_id)
        if account is None:
            raise VideoError("account vanished from the pool")
        await self._mutate(job_id, status=VIDEO_RUNNING)

        lease = await self._pool.acquire(allow={account.id})
        cred = upstream.credential_of(lease.account)
        served = False
        fatal: upstream.UpstreamError | None = None
        try:
            if settings.video.auto_enable_plugin:
                # Free and idempotent.  A turn against an account without the
                # plugin is a guaranteed waste of credit: the agent looks for a
                # tool that is not there, thinks for minutes, and answers prose.
                try:
                    await self._client.enable_video_plugin(cred, settings.video.plugin_name)
                except upstream.InvalidCredential as err:
                    fatal = err
                    raise
                except upstream.UpstreamError as err:
                    log.warning("video %s: plugin enable failed: %s", job_id, err)

            try:
                before = await self._client.credit(cred)
                await self._mutate(job_id, credit_before=before.total)
            except upstream.InvalidCredential as err:
                fatal = err
                raise
            except upstream.UpstreamError:
                pass  # an unreadable balance must not stop the turn it precedes

            # A session id is the one fact the watcher cannot reconstruct, so it
            # is captured before anything that could consume the turn's budget.
            try:
                session_id = await self._client.create_session(cred)
            except upstream.InvalidCredential as err:
                fatal = err
                raise
            started_ms = int(time.time() * 1000)
            await self._mutate(job_id, session_id=session_id, started_at_ms=started_ms)

            text = upstream.video_turn_text(
                job.prompt,
                job.model,
                plugin=settings.video.plugin_name,
                tag=settings.video.options_tag,
                duration=job.duration,
                ratio=job.ratio,
                resolution=job.resolution,
            )
            options = upstream.Options(
                text=text,
                client_intent=upstream.VIDEO_CLIENT_INTENT,
                timeout=float(settings.video.turn_timeout_sec),
                idle_timeout=float(settings.video.idle_timeout_sec),
            )
            try:
                result = await self._client.send_message(
                    cred, options, session_id, float(settings.video.turn_timeout_sec)
                )
                if isinstance(result, tuple):  # a partial answer and the error beside it
                    result, stream_error = result
                    log.warning("video %s: stream broke: %s", job_id, stream_error)
                await self._mutate(job_id, detail=(result.text or "")[:8000])
                served = True
            except upstream.InvalidCredential as err:
                fatal = err
                raise
            except upstream.UpstreamError as err:
                # Inconclusive, not failed: the turn may have been accepted
                # before the connection died.  Record what the caller should
                # know and let the watcher decide by looking at the drive.
                log.warning("video %s: turn error, still watching: %s", job_id, err)
                await self._mutate(job_id, detail=str(err)[:2000])
        finally:
            # `served` is the health signal.  A credential the upstream refused
            # retires the account; a stream that merely died leaves it whole.
            await self._pool.release(
                lease, success=served, error=fatal if not served else None
            )

    async def _mutate(self, job_id: str, **fields: Any) -> None:
        """Write named job fields."""

        def apply(job: VideoJob) -> None:
            for key, value in fields.items():
                setattr(job, key, value)

        await self._db.update_video_job(job_id, apply)

    # ------------------------------------------------------------------- watch

    async def _watch(self, job_id: str, settings: Any) -> None:
        """Poll the drive until the file lands or the budget runs out.

        The cutoff is the instant the message was sent, so a long-lived
        session's older files are not mistaken for this turn's answer: the
        session is the scope of the listing, the timestamp is what makes the
        answer *this* job's.  A resume after a restart re-reads the stored
        cutoff rather than inventing one.
        """
        job = await self._db.video_job(job_id)
        if job is None or not job.session_id:
            raise VideoError("no session to watch")
        account = await self._db.account_by_id(job.account_id)
        if account is None:
            raise VideoError("account vanished from the pool")
        cred = upstream.credential_of(account)
        await self._mutate(job_id, status=VIDEO_RENDERING)

        started_ms = job.started_at_ms or int(job.created_at * 1000)
        budget = float(settings.video.poll_timeout_sec)
        deadline = time.monotonic() + budget
        videos: list[dict[str, Any]] = []

        while time.monotonic() < deadline:
            if job.node_id:
                # A resume found the artefact already recorded: the previous
                # process had got as far as naming the file before it died.
                videos = [{"node_id": job.node_id}]
                break
            current = self._settings_fn()
            await asyncio.sleep(
                max(5, int(current.video.poll_gap_sec)) + random.uniform(0, max(0, int(current.video.poll_jitter_sec)))
            )
            job = await self._db.video_job(job_id)
            # Deleted or cancelled while we slept: stop looking, and do not
            # write a failure onto a row the operator has already thrown away.
            if job is None or job.status in (VIDEO_DONE, VIDEO_FAILED):
                return
            if job.node_id:
                videos = [{"node_id": job.node_id}]
                break
            try:
                artifacts = await self._client.session_artifacts(cred, job.session_id)
            except upstream.UpstreamError as err:
                # A failed lookup is not a failed job: "no media" and "the
                # lookup broke" must stay different answers, and one transient
                # 401 (an expiring token the keeper will renew) must not write
                # the job off.
                log.warning("video %s: summaries failed: %s", job_id, err)
                continue
            fresh = [
                item
                for item in artifacts
                if int(item.get("created_at") or 0) >= started_ms and _is_video(item)
            ]
            if fresh:
                videos = fresh
                await self._mutate(job_id, node_id=str(fresh[0].get("node_id") or ""))
                break

        if not videos:
            # Not a verdict that the render failed.  The upstream bills whether
            # or not it produces a file, and a slow model's artefact can land
            # after this window closes — the probe rounds showed exactly that
            # decoupling.  The message says what is actually known: nothing was
            # seen in the window, and the work may still be running upstream.
            await self._settle_failure(
                job_id,
                cred,
                "no artefact within the polling window; the upstream may still be rendering "
                "(generation is billed regardless of this lookup)",
            )
            return

        await self._harvest(job_id, cred, str(videos[0].get("node_id") or ""), settings)
        try:
            after = await self._client.credit(cred)
            await self._mutate(job_id, credit_after=after.total)
        except upstream.UpstreamError:
            pass

    async def _harvest(self, job_id: str, cred: upstream.Credential, node_id: str, settings: Any) -> None:
        if not node_id:
            raise VideoError("artefact without a node id")
        await self._mutate(job_id, status=VIDEO_DOWNLOADING)
        link = await self._client.drive_download_url(cred, node_id)
        if not link:
            raise VideoError("drive answered without a download url")
        body = await self._fetch(link, settings)
        name = f"{job_id}.mp4"
        path = self._file_path(name)
        if path is None:
            raise VideoError("internal: unsafe media name")
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, body)
        await self._mutate(
            job_id, status=VIDEO_DONE, media_name=name, file_size=len(body), error=""
        )
        log.info("video %s: done, %d bytes -> %s", job_id, len(body), path)

    async def _fetch(self, url: str, settings: Any) -> bytes:
        """Pull the signed URL through the bridge's proxy rules.

        The generated file is served from the same CDN the account's egress is
        fenced to, so fetching it from a local address without the proxy fails
        the way an unproxied API call does — a reset that reads as "the CDN is
        down" but is not.

        The body is read in bounded chunks rather than one `aread()`, so a
        wrong or hostile response cannot pull an unbounded payload into memory:
        the chat media path caps a download the same way, and a video deserves
        a larger ceiling but still a ceiling.
        """
        host = upstream.urlsplit(url).hostname or ""
        client = self._client.public_client(host)
        timeout = httpx.Timeout(float(settings.video.download_timeout_sec), connect=15.0)
        chunks: list[bytes] = []
        total = 0
        async with client.stream("GET", url, timeout=timeout) as response:
            if response.status_code >= 400:
                raise VideoError(f"download HTTP {response.status_code}")
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _MAX_VIDEO_BYTES:
                    raise VideoError(
                        f"download exceeds {_MAX_VIDEO_BYTES // (1024 * 1024)}MB ceiling"
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    async def _settle_failure(self, job_id: str, cred: upstream.Credential, reason: str) -> None:
        try:
            after = await self._client.credit(cred)
            await self._mutate(job_id, credit_after=after.total)
        except upstream.UpstreamError:
            pass
        await self._fail(job_id, reason)

    async def _fail(self, job_id: str, reason: str) -> None:
        def apply(job: VideoJob) -> None:
            job.status = VIDEO_FAILED
            job.error = reason[:1000]

        await self._db.update_video_job(job_id, apply)
        log.warning("video %s: failed: %s", job_id, reason)


def _is_video(artifact: dict[str, Any]) -> bool:
    """Classify a drive artefact the way the drive itself declares it.

    `category` is the primary signal — it is the drive's own grouping and the
    more stable of the two — and the mime type the fallback, so an artefact
    under a category this bridge has never seen is still judged by what it
    actually is.
    """
    category = str(artifact.get("category") or "").strip().lower()
    if category in ("videos", "video"):
        return True
    return str(artifact.get("mime_type") or "").lower().startswith("video/")
