"""One-shot probe: can the bridge's accounts drive the video plugin end to end?

    nohup python3 scripts/probe_video.py --data-dir ./data >> data/probe/probe.out 2>&1 &

Every account is used exactly once — one video turn, no retries.  The point of
the run is the answer, not the throughput: the upstream bills whether or not
the container has a working connector tool, so a retry on a failed turn only
buys the same failure twice.

What one turn does:

1. read the live balance (free GET), both wallets;
2. open a fresh session and send one `@video-creater` turn with a complete
   `<video-generation-options>` block and `client_intent`, then watch the
   stream only for the agent's own words;
3. poll `input-summaries` for an artefact created after the turn started, and
   resolve the first video one to a signed download URL;
4. re-read the balance, record the grants that appeared, and archive the mp4.

The state directory keeps the session id, so the lookup can be re-run later
without spending a new turn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import upstream  # noqa: E402
from app.config import resolve_path  # noqa: E402
from app.db import Database  # noqa: E402
from app.records import MediaItem  # noqa: E402

PROMPT = (
    "一艘红色小纸船漂过雨后积水的街道，镜头缓慢跟随，暖色街灯在湿路面上反光，"
    "最后一个镜头纸船漂出画面。"
)

MODELS = ["MiniMax-H3-Max", "MiniMax-H3", "MiniMax-Hailuo-2.3"]

# The turn label the web bundle's own tool table gives every video tool.  Sent
# because the web client sends it, not because an effect has been measured.
CLIENT_INTENT = "video_generation"

# These two are not yet in the app's Settings; read them leniently so the probe
# runs before the fields exist and still honours them once they do.
DEFAULT_SUMMARIES_PATH = "/minimax-cloud/api/v1/session/{session_id}/input-summaries"
DEFAULT_DRIVE_FILE_PATH = "/minimax-cloud/api/v1/drive/file/{node_id}"

# One turn's HTTP budget.  The agent can spend several minutes thinking before
# it submits the job; cutting the stream early loses its answer, not just the
# file.
TURN_TIMEOUT_SEC = 900.0
IDLE_TIMEOUT_SEC = 180.0

# How long to keep asking the drive after the turn's HTTP answer, per model.
# The render is asynchronous with the turn: the agent replies once the job is
# accepted, the file lands minutes later.
POLL_BUDGET_SEC = {
    "MiniMax-H3-Max": 600.0,
    "MiniMax-H3": 900.0,
    "MiniMax-Hailuo-2.3": 1200.0,
}
POLL_GAP_SEC = (45.0, 90.0)

MIN_CREDIT = 1500.0


def pick_accounts(db_path: str, want: int = 3) -> list[tuple[str, str, float]]:
    """Three distinct, credit-rich accounts, richest first.

    Token freshness is not a criterion: `ensure_tokens` renews whatever is due
    before use, and a renewal is one OAuth call, not a turn.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, credit, user_id, agent_id FROM accounts"
        " WHERE enabled = 1 AND token != '' AND agent_id != '' AND user_id != ''"
    ).fetchall()
    conn.close()
    picked = []
    seen_users = set()
    for row in rows:
        try:
            credit = float(json.loads(row["credit"] or "{}").get("total") or 0.0)
        except (TypeError, ValueError):
            continue
        if credit < MIN_CREDIT or row["user_id"] in seen_users:
            continue
        seen_users.add(row["user_id"])
        name = row["name"] or f"acct-{row['id'][:6]}"
        picked.append((row["id"], f"{name}-{credit:.0f}", credit))
    picked.sort(key=lambda item: -item[2])
    return picked[:want]


async def ensure_tokens(db: Database, client: upstream.MiniMaxClient, ids: list[str]) -> None:
    """Renew any selected account whose token dies within ten minutes."""
    from app.keepalive import Keeper

    keeper = Keeper(db, client, lambda: db.settings())
    now = time.time()
    accounts = {item.id: item for item in await db.list_accounts()}
    for account_id in ids:
        account = accounts.get(account_id)
        if account is None:
            raise SystemExit(f"account {account_id} vanished")
        if account.token_expires_at - now > 600:
            continue
        result = await keeper.renew_account(account_id)
        if not result.ok:
            raise SystemExit(f"account {account_id}: renewal failed: {result.error}")
        print(f"[{account_id}] token renewed until {time.strftime('%H:%M:%S', time.localtime(result.expires_at))}", flush=True)


def build_turn_text(model: str) -> str:
    options = {"duration": 6, "model": model, "ratio": "16:9", "resolution": "768P"}
    block = json.dumps(options, ensure_ascii=False, separators=(",", ":"))
    return f"@video-creater {PROMPT}\n\n<video-generation-options>\n{block}\n</video-generation-options>"


