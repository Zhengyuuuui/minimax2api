"""Persisted records.

The one shape worth calling out is ``Account.token``.  It travels in and out of
this object but is never serialised: ``AccountView`` is a separate projection
that carries a masked form instead, so no handler can leak a JWT by returning an
account.
"""

from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass, field
from typing import Any

# Request and response bodies are recorded for auditing, but an audit row is
# not a trophy case: an image message can carry megabytes of base64, and the
# body is truncated rather than allowed to bloat the store.
HTTP_BODY_LIMIT = 32_768

# Account status values.
STATUS_ACTIVE = "active"
STATUS_COOLDOWN = "cooldown"
STATUS_DISABLED = "disabled"
STATUS_INVALID = "invalid"

KIND_TOKEN = "token"
KIND_GUEST = "guest"
# A credential from the desktop sign-in flow: an opaque OAuth access token sent
# in an Authorization header, where KIND_TOKEN is a web JWT sent as a query
# parameter.  Same upstream, two ways in; see upstream.Credential.
KIND_OAUTH = "oauth"

# How an account was imported.  ``token`` is a browser sign-in or a pasted JWT: a
# credential only, with whatever refresh token the flow managed to capture.
# ``password`` is an email+password entry, which can always be signed in again
# from the console; ``signup`` is a headless registration, which produces both.
SOURCE_TOKEN = "token"
SOURCE_PASSWORD = "password"
SOURCE_SIGNUP = "signup"
SOURCE_DEVICE = "device"

# Model types. Not cosmetic: the gateway dispatches on them.
MODEL_CHAT = "chat"
MODEL_IMAGE = "image"

# Region is an account property, not a global switch: the two deployments have
# separate account databases and a token from one is rejected by the other.
REGION_CN = "cn"
REGION_GLOBAL = "global"

# Check-in outcomes.
SIGNIN_OK = "ok"
SIGNIN_ALREADY = "already"
SIGNIN_FAILED = "failed"
SIGNIN_SKIPPED = "skipped"
# The claim endpoint answers success whether or not the points were issued, so
# "claimed but never paid" is a state only the credit grants can reveal.  It is
# a distinct value because it must not read as a successful check-in.
SIGNIN_UNPAID = "unpaid"

AUDIT_OK = "ok"
AUDIT_ERROR = "error"

MEDIA_IMAGE = "image"


def now_ts() -> float:
    return time.time()


def iso(ts: float) -> str:
    """Render a unix timestamp as an ISO-8601 UTC string for the console."""
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def loads_or_none(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass
class Quota:
    """The last probe of an account."""

    synced_at: float = 0.0
    available: bool = False
    latency_ms: int = 0
    plan: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "syncedAt": iso(self.synced_at),
            "available": self.available,
            "latencyMs": self.latency_ms,
            "plan": self.plan,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, node: Any) -> "Quota | None":
        if not isinstance(node, dict):
            return None
        return cls(
            synced_at=_as_ts(node.get("syncedAt")),
            available=bool(node.get("available")),
            latency_ms=int(_as_float(node.get("latencyMs"))),
            plan=str(node.get("plan") or ""),
            note=str(node.get("note") or ""),
        )


@dataclass
class SigninDay:
    day_no: int = 0
    points: int = 0
    status: int = 0
    is_today: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "dayNo": self.day_no,
            "points": self.points,
            "status": self.status,
            "isToday": self.is_today,
        }

    @classmethod
    def from_json(cls, node: Any) -> "SigninDay | None":
        if not isinstance(node, dict):
            return None
        return cls(
            day_no=int(_as_float(node.get("dayNo"))),
            points=int(_as_float(node.get("points"))),
            status=int(_as_float(node.get("status"))),
            is_today=bool(node.get("isToday")),
        )


