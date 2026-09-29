"""The ASGI app: routing, auth, and the console's static files.

Everything that holds state is built once here and passed down, because the
alternative — each route reaching into a module-level global — is what makes a
single-file example impossible to test.  The lifespan owns the tasks too: a pool
that is never loaded, or a check-in loop that is never stopped, both survive a
worker shutdown and neither is a bug anyone notices until it happens on a server.

Two paths serve the same page.  ``/`` is where somebody who just started the
process lands; ``/admin`` is where the links point.  Both answer the console.
"""

from __future__ import annotations

import asyncio
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from . import admin as admin_mod
from . import device_login
from . import gateway as gateway_mod
from . import keepalive as keepalive_mod
from . import pool as pool_mod
from . import signin as signin_mod
from . import signup as signup_mod
from . import upstream
from .db import Database
from .media import MediaStore

_STATIC = Path(__file__).resolve().parent / "static"

# A request body is capped separately from an audit body: an image message can
# carry megabytes of base64, and refusing those here would refuse the feature.
MAX_REQUEST_BODY = 16 * 1024 * 1024


class AppState:
    """Everything shared, in one object a route can be handed."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.db = Database(str(Path(data_dir) / "bridge.sqlite3"), data_dir)
        self.pool = pool_mod.Pool(self.db, lambda: self.db.settings())
        self.client = upstream.MiniMaxClient(lambda: self.db.settings())
        self.media = MediaStore(self.db, lambda: self.db.settings())
        self.signin = signin_mod.SigninService(
            self.db, self.pool, self.client, lambda: self.db.settings()
        )
        self.gateway = gateway_mod.Gateway(
            self.db, self.pool, self.client, self.media, lambda: self.db.settings()
        )
        self.admin = admin_mod.AdminService(
            self.db,
            self.pool,
            self.client,
            self.media,
            self.signin,
            lambda: self.db.settings(),
        )
        # Needs the admin, so it is built after it: the approved token is imported
        # as an account, and the import already knows how.
        self.device_login = device_login.DeviceLoginService(self.client, self.admin)
        # Same shape of dependency: registration ends by minting an OAuth token and
        # importing it, which is the admin's job.  The check-in service rides along
        # so a new account claims its first day of credit immediately — that credit
        # is issued by the check-in endpoint, not by registration.
        self.signup = signup_mod.SignupService(
            self.client, self.admin, lambda: self.db.settings(), self.db, self.signin
        )
        # Renews the one-hour OAuth tokens before they expire.  Built last because
        # it is the only component that reasons about credentials already in the
        # pool rather than about one arriving.
        self.keepalive = keepalive_mod.Keeper(
            self.db, self.client, lambda: self.db.settings()
        )
        self.admin.keepalive = self.keepalive
        # The request path's safety net: a token can expire between two sweeps, so
        # the pool renews on a refused credential before retiring the account.
        self.pool.renewer = self.keepalive.renew_account

    async def open(self) -> None:
        await self.db.connect()
        self.media.ensure_dir()
        await self.pool.load()
        # Only one background task touches accounts, so there is nothing here to
        # coordinate beyond starting it.
        self.signin.start()
        self.keepalive.start()

    async def close(self) -> None:
        await self.signin.stop()
        await self.keepalive.stop()
        await self.client.aclose()


def create_app(data_dir: str | None = None) -> FastAPI:
    """Build the app.  ``data_dir`` defaults to ./data, which uvicorn's factory
    needs, since it calls this with no arguments."""
    state = AppState(data_dir or "./data")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await state.open()
        task = asyncio.create_task(_purge_loop(state), name="minimaxcode2api-purge")
        try:
            yield
        finally:
            task.cancel()
            await state.close()

    app = FastAPI(title="minimaxcode2api", version="1.0.0", lifespan=lifespan)
    app.state.bridge = state

    app.add_exception_handler(admin_mod.AdminError, _admin_error)
    app.add_exception_handler(gateway_mod.APIError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)

    _register(app, state)
    return app


# -------------------------------------------------------------------- middleware


async def _body_limit(request: Request) -> None:
    """Refuse a request whose body is larger than the whole process will buffer.

    Applied before the handler runs, because an image message can carry megabytes
    of base64 and a reverse proxy is not where that decision belongs.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        size = int(raw)
    except ValueError:
        return None
    if size > MAX_REQUEST_BODY:
        return JSONResponse(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"body over {MAX_REQUEST_BODY} bytes",
                }
            },
            status_code=413,
        )
    return None


# ---------------------------------------------------------------------- routes


