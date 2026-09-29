"""Bind a password to accounts registered before the password step existed.

The first headless registrations created accounts with no password at all —
email-code sign-in never sets one — and stored no email either, so the console
showed an upstream username where a login should be.  The mailboxes are still on
the temporary-mail service and are still addressable, so this recovers both: it
looks up each account's mailbox by the name it was registered under, logs back in
by email code (a *login*, not a registration), sets a password, and writes the
email plus the password back onto the account row.

Usage:
    python scripts/backfill_password.py --list
    python scripts/backfill_password.py --account <id> --password 'NewPass123'
    python scripts/backfill_password.py --all --password 'NewPass123'

``--list`` only reads the local database and the mail worker's address list; it
sends nothing to MiniMax.  The other two log in to MiniMax, which is a real
request against the account, so they are explicit rather than implicit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

import httpx

from app import config, signup
from app.db import Database

# Never hard-code the mail service or its passkey: they are deployment secrets.
# Supply them in the environment (or a .env the app already loads).
MAIL_BASE = os.environ.get("MINIMAX2API_SIGNUP__MAIL_BASE", "")
MAIL_PASS = os.environ.get("MINIMAX2API_SIGNUP__MAIL_PASS", "")


async def mailboxes(client: httpx.AsyncClient) -> list[dict]:
    """Every mailbox the worker knows about, walking the page limit.

    The endpoint caps ``limit`` at 100 and answers a bare 400 above it, so a
    single large request is not an option; the count comes back with the page,
    which is what makes stopping here a decision rather than a guess.
    """
    out: list[dict] = []
    offset = 0
    while True:
        response = await client.get(
            f"{MAIL_BASE}/admin/address?limit=100&offset={offset}",
            headers={"x-admin-auth": MAIL_PASS},
            timeout=20.0,
        )
        try:
            payload = response.json()
        except ValueError:
            break
        page = payload.get("results") or []
        out.extend(page)
        total = int(payload.get("count") or len(out))
        if not page or len(out) >= total:
            break
        offset += len(page)
    return out


async def mailbox_jwt(client: httpx.AsyncClient, address_id: int) -> str:
    response = await client.get(
        f"{MAIL_BASE}/admin/show_password/{address_id}",
        headers={"x-admin-auth": MAIL_PASS},
        timeout=20.0,
    )
    payload = response.json()
    # The field is named jwt on this worker; tolerate the address field's name too.
    return str(payload.get("jwt") or payload.get("token") or "")


def guess_email_for(account_name: str, boxes: list[dict], created_at: float = 0.0) -> str:
    """Match an account to a mailbox by the name the mailbox was created with.

    The mailbox name is what the registration passed as its prefix, and the
    registration is the only thing that ever created one, so a prefix match is
    exact in practice: ``mmxhttp`` found ``mmxhttpab3379``.

    A batch shares one prefix (``batch``), so a prefix match alone is ambiguous.
    There the account row's creation time breaks the tie: the mailbox is created
    a few seconds *before* the account row is written, so the closest mailbox at
    or before it is the right one.  ``count`` > 1 registered them in order, and
    the two clocks are the same host's.
    """
    import time as _time

    prefix = (account_name or "").strip().lower()
    if not prefix:
        return ""
    matches = [box for box in boxes if str(box.get("name", "")).lower().startswith(prefix)]
    if not matches:
        return ""
    if len(matches) == 1:
        return str(matches[0]["name"])
    if not created_at:
        return ""
    best, best_delta = "", None
    for box in matches:
        try:
            stamp = _time.mktime(_time.strptime(box["created_at"], "%Y-%m-%d %H:%M:%S")) - _time.timezone
        except (KeyError, ValueError, TypeError):
            continue
        delta = created_at - stamp
        if delta < 0:
            continue  # the mailbox cannot exist after the account that used it
        if best_delta is None or delta < best_delta:
            best, best_delta = str(box["name"]), delta
    return best


async def current_codes(client: httpx.AsyncClient, jwt: str) -> set[str]:
    """Every six-digit run already sitting in the mailbox.

    Snapshot before sending: the mailbox keeps old messages, and a poll that
    fires before the new one lands would otherwise hand back a code from a
    previous attempt — which the server then rejects as expired, and the retry
    reads the same stale code again.
    """
    import re

    response = await client.get(
        f"{MAIL_BASE}/api/parsed_mails?limit=10&offset=0",
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=20.0,
    )
    try:
        payload = response.json()
    except ValueError:
        return set()
    codes: set[str] = set()
    for message in payload.get("results") or []:
        blob = " ".join(str(message.get(key) or "") for key in ("subject", "text", "html"))
        codes.update(re.findall(r"\b(\d{6})\b", blob))
    return codes


async def bind_one(
    db: Database,
    client: httpx.AsyncClient,
    account_id: str,
    password: str,
    region: str,
) -> dict:
    """Sign one account back in and give it a password, at a human pace.

    Pacing is the point, not decoration: the send endpoint throttles per address
    (code 32) and the account service watches for machine bursts, so each step is
    separated by a pause and a single code is requested per account rather than
    retried in a loop.  A step that needs a second code waits for the first to be
    consumed first — which is what a person with a browser open would do.
    """
    account = await db.account_by_id(account_id)
    if account is None:
        return {"ok": False, "error": "account not found"}

    boxes = await mailboxes(client)
    email = account.email or guess_email_for(account.name, boxes, account.created_at)
    if not email:
        return {
            "ok": False,
            "error": (
                f"could not resolve a mailbox for {account.name!r}; "
                "pass --email to supply one"
            ),
        }
    box = next((b for b in boxes if b["name"] == email), None)
    if box is None:
        return {"ok": False, "error": f"mailbox {email} is gone"}

    jwt = await mailbox_jwt(client, box["id"])
    if not jwt:
        return {"ok": False, "error": f"no mailbox jwt for {email}"}

    settings = config.default_settings(str(Path("./data")))
    settings.signup.region = region
    settings.signup.password = password
    settings.signup.use_proxies = False
    # Generous: a real person waits for the mail, they do not poll every second.
    settings.signup.mail_timeout_sec = 180
    settings.signup.mail_poll_sec = 6

    origin = signup.origin_for(region)
    proxy = (settings.upstream.proxy or "").strip() or None
    account_client = httpx.AsyncClient(
        timeout=30.0, follow_redirects=False, trust_env=False, proxy=proxy
    )
    try:
        session = signup._AccountSession(uuid=account.uuid, device_id=account.device_id)
        session.mail_jwt = jwt

        # Everything already in the inbox is off the table: the mailbox keeps
        # every message it ever received, and the code from the registration that
        # created this account is still sitting there.  Only a code that appears
        # after this point counts, which is what makes a throttled send behave
        # correctly — it delivers nothing new, so the wait simply continues
        # instead of handing back a message that was already spent.
        spent = await _all_codes(client, jwt)

        print(f"  [{email}] requesting a login code")
        await signup._send_code(account_client, origin, session, email, label="login")
        print(f"  [{email}] waiting for the mail (a person would be reading it about now)")
        code = await signup._mail_wait_code(
            account_client, settings.signup, jwt,
            exclude=spent,
            since=time.time() - signup.CODE_VALIDITY_SEC,
        )
        if not code:
            return {"ok": False, "error": "no login code arrived within the wait"}
        spent.add(code)

        # a beat between reading the code and submitting it
        await asyncio.sleep(random.uniform(3, 6))
        status, payload = await signup._signed(
            account_client, origin, session, "/oauth2/login",
            {"loginType": signup.LOGIN_TYPE_EMAIL_CODE, "email": email, "code": code,
             "deviceID": session.device_id},
        )
        if payload.get("code") != 0 or not session.sid:
            return {"ok": False, "error": f"login: {json.dumps(payload)[:200]}"}

        # Password creation is a second code.  Pause so the first send's window has
        # closed, rather than firing immediately and earning another code 32.
        await asyncio.sleep(random.uniform(20, 35))
        code2 = await signup._obtain_code(
            account_client, origin, session, settings.signup, email,
            label="password", used=spent, attempts=2,
        )
        await asyncio.sleep(random.uniform(3, 6))
        await signup._set_password_with_code(
            account_client, origin, session, settings.signup, email, code2
        )
    finally:
        await account_client.aclose()

    saved = await db.update_account(
        account_id,
        lambda item: (setattr(item, "email", email), setattr(item, "password", password)),
    )
    return {"ok": True, "id": account_id, "email": email, "password": password,
            "name": saved.name if saved else account.name}


async def _all_codes(client: httpx.AsyncClient, jwt: str) -> set[str]:
    """Every code currently in the mailbox, for handing back nothing that is old."""
    import re

    codes: set[str] = set()
    for message in await _fetch_mail(client, jwt):
        blob = " ".join(str(message.get(k) or "") for k in ("subject", "text", "html"))
        codes.update(re.findall(r"code is:?\s*\n?\s*(\d{6})", blob, re.IGNORECASE) or re.findall(r"\b(\d{6})\b", blob))
    return codes


async def _fetch_mail(client: httpx.AsyncClient, jwt: str) -> list[dict]:
    response = await client.get(
        f"{MAIL_BASE}/api/parsed_mails?limit=10&offset=0",
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=20.0,
    )
    try:
        return list(response.json().get("results") or [])
    except ValueError:
        return []


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="", help="one account id")
    parser.add_argument("--all", action="store_true", help="every oauth account")
    parser.add_argument("--list", action="store_true", help="show the mapping and exit")
    parser.add_argument("--email", default="", help="override the resolved mailbox")
    parser.add_argument("--password", default="", help="password to set")
    parser.add_argument("--region", default="global")
    parser.add_argument("--data-dir", default="./data")
    # Human-paced by default; the flags exist so a test can turn the waiting off,
    # not so production can run faster.
    parser.add_argument("--gap-min", type=float, default=45.0)
    parser.add_argument("--gap-max", type=float, default=90.0)
    args = parser.parse_args()

    db = Database(str(Path(args.data_dir) / "bridge.sqlite3"), args.data_dir)
    await db.connect()
    try:
        accounts = await db.list_accounts()
        async with httpx.AsyncClient(timeout=30.0) as client:
            boxes = await mailboxes(client)
            if args.list:
                for account in accounts:
                    if account.kind != "oauth":
                        continue
                    email = account.email or guess_email_for(account.name, boxes, account.created_at)
                    state = "has password" if account.password else "no password"
                    print(f"{account.id}  {account.name or '(none)':10} {email or '???':36} {state}")
                return 0

            targets = accounts if args.all else [a for a in accounts if a.id == args.account]
            targets = [a for a in targets if a.kind == "oauth"]
            if not targets:
                print("no matching account", file=sys.stderr)
                return 2

            for account in targets:
                if account.password:
                    print(f"skip {account.name or account.id}: already has a password")
                    continue
                if not args.password:
                    print("--password is required to bind", file=sys.stderr)
                    return 2
                result = await bind_one(db, client, account.id, args.password, args.region)
                print(json.dumps(result, ensure_ascii=False))
                if account is not targets[-1]:
                    # Between accounts: long enough that the address limiter has
                    # recovered and the pattern is "a person doing a few logins",
                    # not a script walking a list.
                    gap = random.uniform(args.gap_min, args.gap_max)
                    print(f"  ...sleeping {gap:.0f}s before the next account")
                    await asyncio.sleep(gap)
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
