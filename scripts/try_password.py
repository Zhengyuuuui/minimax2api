"""Try a password login for the accounts whose password step did not finish.

If the password was set (by hand in a browser, or by a later backfill), this
signs in and mints a fresh OAuth token plus its refresh token, writing both back
onto the account row — the same thing ``reauth.py`` does, but aimed at accounts
that have no password stored yet, so it can be pointed at a guessed one.

Usage:
    python scripts/try_password.py --account <id> --password '...'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import signup
from app.db import Database


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--email", default="")
    parser.add_argument("--password", required=True)
    parser.add_argument("--region", default="global")
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()

    db = Database(str(Path(args.data_dir) / "bridge.sqlite3"), args.data_dir)
    await db.connect()
    try:
        account = await db.account_by_id(args.account)
        if account is None:
            print(json.dumps({"ok": False, "error": "account not found"}))
            return 2
        email = args.email or account.email
        if not email:
            print(json.dumps({"ok": False, "error": "no email known for this account"}))
            return 2

        proxy = args.proxy.strip() or (db.settings().upstream.proxy or "").strip() or None
        origin = signup.origin_for(args.region)
        session = signup._AccountSession(uuid=account.uuid, device_id=account.device_id)

        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=False, trust_env=False, proxy=proxy
        ) as client:
            status, payload = await signup._signed(
                client, origin, session, "/oauth2/login",
                {
                    "loginType": signup.LOGIN_TYPE_PASSWORD,
                    "email": email,
                    "authToken": signup.rsa_encrypt(args.password),
                    "deviceID": session.device_id,
                },
            )
            info = payload.get("statusInfo") if isinstance(payload, dict) else {}
            if not isinstance(payload, dict) or payload.get("code") != 0 or not session.sid:
                print(json.dumps({
                    "ok": False, "account": args.account, "email": email,
                    "code": (info or {}).get("code"), "message": (info or {}).get("message"),
                }, ensure_ascii=False))
                return 1

            token, refresh = await signup._mint_token_full(
                client, origin, session, db.settings().signup
            )

        def apply(item):
            item.token = token
            item.email = email
            item.password = args.password
            item.status = "active"
            item.last_error = ""
            item.fail_count = 0
            item.cooldown_until = 0.0
            if refresh:
                item.remark = f"refresh_token={refresh}"

        saved = await db.update_account(args.account, apply)
        print(json.dumps({
            "ok": True, "account": args.account, "name": saved.name if saved else "",
            "email": email, "token": token[:16] + "...", "refresh": bool(refresh),
        }, ensure_ascii=False))
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
