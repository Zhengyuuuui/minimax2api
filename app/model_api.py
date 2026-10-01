"""Dialect translation for the direct model API.

The desktop client talks to the model with an Anthropic-compatible Messages
endpoint (``/mavis/api/v1/llm/v1/messages``): structured messages, tool use,
streaming SSE, and no server-side agent session.  This module converts an OpenAI
chat request to that shape and the answer back, so the bridge can serve both
dialects from one upstream.

Nothing here touches HTTP or accounts; ``upstream.MiniMaxClient.llm`` sends the
converted body and ``gateway`` wires the two together.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator


def openai_to_anthropic(body: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert an OpenAI chat request into an Anthropic Messages request.

    Unknown fields are dropped rather than forwarded: the endpoint rejects a body
    it does not recognise.
    """
    messages: list[dict[str, Any]] = []
    system_parts: list[str] = []

    for item in body.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role in ("system", "developer"):
            text = _text_of(item.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": item.get("tool_call_id") or "",
                            "content": _text_of(item.get("content")),
                        }
                    ],
                }
            )
            continue
        converted = _convert_openai_message(item)
        if converted is not None:
            messages.append(converted)

    out: dict[str, Any] = {"model": model, "messages": messages}
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    out["max_tokens"] = int(max_tokens) if isinstance(max_tokens, (int, float)) else 4096
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    if body.get("stream"):
        out["stream"] = True
    tools = _convert_tools(body.get("tools"))
    if tools:
        out["tools"] = tools
        tool_choice = _convert_tool_choice(body.get("tool_choice"))
        if tool_choice is not None:
            out["tool_choice"] = tool_choice
    stop = body.get("stop")
    if isinstance(stop, str):
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and all(isinstance(s, str) for s in stop):
        out["stop_sequences"] = stop
    return out


def _convert_openai_message(item: dict[str, Any]) -> dict[str, Any] | None:
    role = item.get("role")
    if role == "assistant":
        blocks: list[dict[str, Any]] = []
        text = _text_of(item.get("content"))
        if text:
            blocks.append({"type": "text", "text": text})
        for call in item.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            arguments = function.get("arguments")
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
            except ValueError:
                parsed = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.get("id") or "",
                    "name": function.get("name") or "",
                    "input": parsed if isinstance(parsed, dict) else {},
                }
            )
        return {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}
    content = item.get("content")
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "text", "text": content}]}
    if isinstance(content, list):
        blocks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url")
                if url:
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        return {"role": "user", "content": blocks or [{"type": "text", "text": ""}]}
    return None


def _convert_tools(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": function.get("description") or "",
                "input_schema": function.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return out


def _convert_tool_choice(choice: Any) -> dict[str, Any] | None:
    if choice in (None, "auto"):
        return {"type": "auto"}
    if choice == "required":
        return {"type": "any"}
    if choice == "none":
        return None
    if isinstance(choice, dict):
        function = choice.get("function") or {}
        name = function.get("name")
        if name:
            return {"type": "tool", "name": name}
    return {"type": "auto"}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def anthropic_to_openai(message: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert a finished (non-streaming) Anthropic message into OpenAI shape."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text_parts.append(block.get("text", ""))
        elif kind == "thinking":
            reasoning.append(block.get("thinking", ""))
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
    finish = {"tool_use": "tool_calls", "max_tokens": "length"}.get(
        message.get("stop_reason") or "", "stop"
    )
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = message.get("usage") or {}
    return {
        "id": "chatcmpl-" + (message.get("id") or ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


def anthropic_stream_to_openai(
    frames: AsyncIterator[bytes], model: str
) -> AsyncIterator[bytes]:
    """Translate an Anthropic SSE stream into an OpenAI SSE stream.

    Only events carrying assistant output are translated: text and thinking
    blocks become choice deltas, ``tool_use`` blocks are accumulated and flushed
    as one ``tool_calls`` delta when the block closes, and the terminal usage
    frame feeds the closing chunk.  Everything else is dropped.
    """

    async def generate() -> AsyncIterator[bytes]:
        created = int(time.time())
        response_id = "chatcmpl-" + str(int(time.time() * 1000))
        role_sent = False
        tool_names: dict[int, tuple[str, str]] = {}
        tool_args: dict[int, str] = {}
        tool_order: list[int] = []
        prompt_tokens = 0
        completion_tokens = 0

        def frame(delta: dict[str, Any], finish: str | None = None, usage: bool = False) -> bytes:
            payload: dict[str, Any] = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage:
                payload["usage"] = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                }
            return _sse(None, payload)

        async for raw in frames:
            _, data = _parse_sse(raw)
            if not data:
                continue
            kind = data.get("type")
            if kind == "message_start":
                usage = (data.get("message") or {}).get("usage") or {}
                prompt_tokens = int(usage.get("input_tokens") or prompt_tokens)
                if not role_sent:
                    role_sent = True
                    yield frame({"role": "assistant"})
            elif kind == "content_block_start":
                block = data.get("content_block") or {}
                if block.get("type") == "tool_use":
                    index = data.get("index", 0)
                    tool_names[index] = (block.get("id", ""), block.get("name", ""))
                    tool_args[index] = ""
                    tool_order.append(index)
            elif kind == "content_block_delta":
                delta = data.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    yield frame({"content": delta.get("text", "")})
                elif dtype == "thinking_delta":
                    yield frame({"reasoning_content": delta.get("thinking", "")})
                elif dtype == "input_json_delta":
                    index = data.get("index", 0)
                    tool_args[index] = tool_args.get(index, "") + (delta.get("partial_json", ""))
            elif kind == "content_block_stop":
                index = data.get("index", 0)
                if index in tool_names:
                    call_id, name = tool_names.pop(index)
                    arguments = tool_args.pop(index, "") or "{}"
                    position = tool_order.index(index) if index in tool_order else index
                    yield frame(
                        {
                            "tool_calls": [
                                {
                                    "index": position,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ]
                        }
                    )
            elif kind == "message_delta":
                usage = data.get("usage") or {}
                completion_tokens = int(usage.get("output_tokens") or completion_tokens)
                stop = (data.get("delta") or {}).get("stop_reason")
                finish = {"tool_use": "tool_calls", "max_tokens": "length"}.get(stop or "", "stop")
                yield frame({}, finish, usage=True)
            elif kind == "message_stop":
                yield b"data: [DONE]\n\n"

    return generate()


def _parse_sse(raw: bytes) -> tuple[str, dict[str, Any] | None]:
    event = ""
    data_lines: list[str] = []
    for line in raw.decode("utf-8", "replace").split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    payload = "\n".join(data_lines)
    if not payload:
        return event, None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return event, None
    return event, parsed if isinstance(parsed, dict) else None


def _sse(event: str | None, payload: dict[str, Any]) -> bytes:
    head = f"event: {event}\n" if event else ""
    return (head + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


__all__ = [
    "openai_to_anthropic",
    "anthropic_to_openai",
    "anthropic_stream_to_openai",
]
