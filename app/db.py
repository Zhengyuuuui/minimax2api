"""SQLite persistence.

One connection, one lock.  Every access is dispatched to a worker thread so the
event loop never blocks on I/O, and the lock means the connection is never used
from two threads at once — which is what ``check_same_thread=False`` requires a
caller to guarantee.  Reads serialise with writes: statements here are simple
index lookups and inserts, so the critical section is microseconds and the
simplicity is worth more than the throughput.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from typing import Any, Callable, TypeVar

from . import config as config_mod
from . import records
from .records import (
    HTTP_BODY_LIMIT,
    Account,
    AccountView,
    Audit,
    Credit,
    MediaItem,
    ModelConfig,
    Quota,
    SigninPanel,
    dumps,
    iso,
    loads_or_none,
    mask_token,
    merge_builtin_models,
)

T = TypeVar("T")

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'token',
    region         TEXT NOT NULL DEFAULT 'global',
    token          TEXT NOT NULL DEFAULT '',
    user_id        TEXT NOT NULL DEFAULT '',
    identifier     TEXT NOT NULL DEFAULT '',
    agent_id       TEXT NOT NULL DEFAULT '',
    device_id      TEXT NOT NULL DEFAULT '',
    uuid           TEXT NOT NULL DEFAULT '',
    screen_width   INTEGER NOT NULL DEFAULT 0,
    screen_height  INTEGER NOT NULL DEFAULT 0,
    base_url       TEXT NOT NULL DEFAULT '',
    grp            TEXT NOT NULL DEFAULT '',
    remark         TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1,
    priority       INTEGER NOT NULL DEFAULT 0,
    max_concurrent INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'active',
    cooldown_until REAL NOT NULL DEFAULT 0,
    fail_count     INTEGER NOT NULL DEFAULT 0,
    success_count  INTEGER NOT NULL DEFAULT 0,
    last_used_at   REAL NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL DEFAULT 0,
    signin_at      REAL NOT NULL DEFAULT 0,
    signin_status  TEXT NOT NULL DEFAULT '',
    signin_streak INTEGER NOT NULL DEFAULT 0,
    signin_points  INTEGER NOT NULL DEFAULT 0,
    signin_total   INTEGER NOT NULL DEFAULT 0,
    signin_error   TEXT NOT NULL DEFAULT '',
    signin_panel   TEXT NOT NULL DEFAULT '',
    credit         TEXT NOT NULL DEFAULT '',
    quota          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);

CREATE TABLE IF NOT EXISTS models (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    upstream       TEXT NOT NULL DEFAULT 'agent',
    upstream_model TEXT NOT NULL DEFAULT '',
    type           TEXT NOT NULL DEFAULT 'chat',
    enabled        INTEGER NOT NULL DEFAULT 1,
    builtin        INTEGER NOT NULL DEFAULT 0,
    description    TEXT NOT NULL DEFAULT '',
    requests       INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audits (
    id               TEXT PRIMARY KEY,
    created_at       REAL NOT NULL DEFAULT 0,
    model            TEXT NOT NULL DEFAULT '',
    account_name     TEXT NOT NULL DEFAULT '',
    status           INTEGER NOT NULL DEFAULT 0,
    outcome          TEXT NOT NULL DEFAULT 'ok',
    latency_ms       INTEGER NOT NULL DEFAULT 0,
    first_token_ms   INTEGER NOT NULL DEFAULT 0,
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    stream           INTEGER NOT NULL DEFAULT 0,
    retries          INTEGER NOT NULL DEFAULT 0,
    ip               TEXT NOT NULL DEFAULT '',
    user_agent       TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    request_body     TEXT NOT NULL DEFAULT '',
    response_body    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audits_created ON audits(created_at);

CREATE TABLE IF NOT EXISTS media (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT 'image',
    url          TEXT NOT NULL DEFAULT '',
    source_url   TEXT NOT NULL DEFAULT '',
    prompt       TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_created ON media(created_at);
"""

# grp is a reserved word in SQLite, so the column is quoted on every access.
_ACCOUNT_COLUMNS = """
    id, name, kind, region, token, user_id, identifier, agent_id, device_id,
    uuid, screen_width, screen_height, base_url, "grp", remark, enabled, priority,
    max_concurrent, status, cooldown_until, fail_count, success_count, last_used_at,
    last_error, created_at, updated_at, signin_at, signin_status, signin_streak,
    signin_points, signin_total, signin_error, signin_panel, credit, quota
"""


