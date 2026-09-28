"""Tests for the parts that are easy to get subtly wrong.

The suite is small on purpose.  It covers what cannot be checked by reading the
code: the signature recipe, the shapes a request is flattened into, and the state
a request drives.  The first two are worth more than they look — one reordered
parameter or one dropped role tag produces a request the upstream accepts and
then answers wrongly, which is the class of bug a unit test can actually catch.

Each group below exists because of a real defect found during development: the
account update bound the wrong number of parameters, and the media path ignored
the suffix the downloader wrote.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time

import pytest

from app import device_login, gateway, pool, prompt, signing
from app.media import MediaStore
from app.prompt import Turn


# ----------------------------------------------------------------- signatures


def test_signature_is_md5_over_salt_and_body():
    assert signing.x_signature(1700000000, '{"a":1}') == signing.md5_hex(
        "1700000000" + signing.SIGNATURE_SALT + '{"a":1}'
    )


def test_yy_covers_the_encoded_url():
    url = "https://agent.minimax.io/x?token=ab&client=web"
    expected = signing.md5_hex(
        signing.encode_uri_component(url)
        + "_"
        + "{}"
        + signing.md5_hex("1700000000000")
        + "ooui"
    )
    assert signing.yy(url, "{}", 1700000000000) == expected


def test_encode_uri_component_matches_javascript():
    """The exact set the browser escapes, and nothing else.

    ``'`` and ``!`` are the two people guess wrong: both survive.  One byte of
    difference changes the URL's MD5, and a changed digest is a request the
    upstream refuses without naming a field.
    """
    for char in "!'()*-._~":
        assert signing.encode_uri_component(char) == char
    assert signing.encode_uri_component(" ") == "%20"
    assert signing.encode_uri_component(" ") != "+"
    assert signing.encode_uri_component("中") == "%E4%B8%AD"
    assert signing.encode_uri_component("a+b") == "a%2Bb"
    assert signing.encode_uri_component("a/b") == "a%2Fb"


def test_form_encode_uses_plus_for_space():
    """The check-in endpoints are serialised the other way round."""
    assert signing.form_encode("a b") == "a+b"
    assert signing.form_encode("a+b") == "a%2Bb"


def test_timestamps_round_trip_through_json():
    """`to_json` renders times as ISO strings, so `from_json` has to parse them.

    A number parser silently returns zero for an ISO date, and a zero `syncedAt`
    makes every freshness check believe a reading is decades old — which is how a
    just-fetched balance got displayed as stale.
    """
    from app.records import Credit, Quota

    credit = Credit(total=776, synced_at=1759000000.0)
    restored = Credit.from_json(credit.to_json())
    assert restored is not None
    assert abs(restored.synced_at - 1759000000.0) < 1.0
    assert restored.total == 776

    quota = Quota(synced_at=1759000000.0, available=True)
    restored_quota = Quota.from_json(quota.to_json())
    assert restored_quota is not None
    assert abs(restored_quota.synced_at - 1759000000.0) < 1.0


# ------------------------------------------------------------------ prompting


def test_prompt_inlines_system_and_numbers_history():
    turns = [
        Turn("system", "be brief"),
        Turn("user", "one"),
        Turn("assistant", "two"),
        Turn("user", "three"),
    ]
    assert prompt.build_prompt(turns) == (
        "[系统指令] be brief\n\n 用户：1. one\n\n助手：two\n\n 用户：2. three"
    )


def test_single_question_is_sent_bare():
    """What the web client sends a lone question as, so what gets sent."""
    assert prompt.build_prompt([Turn("user", "hello")]) == "hello"


def test_images_follow_the_text_and_keep_their_url():
    message = prompt.parse_turn(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这是什么"},
                {"type": "image_url", "image_url": {"url": "https://cdn/x.png"}},
            ],
        }
    )
    text = prompt.build_prompt([message])
    assert text.startswith("这是什么")
    assert text.rstrip().endswith("(https://cdn/x.png)")


def test_image_only_turn_says_something():
    """The agent refuses an empty string, so a picture alone needs a caption."""
    message = prompt.parse_turn({"role": "user", "content": "", "images": ["https://cdn/x.png"]})
    assert prompt.build_prompt([message]) == "看图\n![已忽略的图片 0](https://cdn/x.png)"


def test_tool_and_unknown_roles_are_rendered_as_user_turns():
    """A tool result is history the agent should read, so it is numbered.

    The role is kept as-is — nothing else distinguishes it — but the renderer
    gives it a user turn's position rather than dropping it.
    """
    tool = prompt.parse_turn({"role": "tool", "content": "42"})
    assert prompt.build_prompt([tool, Turn("user", "so?")]) == " 用户：1. 42\n\n 用户：2. so?"
    assert prompt.parse_turn({"content": "no role"}).role == "user"
    assert prompt.parse_turn(None) is None


# ---------------------------------------------------------------------- media


def test_media_path_refuses_traversal_and_finds_the_suffix(tmp_path):
    store = MediaStore(_FakeDB(), lambda: _media_settings(tmp_path))
    assert store.path_for("../etc/passwd") is None
    assert store.path_for("short") is None
    assert store.path_for("abcdef123456") is None
    (tmp_path / "abcdef123456.png").write_bytes(b"x")
    # The downloader writes <id><suffix>, so an id alone is not a filename and the
    # lookup has to know that.
    assert store.path_for("abcdef123456").name == "abcdef123456.png"
    assert store.public_url("abcdef123456") == "/media/abcdef123456"


class _FakeDB:
    async def list_media(self, limit: int = 100):  # noqa: D102
        return []


def _media_settings(root):
    from app.config import MediaSettings, Settings

    return Settings(media=MediaSettings(generated_dir=str(root)))


# ----------------------------------------------------------------------- pool


def test_pool_orders_by_inflight_then_priority():
    """Load is open streams, not completed requests."""
    db, accounts = _fake_pool_db(3)
    holder = _SettingsHolder()
    holder.routing.strategy = "least_inflight"
    engine = pool.Pool(db, holder.settings)
    asyncio.run(engine.load())

    lease_a = asyncio.run(engine.acquire())
    assert lease_a.account.name == "a"
    # `b` is idle, so it is next even though `a` has the higher priority.
    lease_b = asyncio.run(engine.acquire())
    assert lease_b.account.name == "b"
    asyncio.run(engine.release(lease_a, success=True))
    asyncio.run(engine.release(lease_b, success=True))


def test_pool_excludes_tried_accounts_and_reports_why():
    db, _ = _fake_pool_db(2)
    holder = _SettingsHolder()
    # No waiting: this test is about the refusal, not the grace period.
    holder.routing.capacity_wait_sec = 0
    engine = pool.Pool(db, holder.settings)
    asyncio.run(engine.load())
    taken = {account.id for account in engine.accounts()}

    with pytest.raises(pool.NoAvailableAccount) as err:
        asyncio.run(engine.acquire(exclude=taken))
    assert "already tried" in err.value.reason


def test_pool_retires_only_a_refused_credential():
    """A generic failure is a cooldown; a refused credential is permanent."""
    from app import upstream

    db, _ = _fake_pool_db(1)
    holder = _SettingsHolder()
    engine = pool.Pool(db, holder.settings)
    asyncio.run(engine.load())
    lease = asyncio.run(engine.acquire())

    asyncio.run(engine.release(lease, success=False, error=upstream.UpstreamError("too many requests")))
    assert engine.account(lease.account.id).status == "cooldown"

    db2, _ = _fake_pool_db(1)
    engine2 = pool.Pool(db2, holder.settings)
    asyncio.run(engine2.load())
    lease2 = asyncio.run(engine2.acquire())
    asyncio.run(
        engine2.release(lease2, success=False, error=upstream.InvalidCredential("401"))
    )
    assert engine2.account(lease2.account.id).status == "invalid"


def test_release_with_no_error_records_nothing():
    """An account that could not serve, but was not sick, is left alone."""
    db, _ = _fake_pool_db(1)
    holder = _SettingsHolder()
    engine = pool.Pool(db, holder.settings)
    asyncio.run(engine.load())
    lease = asyncio.run(engine.acquire())
    asyncio.run(engine.release(lease, success=False))
    assert engine.account(lease.account.id).status == "active"
    assert engine.account(lease.account.id).fail_count == 0


# ------------------------------------------------------------------- fixtures


class _SettingsHolder:
    """Just enough Settings for a pool to make decisions from."""

    def __init__(self) -> None:
        from app.config import RoutingSettings, Settings

        self.routing = RoutingSettings()
        self._settings = Settings(routing=self.routing)

    settings = property(lambda self: self._settings)

    def settings(self):
        return self._settings


class _FakeAccountDB:
    """The three things the pool asks the store for."""

    def __init__(self, accounts):
        self._accounts = {account.id: account for account in accounts}
        self.writes = []

    async def list_accounts(self):
        return list(self._accounts.values())

    async def save_account_state(self, account_id, apply_fn):
        account = self._accounts[account_id]
        apply_fn(account)
        self.writes.append(account_id)
        return account

    async def upsert_account(self, account):
        self._accounts[account.id] = account
        return account


def _fake_pool_db(count: int):
    from app.records import STATUS_ACTIVE, Account

    accounts = [
        Account(
            id=f"id{index}",
            name=chr(ord("a") + index),
            priority=1 - index,
            enabled=True,
            status=STATUS_ACTIVE,
            max_concurrent=1,
        )
        for index in range(count)
    ]
    return _FakeAccountDB(accounts), accounts


# --------------------------------------------------------------- device login


class _TransportStub:
    """A stand-in for account.minimax.io, so the state machine has no network."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url, timeout=None, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outcome = self.responses.pop(0) if self.responses else ValueError("out of responses")
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"{}"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _AdminStub:
    def __init__(self):
        self.imported = []

    async def import_device_token(self, token, name="", region="global"):
        if token == "bad":
            raise RuntimeError("the token carries no realUserID")
        self.imported.append((token, name, region))
        return {"id": "acct-1", "name": name or "acct-1"}


