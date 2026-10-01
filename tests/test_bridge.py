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

from app import device_login, gateway, pool, prompt, signin as signin_mod, signing
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


# -------------------------------------------------------------- prompt echoes


def _long_prompt() -> str:
    """A prompt long enough for the shared-prefix echo rule to apply."""
    return "[系统指令] You are opencode, an interactive coding agent. " + ("规则条目。\n" * 200)


def test_strip_prompt_echo_drops_the_replayed_request():
    """The agent replays the request before answering, sharing a long prefix.

    The echo is not byte-identical — the agent rewrites the middle — so the
    opening is matched by the shared-prefix length, not equality.
    """
    from app import upstream

    prompt = _long_prompt()
    # Same leading run, then the agent diverges into the answer.
    echo = prompt[:900] + "\n\nBonjour !"
    assert upstream._strip_prompt_echo(echo, prompt) == "Bonjour !"


def test_strip_prompt_echo_keeps_a_real_answer_that_only_starts_alike():
    from app import upstream

    # Too short a shared prefix: this is an answer, not an echo.
    assert upstream._strip_prompt_echo("你好！有什么可以帮你的？", "hi") == "你好！有什么可以帮你的？"
    # An empty prompt never strips anything.
    assert upstream._strip_prompt_echo("just an answer", "") == "just an answer"


def test_collapse_repeated_tail_only_folds_exact_doubling():
    from app import upstream

    assert upstream._collapse_repeated_tail("Hi! 我是 minimax-m3.1-flash 👋Hi! 我是 minimax-m3.1-flash 👋") == (
        "Hi! 我是 minimax-m3.1-flash 👋"
    )
    # A real answer that merely repeats a phrase is not an exact doubling and survives.
    answer = "这是一个正常长度的回答，包含一些重复的词语，但整体不是整段复制。"
    assert upstream._collapse_repeated_tail(answer) == answer


def test_echo_then_doubling_is_fully_removed():
    """The two defects stack: echo prefix plus a doubled answer."""
    from app import upstream

    prompt = _long_prompt()
    answer = "Bonjour ! Comment ça va ?"
    # The whole answer is repeated back-to-back, which is the shape the upstream
    # emits and the one the fold rule targets.
    raw = prompt[:900] + answer + answer
    assert upstream._collapse_repeated_tail(upstream._strip_prompt_echo(raw, prompt)) == answer


def test_streaming_echo_filter_strips_across_deltas():
    """The stream arrives in pieces, so the echo spans several deltas.

    Nothing is emitted until the divergence is past the threshold, so a client
    never briefly receives the request as if it were the answer.
    """
    from app import upstream

    prompt = _long_prompt()
    filt = upstream.StreamingEchoFilter(prompt)
    emitted = []
    for chunk in (prompt[:400], prompt[400:900], "你好！我是回答。"):
        emitted.append(filt.feed(chunk))
    emitted.append(filt.flush())
    assert "".join(emitted) == "你好！我是回答。"


def test_streaming_echo_filter_passes_a_non_echo_through():
    from app import upstream

    prompt = _long_prompt()
    # The reply diverges from the prompt immediately, so it is an answer and is
    # released as soon as the divergence is seen.
    filt = upstream.StreamingEchoFilter(prompt)
    assert "".join(filt.feed(c) for c in ["Bon", "jour"]) + filt.flush() == "Bonjour"
    # A short prompt cannot produce a long shared prefix, so a reply that merely
    # starts with it is not mistaken for an echo.
    short = upstream.StreamingEchoFilter("hi")
    assert "".join(short.feed(c) for c in ["hi", "! there"]) + short.flush() == "hi! there"


def test_openai_request_converts_to_anthropic():
    """The pure model API is Anthropic-shaped; an OpenAI caller is translated."""
    from app import model_api

    body = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
        ],
        "max_tokens": 100,
        "stream": True,
    }
    out = model_api.openai_to_anthropic(body, "MiniMax-M3.1-Flash-Preview")
    assert out["model"] == "MiniMax-M3.1-Flash-Preview"
    assert out["system"] == "be brief"
    assert out["stream"] is True
    assert out["max_tokens"] == 100
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "user"]
    # No system turn survives: it becomes the top-level system field.
    assert all(m["role"] != "system" for m in out["messages"])


