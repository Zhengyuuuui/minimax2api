"""The MiniMax Agent web API, as the browser speaks it.

Two things make this more than an HTTP wrapper.

**The signatures.**  ``x-signature`` covers a timestamp, a static salt and the
body; ``yy`` covers the *encoded URL*, which is where the browser fingerprint
lives.  That has two consequences the code is arranged around:

- the parameter *order* is part of the contract.  Both query strings are joined by
  hand rather than through anything that sorts keys, because ``yy`` is an MD5
  over the encoded URL and reordering invalidates the digest.
- ``unix`` and the digest must come from **one clock reading**.  The URL carries
  ``unix`` and ``yy`` signs it, so building the URL in one place and signing it in
  another needs a second reading — and a few milliseconds of drift makes the
  digest describe a URL that is never sent.  ``agent_target`` / ``signin_target``
  therefore produce both halves together, and nothing outside them assembles a
  URL that is about to be signed.

**Two hosts.**  The conversation endpoint answers on
``agent-stream.<domain>`` while every other call stays on ``agent.<domain>``.
They are different entry points rather than two addresses for one: the same path
on the API host reaches the agent too, but hands it no rendering tool.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import httpx

from . import signing
from .config import DESKTOP_USER_AGENT, Settings, resolve_path
from .security import random_uuid
from .records import REGION_CN

# The value the bundle puts in `op_ticket`.  It stringifies to the literal text
# "undefined" inside URLSearchParams, so the *signed* URL carries it while the
# request that goes on the wire does not — axios drops undefined values.  Using
# one string for both purposes earns a signature the server rejects.
OP_TICKET_LITERAL = "undefined"

DEFAULT_SESSION_PATH = "/minimax-cloud/api/v1/agent/{agent_id}/session"
DEFAULT_MESSAGE_PATH = "/minimax-cloud/api/v1/session/{session_id}/message"
DEFAULT_AGENT_LIST_PATH = "/minimax-cloud/api/v1/agent"
DEFAULT_CONFIG_PATH = "/minimax-cloud/api/v1/config"
DEFAULT_CONNECTIONS_PATH = "/minimax-cloud/api/v1/channel/connections"
DEFAULT_USER_INFO_PATH = "/v1/api/user/info"
DEFAULT_SIGNIN_STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
DEFAULT_SIGNIN_CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"
DEFAULT_CREDIT_PATH = "/matrix/api/v1/commerce/get_membership_info"
DEFAULT_CREDIT_DETAILS_PATH = "/minimax-cloud/api/v1/credit/details"

# The upstream's code for a rejected session, mapped onto InvalidCredential so
# the caller retires the account instead of merely cooling it down.
STATUS_SESSION_EXPIRED = 1022100011

# The agent the gateway drives.  `mavis` rather than `general`, on two pieces of
# evidence that agree: the captured web client opens its session against a mavis
# agent, and the skill carrying the multimodal tooling publishes under `Mavis/`.
# The two client identities the upstream accepts.  They differ in the query
# parameters, the fingerprint values and the user agent; the signature covers all
# of it, so the choice has to be made before the URL is built.
PROFILE_WEB = "web"
PROFILE_DESKTOP = "desktop"

DEFAULT_AGENT_ROLE = "mavis"
FALLBACK_AGENT_ROLE = "general"
KNOWN_AGENT_ROLES = frozenset({"general", "coder", "chat", "mavis", "verifier"})

CONTENT_KEYS = ("msg_content", "msgContent", "content", "text", "delta", "answer")
THINKING_KEYS = (
    "reasoning_content",
    "thinking_content",
    "reason_content",
    "think_content",
    "reasoning",
    "thinking",
)
ERROR_MESSAGE_KEYS = ("error_msg", "error_message", "err_msg", "status_msg")
ERROR_CODE_KEYS = ("status_code", "code", "ret_code")

MAX_JSON_BODY = 1 << 20


class UpstreamError(RuntimeError):
    """Any failure that is not a dead credential."""


class InvalidCredential(UpstreamError):
    """The upstream rejected the credential (401/403 or a rejected session)."""


class AgentIDUnknown(UpstreamError):
    """The account has no agent id prepared yet.

    Separate from ``InvalidCredential`` on purpose: one is the token's fault and
    the other is a missing piece of preparation — and the pool retires an account
    that reports the first.
    """


@dataclass
class Credential:
    """Everything needed to replay one session.

    Two credential shapes reach this class, and they authenticate differently:

    - A **web** credential: the JWT lifted from the browser's localStorage.  It is
      sent as the `token` query parameter, and ``realUserID`` has to be looked up
      separately because the JWT does not carry it.
    - A **desktop** credential: the opaque OAuth ``access_token`` the device flow
      hands back.  It goes in an ``Authorization: Bearer`` header, and the identity
      comes with it.

    The distinction is not cosmetic: sending one as the other earns a 401 that
    looks like an expired credential.
    """

    region: str = "global"
    # Which client this credential claims to be.  A web JWT was issued to a
    # browser and is presented the way a browser presents it; a device-flow token
    # was issued to the desktop client and must not be presented as a browser,
    # because the fingerprint around a credential is part of what it is.
    profile: str = PROFILE_WEB
    token: str = ""
    # realUserID.  Not in the JWT, not derivable from it, wanted by every signed
    # endpoint as `user_id`.  Present in a desktop credential's token response.
    user_id: str = ""
    # The OAuth access token, when the credential came from the sign-in flow
    # rather than from localStorage.
    bearer: str = ""
    agent_id: str = ""
    device_id: str = ""
    uuid: str = ""
    screen_width: int = 0
    screen_height: int = 0
    base_url: str = ""

    def authenticates(self) -> bool:
        """Whether this credential can prove an account at all."""
        return bool(self.token.strip() or self.bearer.strip())

    def label(self) -> str:
        if self.user_id:
            return self.user_id
        shown = self.token or self.bearer
        if len(shown) > 12:
            return shown[:12] + "…"
        return shown


@dataclass
class MediaRef:
    kind: str = "image"
    url: str = ""


@dataclass
class UploadedImage:
    """A remote image reference forwarded to the agent.

    The attachment pipeline needs a signed upload we cannot reproduce from the
    outside, so images are passed through by URL and resolved upstream.
    """

    url: str = ""
    name: str = ""


@dataclass
class Options:
    text: str = ""
    mode: str = ""
    client_intent: str = ""
    # The model the turn asks for, as ``{"model_id", "provider_id", "variant"}``.
    # Empty means "let the upstream pick its default", which is what every build
    # before the selector shape was known did.  See ``ModelConfig.upstream_model``.
    model: dict[str, Any] = field(default_factory=dict)
    images: list[UploadedImage] = field(default_factory=list)
    timeout: float = 0.0
    idle_timeout: float = 0.0
    on_delta: Callable[[str], None] | None = None
    on_thinking: Callable[[str], None] | None = None
    # Sees every decoded frame, before anything decides what it means.
    on_frame: Callable[[dict[str, Any]], None] | None = None


@dataclass
class Result:
    text: str = ""
    thinking: str = ""
    media: list[MediaRef] = field(default_factory=list)
    session_id: str = ""
    message_id: str = ""
    turn_id: str = ""
    stop_reason: str = ""


@dataclass
class Agent:
    """One entry from the account's agent list.

    The numeric id lives in a field called ``name`` — the upstream keeps agent
    *roles* and agent *ids* far apart.
    """

    id: str = ""
    role: str = ""
    root_session_id: str = ""


@dataclass
class PrepareResult:
    agents: list[Agent] = field(default_factory=list)

    @property
    def agent_id(self) -> str:
        """Pick the agent to drive: preferred role, fallback role, then any."""
        for role in (DEFAULT_AGENT_ROLE, FALLBACK_AGENT_ROLE):
            for agent in self.agents:
                if agent.role == role and agent.id:
                    return agent.id
        for agent in self.agents:
            if agent.id:
                return agent.id
        return ""

    def resolve_agent_id(self, stored: str) -> tuple[str, bool]:
        """Return the id to store, and whether it differs from the stored one.

        The stored id is a cache of an earlier discovery, not an instruction: the
        role the gateway drives can change between releases, so it is revisited
        rather than only filled in when empty.  A hand-pinned id — one that does
        not appear in the account's agent list — is left alone, because it cannot
        have come from a discovery.
        """
        preferred = self.agent_id
        if not preferred or preferred == stored:
            return stored, False
        if not stored:
            return preferred, True
        if not any(agent.id == stored for agent in self.agents):
            return stored, False
        return preferred, True


@dataclass
class SigninDay:
    day_no: int = 0
    points: int = 0
    status: int = 0
    is_today: bool = False


@dataclass
class SigninPanel:
    scene: int = 0
    days: list[SigninDay] = field(default_factory=list)

    @property
    def today(self) -> SigninDay | None:
        for day in self.days:
            if day.is_today:
                return day
        return None

    @property
    def claimed_today(self) -> bool:
        # The day's ``status`` is 1 for a day that cannot be claimed yet (a future
        # day), 2 for today before it is claimed, and 3 once it has been claimed.
        # So "already claimed" is status 3, not 1 — reading it as 1 says the
        # opposite of the truth and sends a claim for a day already claimed, which
        # is why a signed-in account kept asking the endpoint and the console kept
        # showing the pre-claim balance.
        day = self.today
        return bool(day and day.status == 3)


@dataclass
class SigninClaim:
    claim_id: int = 0
    result: int = 0
    day_no: int = 0
    points: int = 0
    panel: SigninPanel | None = None

    def is_duplicate(self) -> bool:
        """The endpoint is idempotent: a second claim answers result=2."""
        return self.result == 2


@dataclass
class CreditInfo:
    total: float = 0.0
    free: float = 0.0
    purchased: float = 0.0
    plan_name: str = ""
    plan_type: int = 0


@dataclass
class CreditGrant:
    granted_at: int = 0
    expires_at: int = 0
    granted: float = 0.0
    remaining: float = 0.0


@dataclass
class RawResponse:
    """One raw upstream answer: status, body, and the URL it was signed for."""

    status: int = 0
    body: str = ""
    url: str = ""


@dataclass
class Target:
    """A signed request, with both halves of the digest kept together.

    ``url`` is what goes on the wire; ``sign_url`` is what ``yy`` digests.  They
    are the same string on the agent endpoints and differ on the check-in ones,
    where axios keeps baseURL separate from url and the signature therefore sees
    the relative path alone.

    ``checkin`` records which family of headers the request wants, because guessing
    it back from the two URLs is how a half-migrated request ends up signed for one
    family and sent with the other's headers.
    """

    method: str
    url: str
    sign_url: str
    unix_ms: int
    body: str
    checkin: bool = False


# --------------------------------------------------------------------- helpers


def is_loopback(host: str) -> bool:
    """Whether a host names the local machine.

    A proxy is configured to reach the internet, and 127.0.0.1 is by definition
    not there; sending a loopback request through it hands it to a proxy that
    cannot reach it and gets back an empty-bodied 502 that reads as an upstream
    outage.  Private ranges are *not* bypassed — those are often precisely the
    hosts a proxy exists to reach.
    """
    name = (host or "").strip().lower().strip("[]")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    if name in ("::1", "0:0:0:0:0:0:0:1"):
        return True
    parts = name.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return parts[0] == "127"
    return False


def is_agent_role(value: str) -> bool:
    return (value or "").strip().lower() in KNOWN_AGENT_ROLES


def redact(text: str, cred: Credential) -> str:
    """Blank every credential value found in ``text``.

    http libraries put the request URL into transport errors, and these URLs
    carry the token in their query — so an error passed along verbatim writes a
    live JWT into the next log line, audit row or API response.  That is not
    hypothetical: it is how a failed balance check once printed one.
    """
    for value in (cred.token, cred.uuid, cred.device_id, cred.user_id):
        if value and len(value) >= 6:
            text = text.replace(value, "<redacted>")
    # Anything still holding a query string is dropped rather than kept: the
    # useful part of a transport failure is the host and path.
    return re.sub(r"(https?://[^\s?]+)\?[^\s]*", r"\1?<query>", text)


def _safe_transport_error(err: BaseException, cred: Credential) -> UpstreamError:
    return UpstreamError(redact(f"{type(err).__name__}: {err}", cred))


def _int_of(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def _float_of(value: Any) -> float:
    """Read a numeric field without dropping a fractional balance.

    Balances arrive as decimal strings (``"1492.125"``); truncating one to an int
    is the kind of quiet rounding that makes the console disagree with the site.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _str_of(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _core_of(payload: dict[str, Any]) -> dict[str, Any]:
    core = payload.get("data")
    return core if isinstance(core, dict) else payload


def _deep_string(payload: Any, key: str) -> str:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        for item in payload.values():
            found = _deep_string(item, key)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _deep_string(item, key)
            if found:
                return found
    return ""


def _first_deep_string(payload: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        found = _deep_string(payload, key)
        if found:
            return found
    return ""


def _strip_prompt_echo(text: str, prompt: str) -> str:
    """Drop the request echoed back inside a content frame.

    Some MiniMax agents replay the whole turn they were given before they
    answer: one frame carries the ``content`` the bridge just sent, so the
    flattened system prompt and history come back as if the model had written
    them.  The reply then contains the entire request, which is both wrong and
    enormous.

    The check is deliberately narrow, because a false positive would delete a
    real answer: only a frame that actually carries the sent prompt is touched.
    A frame that merely mentions it, or a prompt too short to be an echo (a bare
    ``hi`` is also a plausible reply), is left alone.
    """
    if not prompt or not text:
        return text
    # A short prompt has no distinctive tail, so an echo cannot be told apart
    # from a genuine answer and is not stripped.
    marker = prompt.strip()
    if len(marker) < 32:
        return text
    if marker not in text:
        return text
    # Everything up to and including the echo is the request, not the answer.
    # ``head`` is any text before the echo and ``tail`` any text after it; both
    # sides can be legitimately non-empty, so both are kept and only the echoed
    # middle is dropped.
    head, _, tail = text.partition(marker)
    return head + tail


def _collapse_repeated_tail(text: str) -> str:
    """Collapse an answer the upstream repeated back-to-back.

    A streamed reply occasionally contains the same sentence twice ("Hi! ... Hi!
    ...").  Only an exact, whole-string doubling of a reasonably long answer is
    collapsed, so ordinary repetition inside a real answer survives.
    """
    body = text.strip()
    if len(body) < 16:
        return text
    half = len(body) // 2
    if len(body) % 2 == 0 and body[:half] == body[half:]:
        return body[:half]
    return text


def _collect_media(payload: Any, media: list[MediaRef]) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, str):
                lowered = key.lower()
                if value.startswith("http") and _looks_like_media_key(lowered):
                    media.append(MediaRef(kind=_media_kind(lowered), url=value))
            else:
                _collect_media(value, media)
    elif isinstance(payload, list):
        for item in payload:
            _collect_media(item, media)


def _looks_like_media_key(key: str) -> bool:
    """Deliberately narrow.

    A bare ``url`` field appears on avatars and citation links as often as on
    generated media, so only explicit media-suffixed keys are trusted.
    """
    if "icon" in key or "avatar" in key:
        return False
    return any(token in key for token in ("image_url", "video_url", "cover_url", "file_url"))


def _media_kind(key: str) -> str:
    return "video" if "video" in key else "image"


def _dedupe_media(items: list[MediaRef]) -> list[MediaRef]:
    seen: set[str] = set()
    out: list[MediaRef] = []
    for item in items:
        key = item.url.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _snippet(body: str) -> str:
    text = (body or "").strip()
    return text[:300] + "…" if len(text) > 300 else text


def _parse_frame(block: str) -> tuple[str, dict[str, Any] | None, str]:
    event = "message"
    data_lines: list[str] = []
    for line in block.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    raw = "\n".join(data_lines)
    if not raw or raw == "[DONE]":
        return event, None, raw
    try:
        parsed = json.loads(raw)
    except ValueError:
        return event, None, raw
    return event, parsed if isinstance(parsed, dict) else None, raw


def _upstream_error(payload: dict[str, Any]) -> str:
    node = payload.get("error")
    if isinstance(node, dict):
        message = _str_of(node.get("message"))
        if message:
            return message
    elif isinstance(node, str) and node:
        return node
    for key in ERROR_MESSAGE_KEYS:
        message = _str_of(payload.get(key))
        # `success` on the happy path is not an error, and `ok` likewise.
        if message and message.lower() not in ("success", "ok"):
            return message
    for key in ERROR_CODE_KEYS:
        code = payload.get(key)
        if isinstance(code, (int, float)) and not isinstance(code, bool) and code != 0:
            return f"upstream status {int(code)}"
    return ""


def _envelope_error(payload: dict[str, Any], label: str) -> UpstreamError | None:
    """Turn the base_resp/statusInfo envelope into an error.

    A rejected session maps onto ``InvalidCredential``: it means the account is
    unusable, so the pool should retire it rather than cool it down for a while
    and try the same dead token again.
    """
    for key in ("base_resp", "statusInfo"):
        node = payload.get(key)
        if not isinstance(node, dict):
            continue
        code = _int_of(node.get("status_code")) or _int_of(node.get("code"))
        if code == 0:
            continue
        if code == STATUS_SESSION_EXPIRED:
            return InvalidCredential(f"minimax {label} 会话已失效 ({code})")
        message = _str_of(node.get("status_msg")) or _str_of(node.get("message"))
        return UpstreamError(f"minimax {label} 返回 {code} {message}".strip())
    return None


def _session_id_from(body: str) -> str:
    """Dig a session id out of a create-session response.

    Three shapes have been observed — ``{"data":{"session_id":…}}``, the field at
    the top level, and a bare JSON string — so all three are accepted rather than
    failing on a cosmetic upstream change.  A response that is not JSON at all is
    refused rather than guessed at: a wrong path is answered with the SPA's HTML
    shell and a 200, and using that as the id builds a URL out of a web page.
    """
    trimmed = (body or "").strip()
    if not trimmed:
        return ""
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        return ""
    if isinstance(parsed, str):
        return parsed.strip()
    for key in ("session_id", "sessionId", "id", "sessionID"):
        found = _deep_string(parsed, key)
        if found:
            return found
    return ""


_QUERY_KEYS = ("token", "uuid", "device_id", "user_id")


def _redact_url(url: str) -> str:
    """Blank the credential-bearing query values, keeping the parameter order.

    The shape is the useful part — ``yy`` covers this string, so a wrong order and
    a wrong digest produce the same rejection — while the values themselves have
    no business in a log.
    """
    path, sep, query = url.partition("?")
    if not sep:
        return url
    pairs = []
    for pair in query.split("&"):
        key, _, _ = pair.partition("=")
        pairs.append(f"{key}=<redacted>" if key in _QUERY_KEYS else pair)
    return f"{path}?{'&'.join(pairs)}"


def _parse_panel(core: dict[str, Any]) -> SigninPanel:
    panel = SigninPanel(scene=_int_of(core.get("scene")))
    days = core.get("days")
    if isinstance(days, list):
        for item in days:
            if not isinstance(item, dict):
                continue
            panel.days.append(
                SigninDay(
                    day_no=_int_of(item.get("day_no")),
                    points=_int_of(item.get("points")),
                    status=_int_of(item.get("status")),
                    is_today=bool(item.get("is_today")),
                )
            )
    return panel


def _join_query(pairs: list[tuple[str, str]], keep_empty: tuple[str, ...] = ()) -> str:
    """Encode the pairs.

    An empty value is normally skipped rather than sent as `key=`, which is what
    the web client does for a fingerprint half it does not have.  ``keep_empty``
    names the exceptions: the desktop client sends ``desktop_version=`` with the
    value blank, and since ``yy`` digests the URL, an absent parameter and an
    empty one are different requests.
    """
    return "&".join(
        f"{key}={signing.encode_uri_component(value)}"
        for key, value in pairs
        if value or key in keep_empty
    )


def credential_of(account: Any) -> Credential:
    """Project a stored account onto a credential.

    The stored token means one of two things depending on ``kind``: a web JWT or a
    desktop access token.  The projection is where that is decided, so no caller
    has to know which header a credential will end up in.
    """
    from .records import KIND_OAUTH

    is_desktop = account.kind == KIND_OAUTH
    return Credential(
        region=account.region,
        profile=PROFILE_DESKTOP if is_desktop else PROFILE_WEB,
        token="" if is_desktop else account.token,
        bearer=account.token if is_desktop else "",
        user_id=account.user_id,
        agent_id=account.agent_id,
        device_id=account.device_id,
        uuid=account.uuid,
        screen_width=account.screen_width,
        screen_height=account.screen_height,
        base_url=account.base_url,
    )


# ---------------------------------------------------------------------- client


class MiniMaxClient:
    """Talks to the MiniMax Agent web API on behalf of one account at a time."""

    def __init__(self, settings_fn: Callable[[], Settings]) -> None:
        self._settings_fn = settings_fn
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def aclose(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.aclose()

    # ---------------------------------------------------------------- addressing

    def base_url(self, settings: Settings, cred: Credential) -> str:
        if cred.base_url:
            return cred.base_url.rstrip("/")
        if cred.region == REGION_CN and settings.upstream.base_url_cn:
            return settings.upstream.base_url_cn.rstrip("/")
        return settings.upstream.base_url.rstrip("/")

    @staticmethod
    def agent_id(settings: Settings, cred: Credential) -> str:
        """The agent to drive: the account's own id, else the global setting.

        A role name is never an id, and is skipped rather than used: the upstream
        answers one with a 200 that opens no session, which makes every request
        look successful and produce nothing.
        """
        for candidate in (cred.agent_id, settings.upstream.agent_id):
            cleaned = (candidate or "").strip()
            if cleaned and not is_agent_role(cleaned):
                return cleaned
        return ""

    @staticmethod
    def stream_base_url(settings: Settings, fallback: str) -> str:
        """The host the conversation endpoint answers on.

        An explicit setting wins; otherwise the ``agent.x`` -> ``agent-stream.x``
        shape is derived, because the alternative to deriving it is hardcoding a
        hostname that is then wrong for every other deployment.  A self-hosted
        relay keeps its own address rather than having a name invented for it.
        """
        explicit = (settings.upstream.stream_base_url or "").strip()
        if explicit:
            return explicit.rstrip("/")
        parsed = urlsplit(fallback)
        host = parsed.hostname or ""
        if not host.startswith("agent."):
            return fallback
        target = "agent-stream." + host[len("agent."):]
        if parsed.port:
            target = f"{target}:{parsed.port}"
        return f"{parsed.scheme}://{target}"

    # ------------------------------------------------------------- signature sets

    @staticmethod
    def _agent_query(settings: Settings, cred: Credential, unix_ms: int) -> str:
        """The metadata query string, in the order the claiming client sends it.

        Order is part of the contract: ``yy`` is an MD5 over the encoded URL, so
        reordering changes the digest and the upstream rejects the rewrite
        without naming a field.

        Two shapes exist, and which one is sent follows from the credential rather
        than from a setting, because the fingerprint is part of what a credential
        *is*: a JWT was issued to a browser, a device-flow token to the desktop
        client, and presenting one with the other's parameters is what a request
        looks like when it is synthetic.  The web shape was reconstructed from the
        site's bundle; the desktop shape is copied field for field from the
        client's own logged requests, ``is_desktop`` and the missing ``region``
        included.
        """
        up = settings.upstream
        if cred.profile == PROFILE_DESKTOP:
            width = cred.screen_width or up.desktop_screen_width
            height = cred.screen_height or up.desktop_screen_height
            pairs: list[tuple[str, str]] = [
                ("device_platform", "web"),
                ("biz_id", "3"),
                ("app_id", "3001"),
                ("version_code", "22201"),
                ("unix", str(unix_ms)),
                ("timezone_offset", "28800"),
                ("is_desktop", "1"),
                ("desktop_version", up.desktop_version),
                ("sys_language", "en"),
                ("lang", "en"),
                ("uuid", cred.uuid),
                ("device_id", cred.device_id),
                ("os_name", up.desktop_os_name),
                ("browser_name", "Chrome"),
                ("device_memory", str(up.desktop_device_memory)),
                ("cpu_core_num", str(up.desktop_cpu_core_num)),
                ("browser_language", "zh-CN"),
                ("browser_platform", up.desktop_browser_platform),
                ("user_id", cred.user_id),
                ("screen_width", str(width)),
                ("screen_height", str(height)),
                # No `token` parameter at all: the desktop client only sets it
                # when it has a localStorage JWT, and a Bearer credential does
                # not.  Its request builder deletes the key in that case rather
                # than sending it blank.
                ("client", "desktop"),
                # `region=en` is added unconditionally by the desktop client's
                # request builder, so it is part of the desktop shape too — the
                # remote-control endpoints that omit it are a different client.
                ("region", "en"),
            ]
            # `desktop_version` stays even when blank; see _join_query.
            return _join_query(pairs, keep_empty=("desktop_version",))

        width = cred.screen_width or up.screen_width
        height = cred.screen_height or up.screen_height
        pairs = [
            ("device_platform", "web"),
            ("biz_id", "3"),
            ("app_id", "3001"),
            ("version_code", "22201"),
            ("unix", str(unix_ms)),
            ("timezone_offset", "28800"),
            ("sys_language", "en"),
            ("lang", "en"),
            ("uuid", cred.uuid),
            ("device_id", cred.device_id),
            ("os_name", "Windows"),
            ("browser_name", "Chrome"),
            ("device_memory", "16"),
            ("cpu_core_num", "8"),
            ("browser_language", "zh-CN"),
            ("browser_platform", "Win32"),
            ("user_id", cred.user_id),
            ("screen_width", str(width)),
            ("screen_height", str(height)),
            ("token", cred.token),
            ("client", "web"),
            ("region", "en"),
        ]
        return _join_query(pairs)

    @staticmethod
    def _signin_query(
        settings: Settings, cred: Credential, unix_ms: int, *, for_signature: bool
    ) -> str:
        """The check-in query string, in the order the web bundle builds it.

        Values are form-encoded the way URLSearchParams does — the same character
        set as ``encodeURIComponent`` except that a space becomes ``+``.
        """
        signin = settings.signin
        lang = (signin.lang or "").strip() or "en"
        os_name = (signin.os_name or "").strip() or "Windows"
        browser_name = (signin.browser_name or "").strip() or "Chrome"
        browser_language = (signin.browser_language or "").strip() or "en-US"
        browser_platform = (signin.browser_platform or "").strip() or "Win32"
        width = cred.screen_width or settings.upstream.screen_width
        height = cred.screen_height or settings.upstream.screen_height
        # The bundle computes -60 * getTimezoneOffset(), and UTC+8 reports -480,
        # so the value that goes on the wire is +28800 rather than -480.
        offset_minutes = signin.timezone_offset_min or 480
        device_memory = max(1, signin.device_memory or 16)
        cpu_cores = max(1, signin.cpu_core_num or 8)
        user_id = (cred.user_id or "").strip() or "0"

        pairs: list[tuple[str, str]] = [
            ("device_platform", "web"),
            ("biz_id", "3"),
            ("app_id", "3001"),
            ("version_code", "22201"),
            ("unix", str(unix_ms)),
            ("timezone_offset", str(offset_minutes * 60)),
            ("sys_language", lang),
            ("lang", lang),
            ("uuid", cred.uuid),
            ("device_id", cred.device_id),
            ("os_name", os_name),
            ("browser_name", browser_name),
            ("device_memory", str(device_memory)),
            ("cpu_core_num", str(cpu_cores)),
            ("browser_language", browser_language),
            ("browser_platform", browser_platform),
            ("user_id", user_id),
            ("op_ticket", OP_TICKET_LITERAL),
            ("screen_width", str(width)),
            ("screen_height", str(height)),
            ("token", "" if cred.bearer else cred.token),
            ("client", "web"),
        ]
        out = []
        for key, value in pairs:
            if key == "op_ticket" and not for_signature:
                # axios drops undefined values when it serialises the request.
                continue
            if not value and key != "op_ticket":
                continue
            out.append(f"{key}={signing.form_encode(value)}")
        return "&".join(out)

    def agent_target(
        self,
        cred: Credential,
        path: str,
        *,
        method: str = "GET",
        body: str = "",
        session_id: str = "",
        stream_host: bool = False,
    ) -> Target:
        """Assemble and sign a call to the agent family of endpoints.

        The client signs a *relative* path for an ordinary call and an absolute URL
        for a streaming one — its request builder only upgrades the URL when the
        call is a stream, because that is the case that must move to the streaming
        host.  ``yy`` digests whatever it is given, so signing the wrong one is a
        digest over a string the server never computes, and the rejection names no
        field.
        """
        settings = self._settings_fn()
        agent_id = self.agent_id(settings, cred)
        resolved = path.replace("{agent_id}", agent_id).replace("{session_id}", session_id)
        base = self.base_url(settings, cred)
        if stream_host:
            base = self.stream_base_url(settings, base)
        unix_ms = int(time.time() * 1000)
        # One reading feeds both the query and the digest; see the module docstring.
        query = self._agent_query(settings, cred, unix_ms)
        wire = f"{base}{resolved}"
        wire = f"{wire}&{query}" if "?" in wire else f"{wire}?{query}"
        if stream_host:
            sign_url = wire
        else:
            signed = f"{resolved}&{query}" if "?" in resolved else f"{resolved}?{query}"
            sign_url = signed
        return Target(method=method, url=wire, sign_url=sign_url, unix_ms=unix_ms, body=body)

    def signin_target(self, cred: Credential, path: str, *, method: str = "GET", body: str = "") -> Target:
        """Assemble a check-in call.  Signs the relative path, not the URL."""
        settings = self._settings_fn()
        unix_ms = int(time.time() * 1000)
        # The two strings differ by `op_ticket`: one is signed, the other is sent.
        signed = f"{path}?{self._signin_query(settings, cred, unix_ms, for_signature=True)}"
        wire = f"{path}?{self._signin_query(settings, cred, unix_ms, for_signature=False)}"
        url = f"{self.base_url(settings, cred)}{wire}"
        return Target(
            method=method, url=url, sign_url=signed, unix_ms=unix_ms, body=body, checkin=True
        )

    # ------------------------------------------------------------------- request

    def public_client(self, host: str) -> httpx.AsyncClient:
        """The bridge's own HTTP client for one host, proxy rules included.

        Sign-in needs to reach ``account.minimax.io`` — a different host from the
        agent API, fenced to exactly the same egress — so it borrows this
        client rather than opening its own: a second place that reads the proxy
        setting is a second place for it to be ignored.
        """
        return self._client_for(self._settings_fn(), host)

    def _client_for(self, settings: Settings, host: str) -> httpx.AsyncClient:
        proxy = (settings.upstream.proxy or "").strip()
        # Loopback never goes through the proxy: see is_loopback.  The two
        # clients are cached separately so connection pooling is preserved in
        # both cases.
        if proxy and not is_loopback(host):
            key = f"proxy:{proxy}"
        else:
            key = "direct"
        client = self._clients.get(key)
        if client is None or client.is_closed:
            kwargs: dict[str, Any] = {"timeout": self._timeout(settings), "trust_env": False}
            if key != "direct":
                kwargs["proxy"] = proxy
            client = httpx.AsyncClient(**kwargs)
            self._clients[key] = client
        return client

    @staticmethod
    def _timeout(settings: Settings) -> httpx.Timeout:
        # The connect side is short and the read side generous: a stream turn can
        # legitimately take minutes to produce its first token, while a proxy or
        # DNS failure should surface immediately.
        return httpx.Timeout(
            connect=15.0,
            read=float(settings.upstream.request_timeout_sec),
            write=30.0,
            pool=30.0,
        )

    def _headers(self, settings: Settings, cred: Credential, target: Target) -> dict[str, str]:
        base = self.base_url(settings, cred)
        user_agent = settings.upstream.user_agent
        if cred.profile == PROFILE_DESKTOP:
            # The desktop client's UA carries its own product token and version;
            # a bare browser UA alongside is_desktop=1 is a pair that never occurs.
            user_agent = DESKTOP_USER_AGENT.format(version=settings.upstream.desktop_ua_version)
        # The check-in call signs `{}` in place of its own body: axios hands the
        # check-in module a JSON literal that never reaches the digest.
        yy_body = "{}" if target.checkin else target.body
        if target.checkin:
            accept = "*/*"
        elif cred.profile == PROFILE_DESKTOP:
            # The desktop client's own Accept.  It sends no accept-language at
            # all, so neither do we: an extra header is exactly the kind of
            # difference this profile exists to avoid.
            accept = "application/json, text/plain, */*"
        else:
            accept = "text/event-stream, application/json, */*"
        headers = {
            "accept": accept,
            "content-type": "application/json",
            "origin": base,
            "referer": base + "/",
            "user-agent": user_agent,
            "token": cred.token,
            "x-timestamp": str(target.unix_ms // 1000),
            "x-signature": signing.x_signature(target.unix_ms // 1000, target.body),
            "yy": signing.yy(target.sign_url, yy_body, target.unix_ms),
        }
        if cred.profile != PROFILE_DESKTOP:
            headers["accept-language"] = settings.upstream.language
        if cred.bearer:
            # The desktop credential's whole point: the signature proves the
            # request shape, the Bearer proves the account.
            headers["Authorization"] = f"Bearer {cred.bearer}"
            headers.pop("token", None)
        return headers

    async def _send(
        self, cred: Credential, target: Target, *, stream: bool, timeout: float | None = None
    ) -> httpx.Response:
        settings = self._settings_fn()
        host = urlsplit(target.url).hostname or ""
        client = self._client_for(settings, host)
        request = client.build_request(
            target.method,
            target.url,
            headers=self._headers(settings, cred, target),
            content=target.body.encode("utf-8") if target.body else None,
        )
        if timeout is not None:
            request.extensions["timeout"] = {
                "connect": 15.0,
                "read": timeout,
                "write": 30.0,
                "pool": 30.0,
            }
        return await client.send(request, stream=stream)

    async def _read_json(self, cred: Credential, target: Target, label: str) -> dict[str, Any]:
        """Issue a signed JSON call and decode the envelope."""
        try:
            response = await self._send(cred, target, stream=False, timeout=60.0)
        except httpx.HTTPError as err:
            raise _safe_transport_error(err, cred) from err
        try:
            # A 401 is checked before the body is read: the upstream answers one
            # with an empty body, and that emptiness is its only signal.
            if response.status_code in (401, 403):
                raise InvalidCredential(f"minimax {label} HTTP {response.status_code}")
            raw = (await response.aread()).decode("utf-8", "replace")
            if response.status_code >= 400:
                raise UpstreamError(f"minimax {label} HTTP {response.status_code}: {_snippet(raw)}")
            if not raw.strip().startswith("{"):
                # A 200 that is not JSON means the request never reached the API.
                # The SPA's HTML shell is the usual culprit.
                raise UpstreamError(f"minimax {label} 返回非 JSON: {_snippet(raw)}")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise UpstreamError(f"minimax {label} 返回非对象")
        finally:
            await response.aclose()
        error = _envelope_error(payload, label)
        if error:
            raise error
        return payload

    # -------------------------------------------------------------- preparation

    async def fetch_user_info(self, cred: Credential) -> dict[str, str]:
        """Read the account's identity.  This is the only way to learn realUserID.

        It is not in the JWT — the token carries a *different* id under
        ``user.id``, and feeding that back as ``user_id`` is rejected exactly like
        omitting it — and the site keeps the real value in localStorage, which a
        server-side client cannot read.

        The endpoint is reachable *without* ``user_id``, which is what makes it
        usable as the bootstrap: the query renders the missing value as "0",
        which is accepted here and rejected everywhere else.
        """
        path = resolve_path(self._settings_fn().upstream.user_info_path, DEFAULT_USER_INFO_PATH)
        payload = await self._read_json(
            cred, self.signin_target(cred, path, method="GET"), "user info"
        )
        core = _core_of(payload)
        inner = core.get("userInfo")
        if isinstance(inner, dict):
            core = inner
        return {
            "real_user_id": _str_of(core.get("realUserID")),
            "name": _str_of(core.get("name")),
            "user_id": _str_of(core.get("userID")),
            "phone": _str_of(core.get("phone")),
            "email": _str_of(core.get("email")),
        }

    async def fetch_agents(self, cred: Credential) -> list[Agent]:
        """List the account's agents.  This is what turns a role into an id."""
        path = resolve_path(
            self._settings_fn().upstream.agent_list_path, DEFAULT_AGENT_LIST_PATH
        )
        payload = await self._read_json(
            cred, self.agent_target(cred, path, method="GET"), "agent list"
        )
        raw = _core_of(payload).get("agents")
        if not isinstance(raw, list):
            return []
        agents: list[Agent] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            agents.append(
                Agent(
                    id=_str_of(item.get("name")),
                    role=_str_of(item.get("agent_role")),
                    root_session_id=_str_of(item.get("root_session_id")),
                )
            )
        return agents

    async def prepare(self, cred: Credential) -> PrepareResult:
        """Run the agent-side opening sequence the web client runs.

        Not optional, and not optional *here*.  ``/config`` is what creates the
        account's record on the agent side; a check-in claimed before it exists
        is registered and never paid out, and running the sequence afterwards
        does not recover the day.  The same call is also what makes a freshly
        registered account able to answer a message at all.

        The three reads are independent, so a failure in one does not stop the
        others — collecting all three gives a better error than stopping at
        whichever happened to be listed first.
        """
        result = PrepareResult()
        first_error: Exception | None = None

        def note(err: Exception) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = err

        settings = self._settings_fn()
        path = resolve_path(settings.upstream.config_path, DEFAULT_CONFIG_PATH)
        try:
            await self._read_json(cred, self.agent_target(cred, path, method="GET"), "config")
        except (httpx.HTTPError, UpstreamError) as err:
            note(err)

        try:
            result.agents = await self.fetch_agents(cred)
        except (httpx.HTTPError, UpstreamError) as err:
            note(err)

        path = resolve_path(settings.upstream.connections_path, DEFAULT_CONNECTIONS_PATH)
        try:
            await self._read_json(cred, self.agent_target(cred, path, method="GET"), "connections")
        except (httpx.HTTPError, UpstreamError) as err:
            note(err)

        if first_error:
            raise first_error
        return result

    async def create_session(self, cred: Credential) -> str:
        """Open a fresh upstream conversation and return its id.

        One session per request is deliberate: the OpenAI surface is stateless,
        the client resends the whole history every turn, and reusing sessions
        would let context leak between unrelated callers.
        """
        settings = self._settings_fn()
        if not cred.authenticates():
            raise InvalidCredential("empty token")
        agent_id = self.agent_id(settings, cred)
        if not agent_id:
            raise AgentIDUnknown("agent id unknown; the account has not been prepared")

        path = resolve_path(settings.upstream.session_path, DEFAULT_SESSION_PATH)
        body = json.dumps({"agent_id": agent_id, "worktreeMode": False}, separators=(",", ":"))
        target = self.agent_target(cred, path, method="POST", body=body)
        try:
            response = await self._send(cred, target, stream=False, timeout=60.0)
        except httpx.HTTPError as err:
            raise _safe_transport_error(err, cred) from err
        try:
            if response.status_code in (401, 403):
                raise InvalidCredential(f"minimax session HTTP {response.status_code}")
            raw = (await response.aread()).decode("utf-8", "replace")
        finally:
            await response.aclose()
        if response.status_code >= 400:
            raise UpstreamError(f"minimax session HTTP {response.status_code}: {_snippet(raw)}")

        session_id = _session_id_from(raw)
        if not session_id:
            raise UpstreamError(
                f"minimax session: no session id in the response "
                f"(path {path}, agent {agent_id}): {_snippet(raw)}"
            )
        return session_id

    async def probe(self, cred: Credential) -> int:
        """Validate a credential by opening a session and discarding it.

        Creating a session is the cheapest authenticated call available: it proves
        the token and fingerprint pair is accepted without spending a turn.
        """
        started = time.monotonic()
        await self.create_session(cred)
        return int((time.monotonic() - started) * 1000)

    # ---------------------------------------------------------------- completion

    async def completion(self, cred: Credential, options: Options) -> Result:
        settings = self._settings_fn()
        if not cred.authenticates():
            raise InvalidCredential("empty token")
        if not options.text and not options.images:
            raise UpstreamError("empty prompt")
        timeout = options.timeout or float(settings.upstream.request_timeout_sec)
        session_id = await asyncio.wait_for(self.create_session(cred), timeout=timeout)
        return await self.send_message(cred, options, session_id, timeout)

    async def send_message(
        self, cred: Credential, options: Options, session_id: str, timeout: float = 0.0
    ) -> Result:
        settings = self._settings_fn()
        timeout = timeout or float(settings.upstream.request_timeout_sec)
        turn_id = random_uuid()
        target = self.agent_target(
            cred,
            resolve_path(settings.upstream.message_path, DEFAULT_MESSAGE_PATH),
            method="POST",
            body=self._message_body(settings, options, turn_id),
            session_id=session_id,
            # The conversation endpoint answers on the streaming host.
            stream_host=True,
        )
        try:
            response = await self._send(cred, target, stream=True, timeout=timeout)
        except httpx.HTTPError as err:
            raise _safe_transport_error(err, cred) from err

        result = Result(session_id=session_id, turn_id=turn_id)
        try:
            if response.status_code in (401, 403):
                raise InvalidCredential(f"minimax message HTTP {response.status_code}")
            if response.status_code >= 400:
                raw = (await response.aread()).decode("utf-8", "replace")
                raise UpstreamError(f"minimax message HTTP {response.status_code}: {_snippet(raw)}")
            try:
                await self._consume_stream(response, options, result)
            except httpx.HTTPError as err:
                # A partial result is still worth handing back: whatever streamed
                # before the connection broke is the caller's answer so far.
                return result, _safe_transport_error(err, cred)
            result.media = _dedupe_media(result.media)
            if not result.text and not result.thinking and not result.media:
                return result, UpstreamError("upstream returned an empty response")
            return result
        finally:
            await response.aclose()

    @staticmethod
    def _message_body(settings: Settings, options: Options, turn_id: str) -> str:
        """Render the body for one turn.

        The agent expects the whole conversation in a single ``content`` string,
        which is what the gateway already produces when it flattens OpenAI
        messages.  ``model`` is only attached when the operator supplies a
        template: an empty object is rejected, and so is a guessed one.
        """
        body: dict[str, Any] = {"content": options.text, "turn_id": turn_id, "worktreeMode": False}
        if options.images:
            body["attachments"] = [
                {"type": "image", "url": image.url, "name": image.name}
                for image in options.images
            ]
        # The model selection: an explicit ``model`` from the caller wins; the
        # operator's template is the fallback for older setups.  The upstream
        # rejects a bare string and a bare ``model_id``, so a template that is not
        # a dict is ignored rather than sent to fail.
        selection: dict[str, Any] | None = None
        if options.model:
            selection = dict(options.model)
        else:
            template = (settings.upstream.model_payload or "").strip()
            if template:
                try:
                    parsed = json.loads(template)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    selection = parsed
        if selection:
            selection.setdefault("provider_id", "minimax")
            body["model"] = selection
        if options.mode:
            body["mode"] = options.mode
        intent = (options.client_intent or "").strip()
        if intent:
            # Only sent when the caller names one: an invented intent is worse
            # than none, and the plain chat path has always worked without it.
            body["client_intent"] = intent
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    async def _consume_stream(
        self, response: httpx.Response, options: Options, result: Result
    ) -> None:
        settings = self._settings_fn()
        idle = options.idle_timeout or float(settings.upstream.stream_idle_timeout_sec)

        lines = response.aiter_lines()
        block: list[str] = []

        while True:
            try:
                # A timeout here is what closes a stalled stream: the upstream
                # holds the connection open between turns, so a read that never
                # returns is indistinguishable from a working one.
                line = await asyncio.wait_for(lines.__anext__(), timeout=idle)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as err:
                raise UpstreamError(f"upstream stream idle for {idle:.0f}s") from err
            if line == "":
                if block:
                    self._handle_frame("\n".join(block), options, result)
                    block = []
                continue
            block.append(line)
        if block:
            self._handle_frame("\n".join(block), options, result)

    @staticmethod
    def _handle_frame(block: str, options: Options, result: Result) -> None:
        event, payload, raw = _parse_frame(block)
        if payload is None:
            if raw and not raw.startswith("{") and raw != "[DONE]" and event != "message":
                # Some frames carry a bare token or a keep-alive comment.
                message = raw.strip()
                if message:
                    raise UpstreamError(message)
            return

        # Seen before anything interprets it, so a frame that is about to be
        # read as an error, or ignored as unrecognised, is still visible.
        if options.on_frame:
            options.on_frame(payload)

        message = _upstream_error(payload)
        if message:
            raise UpstreamError(message)

        for key in ("session_id", "sessionId"):
            found = _deep_string(payload, key)
            if found and not result.session_id:
                result.session_id = found
        for key in ("message_id", "messageId", "msg_id"):
            found = _deep_string(payload, key)
            if found:
                result.message_id = found

        # Thinking first: an agent that exposes its reasoning nests it under a
        # chunk of its own, and those deltas must not be appended to the answer.
        thinking = _first_deep_string(payload, THINKING_KEYS)
        if thinking:
            result.thinking += thinking
            if options.on_thinking:
                options.on_thinking(thinking)

        text = _first_deep_string(payload, CONTENT_KEYS)
        if text:
            text = _strip_prompt_echo(text, options.text)
            if text:
                result.text += text
                if options.on_delta:
                    options.on_delta(text)

        _collect_media(payload, result.media)
        stop = _deep_string(payload, "finish_reason")
        if stop:
            result.stop_reason = stop

    # ------------------------------------------------------------------- signin

    async def signin_status(self, cred: Credential) -> SigninPanel:
        """Fetch the seven-day check-in board."""
        path = resolve_path(
            self._settings_fn().signin.status_path, DEFAULT_SIGNIN_STATUS_PATH
        )
        payload = await self._read_json(
            cred, self.signin_target(cred, path, method="GET"), "signin status"
        )
        return _parse_panel(_core_of(payload))

    async def signin_claim(self, cred: Credential) -> SigninClaim:
        """Claim today's credits.

        The endpoint is idempotent: a second call on the same day answers
        ``claim_result=2`` rather than an error, so a retry after an ambiguous
        failure is safe and does not double-credit.
        """
        path = resolve_path(self._settings_fn().signin.claim_path, DEFAULT_SIGNIN_CLAIM_PATH)
        payload = await self._read_json(
            cred, self.signin_target(cred, path, method="POST", body="{}"), "signin claim"
        )
        core = _core_of(payload)
        claim = SigninClaim(
            claim_id=_int_of(core.get("claim_id")),
            result=_int_of(core.get("claim_result")),
            day_no=_int_of(core.get("day_no")),
            points=_int_of(core.get("points")),
        )
        panel = core.get("panel")
        if isinstance(panel, dict):
            claim.panel = _parse_panel(panel)
        return claim

    async def credit(self, cred: Credential) -> CreditInfo:
        """Read the live balance.

        The balance that matters lives in
        ``op_credit_summary.total_remaining_amount``, which the upstream sends as
        a *string*; the two flat fields beside it belong to the pre-migration
        credit system and read zero on every migrated account, so they are only a
        fallback.
        """
        path = resolve_path(self._settings_fn().signin.credit_path, DEFAULT_CREDIT_PATH)
        payload = await self._read_json(
            cred, self.signin_target(cred, path, method="POST", body="{}"), "credit"
        )
        core = _core_of(payload)
        info = CreditInfo(
            plan_name=_str_of(core.get("plan_name")),
            plan_type=_int_of(core.get("plan_type")),
        )
        summary = core.get("op_credit_summary")
        if isinstance(summary, dict):
            info.total = _float_of(summary.get("total_remaining_amount"))
            info.free = _float_of(summary.get("free_remaining_amount"))
            info.purchased = _float_of(summary.get("purchased_remaining_amount"))
            return info
        info.total = _float_of(core.get("opcredit_balance"))
        if not info.total:
            info.total = _float_of(core.get("total_remains_credit"))
        return info

    async def credit_grants(self, cred: Credential) -> list[CreditGrant]:
        """Read the per-grant breakdown.  GET, not POST.

        The claim endpoint answers success whether or not the points were ever
        issued, so a grant is the only evidence they arrived — which is what makes
        a check-in auditable at all.
        """
        path = resolve_path(
            self._settings_fn().signin.credit_details_path, DEFAULT_CREDIT_DETAILS_PATH
        )
        payload = await self._read_json(
            cred, self.signin_target(cred, path, method="GET"), "credit details"
        )
        raw = _core_of(payload).get("details")
        if not isinstance(raw, list):
            # An account with no credits answers `{"total_count":0}` and omits the
            # array entirely, so a missing list means empty, not broken.
            return []
        return [
            CreditGrant(
                granted_at=_int_of(item.get("granted_at_ms")),
                expires_at=_int_of(item.get("expire_at_ms")),
                granted=float(_int_of(item.get("granted_amount"))),
                remaining=float(_int_of(item.get("remaining_amount"))),
            )
            for item in raw
            if isinstance(item, dict)
        ]

    async def fetch_raw(
        self, cred: Credential, method: str, path: str, body: str = "", *, stream: bool = False
    ) -> RawResponse:
        """One signed request, reported as it came back.

        Diagnostics only.  It answers the question a completion cannot — *what is
        this account actually able to do?* — and the reply that matters most is
        usually the one that does not decode: a page of the SPA's HTML is how a
        wrong path answers, and it looks like success to anything that only
        checks the status.

        ``stream`` routes the call to the conversation host instead of the API
        host: they are different entry points, so a path that answers on one can
        fail on the other.
        """
        target = self.agent_target(
            cred, path, method=method, body=body, stream_host=stream
        )
        try:
            response = await self._send(cred, target, stream=False, timeout=60.0)
        except httpx.HTTPError as err:
            raise _safe_transport_error(err, cred) from err
        try:
            raw = (await response.aread()).decode("utf-8", "replace")
        finally:
            await response.aclose()
        return RawResponse(
            status=response.status_code, body=_snippet(raw), url=_redact_url(target.url)
        )