def _service(responses):
    transport = _TransportStub(responses)

    class Client:
        def public_client(self, host):
            return transport

    admin = _AdminStub()
    return device_login.DeviceLoginService(Client(), admin), transport, admin


@pytest.fixture(autouse=True)
def _spacing_constants_are_restored():
    """``_flow`` lowers the spacing constants to make scenarios fast.

    Without this, a scenario that ran first leaves the production floor at zero
    for every scenario after it, and the test that guards the floor would be
    guarding a value a previous test set.
    """
    saved = (
        device_login._MIN_INTERVAL,
        device_login._TRANSIENT_STEP,
        device_login._MAX_INTERVAL,
    )
    yield
    (
        device_login._MIN_INTERVAL,
        device_login._TRANSIENT_STEP,
        device_login._MAX_INTERVAL,
    ) = saved


def _start_payload() -> dict:
    return {
        "device_code": "DC",
        "user_code": "ABCD-EFGH",
        # A hundredth of a second, so a scenario that polls ten times still
        # finishes in a heartbeat: the machine under test is the state
        # transitions, not the waiting.
        "expires_in": 300,
        "interval": 0.01,
        "verification_uri": "https://account.minimax.io/oauth-authorize",
        "verification_uri_complete": "https://account.minimax.io/oauth-authorize?user_code=ABCD-EFGH",
    }


