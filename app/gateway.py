"""The API surface: `/health`, `/v1/models`, `/v1/chat/completions`, `/v1/messages`.

**Both dialects, one turn.**  OpenAI is the primary surface; Anthropic's
`/v1/messages` is served from the same machinery because half the tools in common
use speak only one of the two.  They differ in more than field names — Anthropic
separates thinking from answer into numbered blocks — so each has its own
serialiser over one shared ``Result``, rather than one serialiser with a dialect
flag threading through every branch.

**Failover is the request's loop, not the pool's.**  Each attempt asks for an
account it has *not* already tried.  A failure that is the account's fault moves
on to the next one; a failure that is the request's own — an unusable prompt, an
unreachable upstream — stops immediately, because spending three accounts on it
would only exhaust them.

**Failures before the first byte are HTTP errors.**  A request that has already
sent tokens cannot become a clean status code, so a mid-stream failure is an SSE
error frame instead.  Splitting on the first byte is the only way a client can
tell "retry everything" from "this stream died".
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, StreamingResponse

from . import model_api as proxy
from . import pool as pool_mod
from . import upstream
from .records import (
    AUDIT_ERROR,
    AUDIT_OK,
    Audit,
    ModelConfig,
)
from .security import new_id


class APIError(Exception):
    """A failure with exactly one HTTP shape."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "type": "api_error"}}


def estimate_tokens(text: str) -> int:
    """A token count without a tokenizer.

    Deliberately approximate, and in the safe direction: it is counted and
    displayed, never billed, and a tokenizer is a dependency this bridge does not
    otherwise need.  CJK characters are counted one per character because four
    Latin characters are not a Chinese character.
    """
    if not text:
        return 0
    wide = sum(1 for char in text if ord(char) > 0x2E80)
    return max(1, int(wide + (len(text) - wide) / 4))


@dataclass
class ChatRequest:
    """A parsed request, in the parts the bridge actually uses."""

    model: str
    stream: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    """What one request writes down about itself.

    Gathered as the request runs rather than after it, because a generator that
    fails halfway has already lost the fields a post-hoc audit would need.
    """

    started_at: float = field(default_factory=time.time)
    model: str = ""
    account_name: str = ""
    stream: bool = False
    retries: int = 0
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    status: int = 200
    outcome: str = AUDIT_OK
    error: str = ""
    request_body: str = ""
    response_body: str = ""
    prompt_text: str = ""
    ip: str = ""
    user_agent: str = ""

    def finish(self, *, status: int, error: BaseException | None = None) -> None:
        self.status = status
        self.outcome = AUDIT_ERROR if (error is not None or status >= 400) else AUDIT_OK
        if error is not None:
            self.error = str(error)[:500]

    def record(self) -> Audit:
        return Audit(
            id=new_id(),
            created_at=self.started_at,
            model=self.model,
            account_name=self.account_name,
            status=self.status,
            outcome=self.outcome,
            latency_ms=self.latency_ms,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            stream=self.stream,
            retries=self.retries,
            ip=self.ip,
            user_agent=self.user_agent[:300],
            error=self.error,
            request_body=self.request_body,
            response_body=self.response_body[:32_000],
        )


