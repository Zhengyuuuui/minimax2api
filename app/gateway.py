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

from . import pool as pool_mod
from . import upstream
from .prompt import Turn, build_prompt, parse_turn
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
    messages: list[Turn] = field(default_factory=list)
    stream: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt(self) -> str:
        return build_prompt(self.messages)

    @property
    def images(self) -> list[str]:
        out: list[str] = []
        for turn in self.messages:
            out.extend(turn.image_urls())
        return out


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
        """Read a request body, refusing the ones that cannot be served."""
        if not isinstance(body, dict):
            raise APIError(400, "invalid_request", "request body must be a JSON object")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise APIError(400, "invalid_request", "messages must be a non-empty array")

        turns: list[Turn] = []
        # Anthropic's `system` is a separate field.  It is folded into the history
        # as its leading entry rather than dropped: the upstream has no role of its
        # own to map it onto, and an unlabelled instruction is how the web client
        # sends one.
        system = body.get("system")
        if isinstance(system, str) and system.strip():
            turns.append(Turn(role="system", content=system))
        elif isinstance(system, list):
            text = "\n".join(
                block.get("text", "")
                for block in system
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
            if text.strip():
                turns.append(Turn(role="system", content=text))

        for item in messages:
            turn = parse_turn(item)
            if turn is not None:
                turns.append(turn)
        if not turns:
            raise APIError(400, "invalid_request", "no usable messages in the request")

        model_id = body.get("model")
        model_id = model_id.strip() if isinstance(model_id, str) else ""
        if not model_id:
            raise APIError(400, "invalid_request", "model is required")
        return ChatRequest(model=model_id, messages=turns, stream=bool(body.get("stream")), raw=body)

    def _trace(self, request: Any, chat: ChatRequest) -> Trace:
        trace = Trace(stream=chat.stream, model=chat.model, prompt_text=chat.prompt)
        client = request.client
        trace.ip = client.host if client else ""
        trace.user_agent = request.headers.get("user-agent") or ""
        audit = self._settings_fn().audit
        if audit.record_body:
            trace.request_body = json.dumps(chat.raw, ensure_ascii=False)[: max(256, audit.body_limit_bytes)]
        return trace

    # ---------------------------------------------------------------- answering

    def _model_selection(self, model: ModelConfig) -> dict[str, Any]:
        """The upstream model selector for a catalogue entry.

        The upstream takes an object — ``{model_id, provider_id, variant}`` — not
        the id string, and rejects a bare string or a bare ``model_id``.  An entry
        with no ``upstream_model`` (the agent default) sends nothing, which lets
        the account's own default answer.
        """
        if not model.upstream_model:
            return {}
        selection: dict[str, Any] = {"model_id": model.upstream_model}
        if model.variant:
            selection["variant"] = model.variant
        return selection

    async def _respond(
        self, request: Any, chat: ChatRequest, model: ModelConfig, trace: Trace, dialect: str
    ) -> Any:
        prompt = chat.prompt
        trace.prompt_tokens = estimate_tokens(prompt)
        options = upstream.Options(
            text=prompt,
            timeout=float(self._settings_fn().upstream.request_timeout_sec),
            images=[upstream.UploadedImage(url=url) for url in chat.images],
            model=self._model_selection(model),
        )

        if chat.stream:
            return StreamingResponse(
                self._stream(request, chat, model, trace, options, dialect),
                media_type="text/event-stream",
                # Both headers matter: the second disables buffering in reverse
                # proxies, which would otherwise hold the whole stream back.
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            result = await self._run(options, trace)
            text = await self._finalize(result, trace, model)
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

        trace.finish(status=200)
        await self._db.append_audit(trace.record())
        if dialect == "anthropic":
            return JSONResponse(_anthropic_message(result, text, model.id, trace))
        return JSONResponse(_openai_message(text, model.id, trace))

    async def _stream(
        self,
        request: Any,
        chat: ChatRequest,
        model: ModelConfig,
        trace: Trace,
        options: upstream.Options,
        dialect: str,
    ) -> AsyncIterator[bytes]:
        try:
            result = await self._run(options, trace)
            text = await self._finalize(result, trace, model)
        except BaseException as err:  # noqa: BLE001 - anything left is reported here
            trace.finish(status=502, error=err)
            await self._db.append_audit(trace.record())
            # The first byte has already gone out, so this can only ever be a
            # frame: an HTTP status here would be ignored by a client that is
            # already reading the body.
            yield _error_frame(err, dialect)
            return

        trace.finish(status=200)
        await self._db.append_audit(trace.record())
        render = _openai_stream if dialect == "openai" else _anthropic_stream
        for frame in render(result, text, model.id, trace):
            yield frame

    async def _run(self, options: upstream.Options, trace: Trace) -> upstream.Result:
        """The failover loop.

        Only failures that were the *account's* fault advance to the next
        attempt, and the exclusion set is what enforces that: an account already
        tried is never taken again, whatever the routing settings say.
        """
        attempts = max(1, self._settings_fn().routing.max_attempts)
        tried: set[str] = set()
        last: BaseException | None = None

        for attempt in range(attempts):
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
                    result = await self._client.completion(credential, options)
                except upstream.AgentIDUnknown as err:
                    # Nothing is wrong with the account's health, it simply has
                    # no agent id yet.  No cooldown: that is for accounts being
                    # rate limited, and this one is not.
                    await self._pool.release(lease, success=False, error=None)
                    released = True
                    tried.add(lease.account.id)
                    last = err
                    continue
                except upstream.InvalidCredential as err:
                    await self._pool.release(lease, success=False, error=err)
                    released = True
                    raise
                except upstream.UpstreamError as err:
                    await self._pool.release(lease, success=False, error=err)
                    released = True
                    tried.add(lease.account.id)
                    last = err
                    continue
                except Exception as err:  # noqa: BLE001 - no upstream failure may escape
                    await self._pool.release(lease, success=False, error=err)
                    released = True
                    tried.add(lease.account.id)
                    last = err
                    continue

                await self._pool.release(lease, success=True)
                released = True
                trace.latency_ms = int((time.monotonic() - started) * 1000)
                return result
            finally:
                # A cancelled request must not keep its account out of the pool.
                # ``CancelledError`` is a BaseException, so none of the handlers
                # above catch it and the lease would otherwise be leaked with its
                # ``inflight`` count permanently incremented — which pins the
                # account at ``max_concurrent`` for the life of the process.
                # ``error=None`` gives the account back without recording a
                # failure: a client hanging up says nothing about the credential.
                if not released:
                    # Shielded: while the task is unwinding from a cancellation,
                    # any new ``await`` here would be cancelled too and the
                    # account would stay pinned.  The release is cheap and must
                    # still happen.
                    await asyncio.shield(
                        self._pool.release(lease, success=False, error=None)
                    )

        assert last is not None
        raise last

    async def _finalize(
        self, result: upstream.Result, trace: Trace, model: ModelConfig
    ) -> str:
        """Turn a finished turn into the text the client gets.

        Media is fetched here, before the serialisers run, so that a failed
        download leaves the upstream's URL in place rather than an empty link.
        """
        text = result.text
        if self._settings_fn().media.auto_download:
            for ref in result.media:
                item = await self._media.download(
                    ref.url,
                    prompt=trace.prompt_text[:2000],
                    model=model.id,
                    account_name=trace.account_name,
                )
                # The reference itself is updated, not just the text: an agent
                # that never wrote the markdown gets the appended link from the
                # same source, and it would otherwise point at a URL that is about
                # to expire.
                if item is not None:
                    ref.url = item.url
                    text = text.replace(item.source_url, item.url)
        text = _append_missing_media(text, result.media)
        trace.response_body = text
        trace.completion_tokens = estimate_tokens(text) + (
            estimate_tokens(result.thinking) if result.thinking else 0
        )
        # Counted once per request, not per token: the counters are a signal for
        # the console, not a billing log.
        await self._db.record_model_usage(model.id, 1, trace.completion_tokens)
        return text


def _append_missing_media(text: str, media: list[upstream.MediaRef]) -> str:
    """Link any generated asset the agent did not mention.

    The upstream usually writes the markdown itself.  When it does not, the image
    is still real and the client should still see it.
    """
    missing = [ref.url for ref in media if ref.url and ref.url not in text]
    if not missing:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + "\n".join(f"![image]({url})" for url in missing)


# --------------------------------------------------------------------- OpenAI


def _sse(event: str | None, payload: dict[str, Any]) -> bytes:
    head = f"event: {event}\n" if event else ""
    return (head + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _openai_stream(
    result: upstream.Result, text: str, model: str, trace: Trace
) -> list[bytes]:
    """The answer as a single content chunk, then the stop frame.

    One chunk rather than one per token: the upstream's stream is not the client's
    — it pauses between tool calls — so splitting it would produce a tokenizer
    that does not exist, at the cost of a stream that stalls.
    """
    created = int(time.time())
    response_id = "chatcmpl-" + new_id()

    def chunk(delta: dict[str, Any], finish: str | None = None, usage: bool = False) -> bytes:
        payload: dict[str, Any] = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage:
            payload["usage"] = {
                "prompt_tokens": trace.prompt_tokens,
                "completion_tokens": trace.completion_tokens,
                "total_tokens": trace.prompt_tokens + trace.completion_tokens,
            }
        return _sse(None, payload)

    return [
        chunk({"role": "assistant"}),
        chunk({"content": text}),
        chunk({}, "stop", usage=True),
        b"data: [DONE]\n\n",
    ]


def _openai_message(text: str, model: str, trace: Trace) -> dict[str, Any]:
    return {
        "id": "chatcmpl-" + new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": trace.prompt_tokens,
            "completion_tokens": trace.completion_tokens,
            "total_tokens": trace.prompt_tokens + trace.completion_tokens,
        },
    }


# ------------------------------------------------------------------ Anthropic


def _anthropic_stream(
    result: upstream.Result, text: str, model: str, trace: Trace
) -> list[bytes]:
    message_id = "msg_" + new_id()
    frames = [
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "usage": {"input_tokens": trace.prompt_tokens, "output_tokens": 0},
                },
            },
        )
    ]

    index = 0
    # Thinking is a separate block, ahead of the answer, because Anthropic clients
    # look for the text block and drop anything else.
    if result.thinking:
        frames += [
            _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "thinking": ""}}),
            _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": result.thinking}}),
            _sse("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
        index += 1

    frames += [
        _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}),
        _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}),
        _sse("content_block_stop", {"type": "content_block_stop", "index": index}),
        _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": trace.completion_tokens}}),
        _sse("message_stop", {"type": "message_stop"}),
    ]
    return frames


def _anthropic_message(
    result: upstream.Result, text: str, model: str, trace: Trace
) -> dict[str, Any]:
    content: list[dict[str, str]] = []
    if result.thinking:
        content.append({"type": "thinking", "thinking": result.thinking})
    content.append({"type": "text", "text": text})
    return {
        "id": "msg_" + new_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": trace.prompt_tokens, "output_tokens": trace.completion_tokens},
    }


def _error_frame(err: BaseException, dialect: str) -> bytes:
    message = str(err)
    if dialect == "anthropic":
        return _sse("error", {"type": "error", "error": {"type": "api_error", "message": message}})
    return _sse(None, {"error": {"message": message, "type": "server_error"}})