@dataclass
class SigninPanel:
    """The seven-day board, persisted so the console need not call upstream."""

    scene: int = 0
    days: list[SigninDay] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"scene": self.scene, "days": [day.to_json() for day in self.days]}

    @classmethod
    def from_json(cls, node: Any) -> "SigninPanel | None":
        if not isinstance(node, dict) or not isinstance(node.get("days"), list):
            return None
        days = [day for day in (SigninDay.from_json(item) for item in node["days"]) if day]
        return cls(scene=int(_as_float(node.get("scene"))), days=days)

    @classmethod
    def from_upstream(cls, node: Any) -> "SigninPanel | None":
        return cls.from_json(node)

    @property
    def today(self) -> SigninDay | None:
        for day in self.days:
            if day.is_today:
                return day
        return None


@dataclass
class Credit:
    """The last observed balance.

    Routing only acts on it while the reading is fresh (see
    ``SigninSettings.credit_fresh_min``): a day-old zero would otherwise strand
    capacity that a single request refills.
    """

    total: float = 0.0
    free: float = 0.0
    purchased: float = 0.0
    plan_name: str = ""
    plan_type: int = 0
    synced_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "free": self.free,
            "purchased": self.purchased,
            "planName": self.plan_name,
            "planType": self.plan_type,
            "syncedAt": iso(self.synced_at),
        }

    @classmethod
    def from_json(cls, node: Any) -> "Credit | None":
        if not isinstance(node, dict):
            return None
        return cls(
            total=_as_float(node.get("total")),
            free=_as_float(node.get("free")),
            purchased=_as_float(node.get("purchased")),
            plan_name=str(node.get("planName") or ""),
            plan_type=int(_as_float(node.get("planType"))),
            synced_at=_as_ts(node.get("syncedAt")),
        )

    def exhausted(self) -> bool:
        return self.total <= 0


@dataclass
class Account:
    name: str = ""
    kind: str = KIND_TOKEN
    region: str = REGION_GLOBAL
    # The JWT lifted out of localStorage._token.
    token: str = ""
    # The account's realUserID.  It is not in the JWT and cannot be derived
    # from it; every signed endpoint wants it as `user_id` and answers a bare
    # 401 without it.
    user_id: str = ""
    # Best-effort email or phone decoded from the token, used to tell accounts
    # apart in the console.
    identifier: str = ""
    # The address the account was registered with, and the password if one was
    # set.  Separate from ``identifier``, which the upstream reports and which
    # may be a username or a phone: neither is the login the operator reads off
    # the console to sign in by hand.  The password is only ever returned masked.
    email: str = ""
    password: str = ""
    # The OAuth refresh token and when the access token stops working.
    #
    # The access token lives one hour, so a pool without these is a pool that
    # empties itself every hour.  The refresh token is what makes the credential
    # renewable without a mailbox; ``token_expires_at`` lets the keeper refresh
    # *before* a request fails, instead of discovering it from a 401.
    refresh_token: str = ""
    token_expires_at: float = 0.0
    # The numeric agent id.  A role name is never an id: the upstream answers
    # one with a 200 that opens no session.
    agent_id: str = ""
    # The browser fingerprint.  Not decoration: `yy` is computed over the query
    # string built from these, so a token only replays with the fingerprint it
    # was issued to.  device_id must be digits.
    device_id: str = ""
    uuid: str = ""
    screen_width: int = 0
    screen_height: int = 0
    base_url: str = ""
    group: str = ""
    remark: str = ""
    # How the account arrived.  The two are not interchangeable: a password
    # import brought an email and a password and can be re-signed-in from the
    # console alone, while a browser sign-in brought only a token and depends on
    # its refresh token to survive.  The console shows which is which so an
    # operator knows what an account can do without opening it.
    source: str = SOURCE_TOKEN
    enabled: bool = True
    priority: int = 0
    max_concurrent: int = 1
    status: str = STATUS_ACTIVE
    cooldown_until: float = 0.0
    fail_count: int = 0
    success_count: int = 0
    last_used_at: float = 0.0
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    id: str = ""
    quota: Quota | None = None
    signin_at: float = 0.0
    signin_status: str = ""
    signin_streak: int = 0
    signin_points: int = 0
    signin_total: float = 0.0
    signin_error: str = ""
    signin_panel: SigninPanel | None = None
    credit: Credit | None = None


