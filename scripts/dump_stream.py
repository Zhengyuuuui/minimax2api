"""Dump the raw SSE frames the upstream sends for one streaming turn.

    python3 scripts/dump_stream.py --data-dir ./data

Diagnostic only: it talks straight to the direct model API with one pooled
account, prints every frame verbatim, and stops.  The point is to see where the
token usage actually appears and what values it carries — the console's numbers
can only be trusted once the frames behind them have been read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import upstream  # noqa: E402
from app.db import Database  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--account", default="", help="account id; default: first usable")
    parser.add_argument("--model", default="MiniMax-M3.1-Flash-Thinking")
    parser.add_argument("--prompt", default="只回复两个字：你好")
    args = parser.parse_args()

    db = Database(str(Path(args.data_dir) / "bridge.sqlite3"), args.data_dir)
    await db.connect()
    settings = db.settings()
    client = upstream.MiniMaxClient(lambda: db.settings())

    account = None
    for item in await db.list_accounts():
        if not item.enabled or not item.agent_id:
            continue
        if args.account and item.id != args.account:
            continue
        account = item
        break
    if account is None:
        print("no usable account")
        return 1
    print(f"account={account.id} model={args.model}", flush=True)

    cred = upstream.credential_of(account)
    body = {
        "model": args.model,
        "max_tokens": 200,
        "stream": True,
        "messages": [{"role": "user", "content": args.prompt}],
    }

    response = await client.llm(cred, body, stream=True, timeout=60.0)
    print(f"HTTP {response.status_code} {response.headers.get('content-type')}", flush=True)
    frame_no = 0
    try:
        async for chunk in response.aiter_bytes():
            text = chunk.decode("utf-8", "replace")
            frame_no += 1
            print(f"\n=== raw chunk #{frame_no} ({len(chunk)} bytes) ===", flush=True)
            print(text, flush=True)
    finally:
        await response.aclose()
    print(f"\n[total {frame_no} raw chunks]", flush=True)

    await client.aclose()
    await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