class Gateway:
    """Owns the API surface.  Everything a request needs is a field here."""

    def __init__(self, db: Any, pool: Any, client: Any, media: Any, settings_fn: Any) -> None:
        self._db = db
        self._pool = pool
        self._client = client
        self._media = media
        self._settings_fn = settings_fn

    # ------------------------------------------------------------------ models

    async def models(self) -> dict[str, Any]:
        entries = [model for model in await self._db.list_models() if model.enabled]
        return {
            "object": "list",
            "data": [
                {
                    "id": model.id,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "minimax",
                    "description": model.description,
                }
                for model in entries
            ],
        }

    async def resolve_model(self, model_id: str) -> ModelConfig:
        """Look up a catalogue entry.

        An unknown model is refused rather than defaulted: substituting one
        silently would make a typo look like a successful answer.
        """
        model = await self._db.model_by_id((model_id or "").strip())
        if model is None or not model.enabled:
            known = ", ".join(entry.id for entry in await self._db.list_models() if entry.enabled)
            raise APIError(
                400,
                "model_not_found",
                f"model {model_id!r} is not available; available: {known}",
            )
        return model

    async def health(self) -> dict[str, Any]:
        usable = self._pool.usable_count()
        settings = self._settings_fn()
        return {
            "status": "ok" if usable else "degraded",
            "accounts": len(self._pool.accounts()),
            "usableAccounts": usable,
            "signinEnabled": settings.signin.enabled,
            "upstream": settings.upstream.base_url,
            "time": int(time.time()),
        }

    # --------------------------------------------------------------- endpoints

    async def chat_completions(self, request: Any, body: dict[str, Any]) -> Any:
        chat = self.parse_chat(body)
        trace = self._trace(request, chat)
        model = await self.resolve_model(chat.model)
        return await self._respond(request, chat, model, trace, "openai")

    async def anthropic_messages(self, request: Any, body: dict[str, Any]) -> Any:
        chat = self.parse_chat(body, anthropic=True)
        trace = self._trace(request, chat)
        model = await self.resolve_model(chat.model)
        return await self._respond(request, chat, model, trace, "anthropic")

    def parse_chat(self, body: Any, *, anthropic: bool = False) -> ChatRequest:
        """Read a request body, refusing the ones that cannot be served.

        The body is forwarded as structured messages, so this only validates the
        shape it needs to route on: a non-empty ``messages`` array and a model.
        The upstream expects the caller's own message shape, so nothing is
        flattened or reshaped here.
        """
        if not isinstance(body, dict):
            raise APIError(400, "invalid_request", "request body must be a JSON object")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise APIError(400, "invalid_request", "messages must be a non-empty array")

        model_id = body.get("model")
        model_id = model_id.strip() if isinstance(model_id, str) else ""
        if not model_id:
            raise APIError(400, "invalid_request", "model is required")
        return ChatRequest(model=model_id, stream=bool(body.get("stream")), raw=body)

    def _trace(self, request: Any, chat: ChatRequest) -> Trace:
        # The direct model API takes structured messages, so there is no flattened
        # prompt to record; the request body itself is the audit trail.
        trace = Trace(stream=chat.stream, model=chat.model)
        client = request.client
        trace.ip = client.host if client else ""
        trace.user_agent = request.headers.get("user-agent") or ""
        audit = self._settings_fn().audit
        if audit.record_body:
            trace.request_body = json.dumps(chat.raw, ensure_ascii=False)[: max(256, audit.body_limit_bytes)]
        return trace

    # ---------------------------------------------------------------- answering

    async def _respond(
        self, request: Any, chat: ChatRequest, model: ModelConfig, trace: Trace, dialect: str
    ) -> Any:
        """Answer one turn through the direct model API.

        The upstream is Anthropic-shaped, so an Anthropic caller is forwarded as
        it stands and an OpenAI caller is translated on the way out and back on
        the way in.  Either way the body is sent as structured messages: no
        flattened prompt, no server-side session, no echo.
        """
        if dialect == "anthropic":
            upstream_body = self._anthropic_body(chat, model)
        else:
            upstream_body = proxy.openai_to_anthropic(chat.raw, model.upstream_model)

        if chat.stream:
            return StreamingResponse(
                self._stream(model, trace, upstream_body, dialect),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            response = await self._run_llm(upstream_body, trace, stream=False)
            raw = await response.aread()
            await response.aclose()
        except upstream.InvalidCredential as err:
            trace.finish(status=502, error=err)
            await self._db.append_audit(trace.record())
            raise APIError(502, "upstream_error", str(err)) from err
        except upstream.UpstreamError as err:
            trace.finish(status=502, error=err)
            await self._db.append_audit(trace.record())
            raise APIError(502, "upstream_error", str(err)) from err
        except pool_mod.NoAvailableAccount as err:
            trace.finish(status=503, error=err)
            await self._db.append_audit(trace.record())
            raise APIError(503, "no_account", err.reason) from err

        try:
            message = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as err:
            trace.finish(status=502, error=err)
            await self._db.append_audit(trace.record())
            raise APIError(502, "upstream_error", "upstream returned invalid JSON") from err

        usage = message.get("usage") or {}
        trace.prompt_tokens = int(usage.get("input_tokens") or 0)
        trace.completion_tokens = int(usage.get("output_tokens") or 0)
        trace.response_body = json.dumps(message, ensure_ascii=False)[: self._settings_fn().audit.body_limit_bytes]
        await self._db.record_model_usage(model.id, 1, trace.completion_tokens)
        trace.finish(status=200)
        await self._db.append_audit(trace.record())

        if dialect == "anthropic":
            return JSONResponse(message)
        return JSONResponse(proxy.anthropic_to_openai(message, model.id))

    def _anthropic_body(self, chat: ChatRequest, model: ModelConfig) -> dict[str, Any]:
        """Read the caller's Anthropic body, swapping in the upstream model id."""
        body = dict(chat.raw)
        body["model"] = model.upstream_model
        body.pop("stream", None) if not chat.stream else None
        if chat.stream:
            body["stream"] = True
        if "max_tokens" not in body:
            body["max_tokens"] = 4096
        return body

    async def _stream(
        self,
        model: ModelConfig,
        trace: Trace,
        upstream_body: dict[str, Any],
        dialect: str,
    ) -> AsyncIterator[bytes]:
        """Relay the upstream SSE stream, translating for OpenAI callers.

        Anthropic callers get the upstream's own frames; OpenAI callers get them
        converted.  The account is released only when the stream is finished, so
        the retry-on-another-account rule stays per-request.
        """
        upstream_body["stream"] = True
        try:
            response = await self._run_llm(upstream_body, trace, stream=True)
        except BaseException as err:  # noqa: BLE001 - reported as a frame below
            trace.finish(status=502, error=err)
            await self._db.append_audit(trace.record())
            yield _error_frame(err, dialect)
            return

        frames = _aiter_sse(response)
        if dialect == "openai":
            frames = proxy.anthropic_stream_to_openai(frames, model.id)
        try:
            async for frame in frames:
                yield frame
        except BaseException as err:  # noqa: BLE001 - partial output already sent
            trace.finish(status=502, error=err)
            await self._db.append_audit(trace.record())
            return
        finally:
            await response.aclose()

        trace.finish(status=200)
        await self._db.append_audit(trace.record())

    async def _run_llm(
        self, body: dict[str, Any], trace: Trace, *, stream: bool
    ) -> Any:
        """The failover loop for the direct model API.

        Only failures that were the *account's* fault advance to the next attempt;
        a credential refused by the upstream is reported at once rather than
        retried into the same rejection.
        """
        attempts = max(1, self._settings_fn().routing.max_attempts)
        tried: set[str] = set()
        renewed: set[str] = set()
        last: BaseException | None = None
        attempt = 0

        while attempt < attempts:
            if attempt:
                await asyncio.sleep(min(0.25 * attempt, 1.0))
            try:
                lease = await self._pool.acquire(exclude=tried)
            except pool_mod.NoAvailableAccount as err:
                if last is not None:
                    raise last
                raise

            trace.retries = attempt
            trace.account_name = lease.name
            credential = upstream.credential_of(lease.account)
            started = time.monotonic()
            released = False
            try:
                try:
                    response = await self._client.llm(credential, body, stream=stream)
                except upstream.InvalidCredential as err:
                    # Hand the refusal to the pool: for an OAuth account it renews
                    # the token and re-reads the row, so an expired one-hour token
                    # costs one extra round-trip rather than a failed request.
                    # The account is only excluded when renewal did not restore
                    # it, which lets the next iteration retry the same account
                    # with its fresh token.
                    await self._pool.release(lease, success=False, error=err)
                    released = True
                    refreshed = self._pool.account(lease.account.id)
                    if refreshed is not None and refreshed.status == "active":
                        # Renewal worked: give the same account one immediate
                        # retry without spending the attempt budget, so a pool
                        # configured with a single attempt still recovers.
                        if lease.account.id not in renewed:
                            renewed.add(lease.account.id)
                            continue
                        tried.add(lease.account.id)
                    else:
                        tried.add(lease.account.id)
                    last = err
                    attempt += 1
                    continue
                except upstream.UpstreamError as err:
                    await self._pool.release(lease, success=False, error=err)
                    released = True
                    tried.add(lease.account.id)
                    last = err
                    attempt += 1
                    continue
                except Exception as err:  # noqa: BLE001 - no upstream failure may escape
                    await self._pool.release(lease, success=False, error=err)
                    released = True
                    tried.add(lease.account.id)
                    last = err
                    attempt += 1
                    continue

                await self._pool.release(lease, success=True)
                released = True
                trace.latency_ms = int((time.monotonic() - started) * 1000)
                return response
            finally:
                if not released:
                    await asyncio.shield(
                        self._pool.release(lease, success=False, error=None)
                    )

        assert last is not None
        raise last

def _aiter_sse(response: Any) -> AsyncIterator[bytes]:
    """Yield the upstream's SSE frames one whole event at a time.

    ``aiter_bytes`` — not ``aiter_raw`` — because the transport may have
    negotiated gzip and the frames have to be decoded before a client can read
    them.  Chunks may split or merge events, so the blank-line delimiter is what
    is forwarded, not the transport chunk.
    """
    buffer = b""

    async def generate() -> AsyncIterator[bytes]:
        nonlocal buffer
        async for chunk in response.aiter_bytes():
            buffer += chunk
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                yield frame + b"\n\n"
        if buffer.strip():
            yield buffer

    return generate()


def _sse(event: str | None, payload: dict[str, Any]) -> bytes:
    head = f"event: {event}\n" if event else ""
    return (head + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _error_frame(err: BaseException, dialect: str) -> bytes:
    message = str(err)
    if dialect == "anthropic":
        return _sse("error", {"type": "error", "error": {"type": "api_error", "message": message}})
    return _sse(None, {"error": {"message": message, "type": "server_error"}})