async def read_wallets(client: upstream.MiniMaxClient, cred: upstream.Credential) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        info = await client.credit(cred)
        out["credit"] = {
            "total": info.total,
            "free": info.free,
            "purchased": info.purchased,
            "plan_name": info.plan_name,
            "plan_type": info.plan_type,
        }
    except Exception as err:  # noqa: BLE001 - a failed read must not stop the turn
        out["credit_error"] = str(err)
    try:
        grants = await client.credit_grants(cred)
        out["grants"] = [
            {
                "granted": item.granted,
                "remaining": item.remaining,
                "granted_at_ms": item.granted_at,
                "expires_at_ms": item.expires_at,
            }
            for item in grants[:10]
        ]
    except Exception as err:  # noqa: BLE001
        out["grants_error"] = str(err)
    return out


def _path_setting(settings: Any, name: str, fallback: str) -> str:
    return resolve_path(str(getattr(settings.upstream, name, "") or ""), fallback)


async def fetch_artifacts(
    client: upstream.MiniMaxClient, cred: upstream.Credential, session_id: str, settings: Any
) -> list[dict[str, Any]]:
    path = _path_setting(settings, "summaries_path", DEFAULT_SUMMARIES_PATH)
    target = client.agent_target(cred, path.format(session_id=session_id), method="GET")
    # `fetch_raw` snippets the body to 300 chars, which truncates a real
    # summaries answer; `_read_json` is the same signed GET, undigested.
    payload = await client._read_json(cred, target, "summaries")  # noqa: SLF001
    out = []
    for item in payload.get("summaries") or []:
        for artifact in item.get("artifacts") or []:
            out.append(artifact)
    return out


async def resolve_download_url(
    client: upstream.MiniMaxClient, cred: upstream.Credential, node_id: str, settings: Any
) -> str:
    path = _path_setting(settings, "drive_file_path", DEFAULT_DRIVE_FILE_PATH)
    target = client.agent_target(
        cred, f"{path.format(node_id=node_id)}/download-url", method="GET"
    )
    payload = await client._read_json(cred, target, "download-url")  # noqa: SLF001
    return absolute_url(str(payload.get("download_url") or ""))


def is_video(artifact: dict[str, Any]) -> bool:
    category = str(artifact.get("category") or "").lower()
    mime = str(artifact.get("mime_type") or "").lower()
    return category in ("videos", "video") or mime.startswith("video/")


def absolute_url(raw: str) -> str:
    raw = (raw or "").strip()
    if raw and not raw.startswith(("http://", "https://")):
        return "https://" + raw
    return raw


async def download(
    client: upstream.MiniMaxClient, url: str, target: Path, media_id: str
) -> dict[str, Any]:
    """Pull the file through the bridge's own client (proxy rules included)."""
    import httpx

    host = (url.split("/")[2] if "//" in url else "").strip()
    info: dict[str, Any] = {}
    try:
        http = client.public_client(host)
        response = await http.get(url, timeout=httpx.Timeout(180.0, connect=15.0))
        try:
            info["status"] = response.status_code
            info["content_type"] = response.headers.get("content-type", "")
            data = await response.aread()
        finally:
            await response.aclose()
        if response.status_code < 400:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            info["bytes"] = len(data)
            info["local"] = str(target)
            info["media_url"] = f"/media/{media_id}"
    except Exception as err:  # noqa: BLE001
        info["error"] = str(err)
    return info