def _register(app: FastAPI, state: AppState) -> None:
    from fastapi import Query

    async def json_body(request: Request) -> Any:
        try:
            return await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is a client error
            raise gateway_mod.APIError(400, "invalid_request", "request body is not valid JSON")

    # ---------------------------------------------------------------- serving

    @app.get("/health")
    async def health() -> Any:
        return await state.gateway.health()

    @app.get("/v1/models")
    async def models() -> Any:
        return await state.gateway.models()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        gate = await _body_limit(request)
        if gate is not None:
            return gate
        return await state.gateway.chat_completions(request, await json_body(request))

    @app.post("/v1/messages")
    async def messages(request: Request) -> Any:
        gate = await _body_limit(request)
        if gate is not None:
            return gate
        return await state.gateway.anthropic_messages(request, await json_body(request))

    # ------------------------------------------------------------------- media

    @app.get("/media/{media_id}")
    async def media(media_id: str) -> Any:
        # The id shape is checked before it ever reaches the filesystem; see
        # MediaStore.path_for.
        path = state.media.path_for(media_id)
        if path is None:
            return JSONResponse({"error": {"code": "not_found", "message": "media not found"}}, status_code=404)
        return FileResponse(path, filename=media_id, media_type=_media_type(path))

    # ------------------------------------------------------------------- admin

    @app.get("/admin/api/accounts")
    async def list_accounts(request: Request) -> Any:
        return await state.admin.list_accounts()

    @app.post("/admin/api/accounts/probe-all")
    async def probe_all_accounts(request: Request) -> Any:
        body = await json_body(request)
        ids = body.get("accountIds") if isinstance(body, dict) else None
        return await state.admin.probe_all(ids if isinstance(ids, list) else None)

    @app.post("/admin/api/accounts")
    async def import_accounts(request: Request) -> Any:
        return await state.admin.import_accounts(await json_body(request))

    @app.post("/admin/api/accounts/password")
    async def import_password_accounts(request: Request) -> Any:
        """Import email+password entries, signing each in for a token."""
        return await state.admin.import_password_accounts(await json_body(request))

    @app.post("/admin/api/accounts/discover")
    async def import_accounts_silent(request: Request) -> Any:
        """Import without contacting the upstream, for a bulk token dump."""
        return await state.admin.import_accounts(await json_body(request), discover=False)

    @app.patch("/admin/api/accounts/{account_id}")
    async def update_account(account_id: str, request: Request) -> Any:
        return await state.admin.update_account(account_id, await json_body(request))

    @app.delete("/admin/api/accounts")
    async def delete_accounts(request: Request) -> Any:
        body = await json_body(request)
        ids = body.get("ids") if isinstance(body, dict) else body
        return await state.admin.delete_accounts(ids or [])

    @app.post("/admin/api/accounts/{account_id}/{action}")
    async def account_action(account_id: str, action: str, request: Request) -> Any:
        return await state.admin.account_action(account_id, action)

    @app.get("/admin/api/accounts/{account_id}/password")
    async def account_password(account_id: str) -> Any:
        return await state.admin.account_password(account_id)

    @app.post("/admin/api/login-device")
    async def login_device_start(request: Request) -> Any:
        """Begin a browser sign-in.  Answers a URL to click and a code to show."""
        body = await json_body(request)
        name = body.get("name") if isinstance(body, dict) else ""
        region = body.get("region") if isinstance(body, dict) else ""
        return await state.device_login.start(str(name or ""), str(region or ""))

    @app.get("/admin/api/login-device/{session_id}")
    async def login_device_status(session_id: str) -> Any:
        return state.device_login.get(session_id)

    @app.delete("/admin/api/login-device/{session_id}")
    async def login_device_cancel(session_id: str) -> Any:
        return await state.device_login.cancel(session_id)

    @app.post("/admin/api/signup")
    async def signup_start(request: Request) -> Any:
        """Begin a headless registration.  Answers a session to poll."""
        body = await json_body(request)
        name = body.get("name") if isinstance(body, dict) else ""
        region = body.get("region") if isinstance(body, dict) else ""
        password = body.get("password") if isinstance(body, dict) else ""
        count = body.get("count") if isinstance(body, dict) else 1
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 1
        return await state.signup.start(
            str(name or ""), str(region or ""), count, str(password or "")
        )

    @app.get("/admin/api/signup/{session_id}")
    async def signup_status(session_id: str) -> Any:
        return state.signup.get(session_id)

    @app.delete("/admin/api/signup/{session_id}")
    async def signup_cancel(session_id: str) -> Any:
        return await state.signup.cancel(session_id)

    # ------------------------------------------------------------------ proxies

    @app.get("/admin/api/proxies")
    async def list_proxies(request: Request) -> Any:
        return await state.admin.list_proxies()

    @app.post("/admin/api/proxies")
    async def add_proxies(request: Request) -> Any:
        return await state.admin.add_proxies(await json_body(request))

    @app.patch("/admin/api/proxies/{proxy_id}")
    async def update_proxy(proxy_id: str, request: Request) -> Any:
        return await state.admin.update_proxy(proxy_id, await json_body(request))

    @app.delete("/admin/api/proxies/{proxy_id}")
    async def delete_proxy(proxy_id: str) -> Any:
        return await state.admin.delete_proxy(proxy_id)

    @app.post("/admin/api/proxies/{proxy_id}/check")
    async def check_proxy(proxy_id: str) -> Any:
        return await state.admin.check_proxy(proxy_id)

    @app.post("/admin/api/proxies/check-all")
    async def check_all_proxies(request: Request) -> Any:
        return await state.admin.check_all_proxies()

    @app.get("/admin/api/groups")
    async def groups(request: Request) -> Any:
        return await state.admin.groups()

    @app.get("/admin/api/models")
    async def list_models(request: Request) -> Any:
        return await state.admin.list_models()

    @app.patch("/admin/api/models/{model_id}")
    async def update_model(model_id: str, request: Request) -> Any:
        return await state.admin.update_model(model_id, await json_body(request))

    @app.get("/admin/api/audits")
    async def list_audits(
        request: Request,
        limit: int = Query(50),
        offset: int = Query(0),
        model: str = Query(""),
        outcome: str = Query(""),
    ) -> Any:
        return await state.admin.list_audits(limit, offset, model, outcome)

    @app.delete("/admin/api/audits")
    async def clear_audits(request: Request) -> Any:
        await state.admin.clear_audits()
        return {"ok": True}

    @app.get("/admin/api/stats")
    async def stats(request: Request) -> Any:
        return await state.admin.stats()

    @app.get("/admin/api/media")
    async def list_media(request: Request) -> Any:
        return await state.admin.list_media()

    @app.delete("/admin/api/media/{media_id}")
    async def delete_media(media_id: str, request: Request) -> Any:
        return await state.admin.delete_media(media_id)

    @app.get("/admin/api/media/size")
    async def media_size(request: Request) -> Any:
        return await state.admin.media_size()

    @app.get("/admin/api/settings")
    async def get_settings(request: Request) -> Any:
        return await state.admin.get_settings()

    @app.post("/admin/api/settings")
    async def update_settings(request: Request) -> Any:
        return await state.admin.update_settings(await json_body(request))

    @app.get("/admin/api/signin")
    async def signin_status(request: Request) -> Any:
        return await state.admin.signin_status()

    @app.get("/admin/api/keepalive")
    async def keepalive_status(request: Request) -> Any:
        return await state.admin.keepalive_status()

    @app.post("/admin/api/keepalive/sweep")
    async def keepalive_sweep(request: Request) -> Any:
        return await state.admin.keepalive_sweep()

    @app.post("/admin/api/signin/run")
    async def signin_run(request: Request) -> Any:
        body = await json_body(request)
        ids = body.get("accountIds") if isinstance(body, dict) else None
        return await state.admin.signin_run_now(ids if isinstance(ids, list) else None)

    @app.get("/admin/api/credit")
    async def credit_status(request: Request) -> Any:
        return await state.admin.credit_status()

    @app.post("/admin/api/credit/refresh")
    async def credit_refresh(request: Request) -> Any:
        """Refresh balances now.  Body may name ids, else every stale account."""
        body = await json_body(request)
        ids = body.get("accountIds") if isinstance(body, dict) else None
        return await state.admin.credit_refresh(ids if isinstance(ids, list) else None)

    # ------------------------------------------------------------------- pages

    for path in ("/", "/admin", "/admin/"):
        @app.get(path, include_in_schema=False)
        async def console() -> Any:
            # The console is one file, which is also why there is no build step:
            # whatever this serves is the version that was written.
            return FileResponse(_STATIC / "index.html")

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Any) -> Any:
        """Unknown API paths answer JSON; unknown page paths answer the console.

        The split matters for a client that asked the API: a page of HTML in place
        of an error is indistinguishable from the upstream's own habit.
        """
        if request.method == "GET" and not request.url.path.startswith(("/v1", "/admin/api", "/media")):
            if (_STATIC / "index.html").is_file():
                return FileResponse(_STATIC / "index.html")
        return JSONResponse({"error": "not found"}, status_code=404)


async def _purge_loop(state: AppState) -> None:
    """Trim the audit table in the background, hourly.

    Retention is a settings problem, not a request problem: a request that has to
    remember it also knows how to be lost in a crash.
    """
    while True:
        await asyncio.sleep(3600)
        settings = state.db.settings()
        await state.db.purge_audits(settings.audit.retention_days, settings.audit.max_records)


def _media_type(path: Path) -> str:
    import mimetypes

    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


# -------------------------------------------------------------------- handlers


async def _admin_error(request: Request, exc: admin_mod.AdminError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


async def _api_error(request: Request, exc: gateway_mod.APIError) -> JSONResponse:
    return JSONResponse(exc.payload(), status_code=exc.status)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """A malformed body is reported in the shape a client already parses."""
    return JSONResponse(
        {"error": {"code": "invalid_request", "message": _first_reason(exc)}},
        status_code=400,
    )


def _first_reason(exc: RequestValidationError) -> str:
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc") or ())
        message = item.get("msg") or "invalid value"
        return f"{location}: {message}" if location else message
    return "invalid request"