@dataclass
class AccountView:
    """The API projection.  Never carries the token."""

    id: str
    name: str
    kind: str
    region: str
    user_id: str
    identifier: str
    email: str
    agent_id: str
    device_id: str
    uuid: str
    screen_width: int
    screen_height: int
    base_url: str
    group: str
    remark: str
    source: str
    enabled: bool
    priority: int
    max_concurrent: int
    status: str
    cooldown_until: str
    fail_count: int
    success_count: int
    last_used_at: str
    last_error: str
    created_at: str
    updated_at: str
    quota: dict[str, Any] | None
    token_masked: str
    password_masked: str
    # Whether renewal is possible and when the current token dies; the refresh
    # token itself is never projected, only the fact that one exists.
    renewable: bool
    token_expires_at: str
    inflight: int
    signin_at: str
    signin_status: str
    signin_streak: int
    signin_points: int
    signin_total: float
    signin_error: str
    signin_panel: dict[str, Any] | None
    credit: dict[str, Any] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "region": self.region,
            "userId": self.user_id,
            "identifier": self.identifier,
            "email": self.email,
            "agentID": self.agent_id,
            "deviceID": self.device_id,
            "uuid": self.uuid,
            "screenWidth": self.screen_width,
            "screenHeight": self.screen_height,
            "baseURL": self.base_url,
            "group": self.group,
            "remark": self.remark,
            "source": self.source,
            "enabled": self.enabled,
            "priority": self.priority,
            "maxConcurrent": self.max_concurrent,
            "status": self.status,
            "cooldownUntil": self.cooldown_until,
            "failCount": self.fail_count,
            "successCount": self.success_count,
            "lastUsedAt": self.last_used_at,
            "lastError": self.last_error,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "quota": self.quota,
            "tokenMasked": self.token_masked,
            "passwordMasked": self.password_masked,
            "renewable": self.renewable,
            "tokenExpiresAt": self.token_expires_at,
            "inflight": self.inflight,
            "signinAt": self.signin_at,
            "signinStatus": self.signin_status,
            "signinStreak": self.signin_streak,
            "signinPoints": self.signin_points,
            "signinTotal": self.signin_total,
            "signinError": self.signin_error,
            "signinPanel": self.signin_panel,
            "credit": self.credit,
        }


@dataclass
class Audit:
    id: str = ""
    created_at: float = 0.0
    model: str = ""
    account_name: str = ""
    status: int = 0
    outcome: str = AUDIT_OK
    latency_ms: int = 0
    first_token_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stream: bool = False
    retries: int = 0
    ip: str = ""
    user_agent: str = ""
    error: str = ""
    request_body: str = ""
    response_body: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "createdAt": iso(self.created_at),
            "model": self.model,
            "accountName": self.account_name,
            "status": self.status,
            "outcome": self.outcome,
            "latencyMs": self.latency_ms,
            "firstTokenMs": self.first_token_ms,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "stream": self.stream,
            "retries": self.retries,
            "ip": self.ip,
            "userAgent": self.user_agent,
            "error": self.error,
            "requestBody": self.request_body,
            "responseBody": self.response_body,
        }


@dataclass
class MediaItem:
    id: str = ""
    kind: str = MEDIA_IMAGE
    url: str = ""
    source_url: str = ""
    prompt: str = ""
    model: str = ""
    account_name: str = ""
    created_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "url": self.url,
            "sourceUrl": self.source_url,
            "prompt": self.prompt,
            "model": self.model,
            "accountName": self.account_name,
            "createdAt": iso(self.created_at),
        }


# Proxy status values.  ``unknown`` is the state an entry is added in: it has not
# been dialled yet, and "not tried" is not "dead".
PROXY_UNKNOWN = "unknown"
PROXY_OK = "ok"
PROXY_BAD = "bad"

# How a proxy is chosen for the next registration.  ``rotate`` walks the list in
# order so successive accounts leave by different addresses; ``random`` picks one;
# ``single`` pins the first usable entry (useful when only one is trustworthy).
PROXY_STRATEGIES = ("rotate", "random", "single")