async def _flow(responses, name="", timeout=2.0, relax=True, region="global"):
    """Start a login, run its poll task to a decision, return everything.

    One event loop for the whole scenario: the poll task is created inside
    ``start``, and a second ``asyncio.run`` would close the loop out from under it
    and leave the session pending forever.

    The spacing constants are relaxed to milliseconds so a scenario that polls
    ten times still finishes in a heartbeat.  What is under test is the state
    machine, not the waiting; ``relax=False`` keeps the production floor, which is
    what the floor's own test uses.
    """
    if relax:
        device_login._MIN_INTERVAL = 0.0
        device_login._TRANSIENT_STEP = 0.001
        device_login._MAX_INTERVAL = 0.002
    service, transport, admin = _service(responses)
    public = await service.start(name, region)
    deadline = asyncio.get_event_loop().time() + timeout
    session = public
    while session["status"] == device_login.PENDING and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
        session = service.get(public["id"])
    return service, transport, admin, public, session


def test_device_code_origins_follow_the_region():
    """The two deployments have separate account services.

    From the desktop client's own origin table: cn -> account.minimax.cn,
    en -> account.minimax.io.  A token is accepted by exactly one agent
    deployment, so the region decides which service issues it.
    """
    assert device_login.device_code_url("cn") == "https://account.minimax.cn/oauth2/device/code"
    assert device_login.token_url("cn") == "https://account.minimax.cn/oauth2/token"
    assert device_login.device_code_url("global").startswith("https://account.minimax.io/")

    # The declared OAuth contract, which is what the desktop client sends.
    assert device_login.OAUTH_SCOPE == "agent.default"
    assert device_login.OAUTH_AUDIENCE == "agent-backend"