async def probe_one(
    client: upstream.MiniMaxClient,
    account_id: str,
    label: str,
    model: str,
    state_dir: Path,
    media_dir: Path,
    db: Database,
) -> dict[str, Any]:
    settings = db.settings()
    accounts = {item.id: item for item in await db.list_accounts()}
    account = accounts[account_id]
    cred = upstream.credential_of(account)

    text = build_turn_text(model)
    state: dict[str, Any] = {
        "account_id": account_id,
        "label": label,
        "model": model,
        "turn_text": text,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_ms": int(time.time() * 1000),
    }
    state["before"] = await read_wallets(client, cred)

    print(f"[{label}] {model}: turn start", flush=True)
    started = time.monotonic()
    interesting: list[dict[str, Any]] = []

    def on_frame(payload: dict[str, Any]) -> None:
        # Keep the frames that talk about tools or video; a full turn can carry
        # hundreds of token frames and they are not the evidence.
        dumped = json.dumps(payload, ensure_ascii=False, default=str)
        lowered = dumped.lower()
        if any(token in lowered for token in ("tool", "video", "submit", "artifact", "error")):
            if len(interesting) < 400:
                interesting.append(payload)

    options = upstream.Options(
        text=text,
        client_intent=CLIENT_INTENT,
        timeout=TURN_TIMEOUT_SEC,
        idle_timeout=IDLE_TIMEOUT_SEC,
        on_frame=on_frame,
    )
    turn_error = ""
    session_id = ""
    try:
        # Deliberately not `completion()`: if the message stream breaks, the
        # session the upstream already opened must still survive on disk —
        # without that id, a slow model's late-landing file cannot be found.
        session_id = await client.create_session(cred)
        state["session_id"] = session_id
        _dump(state_dir / f"{model}.json", state)
        result = await client.send_message(cred, options, session_id, TURN_TIMEOUT_SEC)
        if isinstance(result, tuple):  # a partial result with the error beside it
            result, turn_error = (result[0], str(result[1]))
        session_id = result.session_id
        state["agent_text"] = result.text
        state["thinking_chars"] = len(result.thinking)
        state["stream_media"] = [item.url for item in result.media]
        state["stop_reason"] = result.stop_reason
    except Exception as err:  # noqa: BLE001
        turn_error = f"{type(err).__name__}: {err}"
    state["turn_error"] = turn_error
    state["turn_seconds"] = round(time.monotonic() - started, 1)
    state["session_id"] = session_id
    state["frame_count"] = len(interesting)
    state["frames"] = interesting
    _dump(state_dir / f"{model}.json", state)
    if not session_id:
        print(f"[{label}] {model}: no session id; turn failed ({turn_error})", flush=True)
        return state

    budget = POLL_BUDGET_SEC.get(model, 600.0)
    deadline = time.monotonic() + budget
    artifacts: list[dict[str, Any]] = []
    link = ""
    node_id = ""
    while time.monotonic() < deadline:
        await asyncio.sleep(random.uniform(*POLL_GAP_SEC))
        try:
            found = await fetch_artifacts(client, cred, session_id, settings)
        except Exception as err:  # noqa: BLE001
            print(f"[{label}] summaries: {err}", flush=True)
            continue
        fresh = [item for item in found if int(item.get("created_at") or 0) >= state["started_ms"]]
        if fresh:
            artifacts = fresh
            print(f"[{label}] {model}: {len(fresh)} artefact(s) after turn start", flush=True)
            break
        print(f"[{label}] {model}: no artefact yet ({budget - (deadline - time.monotonic()):.0f}s in)", flush=True)

    videos = [item for item in artifacts if is_video(item)]
    state["artifacts"] = artifacts
    if videos:
        node_id = str(videos[0].get("node_id") or "")
        state["node_id"] = node_id
        link = ""
        try:
            link = await resolve_download_url(client, cred, node_id, settings)
        except Exception as err:  # noqa: BLE001
            state["download_url_error"] = str(err)
        state["download_url"] = link
        if link:
            from app.security import new_id

            media_id = new_id()
            target = media_dir / f"{media_id}.mp4"
            state["download"] = await download(client, link, target, media_id)
            if state["download"].get("local"):
                await db.add_media(
                    MediaItem(
                        id=media_id,
                        kind="video",
                        url=state["download"]["media_url"],
                        source_url=link,
                        prompt=PROMPT,
                        model=model,
                        account_name=label,
                        created_at=time.time(),
                    )
                )

    state["after"] = await read_wallets(client, cred)
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _dump(state_dir / f"{model}.json", state)
    print(
        f"[{label}] {model}: done — videos={len(videos)}, "
        f"credit {state.get('before', {}).get('credit', {}).get('total')} -> "
        f"{state.get('after', {}).get('credit', {}).get('total')}",
        flush=True,
    )
    return state


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