@dataclass
class Proxy:
    """One outbound proxy, and the last thing known about it.

    ``exit_ip`` is the point of the record: the risk control that matters is
    per-address, so a proxy without a resolved exit address cannot be counted
    against a limit.  It is filled by ``proxy.check`` rather than at add time,
    because adding a URL is a paste and dialling it is a network call.
    """

    id: str = ""
    url: str = ""
    scheme: str = ""
    enabled: bool = True
    # Last check result.
    status: str = PROXY_UNKNOWN
    exit_ip: str = ""
    country: str = ""
    city: str = ""
    isp: str = ""
    latency_ms: int = 0
    checked_at: float = 0.0
    # A proxy that keeps failing is retired without being deleted, so its record
    # of which addresses it once served survives.
    fail_count: int = 0
    # How many accounts were registered through it.
    used_count: int = 0
    last_used_at: float = 0.0
    created_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "scheme": self.scheme,
            "enabled": self.enabled,
            "status": self.status,
            "exitIp": self.exit_ip,
            "country": self.country,
            "city": self.city,
            "isp": self.isp,
            "latencyMs": self.latency_ms,
            "checkedAt": iso(self.checked_at),
            "failCount": self.fail_count,
            "usedCount": self.used_count,
            "lastUsedAt": iso(self.last_used_at),
            "createdAt": iso(self.created_at),
        }


@dataclass
class IpUsage:
    """How many accounts were registered from one exit address.

    The address is the unit risk control counts in, and it is shared by every
    proxy that egresses through it — which is why this is its own table and not a
    column on ``Proxy``: two entries in the pool can and do resolve to one IP.
    """

    ip: str = ""
    count: int = 0
    last_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {"ip": self.ip, "count": self.count, "lastAt": iso(self.last_at)}


@dataclass
class ModelConfig:
    """A catalogue entry.

    ``upstream`` is a *local* dispatch hint (``agent`` / ``chat`` / ``think`` /
    ``image``) and never leaves the process.  ``upstream_model`` is a value the
    upstream itself recognises; it is empty for the chat entries, which have no
    model selector to fill in.
    """

    id: str = ""
    name: str = ""
    upstream: str = "agent"
    upstream_model: str = ""
    # The upstream variant: "" for the plain model, "thinking" for its reasoning
    # variant.  Sent alongside ``upstream_model`` in the model selection object.
    variant: str = ""
    type: str = MODEL_CHAT
    enabled: bool = True
    builtin: bool = False
    description: str = ""
    requests: int = 0
    tokens: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "upstream": self.upstream,
            "upstreamModel": self.upstream_model,
            "variant": self.variant,
            "type": self.type,
            "enabled": self.enabled,
            "builtin": self.builtin,
            "description": self.description,
            "requests": self.requests,
            "tokens": self.tokens,
        }

    @classmethod
    def from_row(cls, row: Any) -> "ModelConfig":
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            upstream=row["upstream"],
            upstream_model=row["upstream_model"] or "",
            variant=(row["variant"] or "") if "variant" in keys else "",
            type=row["type"],
            enabled=bool(row["enabled"]),
            builtin=bool(row["builtin"]),
            description=row["description"] or "",
            requests=row["requests"],
            tokens=row["tokens"],
        )