def test_cn_login_polls_the_cn_token_endpoint():
    async def scenario():
        # Only the start response is canned; the poll reuses the transport stub.
        responses = [_start_payload(), {"error": "expired_token"}]
        service, transport, _, _, _ = await _flow(responses, region="cn")
        return transport

    transport = asyncio.run(scenario())
    assert transport.calls[0]["url"].startswith("https://account.minimax.cn/")
    assert transport.calls[1]["url"].startswith("https://account.minimax.cn/oauth2/token")
    assert transport.calls[0]["json"]["scope"] == device_login.OAUTH_SCOPE
    assert transport.calls[0]["json"]["audience"] == device_login.OAUTH_AUDIENCE


def test_pkce_challenge_is_s256_of_the_verifier():
    verifier, challenge = device_login.pkce_pair()
    import hashlib

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected
    assert "=" not in challenge and "+" not in challenge and "/" not in challenge


def test_token_extraction_walks_the_envelope():
    assert device_login.extract_token({"access_token": "a.b.c"}) == "a.b.c"
    assert device_login.extract_token({"data": {"access_token": "a.b.c"}}) == "a.b.c"
    # A loose name only counts when it actually looks like a JWT: a session id
    # sitting in the same response must not be mistaken for a credential.
    assert device_login.extract_token({"data": {"token": "sess-1"}}) == ""
    assert device_login.extract_token({"data": {"token": "a.b.c"}}) == "a.b.c"
    assert device_login.extract_token({"user_id": "7"}) == ""
    assert device_login.extract_token(["nope", {"access_token": "x.y.z"}]) == "x.y.z"


def test_production_interval_floor_is_respected():
    """The relaxation above must not become the shipping behaviour."""
    payload = _start_payload()
    payload["interval"] = 0

    async def scenario():
        service, _, _, _, _ = await _flow(
            [payload, {"error": "expired_token"}], timeout=0.2, relax=False
        )
        return list(service._sessions.values())[0].interval

    # A zero from the server is clamped to the floor, not replaced by the default.
    assert asyncio.run(scenario()) == 1.0
    assert device_login._clamp_interval(0) == 1.0
    assert device_login._clamp_interval(None) == device_login._DEFAULT_INTERVAL
    assert device_login._clamp_interval(999) == device_login._MAX_INTERVAL


def test_public_view_hides_the_secrets():
    _, _, _, public, session = asyncio.run(_flow([_start_payload(), {"error": "expired_token"}]))
    # The code and the verifier are the two things that redeem a grant; the
    # console sees a link and a status.
    blob = json.dumps(public)
    assert "device_code" not in blob and "verifier" not in blob
    assert public["userCode"] == "ABCD-EFGH"
    assert public["verifyUrl"].endswith("user_code=ABCD-EFGH")
    assert session["status"] == device_login.EXPIRED


