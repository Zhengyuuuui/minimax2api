"""Start the bridge: ``python run.py [--host --port --data-dir]``.

Uvicorn is required rather than implemented here: SSE streaming needs a server
that writes as it goes, and a stdlib ``http.server`` fallback would mean a thread
per connection for something this small.  A missing dependency is reported as
such and the process exits without pretending otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="minimaxcode2api", description="MiniMax Agent 反代（无鉴权）")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "4555")))
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("DATA_DIR", "./data"),
        help="where the SQLite store and generated media live",
    )
    parser.add_argument("--reload", action="store_true", help="development mode")
    args = parser.parse_args()

    try:
        import uvicorn
    except ModuleNotFoundError:
        print("uvicorn is required: pip install -r requirements.txt", file=sys.stderr)
        return 2

    os.makedirs(args.data_dir, exist_ok=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # Nothing authenticates: the console and every endpoint are open to
        # whoever can reach the port.
        print(
            f"[minimaxcode2api] 警告: 正在监听 {args.host}，本服务没有鉴权，"
            "请只在本机使用或置于带鉴权的反代之后",
            file=sys.stderr,
            flush=True,
        )
    print(
        f"[minimaxcode2api] listening on http://{args.host}:{args.port}"
        f"  console: http://{args.host}:{args.port}/admin  data: {args.data_dir}",
        flush=True,
    )
    uvicorn.run(
        "app.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        # One line per request is useful while developing and noise in a service
        # that is already recording every request in its own audit table.
        access_log=args.reload,
        log_level="info",
        timeout_keep_alive=75,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