async def collect_once(
    client: upstream.MiniMaxClient,
    cred: upstream.Credential,
    session_id: str,
    started_ms: int,
    settings: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    found = await fetch_artifacts(client, cred, session_id, settings)
    fresh = [item for item in found if int(item.get("created_at") or 0) >= started_ms]
    return fresh, [item for item in fresh if is_video(item)]


async def harvest(
    client: upstream.MiniMaxClient,
    cred: upstream.Credential,
    videos: list[dict[str, Any]],
    media_dir: Path,
    db: Database,
    state: dict[str, Any],
    prompt: str,
    label: str,
    settings: Any,
) -> None:
    """Resolve the first video artefact to a signed URL, download it, record it."""
    from app.security import new_id

    node_id = str(videos[0].get("node_id") or "")
    state["node_id"] = node_id
    link = ""
    try:
        link = await resolve_download_url(client, cred, node_id, settings)
    except Exception as err:  # noqa: BLE001
        state["download_url_error"] = str(err)
    state["download_url"] = link
    if not link:
        return
    media_id = new_id()
    target = media_dir / f"{media_id}.mp4"
    state["download"] = await download(client, link, target, media_id)
    if state["download"].get("local"):
        await db.add_media(
            MediaItem(
                id=media_id,
                kind="video",
                url=state["download"]["media_url"],
                source_url=link,
                prompt=prompt,
                model=str(state.get("model") or ""),
                account_name=label,
                created_at=time.time(),
            )
        )


async def resume_session(
    client: upstream.MiniMaxClient,
    db: Database,
    account_id: str,
    session_id: str,
    model: str,
    started_ms: int,
    label: str,
    media_dir: Path,
    state_path: Path,
) -> dict[str, Any]:
    """Free follow-up on a turn whose stream was cut: poll the drive, harvest.

    No message is sent.  The turn is already running upstream — this only asks
    the two drive endpoints until the artefact lands, then downloads it.
    """
    settings = db.settings()
    accounts = {item.id: item for item in await db.list_accounts()}
    cred = upstream.credential_of(accounts[account_id])

    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {}
    state.update(
        {
            "account_id": account_id,
            "session_id": session_id,
            "model": model,
            "label": label,
            "started_ms": started_ms,
            "resumed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    state["before"] = await read_wallets(client, cred)

    budget = POLL_BUDGET_SEC.get(model, 1200.0) + 1800.0
    deadline = time.monotonic() + budget
    attempts = 0
    videos: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        await asyncio.sleep(random.uniform(60.0, 120.0))
        attempts += 1
        try:
            artifacts, videos = await collect_once(client, cred, session_id, started_ms, settings)
        except Exception as err:  # noqa: BLE001
            print(f"[{label}] {model}: summaries (#{attempts}) {err}", flush=True)
            continue
        print(
            f"[{label}] {model}: poll #{attempts} — artifacts={len(artifacts)} videos={len(videos)}",
            flush=True,
        )
        if videos:
            break
        state["artifacts"] = artifacts
        _dump(state_path, state)

    state["artifacts"] = artifacts
    state["videos"] = videos
    if videos:
        await harvest(client, cred, videos, media_dir, db, state, PROMPT, label, settings)
    state["after"] = await read_wallets(client, cred)
    state["resume_finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _dump(state_path, state)
    print(
        f"[{label}] {model}: resume done — videos={len(videos)}, "
        f"local={state.get('download', {}).get('local', '-')}",
        flush=True,
    )
    return state


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument(
        "--resume",
        action="append",
        default=[],
        metavar="ACCOUNT:SESSION:MODEL:STARTEDMS",
        help="follow up a submitted turn for free; repeat per job",
    )
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    db = Database(str(data_dir / "bridge.sqlite3"), str(data_dir))
    await db.connect()
    settings = db.settings()
    client = upstream.MiniMaxClient(lambda: db.settings())
    media_dir = Path(settings.media.generated_dir)

    if args.resume:
        state_dir = Path(args.state_dir) if args.state_dir else data_dir / "probe" / "resume"
        state_dir.mkdir(parents=True, exist_ok=True)
        await ensure_tokens(db, client, [item.split(":")[0] for item in args.resume])
        accounts = {item.id: item for item in await db.list_accounts()}
        for spec in args.resume:
            account_id, session_id, model, started_ms = spec.split(":")
            label = accounts[account_id].name or f"acct-{account_id[:6]}"
            await resume_session(
                client,
                db,
                account_id,
                session_id,
                model,
                int(started_ms),
                label,
                media_dir,
                state_dir / f"{label}-{model}.json",
            )
        await client.aclose()
        await db.close()
        print("resume finished", flush=True)
        return 0

    chosen = pick_accounts(str(data_dir / "bridge.sqlite3"))
    if len(chosen) < len(MODELS):
        print(f"only {len(chosen)} eligible accounts; need {len(MODELS)}", flush=True)
        await client.aclose()
        await db.close()
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    state_dir = data_dir / "probe" / stamp
    media_dir = Path(settings.media.generated_dir)
    print(f"accounts: {chosen}\nstate: {state_dir}", flush=True)

    await ensure_tokens(db, client, [item[0] for item in chosen])

    for (account_id, label, credit), model in zip(chosen, MODELS):
        await probe_one(client, account_id, label, model, state_dir, media_dir, db)
        await asyncio.sleep(random.uniform(45.0, 120.0))

    await client.aclose()
    await db.close()
    print("probe finished", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