def test_polling_survives_pending_then_imports():
    responses = [
        _start_payload(),
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"access_token": "header.payload.sig"},
    ]
    _, transport, admin, _, session = asyncio.run(_flow(responses, "主号"))
    assert session["status"] == device_login.AUTHORIZED
    assert admin.imported == [("header.payload.sig", "主号", "global")]
    assert session["accountId"] == "acct-1"

    # The token request carries the verifier, not the challenge, and no app_id or
    # scope: this server reports either addition as invalid_request.
    token_call = transport.calls[-1]
    assert token_call["url"].endswith("/oauth2/token")
    assert token_call["data"]["grant_type"] == device_login.DEVICE_GRANT_TYPE
    assert token_call["data"]["code_verifier"]
    assert "app_id" not in token_call["data"] and "scope" not in token_call["data"]


def test_start_sends_the_challenge_not_the_verifier():
    _, transport, _, _, _ = asyncio.run(_flow([_start_payload(), {"error": "expired_token"}]))
    body = transport.calls[0]
    assert body["url"].endswith("/oauth2/device/code")
    assert body["json"]["client_id"] == device_login.CLIENT_ID
    assert body["json"]["code_challenge_method"] == "S256"
    assert "code_verifier" not in json.dumps(body["json"])


def test_denied_and_expired_are_terminal_states():
    for error, expected in (("access_denied", device_login.DENIED), ("expired_token", device_login.EXPIRED)):
        _, _, _, _, session = asyncio.run(_flow([_start_payload(), {"error": error}]))
        assert session["status"] == expected


def test_slow_down_widens_the_gap_and_keeps_going():
    responses = [_start_payload()] + [{"error": "slow_down"}] * 3 + [{"access_token": "a.b.c"}]
    _, _, _, _, session = asyncio.run(_flow(responses))
    assert session["status"] == device_login.AUTHORIZED


def test_four_network_failures_give_up():
    """A missing proxy must not look like a code that timed out."""
    responses = [_start_payload()] + [RuntimeError("ConnectError")] * device_login._MAX_TRANSIENT
    _, _, _, _, session = asyncio.run(_flow(responses))
    assert session["status"] == device_login.FAILED
    assert "consecutive network failures" in session["error"]


def test_approved_without_a_token_is_a_failure():
    _, _, _, _, session = asyncio.run(_flow([_start_payload(), {}]))
    assert session["status"] == device_login.FAILED
    assert "no token" in session["error"]


def test_import_failure_is_reported_not_swallowed():
    _, _, _, _, session = asyncio.run(_flow([_start_payload(), {"access_token": "bad"}]))
    assert session["status"] == device_login.FAILED
    assert "realUserID" in session["error"]


def test_cancel_stops_a_pending_session():
    async def scenario():
        responses = [_start_payload()] + [{"error": "authorization_pending"}] * 500
        service, _, _, public, _ = await _flow(responses, timeout=0.2)
        assert public["status"] == device_login.PENDING
        return await service.cancel(public["id"])

    # The cancel has to share the loop that owns the poll task, or the task it
    # reaps is a different one.
    assert asyncio.run(scenario())["status"] == device_login.CANCELED


# ----------------------------------------------------------- credential kinds


def _query_of(target) -> dict[str, str]:
    from urllib.parse import parse_qs, urlsplit

    return {key: value[0] for key, value in parse_qs(urlsplit(target.url).query).items()}


def test_web_profile_claims_to_be_a_browser():
    """A JWT was issued to a browser, so it is presented as one."""
    from app import upstream

    cred = upstream.Credential(token="web.jwt.value", user_id="42", profile=upstream.PROFILE_WEB)
    client = upstream.MiniMaxClient(lambda: _settings())
    target = client.agent_target(cred, "/x", method="GET")
    query = _query_of(target)
    assert query["client"] == "web"
    assert query["region"] == "en"
    assert query["token"] == "web.jwt.value"
    assert query["browser_platform"] == "Win32"
    assert "is_desktop" not in query
    headers = client._headers(_settings(), cred, target)
    assert headers["Authorization" if "Authorization" in headers else "token"] == "web.jwt.value"
    assert "Authorization" not in headers
    assert headers["user-agent"].startswith("Mozilla/5.0")