def account_from_row(row: sqlite3.Row) -> Account:
    account = Account(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        region=row["region"],
        token=row["token"],
        user_id=row["user_id"],
        identifier=row["identifier"],
        agent_id=row["agent_id"],
        device_id=row["device_id"],
        uuid=row["uuid"],
        base_url=row["base_url"],
        group=row["grp"],
        remark=row["remark"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        max_concurrent=row["max_concurrent"],
        status=row["status"],
        cooldown_until=row["cooldown_until"],
        fail_count=row["fail_count"],
        success_count=row["success_count"],
        last_used_at=row["last_used_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        signin_at=row["signin_at"],
        signin_status=row["signin_status"],
        signin_streak=row["signin_streak"],
        signin_points=row["signin_points"],
        signin_total=row["signin_total"],
        signin_error=row["signin_error"],
        quota=Quota.from_json(loads_or_none(row["quota"])),
        signin_panel=SigninPanel.from_json(loads_or_none(row["signin_panel"])),
        credit=Credit.from_json(loads_or_none(row["credit"])),
    )
    try:
        account.screen_width = int(row["screen_width"])
        account.screen_height = int(row["screen_height"])
    except (TypeError, ValueError):
        account.screen_width = 0
        account.screen_height = 0
    return account


def account_view(account: Account, inflight: int = 0) -> AccountView:
    return AccountView(
        id=account.id,
        name=account.name,
        kind=account.kind,
        region=account.region,
        user_id=account.user_id,
        identifier=account.identifier,
        agent_id=account.agent_id,
        device_id=account.device_id,
        uuid=account.uuid,
        screen_width=account.screen_width,
        screen_height=account.screen_height,
        base_url=account.base_url,
        group=account.group,
        remark=account.remark,
        enabled=account.enabled,
        priority=account.priority,
        max_concurrent=account.max_concurrent,
        status=account.status,
        cooldown_until=iso(account.cooldown_until),
        fail_count=account.fail_count,
        success_count=account.success_count,
        last_used_at=iso(account.last_used_at),
        last_error=account.last_error,
        created_at=iso(account.created_at),
        updated_at=iso(account.updated_at),
        quota=account.quota.to_json() if account.quota else None,
        token_masked=mask_token(account.token),
        inflight=inflight,
        signin_at=iso(account.signin_at),
        signin_status=account.signin_status,
        signin_streak=account.signin_streak,
        signin_points=account.signin_points,
        signin_total=account.signin_total,
        signin_error=account.signin_error,
        signin_panel=account.signin_panel.to_json() if account.signin_panel else None,
        credit=account.credit.to_json() if account.credit else None,
    )


class Database:
    def __init__(self, path: str, data_dir: str) -> None:
        self.path = path
        self.data_dir = data_dir
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._settings: config_mod.Settings = config_mod.default_settings(data_dir)

    # ------------------------------------------------------------------ plumbing

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn(conn)`` on a worker thread, under the lock."""
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return await asyncio.to_thread(self._run_sync, fn, *args)

    def _run_sync(self, fn: Callable[..., T], *args: Any) -> T:
        with self._lock:
            return fn(self._conn, *args)

    def _connect_sync(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        self._conn = conn

        row = conn.execute("SELECT json FROM settings WHERE id = 1").fetchone()
        raw = row["json"] if row else None
        # A stored document is authoritative, but repair happens immediately:
        # a value written by an earlier build and found broken afterwards has to
        # be overwritten on sight, not carried forward.
        settings = config_mod.parse_json(raw, self.data_dir) if raw else config_mod.default_settings(self.data_dir)
        if config_mod.normalize(settings, self.data_dir):
            conn.execute(
                "INSERT INTO settings (id, json) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                (config_mod.to_json(settings),),
            )
        self._settings = settings

        models = [ModelConfig.from_row(row) for row in conn.execute("SELECT * FROM models")]
        if merge_builtin_models(models):
            conn.executemany(
                "INSERT INTO models (id, name, upstream, upstream_model, type, enabled, builtin,"
                " description, requests, tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET upstream_model = excluded.upstream_model",
                [
                    (
                        model.id,
                        model.name,
                        model.upstream,
                        model.upstream_model,
                        model.type,
                        int(model.enabled),
                        int(model.builtin),
                        model.description,
                        model.requests,
                        model.tokens,
                    )
                    for model in models
                ],
            )
            conn.commit()

    # ------------------------------------------------------------------ settings

    def settings(self) -> config_mod.Settings:
        """Live settings, read from memory.  Hot paths must not touch the DB."""
        return self._settings

    async def update_settings(self, payload: dict[str, Any]) -> config_mod.Settings:
        def apply(conn: sqlite3.Connection) -> config_mod.Settings:
            settings = config_mod.parse_json(self._raw_settings(conn), self.data_dir)
            config_mod.apply_update(settings, payload)
            config_mod.normalize(settings, self.data_dir)
            self._raw_settings(conn, config_mod.to_json(settings))
            conn.commit()
            return settings

        settings = await self.run(apply)
        self._settings = settings
        return settings

    def _raw_settings(self, conn: sqlite3.Connection, value: str | None = None) -> str:
        if value is not None:
            conn.execute(
                "INSERT INTO settings (id, json) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                (value,),
            )
            conn.commit()
            return value
        row = conn.execute("SELECT json FROM settings WHERE id = 1").fetchone()
        return row["json"] if row else config_mod.to_json(config_mod.default_settings(self.data_dir))

    # --------------------------------------------------------------------- auth

    # ----------------------------------------------------------------- accounts

    async def list_accounts(self) -> list[Account]:
        def query(conn: sqlite3.Connection) -> list[Account]:
            rows = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts ORDER BY created_at, id"
            ).fetchall()
            return [account_from_row(row) for row in rows]

        return await self.run(query)

    async def account_by_id(self, account_id: str) -> Account | None:
        def query(conn: sqlite3.Connection) -> Account | None:
            row = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            return account_from_row(row) if row else None

        return await self.run(query)

    async def upsert_account(self, account: Account) -> Account:
        def apply(conn: sqlite3.Connection) -> Account:
            now = records.now_ts()
            account.created_at = now
            account.updated_at = now
            conn.execute(
                'INSERT INTO accounts (id, name, kind, region, token, user_id, identifier,'
                " agent_id, device_id, uuid, screen_width, screen_height, base_url, \"grp\", remark,"
                " enabled, priority, max_concurrent, status, cooldown_until, fail_count, success_count,"
                " last_used_at, last_error, created_at, updated_at, signin_at, signin_status,"
                " signin_streak, signin_points, signin_total, signin_error, signin_panel, credit, quota)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " name = excluded.name, kind = excluded.kind, region = excluded.region,"
                " token = excluded.token, user_id = excluded.user_id, identifier = excluded.identifier,"
                " agent_id = excluded.agent_id, device_id = excluded.device_id, uuid = excluded.uuid,"
                " screen_width = excluded.screen_width, screen_height = excluded.screen_height,"
                ' base_url = excluded.base_url, "grp" = excluded."grp", remark = excluded.remark,'
                " enabled = excluded.enabled, priority = excluded.priority,"
                " max_concurrent = excluded.max_concurrent, status = excluded.status,"
                " cooldown_until = excluded.cooldown_until, updated_at = excluded.updated_at",
                _account_params(account),
            )
            conn.commit()
            return account

        return await self.run(apply)

    async def update_account(self, account_id: str, apply_fn: Callable[[Account], None]) -> Account | None:
        def apply(conn: sqlite3.Connection) -> Account | None:
            row = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if not row:
                return None
            account = account_from_row(row)
            apply_fn(account)
            account.updated_at = records.now_ts()
            conn.execute(
                'UPDATE accounts SET name = ?, kind = ?, region = ?, token = ?, user_id = ?,'
                " identifier = ?, agent_id = ?, device_id = ?, uuid = ?, screen_width = ?,"
                " screen_height = ?, base_url = ?, \"grp\" = ?, remark = ?, enabled = ?, priority = ?,"
                " max_concurrent = ?, status = ?, cooldown_until = ?, fail_count = ?, success_count = ?,"
                " last_used_at = ?, last_error = ?, updated_at = ?, signin_at = ?, signin_status = ?,"
                " signin_streak = ?, signin_points = ?, signin_total = ?, signin_error = ?,"
                " signin_panel = ?, credit = ?, quota = ? WHERE id = ?",
                _update_params(account) + (account_id,),
            )
            conn.commit()
            return account

        return await self.run(apply)

    async def save_account_state(
        self, account_id: str, apply_fn: Callable[[Account], None]
    ) -> Account | None:
        """Bookkeeping write: status, counters, cooldown.  No updated_at touch."""

        def apply(conn: sqlite3.Connection) -> Account | None:
            row = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if not row:
                return None
            account = account_from_row(row)
            apply_fn(account)
            conn.execute(
                'UPDATE accounts SET status = ?, cooldown_until = ?, fail_count = ?,'
                " success_count = ?, last_used_at = ?, last_error = ?, signin_at = ?,"
                " signin_status = ?, signin_streak = ?, signin_points = ?, signin_total = ?,"
                " signin_error = ?, signin_panel = ?, credit = ?, quota = ? WHERE id = ?",
                _state_params(account) + (account_id,),
            )
            conn.commit()
            return account

        return await self.run(apply)

    async def delete_accounts(self, account_ids: list[str]) -> int:
        if not account_ids:
            return 0

        def apply(conn: sqlite3.Connection) -> int:
            marks = ",".join("?" for _ in account_ids)
            cursor = conn.execute(f"DELETE FROM accounts WHERE id IN ({marks})", account_ids)
            conn.commit()
            return cursor.rowcount or 0

        return await self.run(apply)

    async def account_groups(self) -> list[str]:
        def query(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                'SELECT DISTINCT "grp" FROM accounts WHERE "grp" != \'\' ORDER BY "grp"'
            ).fetchall()
            return [row["grp"] for row in rows]

        return await self.run(query)

    # ------------------------------------------------------------------- models

    async def list_models(self) -> list[ModelConfig]:
        def query(conn: sqlite3.Connection) -> list[ModelConfig]:
            rows = conn.execute("SELECT * FROM models ORDER BY id").fetchall()
            return [ModelConfig.from_row(row) for row in rows]

        return await self.run(query)

    async def model_by_id(self, model_id: str) -> ModelConfig | None:
        def query(conn: sqlite3.Connection) -> ModelConfig | None:
            row = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
            return ModelConfig.from_row(row) if row else None

        return await self.run(query)

    async def update_model(
        self, model_id: str, apply_fn: Callable[[ModelConfig], None]
    ) -> ModelConfig | None:
        def apply(conn: sqlite3.Connection) -> ModelConfig | None:
            row = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
            if not row:
                return None
            model = ModelConfig.from_row(row)
            apply_fn(model)
            conn.execute(
                "UPDATE models SET enabled = ?, description = ?, requests = ?, tokens = ? WHERE id = ?",
                (int(model.enabled), model.description, model.requests, model.tokens, model_id),
            )
            conn.commit()
            return model

        return await self.run(apply)

    async def record_model_usage(self, model_id: str, requests: int, tokens: int) -> None:
        if not model_id:
            return

        def apply(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE models SET requests = requests + ?, tokens = tokens + ? WHERE id = ?",
                (requests, tokens, model_id),
            )
            conn.commit()

        await self.run(apply)

    # ------------------------------------------------------------------- audits

    async def append_audit(self, audit: Audit) -> None:
        def apply(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO audits (id, created_at, model, account_name, status, outcome,"
                " latency_ms, first_token_ms, prompt_tokens, completion_tokens, stream, retries, ip,"
                " user_agent, error, request_body, response_body)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit.id,
                    audit.created_at,
                    audit.model,
                    audit.account_name,
                    audit.status,
                    audit.outcome,
                    audit.latency_ms,
                    audit.first_token_ms,
                    audit.prompt_tokens,
                    audit.completion_tokens,
                    int(audit.stream),
                    audit.retries,
                    audit.ip,
                    audit.user_agent,
                    audit.error,
                    audit.request_body[: records.HTTP_BODY_LIMIT],
                    audit.response_body[: records.HTTP_BODY_LIMIT],
                ),
            )
            conn.commit()

        await self.run(apply)

    async def list_audits(
        self, limit: int = 100, offset: int = 0, model: str = "", outcome: str = ""
    ) -> tuple[list[Audit], int]:
        def query(conn: sqlite3.Connection) -> tuple[list[Audit], int]:
            clauses = []
            params: list[Any] = []
            if model:
                clauses.append("model = ?")
                params.append(model)
            if outcome:
                clauses.append("outcome = ?")
                params.append(outcome)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            total = conn.execute(f"SELECT COUNT(*) AS n FROM audits{where}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM audits{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return [_audit_from_row(row) for row in rows], total

        return await self.run(query)

    async def audit_by_id(self, audit_id: str) -> Audit | None:
        def query(conn: sqlite3.Connection) -> Audit | None:
            row = conn.execute("SELECT * FROM audits WHERE id = ?", (audit_id,)).fetchone()
            return _audit_from_row(row) if row else None

        return await self.run(query)

    async def clear_audits(self) -> None:
        def apply(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM audits")
            conn.commit()

        await self.run(apply)

    async def purge_audits(self, retention_days: int, max_records: int) -> int:
        def apply(conn: sqlite3.Connection) -> int:
            removed = 0
            if retention_days > 0:
                cutoff = records.now_ts() - retention_days * 86400
                cursor = conn.execute("DELETE FROM audits WHERE created_at < ?", (cutoff,))
                removed += cursor.rowcount or 0
            if max_records > 0:
                total = conn.execute("SELECT COUNT(*) AS n FROM audits").fetchone()["n"]
                if total > max_records:
                    cursor = conn.execute(
                        "DELETE FROM audits WHERE id IN (SELECT id FROM audits"
                        " ORDER BY created_at ASC LIMIT ?)",
                        (total - max_records,),
                    )
                    removed += cursor.rowcount or 0
            if removed:
                conn.commit()
            return removed

        return await self.run(apply)

    async def dashboard_stats(self, days: int = 14) -> dict[str, Any]:
        def query(conn: sqlite3.Connection) -> dict[str, Any]:
            since = records.now_ts() - days * 86400
            trend = [
                {
                    "date": row["day"],
                    "requests": row["n"],
                    "errors": row["errors"],
                    "tokens": row["tokens"],
                }
                for row in conn.execute(
                    "SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') AS day,"
                    " COUNT(*) AS n, SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) AS errors,"
                    " SUM(prompt_tokens + completion_tokens) AS tokens"
                    " FROM audits WHERE created_at >= ? GROUP BY day ORDER BY day",
                    (since,),
                ).fetchall()
            ]
            models = [
                {"model": row["model"], "requests": row["n"]}
                for row in conn.execute(
                    "SELECT model, COUNT(*) AS n FROM audits WHERE created_at >= ?"
                    " GROUP BY model ORDER BY n DESC LIMIT 10",
                    (since,),
                ).fetchall()
            ]
            accounts = [
                {"accountName": row["account_name"], "requests": row["n"]}
                for row in conn.execute(
                    "SELECT account_name, COUNT(*) AS n FROM audits WHERE created_at >= ?"
                    " GROUP BY account_name ORDER BY n DESC LIMIT 10",
                    (since,),
                ).fetchall()
            ]
            totals = conn.execute(
                "SELECT COUNT(*) AS n, AVG(latency_ms) AS avg_latency FROM audits"
            ).fetchone()
            successes = conn.execute(
                "SELECT COUNT(*) AS n FROM audits WHERE outcome = 'ok'"
            ).fetchone()["n"]
            total_requests = totals["n"] if totals else 0
            return {
                "trend": trend,
                "models": models,
                "accounts": accounts,
                "totals": {
                    "requests": total_requests,
                    "successes": successes,
                    "failures": (total_requests or 0) - (successes or 0),
                    "avgLatencyMs": int(totals["avg_latency"] or 0) if totals else 0,
                },
            }

        return await self.run(query)

    # -------------------------------------------------------------------- media

    async def add_media(self, item: MediaItem) -> None:
        def apply(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO media (id, kind, url, source_url, prompt, model, account_name, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.kind,
                    item.url,
                    item.source_url,
                    item.prompt,
                    item.model,
                    item.account_name,
                    item.created_at,
                ),
            )
            conn.commit()

        await self.run(apply)

    async def list_media(self, limit: int = 200) -> list[MediaItem]:
        def query(conn: sqlite3.Connection) -> list[MediaItem]:
            rows = conn.execute(
                "SELECT * FROM media ORDER BY created_at DESC, id LIMIT ?", (limit,)
            ).fetchall()
            return [
                MediaItem(
                    id=row["id"],
                    kind=row["kind"],
                    url=row["url"],
                    source_url=row["source_url"],
                    prompt=row["prompt"],
                    model=row["model"],
                    account_name=row["account_name"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

        return await self.run(query)

    async def delete_media(self, media_id: str) -> MediaItem | None:
        def apply(conn: sqlite3.Connection) -> MediaItem | None:
            row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
            conn.commit()
            return MediaItem(
                id=row["id"],
                kind=row["kind"],
                url=row["url"],
                source_url=row["source_url"],
                prompt=row["prompt"],
                model=row["model"],
                account_name=row["account_name"],
                created_at=row["created_at"],
            )

        return await self.run(apply)


def _account_params(account: Account) -> tuple[Any, ...]:
    return (
        account.id,
        account.name,
        account.kind,
        account.region,
        account.token,
        account.user_id,
        account.identifier,
        account.agent_id,
        account.device_id,
        account.uuid,
        account.screen_width,
        account.screen_height,
        account.base_url,
        account.group,
        account.remark,
        int(account.enabled),
        account.priority,
        account.max_concurrent,
        account.status,
        account.cooldown_until,
        account.fail_count,
        account.success_count,
        account.last_used_at,
        account.last_error,
        account.created_at,
        account.updated_at,
        account.signin_at,
        account.signin_status,
        account.signin_streak,
        account.signin_points,
        account.signin_total,
        account.signin_error,
        dumps(account.signin_panel.to_json()) if account.signin_panel else "",
        dumps(account.credit.to_json()) if account.credit else "",
        dumps(account.quota.to_json()) if account.quota else "",
    )


def _state_params(account: Account) -> tuple[Any, ...]:
    """The fifteen bookkeeping columns, in save_account_state's own order.

    Deliberately not shared with _account_params: the two statements touch
    different sets, and reusing one for the other is how a column count drifts by
    one and only fails on the update that happened to be added later.
    """
    return (
        account.status,
        account.cooldown_until,
        account.fail_count,
        account.success_count,
        account.last_used_at,
        account.last_error,
        account.signin_at,
        account.signin_status,
        account.signin_streak,
        account.signin_points,
        account.signin_total,
        account.signin_error,
        dumps(account.signin_panel.to_json()) if account.signin_panel else "",
        dumps(account.credit.to_json()) if account.credit else "",
        dumps(account.quota.to_json()) if account.quota else "",
    )


def _update_params(account: Account) -> tuple[Any, ...]:
    """Every column update_account writes, in the order its statement lists them.

    ``created_at`` is absent on purpose: an edit is not a creation, and a
    full-statement params list that also carried it would need the statement to
    skip it in exactly one place.
    """
    return (
        account.name,
        account.kind,
        account.region,
        account.token,
        account.user_id,
        account.identifier,
        account.agent_id,
        account.device_id,
        account.uuid,
        account.screen_width,
        account.screen_height,
        account.base_url,
        account.group,
        account.remark,
        int(account.enabled),
        account.priority,
        account.max_concurrent,
        account.status,
        account.cooldown_until,
        account.fail_count,
        account.success_count,
        account.last_used_at,
        account.last_error,
        account.updated_at,
        account.signin_at,
        account.signin_status,
        account.signin_streak,
        account.signin_points,
        account.signin_total,
        account.signin_error,
        dumps(account.signin_panel.to_json()) if account.signin_panel else "",
        dumps(account.credit.to_json()) if account.credit else "",
        dumps(account.quota.to_json()) if account.quota else "",
    )


def _audit_from_row(row: sqlite3.Row) -> Audit:
    return Audit(
        id=row["id"],
        created_at=row["created_at"],
        model=row["model"],
        account_name=row["account_name"],
        status=row["status"],
        outcome=row["outcome"],
        latency_ms=row["latency_ms"],
        first_token_ms=row["first_token_ms"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        stream=bool(row["stream"]),
        retries=row["retries"],
        ip=row["ip"],
        user_agent=row["user_agent"],
        error=row["error"],
        request_body=row["request_body"],
        response_body=row["response_body"],
    )