def test_anthropic_message_converts_to_openai_with_tools():
    from app import model_api

    message = {
        "id": "abc",
        "stop_reason": "tool_use",
        "content": [
            {"type": "thinking", "thinking": "considering"},
            {"type": "text", "text": "let me add"},
            {"type": "tool_use", "id": "call_1", "name": "add", "input": {"a": 1, "b": 2}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    out = model_api.anthropic_to_openai(message, "minimax-m3.1-flash")
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "let me add"
    assert choice["message"]["reasoning_content"] == "considering"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "add"
    assert call["id"] == "call_1"
    assert out["usage"]["total_tokens"] == 15


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


def test_cancelled_request_returns_its_account_to_the_pool():
    """A client hanging up must not pin the account at ``max_concurrent``.

    The failure this guards against: the streaming request holds a lease, the
    client disconnects, ``CancelledError`` unwinds past every ``except Exception``
    in the failover loop, and the lease's ``inflight`` counter is never
    decremented.  With a single account at ``max_concurrent=1`` the pool is then
    saturated for the life of the process and every later request waits out the
    capacity timeout before failing — a hang with no error to explain it.
    """
    db, _ = _fake_pool_db(1)
    holder = _SettingsHolder()
    engine = pool.Pool(db, holder.settings)

    async def scenario():
        await engine.load()

        class StuckClient:
            async def llm(self, credential, body, *, stream, timeout=0.0):
                await asyncio.Future()  # never resolves; the request is cancelled

        bridge = gateway.Gateway(db, engine, StuckClient(), None, holder.settings)
        trace = gateway.Trace()
        task = asyncio.create_task(bridge._run_llm({"model": "x", "messages": []}, trace, stream=True))
        await asyncio.sleep(0.05)  # let it take the lease and block
        assert engine.snapshot()[0]["inflight"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The account is free again, so the next turn can use it.
        assert engine.snapshot()[0]["inflight"] == 0
        lease = await engine.acquire()
        await engine.release(lease, success=True)

    asyncio.run(scenario())


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

    async def import_device_token(self, token, name="", region="global", **extra):
        if token == "bad":
            raise RuntimeError("the token carries no realUserID")
        self.imported.append((token, name, region))
        self.last_extra = extra
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


def test_device_login_imports_the_refresh_token_it_was_sent():
    """A browser sign-in must not lose the refresh token.

    The access token answers requests for an hour; the refresh token is the only
    way back to a new one.  An account imported without it looks healthy and then
    dies an hour later with no path back — which is exactly the "网页登录导入"
    account that shows 不可续.
    """
    responses = [
        _start_payload(),
        {
            "access_token": "header.payload.sig",
            "refresh_token": "mmort_abc",
            "expires_in": 3600,
        },
    ]
    _, _, admin, _, session = asyncio.run(_flow(responses, "网页号"))
    assert session["status"] == device_login.AUTHORIZED
    assert admin.last_extra.get("refresh_token") == "mmort_abc"
    assert admin.last_extra.get("expires_in") == 3600


def test_find_refresh_token_reads_nested_envelopes():
    assert device_login.find_refresh_token({"data": {"refreshToken": "r1"}}) == "r1"
    assert device_login.find_refresh_token({"a": [{"refresh_token": "r2"}]}) == "r2"
    assert device_login.find_refresh_token({"access_token": "only"}) == ""
    assert device_login.find_expires_in({"data": {"expires_in": "3600"}}) == 3600.0


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


# --------------------------------------------------------------------- proxies


def test_proxy_scheme_normalises_socks5h():
    """socks5h asks the proxy to resolve the name; httpx always does, so store socks5."""
    from app import proxy

    assert proxy.scheme_of("socks5h://u:p@1.2.3.4:1080") == "socks5"
    assert proxy.scheme_of("SOCKS5://1.2.3.4:1080") == "socks5"
    assert proxy.scheme_of("http://1.2.3.4:8080") == "http"
    assert proxy.scheme_of("ftp://1.2.3.4:21") == ""


def test_proxy_validity_needs_a_scheme_and_a_host():
    """A port is optional (scheme default), a host is not."""
    from app import proxy

    assert proxy.is_valid("http://1.2.3.4:8080")
    assert proxy.is_valid("socks5://user:pass@host.example:1080")
    assert proxy.is_valid("http://1.2.3.4")  # default port
    assert not proxy.is_valid("1.2.3.4:8080")  # no scheme
    assert not proxy.is_valid("http://")  # no host
    assert not proxy.is_valid("")
    assert not proxy.is_valid("socks5://:1080")  # host empty


def test_make_proxy_normalises_the_stored_url():
    from app import proxy

    entry = proxy.make_proxy("socks5h://u:p@1.2.3.4:1080")
    assert entry.url == "socks5://u:p@1.2.3.4:1080"
    assert entry.scheme == "socks5"
    assert entry.id and entry.status == "unknown"


def test_proxy_redact_hides_only_the_password():
    from app import proxy

    assert proxy.redact("socks5://user:secret@1.2.3.4:1080") == "socks5://user:***@1.2.3.4:1080"
    assert proxy.redact("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert proxy.redact("") == ""


def test_selector_rotates_and_skips_dead_entries():
    from app import proxy
    from app.records import PROXY_BAD, PROXY_OK

    a = proxy.make_proxy("http://1.1.1.1:1080")
    b = proxy.make_proxy("http://2.2.2.2:1080")
    c = proxy.make_proxy("http://3.3.3.3:1080")
    b.status = PROXY_BAD  # a failed check is skipped
    c.enabled = False  # a disabled one too

    picker = proxy.Selector("rotate")
    picks = [picker.pick([a, b, c]).id for _ in range(3)]
    assert picks == [a.id, a.id, a.id]

    assert picker.pick([b, c]) is None


def test_selector_single_pins_the_first_usable():
    from app import proxy
    from app.records import PROXY_BAD

    a = proxy.make_proxy("http://1.1.1.1:1080")
    b = proxy.make_proxy("http://2.2.2.2:1080")
    a.status = PROXY_BAD
    picker = proxy.Selector("single")
    assert picker.pick([a, b]).id == b.id


def test_apply_result_keeps_the_fail_streak_honest():
    from app import proxy
    from app.proxy import CheckResult

    entry = proxy.make_proxy("http://1.2.3.4:1080")
    proxy.apply_result(entry, CheckResult(ok=True, exit_ip="9.9.9.9", country="US", latency_ms=120))
    assert entry.status == "ok" and entry.exit_ip == "9.9.9.9" and entry.fail_count == 0

    proxy.apply_result(entry, CheckResult(ok=False, error="timeout"))
    assert entry.status == "bad" and entry.fail_count == 1
    # A later success clears the streak, because "it failed twice then worked" is
    # a working proxy, not a two-thirds-broken one.
    proxy.apply_result(entry, CheckResult(ok=True, exit_ip="9.9.9.9"))
    assert entry.fail_count == 0


class _ProxyDBStub:
    def __init__(self, proxies):
        self._proxies = proxies
        self.ip_counts = {}

    async def list_proxies(self):
        return list(self._proxies)

    async def ip_usage(self, ip):
        return self.ip_counts.get(ip, 0)

    async def update_proxy(self, proxy_id, apply_fn):
        for entry in self._proxies:
            if entry.id == proxy_id:
                apply_fn(entry)
                return entry
        return None

    async def record_ip_use(self, ip):
        self.ip_counts[ip] = self.ip_counts.get(ip, 0) + 1
        return self.ip_counts[ip]


def test_signup_picks_around_a_full_address():
    """An address at its limit is skipped, not reused; the next entry is taken."""
    from app import proxy, signup
    from app.config import SignupSettings

    used = proxy.make_proxy("http://1.1.1.1:1080")
    used.exit_ip = "8.8.8.8"
    fresh = proxy.make_proxy("http://2.2.2.2:1080")
    fresh.exit_ip = "9.9.9.9"
    db = _ProxyDBStub([used, fresh])
    db.ip_counts["8.8.8.8"] = 3

    settings = SignupSettings(per_ip_limit=3, use_proxies=True)
    settings.proxy_strategy = "rotate"
    service = signup.SignupService(None, None, lambda: _signup_holder(settings), db)

    session = signup.SignupSession(id="s", region="global", settings=settings)
    picked, exhausted = asyncio.run(service._pick_proxy(session))
    assert picked is not None and picked.id == fresh.id
    assert exhausted is False


def test_signup_without_a_pool_falls_back_to_the_machine_egress():
    """An empty proxy pool is not a failure — it means "no pool configured".

    Registration then leaves by ``upstream.proxy`` when one is set and directly
    when it is not, instead of refusing the whole batch with "no usable proxy".
    """
    from app import signup
    from app.config import SignupSettings

    class _Settings:
        class upstream:  # noqa: N801 - mirror the settings shape
            proxy = "http://127.0.0.1:7890"

    class _Holder:
        signup = SignupSettings(use_proxies=True)
        upstream = _Settings.upstream

    class _EmptyProxyDB:
        async def list_proxies(self):
            return []

    service = signup.SignupService(None, None, lambda: _Holder(), _EmptyProxyDB())
    session = signup.SignupSession(id="s", region="global", settings=_Holder.signup)
    picked, exhausted = asyncio.run(service._pick_proxy(session))
    assert picked is None
    assert exhausted is False  # empty pool must not abort the batch


def test_signup_start_carries_a_per_run_password():
    """A password typed at the registration panel is used for those accounts.

    It overrides the configured default for this run only, and a blank panel
    leaves the default in place (which may be blank — no password at all).
    """
    from app import signup
    from app.config import SignupSettings

    class _Holder:
        signup = SignupSettings(enabled=True, mail_pass="x", batch_max=5)

    service = signup.SignupService(None, None, lambda: _Holder(), None)

    async def scenario():
        session = await service.start("n", "global", 1, "MyPass9!")
        return service.get(session["id"])

    public = asyncio.run(scenario())
    assert public["status"] == signup.PENDING
    # The stored session carries the override, which _run hands to register_account.
    internal = list(service._sessions.values())[0]
    assert internal.password == "MyPass9!"
    asyncio.run(service.cancel(public["id"]))


def _signup_holder(signup_settings):
    class _Holder:
        def __init__(self, signup):
            self.signup = signup

    return _Holder(signup_settings)


def test_new_account_gets_a_check_in_after_registration():
    """Registration is what creates the account; the claim is what funds it.

    A freshly registered account sits at zero until the check-in endpoint is
    called, and a zero account looks identical to a broken one.  So the signup
    service runs the same check-in the console's manual button runs — and a
    check-in failure must never fail the registration, because the account is
    already in the pool and worth keeping either way.
    """
    from app import signin as signin_mod
    from app import signup
    from app.config import SignupSettings

    class _DBStub:
        def __init__(self):
            self.accounts = {}

        async def account_by_id(self, account_id):
            return self.accounts.get(account_id)

    class _SigninStub:
        def __init__(self, *, fail=False):
            self.calls = []
            self.fail = fail

        async def run_account(self, account):
            self.calls.append(account.id)
            if self.fail:
                raise RuntimeError("check-in endpoint refused")
            return signin_mod.Outcome(account_id=account.id, status="ok")

    async def scenario(fail):
        db = _DBStub()
        account = type("A", (), {"id": "acct-1", "name": "mmx"})()
        db.accounts["acct-1"] = account
        signin = _SigninStub(fail=fail)
        service = signup.SignupService(
            None, None, lambda: _signup_holder(SignupSettings()), db, signin
        )
        session = signup.SignupSession(id="s", region="global", settings=SignupSettings())
        await service._signin_new_account(session, {"id": "acct-1"})
        return signin.calls

    assert asyncio.run(scenario(False)) == ["acct-1"]
    # A failure is swallowed: registration is not undone by a missed claim.
    assert asyncio.run(scenario(True)) == ["acct-1"]


def test_credit_refresh_reads_stale_but_not_fresh_accounts():
    """The five-minute pass costs one read per stale account, no more.

    A pool polled in full every cycle is a burst of requests against a
    risk-control endpoint for numbers that mostly have not moved, so an account
    whose snapshot is still fresh is skipped.  A named account is always read:
    that is the console's "refresh now", which wants a live number.

    Disabled accounts are read too.  An account is turned off to park it, not to
    make its balance lie, and the number decides whether it is worth turning on.
    """
    import time as _time

    from app import signin as signin_mod
    from app.config import SigninSettings
    from app.records import REGION_CN, STATUS_ACTIVE, Account, Credit

    now = _time.time()

    class _DB:
        def __init__(self):
            self.accounts = {
                "fresh": Account(id="fresh", enabled=True, status=STATUS_ACTIVE,
                                 credit=Credit(total=10, synced_at=now)),
                "stale": Account(id="stale", enabled=True, status=STATUS_ACTIVE,
                                 credit=Credit(total=20, synced_at=now - 3600)),
                "never": Account(id="never", enabled=True, status=STATUS_ACTIVE),
                # Disabled, but still worth a read: parked, not hidden.
                "off": Account(id="off", enabled=False, status=STATUS_ACTIVE),
                # The mainland deployment has no such endpoint to read.
                "cn": Account(id="cn", enabled=True, status=STATUS_ACTIVE, region=REGION_CN),
            }
            self.saved = []

        async def list_accounts(self):
            return list(self.accounts.values())

        async def save_account_state(self, account_id, apply_fn):
            account = self.accounts[account_id]
            apply_fn(account)
            self.saved.append(account_id)
            return account

    class _Client:
        def __init__(self):
            self.reads = []

        async def credit(self, credential):
            self.reads.append(credential.token)

            class _Info:
                total = 999.0
                free = 999.0
                purchased = 0.0
                plan_name = ""
                plan_type = 1

            return _Info()

    settings = SigninSettings(credit_refresh_min=5, gap_seconds=0)

    class _Holder:
        signin = settings

    db = _DB()
    client = _Client()

    class _Pool:
        async def note_saved(self, account):
            return None

    service = signin_mod.SigninService(db, _Pool(), client, lambda: _Holder())

    summary = asyncio.run(service.refresh_credits())
    assert summary["refreshed"] == 3  # stale + never + disabled
    assert summary["skipped"] == 2  # fresh + cn
    assert set(db.saved) == {"stale", "never", "off"}
    assert db.accounts["fresh"].credit.total == 10  # untouched

    # A named account is read even though its snapshot is fresh.
    db.saved.clear()
    asyncio.run(service.refresh_credits(["fresh"]))
    assert db.saved == ["fresh"]


def test_signin_rereads_balance_after_a_successful_claim():
    """The balance is read before the claim, so it must be read again after.

    Otherwise the console keeps showing the pre-claim figure and the account
    looks like the check-in did nothing.
    """
    from app import signin as signin_mod
    from app import upstream
    from app.config import SigninSettings
    from app.records import STATUS_ACTIVE, Account

    class _DB:
        def __init__(self):
            self.account = Account(id="a", enabled=True, status=STATUS_ACTIVE)

        async def save_account_state(self, account_id, apply_fn):
            apply_fn(self.account)
            return self.account

        async def account_by_id(self, account_id):
            return self.account

    class _Pool:
        async def note_saved(self, account):
            return None

    class _Client:
        def __init__(self):
            self.credit_calls = 0

        async def prepare(self, credential):
            class _P:
                agents = []

                def resolve_agent_id(self, current):
                    return current, False

            return _P()

        async def signin_status(self, credential):
            return upstream.SigninPanel(
                scene=1,
                days=[upstream.SigninDay(day_no=1, points=800, status=2, is_today=True)],
            )

        async def signin_claim(self, credential):
            return upstream.SigninClaim(claim_id=1, result=1, day_no=1, points=800)

        async def credit_grants(self, credential):
            import time as _t

            # A grant that just arrived, so the claim counts as paid rather than
            # "unpaid" (the endpoint's success is not itself proof).
            return [
                upstream.CreditGrant(
                    granted_at=int(_t.time() * 1000),
                    expires_at=0,
                    granted=800.0,
                    remaining=800.0,
                )
            ]

        async def credit(self, credential):
            self.credit_calls += 1

            class _Info:
                total = 1600
                free = 1600
                purchased = 0
                plan_name = ""
                plan_type = 1

            return _Info()

    class _Holder:
        signin = SigninSettings(timeout_sec=5, gap_seconds=0)

    db = _DB()
    client = _Client()
    service = signin_mod.SigninService(db, _Pool(), client, lambda: _Holder())
    outcome = asyncio.run(service.run_account(db.account))

    assert outcome.status == signin_mod.SIGNIN_OK
    # Read once before the claim, once after to pick up the points.
    assert client.credit_calls == 2
    assert db.account.credit.total == 1600


# -------------------------------------------------------- env config + masking


def test_env_parses_dotenv_and_lets_real_vars_win(tmp_path):
    from app import env

    path = tmp_path / ".env"
    path.write_text(
        "# a comment\nexport MINIMAX2API_SIGNUP__MAIL_DOMAIN=\"quoted.example\"\n"
        "MINIMAX2API_SIGNUP__MAIL_PASS='single-quoted'\nPLAIN=value\n",
        encoding="utf-8",
    )
    parsed = env.load_dotenv(path)
    assert parsed["MINIMAX2API_SIGNUP__MAIL_DOMAIN"] == "quoted.example"
    assert parsed["MINIMAX2API_SIGNUP__MAIL_PASS"] == "single-quoted"
    assert parsed["PLAIN"] == "value"

    merged = env.environ(dotenv=path, refresh=True)
    assert merged["MINIMAX2API_SIGNUP__MAIL_DOMAIN"] == "quoted.example"


def test_env_applies_over_settings_and_reports_paths():
    from app import config, env

    settings = config.default_settings("./data")
    applied = env.apply_env(
        settings,
        {
            "MINIMAX2API_SIGNUP__MAIL_PASS": "envsecret",
            "MINIMAX2API_SIGNUP__PER_IP_LIMIT": "7",
            "MINIMAX2API_SIGNUP__USE_PROXIES": "false",
            "MINIMAX2API_UPSTREAM__PROXY": "http://1.2.3.4:8080",
        },
    )
    assert settings.signup.mail_pass == "envsecret"
    assert settings.signup.per_ip_limit == 7
    assert settings.signup.use_proxies is False
    assert settings.upstream.proxy == "http://1.2.3.4:8080"
    assert "signup.mail_pass" in applied


def test_env_refuses_a_bad_number_rather_than_zeroing_it():
    """A zero would be indistinguishable from an intentional zero."""
    from app import config, env

    settings = config.default_settings("./data")
    env.apply_env(settings, {"MINIMAX2API_SIGNUP__PER_IP_LIMIT": "not-a-number"})
    assert settings.signup.per_ip_limit == 3


def test_secrets_are_masked_and_flagged():
    from app import config

    settings = config.default_settings("./data")
    settings.signup.mail_pass = "REAL"
    settings.upstream.proxy = "socks5://u:p@1.2.3.4:1080"
    view = config.to_dict_masked(settings, {"signup.mail_pass"})
    assert view["signup"]["mail_pass"] == config.SECRET_MASK
    assert view["upstream"]["proxy"] == config.SECRET_MASK
    assert view["signup"]["mail_base"] == settings.signup.mail_base  # not a secret
    # ``signup.password`` defaults to a non-empty value, so it is masked too.
    assert set(view["_secrets"]) == {"signup.mail_pass", "signup.password", "upstream.proxy"}
    assert view["_locked"] == ["signup.mail_pass"]


def test_saving_the_mask_back_does_not_destroy_the_secret():
    """The console round-trips the masked value on save."""
    from app import config

    settings = config.default_settings("./data")
    settings.signup.mail_pass = "REAL"
    config.apply_update(settings, {"signup": {"mail_pass": config.SECRET_MASK}})
    assert settings.signup.mail_pass == "REAL"

    # An explicit empty string is a clear, and is honoured.
    config.apply_update(settings, {"signup": {"mail_pass": ""}})
    assert settings.signup.mail_pass == ""


def test_locked_fields_follow_the_prefixed_namespace():
    from app import env

    locked = env.locked_fields(
        {
            "MINIMAX2API_SIGNUP__MAIL_PASS": "x",
            "MINIMAX2API_UPSTREAM__PROXY": "y",
            "UNRELATED": "z",
        }
    )
    assert locked == {"signup.mail_pass", "upstream.proxy"}


# ------------------------------------------------- email/password on accounts


def test_account_view_masks_the_password_and_exposes_the_email():
    from app import config
    from app.admin import AccountInput
    from app.db import account_view
    from app.records import KIND_OAUTH

    item = AccountInput(
        token="mmoat_x", name="n", kind=KIND_OAUTH, email="a@b.cc", password="s3cret"
    )
    account = item.account()
    assert account.email == "a@b.cc"
    assert account.password == "s3cret"

    view = account_view(account, 0).to_json()
    assert view["email"] == "a@b.cc"
    # the plaintext password never rides along with the list
    assert "password" not in view
    assert view["passwordMasked"] == "•" * 8


def test_mask_password_reports_presence_not_length():
    from app.records import mask_password

    assert mask_password("") == ""
    assert mask_password("short") == "•" * 8
    assert mask_password("a-very-long-password-indeed") == "•" * 8


def test_old_database_gains_the_new_columns(tmp_path):
    """CREATE TABLE IF NOT EXISTS does not add columns to a table that exists."""
    import sqlite3

    from app.db import _migrate

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, identifier TEXT, user_id TEXT)"
    )
    _migrate(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert "email" in columns and "password" in columns
    # idempotent: running it again is not an error
    _migrate(conn)
    conn.close()


# ------------------------------------------------------------------- keepalive


def test_refresh_grant_posts_form_and_keeps_the_rotated_token():
    """The refresh call is form-encoded and the server rotates the token."""
    from app import keepalive

    seen = {}

    class _Response:
        status_code = 200

        def json(self):
            return {
                "access_token": "mmoat_new",
                "expires_in": 3600,
                "refresh_token": "mmort_new",
                "token_type": "Bearer",
            }

    class _Client:
        async def post(self, url, data=None, headers=None, timeout=None):
            seen["url"] = url
            seen["data"] = data
            seen["headers"] = headers
            return _Response()

    result = asyncio.run(
        keepalive.refresh_with_token(_Client(), "https://account.minimax.io", "mmort_old")
    )
    assert result.ok and result.method == "refresh"
    assert result.token == "mmoat_new"
    # rotation is stored, because keeping the old one works until it does not
    assert result.refresh_token == "mmort_new"
    assert seen["data"]["grant_type"] == "refresh_token"
    assert seen["data"]["client_id"] == "mcode-public"
    # app_id belongs to the browser's authorize call, not the token endpoint
    assert "app_id" not in seen["data"]
    assert seen["headers"]["content-type"] == "application/x-www-form-urlencoded"


def test_refused_refresh_is_an_answer_not_a_transport_failure():
    from app import keepalive

    class _Response:
        def json(self):
            return {"error": "invalid_grant", "error_description": "refresh token expired"}

    class _Client:
        async def post(self, *a, **k):
            return _Response()

    result = asyncio.run(
        keepalive.refresh_with_token(_Client(), "https://account.minimax.io", "mmort_dead")
    )
    assert not result.ok
    assert not result.transient
    assert "expired" in result.error


def test_transport_failure_is_marked_transient():
    """A network error must not read as a dead credential."""
    import httpx

    from app import keepalive

    class _Client:
        async def post(self, *a, **k):
            raise httpx.ConnectError("proxy unreachable")

    result = asyncio.run(
        keepalive.refresh_with_token(_Client(), "https://account.minimax.io", "mmort_x")
    )
    assert not result.ok
    assert result.transient


def test_renew_without_credentials_reports_what_is_missing():
    from app import keepalive
    from app.records import KIND_OAUTH, Account

    account = Account(kind=KIND_OAUTH, email="", password="")
    from app.config import SignupSettings

    result = asyncio.run(
        keepalive.renew_with_password(None, account, SignupSettings())
    )
    assert not result.ok
    assert "email/password" in result.error


def test_sweep_skips_accounts_that_cannot_be_renewed(tmp_path):
    """An unrenewable JWT is not a failure; it just has no path back."""
    from app import keepalive, config
    from app.records import Account, KIND_OAUTH, KIND_TOKEN

    class _DB:
        async def list_accounts(self):
            return [
                Account(id="a", kind=KIND_OAUTH, refresh_token="", email="", password=""),
                Account(id="b", kind=KIND_TOKEN, region="global"),
            ]

    keeper = keepalive.Keeper(_DB(), None, lambda: config.default_settings("./data"))
    summary = asyncio.run(keeper.sweep())
    assert summary["skipped"] == 1   # the oauth one with no way to renew
    assert summary["due"] == 0       # the token-kind account is not considered
    assert summary["failed"] == 0    # and neither is reported as a failure


def test_sweep_only_renews_enabled_accounts():
    """A parked account is not spending tokens, so it is not renewed.

    Renewing a disabled account spends a request from the same address on a
    credential nobody is using, and pushes back the renewals of the accounts that
    are actually serving traffic.  Enabled accounts with no expiry recorded are
    always due: that is how a freshly imported account gets its first keep-alive.
    """
    from app import config, keepalive
    from app.records import Account, KIND_OAUTH

    class _DB:
        async def list_accounts(self):
            return [
                Account(id="on", kind=KIND_OAUTH, enabled=True, email="a@x", password="p"),
                Account(id="off", kind=KIND_OAUTH, enabled=False, email="b@x", password="p"),
            ]

    keeper = keepalive.Keeper(_DB(), None, lambda: config.default_settings("./data"))
    summary = asyncio.run(keeper.sweep())
    assert summary["due"] == 1  # the enabled one only
    # `off` was not even looked at, so it is not in the skipped tally either.
    assert summary["skipped"] == 0


# ------------------------------------------------- check-in renews on a 401


class _RenewableSigninHarness:
    """Signin whose upstream answers 401 until the credential is renewed.

    The point of the fake is the *transition*: a stale token is refused on every
    call, and only after the renewer swaps it does the account work.  That is
    what a one-hour OAuth token looks like when a claim fires late.
    """

    def __init__(self, *, renewable=True):
        from app.records import STATUS_ACTIVE, Account

        self.account = Account(id="a", name="a", enabled=True, status=STATUS_ACTIVE, token="old")
        self.notes = []
        self._renewable = renewable
        self.renew_calls = 0
        self.client = _StaleUntilRenewedClient(self)

        class _DB:
            async def save_account_state(_self, account_id, apply_fn):
                apply_fn(self.account)
                return self.account

            async def account_by_id(_self, account_id):
                return self.account

        class _Pool:
            async def note_saved(_self, account):
                # Snapshot the token: the point is that the pool is told with the
                # *renewed* credential, not merely told at all.
                self.notes.append((account.id, account.token))

        class _Holder:
            from app.config import SigninSettings

            signin = SigninSettings(timeout_sec=5, gap_seconds=0)

        from app import signin as signin_mod

        self.service = signin_mod.SigninService(_DB(), _Pool(), self.client, lambda: _Holder())

        async def _renewer(account_id):
            self.renew_calls += 1
            if not self._renewable:
                return None
            self.account.token = "new"

            class _R:
                ok = True

            return _R()

        self.service.renewer = _renewer


class _StaleUntilRenewedClient:
    """Every authenticated call refuses a token that has not been renewed."""

    def __init__(self, harness):
        self.harness = harness

    async def credit(self, credential):
        self._gate(credential)
        return _CreditInfo()

    async def prepare(self, credential):
        self._gate(credential)

        class _P:
            agents = []

            def resolve_agent_id(self, current):
                return current, False

        return _P()

    async def signin_status(self, credential):
        self._gate(credential)
        from app import upstream

        return upstream.SigninPanel(
            scene=1,
            days=[upstream.SigninDay(day_no=1, points=800, status=0, is_today=True)],
        )

    async def signin_claim(self, credential):
        self._gate(credential)
        from app import upstream

        return upstream.SigninClaim(claim_id=1, result=1, day_no=1, points=800)

    async def credit_grants(self, credential):
        import time as _t

        from app import upstream

        return [
            upstream.CreditGrant(
                granted_at=int(_t.time() * 1000), expires_at=0, granted=800.0, remaining=800.0
            )
        ]

    def _gate(self, credential):
        from app import upstream

        if credential.token == "old":
            raise upstream.InvalidCredential("minimax agent list HTTP 401")


class _CreditInfo:
    total = 800.0
    free = 800.0
    purchased = 0.0
    plan_name = ""
    plan_type = 1


def test_checkin_renews_once_and_succeeds_after_a_401():
    """An expired token is renewed, then the claim runs — not reported as failure."""
    h = _RenewableSigninHarness()
    outcome = asyncio.run(h.service.run_account(h.account))
    assert outcome.status == signin_mod.SIGNIN_OK
    assert h.renew_calls == 1
    # The pool cache has to be told, or the next request replays the dead token.
    assert h.account.token == "new"
    assert ("a", "new") in h.notes


def test_checkin_reports_failure_when_not_renewable():
    """No renewal path means a real failure, not a silent one."""
    h = _RenewableSigninHarness(renewable=False)
    outcome = asyncio.run(h.service.run_account(h.account))
    assert outcome.status == signin_mod.SIGNIN_FAILED
    assert "not renewable" in outcome.reason


def test_checkin_reports_failure_when_renewed_token_is_still_refused():
    """A credential refused *after* renewal is the genuine dead-account case."""
    h = _RenewableSigninHarness()

    async def _fake_renew_but_still_bad(account_id):
        h.renew_calls += 1

        class _R:
            ok = True

        return _R()

    # The renewer claims success but leaves the token stale, so the retry 401s.
    h.service.renewer = _fake_renew_but_still_bad
    outcome = asyncio.run(h.service.run_account(h.account))
    assert outcome.status == signin_mod.SIGNIN_FAILED
    assert "even after renewal" in outcome.reason


def test_mainland_account_skips_before_any_credential_check():
    """A CN account is never renewed for check-in; there is nothing to check into."""
    h = _RenewableSigninHarness()
    from app.records import REGION_CN

    h.account.region = REGION_CN
    outcome = asyncio.run(h.service.run_account(h.account))
    assert outcome.status == signin_mod.SIGNIN_SKIPPED
    assert h.renew_calls == 0
