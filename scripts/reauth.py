"""Re-authorise one existing account and mint a fresh OAuth token.

The device-flow access token lives one hour (``expires_in=3600``), so an account
that has been in the pool longer than that is holding a dead credential.  There
was no refresh path when these were imported: the ``refresh_token`` the token
endpoint returns was never stored, so the only way back is to sign in again.

For an account that has an email and a password, that is a password login (no
mailbox needed) followed by a fresh device grant — the same sequence the headless
registration ends with, minus the registration.  The new token, and the refresh
token this time, are written back onto the same account row.

Usage:
    python scripts/reauth.py --account <id>
    python scripts/reauth.py --all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config, signup
from app.db import Database


async def reauth(db: Database, client: httpx.AsyncClient, account_id: str) -> dict:
    account = await db.account_by_id(account_id)
    if account is None:
        return {"ok": False, "error": "account not found"}
    if not account.email or not account.password:
        return {"ok": False, "error": "account has no saved email/password to sign in with"}

    region = account.region or "global"
    origin = signup.origin_for(region)
    settings = config.default_settings("./data")
    settings.signup.region = region

    session = signup._AccountSession(uuid=account.uuid, device_id=account.device_id)
    # Password login: no email code, the password is the second factor.
    status, payload = await signup._signed(
        client, origin, session, "/oauth2/login",
        {
            "loginType": signup.LOGIN_TYPE_PASSWORD,
            "email": account.email,
            "authToken": signup.rsa_encrypt(account.password),
            "deviceID": session.device_id,
        },
    )
    if payload.get("code") != 0 or not session.sid:
        return {"ok": False, "error": f"password login: {json.dumps(payload, ensure_ascii=False)[:240]}"}

    token, refresh_token = await signup._mint_token_full(client, origin, session, settings.signup)

    saved = await db.update_account(
        account_id,
        lambda item: (
            setattr(item, "token", token),
            setattr(item, "status", "active"),
            setattr(item, "last_error", ""),
            setattr(item, "fail_count", 0),
            setattr(item, "cooldown_until", 0.0),
            setattr(item, "remark", _merge_remark(item.remark, refresh_token)),
        ),
    )
    return {
        "ok": True,
        "id": account_id,
        "name": saved.name if saved else account.name,
        "token": token[:16] + "...",
        "refreshStored": bool(refresh_token),
    }


def _merge_remark(remark: str, refresh_token: str) -> str:
    if not refresh_token:
        return remark
    return f"refresh_token={refresh_token}"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--data-dir", default="./data")
    args = parser.parse_args()

    db = Database(str(Path(args.data_dir) / "bridge.sqlite3"), args.data_dir)
    await db.connect()
    try:
        # The proxy the running server uses lives in the same settings document.
        proxy = (db.settings().upstream.proxy or "").strip() or None
        accounts = await db.list_accounts()
        targets = accounts if args.all else [a for a in accounts if a.id == args.account]
        if not targets:
            print("no matching account", file=sys.stderr)
            return 2
        for account in targets:
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=False, trust_env=False, proxy=proxy
            ) as client:
                result = await reauth(db, client, account.id)
            print(json.dumps(result, ensure_ascii=False))
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