def test_desktop_profile_claims_to_be_the_app():
    """A device-flow token was issued to the app, so it is presented as the app.

    The shape is copied from the client's own logged requests, `is_desktop`,
    `client=desktop` and the missing `region` included: the fingerprint is part of
    what the credential is, and mixing a Bearer token with browser parameters is
    exactly what a synthetic request looks like.
    """
    from app import upstream

    cred = upstream.Credential(
        bearer="opaque-access-token", user_id="42", profile=upstream.PROFILE_DESKTOP
    )
    client = upstream.MiniMaxClient(lambda: _settings())
    target = client.agent_target(cred, "/x", method="GET")
    query = _query_of(target)
    assert query["client"] == "desktop"
    assert query["is_desktop"] == "1"
    # The desktop *cloud* client adds region=en unconditionally; only the
    # remote-control endpoints omit it, and those are a different client.
    assert query["region"] == "en"
    # The opaque token is never sent as a query parameter; it would make the
    # request a malformed web call rather than a desktop one.
    assert "token" not in query
    assert query["os_name"] == "macOS"
    assert query["browser_platform"] == "MacIntel"

    headers = client._headers(_settings(), cred, target)
    assert headers["Authorization"] == "Bearer opaque-access-token"
    assert "token" not in headers
    # The app's UA carries its own product token; a bare browser UA beside
    # is_desktop=1 is a pair that never occurs in the wild.
    assert headers["user-agent"].startswith("MiniMaxAgent ")

    # Both halves of the signature survive the different shape.
    assert re.fullmatch(r"[0-9a-f]{32}", headers["yy"])
    assert re.fullmatch(r"[0-9a-f]{32}", headers["x-signature"])


def test_credential_of_derives_the_profile_from_the_account_kind():
    from app import upstream
    from app.records import KIND_OAUTH, Account

    web = upstream.credential_of(Account(kind="token", token="a.b.c"))
    assert web.profile == upstream.PROFILE_WEB
    assert web.token == "a.b.c" and web.bearer == ""

    desktop = upstream.credential_of(Account(kind=KIND_OAUTH, token="opaque"))
    assert desktop.profile == upstream.PROFILE_DESKTOP
    assert desktop.token == "" and desktop.bearer == "opaque"


def test_non_stream_calls_sign_the_relative_path():
    """Ordinary calls are signed as paths; only a stream is signed as a URL.

    The app's request builder upgrades the target to an absolute URL only when the
    call streams, because that is the one that has to move to the streaming host.
    yy digests whatever it is handed, so the two disagree in a way the upstream
    reports without naming a field.
    """
    from app import upstream

    cred = upstream.Credential(token="jwt", user_id="42")
    client = upstream.MiniMaxClient(lambda: _settings())
    plain = client.agent_target(cred, "/v1/api/user/info", method="GET")
    assert plain.url.startswith("https://")
    assert not plain.sign_url.startswith("http")
    assert plain.sign_url.startswith("/v1/api/user/info?")

    streamed = client.agent_target(
        cred, "/minimax-cloud/api/v1/session/{session_id}/message",
        method="POST", session_id="s1", stream_host=True,
    )
    assert streamed.sign_url.startswith("https://")
    assert streamed.sign_url == streamed.url


def _settings():
    from app.config import Settings

    return Settings()


# -------------------------------------------------------------------- gateway


def test_token_estimate_counts_cjk_as_one_token_each():
    assert gateway.estimate_tokens("") == 0
    assert gateway.estimate_tokens("abcd") == 1
    assert gateway.estimate_tokens("中") == 1
    assert gateway.estimate_tokens("中文") == 2