def builtin_models() -> list[ModelConfig]:
    """The catalogue exposed through ``/v1/models``.

    Every chat entry reaches the same upstream agent: a conversation cannot be
    routed to a different service by naming a different model.  What an entry
    can do is *name* the model, which is what ``upstream_model`` carries — and
    since the upstream's model selector shape was never captured, an empty value
    means "send no model field at all", which is what every working build did.
    """
    return [
        ModelConfig(
            id="minimax-agent",
            name="MiniMax Agent",
            upstream="agent",
            type=MODEL_CHAT,
            enabled=True,
            builtin=True,
            description="通用 Agent，自动规划并调用工具",
        ),
        ModelConfig(
            id="minimax-m3",
            name="MiniMax M3",
            upstream="chat",
            upstream_model="MiniMax-M3",
            type=MODEL_CHAT,
            enabled=True,
            builtin=True,
            description="对话模式，响应更快",
        ),
        ModelConfig(
            id="minimax-m3-thinking",
            name="MiniMax M3 Thinking",
            upstream="think",
            upstream_model="MiniMax-M3",
            variant="thinking",
            type=MODEL_CHAT,
            enabled=True,
            builtin=True,
            description="深度思考模式，推理内容走 reasoning_content",
        ),
        ModelConfig(
            id="minimax-m3.1-flash",
            name="MiniMax M3.1 Flash Preview",
            upstream="chat",
            upstream_model="MiniMax-M3.1-Flash-Preview",
            type=MODEL_CHAT,
            enabled=True,
            builtin=True,
            description="新一代 Flash 预览版，512K/1M 上下文，响应更快",
        ),
        ModelConfig(
            id="minimax-m3.1-flash-thinking",
            name="MiniMax M3.1 Flash Preview Thinking",
            upstream="think",
            upstream_model="MiniMax-M3.1-Flash-Preview",
            variant="thinking",
            type=MODEL_CHAT,
            enabled=True,
            builtin=True,
            description="M3.1 Flash 预览版的深度思考模式",
        ),
        ModelConfig(
            id="minimax-m2.7",
            name="MiniMax M2.7",
            upstream="chat",
            upstream_model="MiniMax-M2.7",
            type=MODEL_CHAT,
            enabled=True,
            builtin=True,
            description="上一代对话模型",
        ),
        ModelConfig(
            id="minimax-m2.7-highspeed",
            name="MiniMax M2.7 HighSpeed",
            upstream="chat",
            upstream_model="MiniMax-M2.7-highspeed",
            type=MODEL_CHAT,
            enabled=True,
            builtin=True,
            description="上一代对话模型的高速版",
        ),
        ModelConfig(
            id="minimax-image",
            name="MiniMax Image",
            upstream="image",
            type=MODEL_IMAGE,
            enabled=True,
            builtin=True,
            description="图像生成，图片以 Markdown 回在正文里",
        ),
    ]


def merge_builtin_models(models: list[ModelConfig]) -> bool:
    """Add any built-in entry the stored catalogue is missing; report changes.

    Without this a new built-in never reaches an existing install: the catalogue
    is only seeded when it is empty, so every old install keeps the list it froze
    on day one.  Entries already present keep their stored state — the
    operator's enable/disable choices and usage counters live there — with one
    exception: an ``upstream_model`` a built-in entry is missing is adopted,
    because there is no console or API that sets it and an empty value can
    therefore never be intent.
    """
    by_id = {model.id: model for model in builtin_models()}
    known = {model.id for model in models if model.id}
    changed = False
    for model in models:
        if not model.builtin:
            continue
        builtin = by_id.get(model.id)
        if not builtin:
            continue
        if builtin.upstream_model and not model.upstream_model:
            model.upstream_model = builtin.upstream_model
            changed = True
        # The variant follows the same rule: no console sets it, so an empty one
        # is "not yet known", not an operator's choice of the plain model.
        if builtin.variant and not model.variant:
            model.variant = builtin.variant
            changed = True
    for model in builtin_models():
        if model.id in known:
            continue
        models.append(model)
        known.add(model.id)
        changed = True
    return changed


def mask_token(token: str) -> str:
    """Show enough of a JWT to recognise it and not enough to use it."""
    if not token:
        return ""
    if len(token) <= 12:
        return token[:4] + "***"
    return token[:10] + "…" + token[-6:]


def mask_password(password: str) -> str:
    """Report that a password exists without revealing its length or content.

    Unlike a token there is nothing here worth recognising — a password has no
    structure and no issuer — so the mask is a fixed run of dots rather than a
    prefix/suffix view.
    """
    return "•" * 8 if password else ""


def _as_ts(value: Any) -> float:
    """Read back a timestamp written by ``iso``.

    ``to_json`` renders times as ISO strings, so a round trip has to parse that
    form: feeding the string to a number parser yields zero, and a zero
    ``syncedAt`` is what makes freshness checks think a reading is decades old.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return 0.0
    return 0.0


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0
