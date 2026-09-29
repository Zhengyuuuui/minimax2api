"""Runtime settings.

Every value here is tunable from the admin console while the process runs, and
persisted as one JSON document in SQLite.  The shapes mirror the upstream
protocol instead of the console: paths are settings because the upstream bundle
is free to rename them, and a capture that disagrees should be a console edit
rather than a rebuild.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace
from typing import Any

from .records import PROXY_STRATEGIES

# The web deployment's own clients are browsers, so the web profile presents one.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# The desktop client identifies itself and carries its app version in the UA.
# Observed verbatim on an authenticated desktop request (MiniMax Code 3.0.73).
DESKTOP_USER_AGENT = (
    "MiniMaxAgent Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) MiniMaxAgent/{version} "
    "Chrome/148.0.7778.280 Safari/537.36"
)
DEFAULT_DESKTOP_VERSION = "3.0.73"

# Defaults an earlier release shipped that turned out to be unusable.  They are
# named so ``normalize`` can recognise and repair a stored copy: a default that
# was frozen into an existing settings file would otherwise stay wrong forever
# with no symptom.
LEGACY_SESSION_PATH = "/agent/{agent_id}/session"
LEGACY_AGENT_ID = "general"
LEGACY_MESSAGE_PATH = "/archon/api/v1/session/{session_id}/message"

STRATEGIES = ("least_inflight", "round_robin", "priority", "random")


@dataclass
class UpstreamSettings:
    # Two hosts side by side: MiniMax runs separate mainland and international
    # deployments with separate account databases, so a +86 token is only usable
    # against the former and an email/overseas token only against the latter.
    base_url: str = "https://agent.minimax.io"
    base_url_cn: str = "https://agent.minimaxi.com"
    # Empty on purpose: "general" is an agent *role*, and the upstream answers it
    # with a 200 that opens no session.  The real id is a per-account number,
    # discovered when the account is prepared.
    agent_id: str = ""
    session_path: str = "/minimax-cloud/api/v1/agent/{agent_id}/session"
    message_path: str = "/minimax-cloud/api/v1/session/{session_id}/message"
    # Empty means "derive from base_url": agent.x -> agent-stream.x.  The two
    # hosts are different entry points rather than two addresses for one, and the
    # message endpoint only renders its tools on the streaming side.
    stream_base_url: str = ""
    user_info_path: str = "/v1/api/user/info"
    agent_list_path: str = "/minimax-cloud/api/v1/agent"
    config_path: str = "/minimax-cloud/api/v1/config"
    connections_path: str = "/minimax-cloud/api/v1/channel/connections"
    # The agent's model field is an object upstream and an empty one is
    # rejected, so it is only sent when the operator supplies a template.
    model_payload: str = ""
    language: str = "zh-CN,zh;q=0.9,en;q=0.8"
    screen_width: int = 1920
    screen_height: int = 1080
    # The desktop profile's fingerprint, taken from the client's own requests.
    # These are sent only for accounts that signed in through the desktop flow;
    # see upstream.Credential.profile.  A web JWT keeps the browser-shaped values
    # above, and mixing the two is what makes a request look synthetic.
    #
    # `desktop_ua_version` is what the user agent advertises; the `desktop_version`
    # *query parameter* is a separate field, and in the captured requests it is
    # sent empty — so it is kept blank rather than filled with the app version.
    desktop_ua_version: str = DEFAULT_DESKTOP_VERSION
    desktop_version: str = ""
    desktop_os_name: str = "macOS"
    desktop_browser_platform: str = "MacIntel"
    desktop_device_memory: int = 32
    desktop_cpu_core_num: int = 12
    desktop_screen_width: int = 1800
    desktop_screen_height: int = 1169
    request_timeout_sec: int = 300
    stream_idle_timeout_sec: int = 120
    # Required for the international site: every business call behind it is
    # fenced to overseas egress, and a direct call answers a bare 401.
    proxy: str = ""
    user_agent: str = DEFAULT_USER_AGENT


@dataclass
class RoutingSettings:
    strategy: str = "least_inflight"
    cooldown_base_sec: int = 60
    cooldown_max_sec: int = 900
    max_attempts: int = 3
    capacity_wait_sec: int = 20
    sticky_ttl_sec: int = 300
    prefer_idle: bool = True


@dataclass
class AuditSettings:
    retention_days: int = 7
    max_records: int = 5000
    record_body: bool = True
    body_limit_bytes: int = 8192


@dataclass
class MediaSettings:
    generated_dir: str = ""
    public_base_url: str = ""
    max_total_size_mb: int = 2048
    auto_download: bool = True


@dataclass
class SigninSettings:
    # Off by default: it spends a request against every account in the pool.
    enabled: bool = False
    hour: int = 9
    minute: int = 5
    # Check-in is a risk-control sensitive endpoint; a burst of parallel calls
    # from one address is exactly the pattern that gets accounts flagged.
    gap_seconds: int = 2
    timeout_sec: int = 30
    skip_zero_credit: bool = False
    credit_fresh_min: int = 360
    # How often a background pass re-reads balances that have gone stale.  Short
    # on purpose: the console's balance is what an operator trusts, and a number
    # that quietly disagrees with the website is worse than none.
    credit_refresh_min: int = 5
    lang: str = "en"
    os_name: str = "Windows"
    browser_name: str = "Chrome"
    browser_language: str = "en-US"
    browser_platform: str = "Win32"
    device_memory: int = 16
    cpu_core_num: int = 8
    # UTC+8 by default.  0 is repaired rather than honoured, because a stored 0
    # cannot be told apart from an absent field.
    timezone_offset_min: int = 480
    status_path: str = "/minimax-cloud/api/v1/signin/status"
    claim_path: str = "/minimax-cloud/api/v1/signin/claim"
    credit_path: str = "/matrix/api/v1/commerce/get_membership_info"
    credit_details_path: str = "/minimax-cloud/api/v1/credit/details"


@dataclass
class SignupSettings:
    """Headless account registration.

    Creating an account is the one part of the bridge that talks to a service
    other than MiniMax: an email address has to be obtained and read from
    somewhere, and the temporary-mail worker is that somewhere.  The shape below
    matches ``cloudflare_temp_email`` (``/admin/new_address`` with an
    ``x-admin-auth`` header, ``/api/parsed_mails`` with a bearer JWT), which is
    the deployed worker.

    The MiniMax side needs nothing new: the register call is the same signed
    ``account.minimax.io`` surface the device flow already reaches, and the token
    it ends up with is imported through the existing OAuth path.
    """

    enabled: bool = True
    # Region whose account service is registered against.  The two deployments
    # have separate account databases, so registration is per-region like login.
    region: str = "global"
    # The temporary-mail service the headless registration reads codes from.
    # Empty by default: an operator supplies their own via env
    # (``MINIMAX2API_SIGNUP__MAIL_BASE`` / ``__MAIL_DOMAIN``), because a mail
    # service is deployment-specific and shipping one address couples every
    # install to it.
    mail_base: str = ""
    mail_domain: str = ""
    mail_pass: str = ""
    # Address prefix; a random suffix is appended so two runs cannot collide.
    name_prefix: str = "mmx"
    # How long to wait for a verification mail, and how often to look.
    mail_timeout_sec: int = 120
    mail_poll_sec: int = 3
    # Set a password on the fresh account so a future re-login needs no mailbox.
    password: str = ""
    # OAuth grant the registration is authorised against, then imported.
    oauth_scope: str = "agent.default"
    oauth_audience: str = "agent-backend"
    # ---- anti-throttle (proxy pool) ----
    # Registration is counted per exit address by risk control, so accounts are
    # spread across proxies and each address is capped.  ``0`` would mean "no
    # limit", which is never what an operator wants silently, so the floor is 1.
    use_proxies: bool = True
    per_ip_limit: int = 3
    proxy_strategy: str = "rotate"
    # A proxy that fails this many checks in a row is skipped by the selector.
    proxy_max_fail: int = 3
    # Where the checkout asks what the world sees; used by the proxy checker.
    proxy_check_url: str = "https://ipwho.is/"
    # Registrations are serial and spaced: a burst from one process is itself a
    # pattern, and this also keeps within the mail service's own rate limits.
    batch_max: int = 20
    gap_seconds: int = 3


@dataclass
class KeepaliveSettings:
    """Renewal of the one-hour OAuth tokens.

    Separate from ``signup`` because it applies to every account already in the
    pool, however it arrived — the browser sign-in produces the same one-hour
    token the headless registration does.
    """

    enabled: bool = True
    # How often the background sweep looks for tokens near expiry.  The access
    # token the site issues lives one hour (``expires_in=3600``); a half-hour
    # sweep means each credential is renewed once an hour, not on every pass, so
    # the refresh endpoint is touched twice an hour per account rather than every
    # few minutes.  Renewal only happens for tokens inside the margin
    # (``keepalive.REFRESH_MARGIN_SEC``, which must exceed this interval so a token
    # cannot pass from "not yet due" to expired between two sweeps).
    #
    # This is deliberately independent of the balance refresh
    # (``signin.credit_refresh_min``): keeping a credential alive and keeping a
    # number current are separate jobs with separate failure modes, and one must
    # not be tuned by changing the other.
    interval_sec: int = 1800


@dataclass
class Settings:
    upstream: UpstreamSettings = field(default_factory=UpstreamSettings)
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    audit: AuditSettings = field(default_factory=AuditSettings)
    media: MediaSettings = field(default_factory=MediaSettings)
    signin: SigninSettings = field(default_factory=SigninSettings)
    signup: SignupSettings = field(default_factory=SignupSettings)
    keepalive: KeepaliveSettings = field(default_factory=KeepaliveSettings)


def default_settings(data_dir: str) -> Settings:
    return Settings(
        media=MediaSettings(generated_dir=f"{data_dir}/generated"),
        signin=SigninSettings(),
    )


# --------------------------------------------------------------------- normalize


def _int_range(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    if number < low or number > high:
        return fallback
    return number


def normalize(settings: Settings, data_dir: str) -> bool:
    """Repair out-of-range or known-broken values; report whether it changed anything.

    Only *absent* values are filled in normally.  The two legacy defaults are
    overwritten on sight: neither can work, so repairing one is never a loss of
    intent.
    """
    before = replace(settings)
    default = default_settings(data_dir)

    up = settings.upstream
    if not up.base_url:
        up.base_url = default.upstream.base_url
    if not up.base_url_cn:
        up.base_url_cn = default.upstream.base_url_cn
    if up.agent_id == LEGACY_AGENT_ID:
        up.agent_id = default.upstream.agent_id
    if not up.session_path:
        up.session_path = default.upstream.session_path
    if up.session_path == LEGACY_SESSION_PATH:
        # The short form resolves to the site's SPA fallback, which answers 200
        # with a page of HTML rather than a session.
        up.session_path = default.upstream.session_path
    if not up.message_path:
        up.message_path = default.upstream.message_path
    if up.message_path == LEGACY_MESSAGE_PATH:
        # Not broken, merely the wrong door: it answers, and the agent replies,
        # but the entry point hands it no rendering tool.  It is migrated with
        # the streaming host because the two were captured as a pair.
        up.message_path = default.upstream.message_path
        up.stream_base_url = default.upstream.stream_base_url
    if not up.user_info_path:
        up.user_info_path = default.upstream.user_info_path
    if not up.agent_list_path:
        up.agent_list_path = default.upstream.agent_list_path
    if not up.config_path:
        up.config_path = default.upstream.config_path
    if not up.connections_path:
        up.connections_path = default.upstream.connections_path
    up.screen_width = max(1, up.screen_width)
    up.screen_height = max(1, up.screen_height)
    if not up.language:
        up.language = default.upstream.language
    up.request_timeout_sec = max(5, up.request_timeout_sec)
    up.stream_idle_timeout_sec = max(5, up.stream_idle_timeout_sec)
    if not up.user_agent:
        up.user_agent = default.upstream.user_agent
    if not up.desktop_ua_version:
        up.desktop_ua_version = default.upstream.desktop_ua_version
    if not up.desktop_os_name:
        up.desktop_os_name = default.upstream.desktop_os_name
    if not up.desktop_browser_platform:
        up.desktop_browser_platform = default.upstream.desktop_browser_platform
    up.desktop_device_memory = max(1, up.desktop_device_memory)
    up.desktop_cpu_core_num = max(1, up.desktop_cpu_core_num)
    up.desktop_screen_width = max(1, up.desktop_screen_width)
    up.desktop_screen_height = max(1, up.desktop_screen_height)

    routing = settings.routing
    if routing.strategy not in STRATEGIES:
        routing.strategy = default.routing.strategy
    routing.cooldown_base_sec = max(1, routing.cooldown_base_sec)
    routing.cooldown_max_sec = max(routing.cooldown_base_sec, routing.cooldown_max_sec)
    routing.max_attempts = _int_range(routing.max_attempts, 1, 20, default.routing.max_attempts)
    routing.capacity_wait_sec = max(0, routing.capacity_wait_sec)
    routing.sticky_ttl_sec = max(0, routing.sticky_ttl_sec)

    audit = settings.audit
    audit.retention_days = max(0, audit.retention_days)
    audit.max_records = max(100, audit.max_records)
    audit.body_limit_bytes = max(256, audit.body_limit_bytes)

    media = settings.media
    if not media.generated_dir:
        media.generated_dir = default.media.generated_dir
    media.max_total_size_mb = max(64, media.max_total_size_mb)

    signin = settings.signin
    signin.hour = _int_range(signin.hour, 0, 23, default.signin.hour)
    signin.minute = _int_range(signin.minute, 0, 59, default.signin.minute)
    signin.gap_seconds = max(0, signin.gap_seconds)
    signin.timeout_sec = max(5, signin.timeout_sec)
    signin.credit_fresh_min = max(1, signin.credit_fresh_min)
    signin.credit_refresh_min = max(0, signin.credit_refresh_min)
    signin.device_memory = max(1, signin.device_memory)
    signin.cpu_core_num = max(1, signin.cpu_core_num)
    if not signin.lang:
        signin.lang = default.signin.lang
    if not signin.os_name:
        signin.os_name = default.signin.os_name
    if not signin.browser_name:
        signin.browser_name = default.signin.browser_name
    if not signin.browser_language:
        signin.browser_language = default.signin.browser_language
    if not signin.browser_platform:
        signin.browser_platform = default.signin.browser_platform
    if not signin.status_path:
        signin.status_path = default.signin.status_path
    if not signin.claim_path:
        signin.claim_path = default.signin.claim_path
    if not signin.credit_path:
        signin.credit_path = default.signin.credit_path
    if not signin.credit_details_path:
        signin.credit_details_path = default.signin.credit_details_path

    signup = settings.signup
    if signup.region not in ("cn", "global"):
        signup.region = default.signup.region
    if not signup.mail_base:
        signup.mail_base = default.signup.mail_base
    if not signup.mail_domain:
        signup.mail_domain = default.signup.mail_domain
    if not signup.name_prefix:
        signup.name_prefix = default.signup.name_prefix
    signup.mail_timeout_sec = _int_range(signup.mail_timeout_sec, 10, 600, default.signup.mail_timeout_sec)
    signup.mail_poll_sec = _int_range(signup.mail_poll_sec, 1, 60, default.signup.mail_poll_sec)
    if not signup.oauth_scope:
        signup.oauth_scope = default.signup.oauth_scope
    if not signup.oauth_audience:
        signup.oauth_audience = default.signup.oauth_audience
    if signup.proxy_strategy not in PROXY_STRATEGIES:
        signup.proxy_strategy = default.signup.proxy_strategy
    signup.per_ip_limit = _int_range(signup.per_ip_limit, 1, 100, default.signup.per_ip_limit)
    signup.proxy_max_fail = _int_range(signup.proxy_max_fail, 1, 50, default.signup.proxy_max_fail)
    if not signup.proxy_check_url:
        signup.proxy_check_url = default.signup.proxy_check_url
    signup.batch_max = _int_range(signup.batch_max, 1, 200, default.signup.batch_max)
    signup.gap_seconds = _int_range(signup.gap_seconds, 0, 3600, default.signup.gap_seconds)

    keepalive = settings.keepalive
    keepalive.interval_sec = _int_range(keepalive.interval_sec, 60, 86400, default.keepalive.interval_sec)

    return settings != before


# ------------------------------------------------------------------ conversion


def resolve_path(stored: str, fallback: str) -> str:
    """Read a stored endpoint path, refusing anything that cannot be templated.

    The template is validated against the fallback it replaces — a stored path
    with no ``{agent_id}`` or ``{session_id}`` cannot describe the same endpoint,
    and substituting into it silently builds a URL that was never tested.
    """
    candidate = (stored or "").strip()
    if not candidate:
        return fallback
    if candidate != fallback and set(fallback.split("{")[1:]) - set(candidate.split("{")[1:]):
        return fallback
    return candidate


SECTIONS = (
    "upstream",
    "routing",
    "audit",
    "media",
    "signin",
    "signup",
    "keepalive",
)

# Fields whose value must not travel to the console in the clear.
#
# ``/admin/api/settings`` answers whoever can reach the port, and the process has
# no auth of its own, so a value here is echoed as a placeholder: the console can
# show that a secret is set and edit it, but a page that is open on a shared
# screen is not handing out a mail passkey.  ``proxy`` is a URL that may embed a
# password, so it is masked like the rest.
SECRET_FIELDS = frozenset(
    {
        "signup.mail_pass",
        "signup.password",
        "upstream.proxy",
    }
)

# The placeholder a secret is replaced with on the way out.  Chosen so it cannot
# be mistaken for a real value: nothing sane sets a passkey to this text, and the
# write path treats exactly this string as "leave it as it was".
SECRET_MASK = "********"


def is_secret(path: str) -> bool:
    return path in SECRET_FIELDS


def to_dict(settings: Settings) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for section in SECTIONS:
        holder = getattr(settings, section)
        payload[section] = {item.name: getattr(holder, item.name) for item in fields(holder)}
    return payload


def to_dict_masked(settings: Settings, env_names: set[str] | None = None) -> dict[str, Any]:
    """The console's view: secrets replaced, env-controlled fields flagged.

    Two annotations ride along, both ``"_"-prefixed`` so they read as metadata
    rather than something the settings form would try to submit:

    - ``_secrets`` — the field paths that were masked, so the form knows which
      inputs get a reveal control and which must be left alone when blank.
    - ``_locked`` — paths the environment controls; the form renders them
      read-only, because an edit here is accepted and then discarded.
    """
    payload = to_dict(settings)
    masked: list[str] = []
    for section, values in payload.items():
        for field_name, value in list(values.items()):
            path = f"{section}.{field_name}"
            if is_secret(path) and value:
                values[field_name] = SECRET_MASK
                masked.append(path)
    payload["_secrets"] = masked
    payload["_locked"] = sorted(env_names or ())
    return payload


def from_dict(payload: dict[str, Any], data_dir: str) -> Settings:
    """Build settings from a plain dict, ignoring unknown keys and bad values.

    Bad values are tolerated on purpose: a partially-typed or hand-edited
    settings document must not keep the process from booting, and ``normalize``
    repairs whatever is out of range afterwards.
    """
    settings = default_settings(data_dir)
    if not isinstance(payload, dict):
        return settings
    for section in SECTIONS:
        node = payload.get(section)
        if not isinstance(node, dict):
            continue
        holder = getattr(settings, section)
        for key, value in node.items():
            if hasattr(holder, key) and isinstance(value, type(getattr(holder, key))):
                setattr(holder, key, value)
    return settings


def to_json(settings: Settings) -> str:
    return json.dumps(to_dict(settings), ensure_ascii=False, indent=2)


def parse_json(raw: str, data_dir: str) -> Settings:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return default_settings(data_dir)
    return from_dict(payload, data_dir)


def coerce_bool(value: Any, current: bool) -> bool:
    """Read a boolean out of a value that may be a string, as JSON round-trips do."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return current


def apply_update(settings: Settings, payload: dict[str, Any]) -> bool:
    """Apply a settings update, coercing scalars; report whether anything changed."""
    if not isinstance(payload, dict):
        return False
    changed = False
    for section in SECTIONS:
        node = payload.get(section)
        if not isinstance(node, dict):
            continue
        holder = getattr(settings, section)
        for key, value in node.items():
            if not hasattr(holder, key):
                continue
            current = getattr(holder, key)
            # The console round-trips a masked secret back on save; writing the
            # placeholder would destroy the real value, so it is read as "not
            # provided".  Clearing a secret is done by sending an empty string.
            if is_secret(f"{section}.{key}") and value == SECRET_MASK:
                continue
            if isinstance(current, bool):
                coerced = coerce_bool(value, current)
            elif isinstance(current, str) and isinstance(value, (str, int, float)):
                coerced = str(value).strip()
            elif isinstance(current, (int, float)) and not isinstance(current, bool):
                if isinstance(value, bool):
                    continue
                try:
                    coerced = type(current)(value)
                except (TypeError, ValueError):
                    continue
            else:
                continue
            if coerced != current:
                setattr(holder, key, coerced)
                changed = True
    return changed
