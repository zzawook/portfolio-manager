import base64
import json
import shutil
import stat
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from rebalancer.assistant import TOOLS, known_tool_name
from rebalancer.chatgpt_auth import (
    AUTHORIZE_URL,
    DYNAMIC_CLIENT,
    PLAN_SCOPE,
    RESOURCE,
    TOKEN_URL,
    ChatGPTAuth,
    ChatGPTAuthError,
)
from rebalancer.config import AssistantSettings
from rebalancer.llm import ChatGPTPlanProvider, ProviderError, ToolCall, make_provider, plan_input
from rebalancer.server import App, create_app

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeResp:
    def __init__(self, payload=None, status=200, lines=None):
        self.payload = payload or {}
        self.status_code = status
        self.lines = lines or []
        self.text = json.dumps(self.payload)
        self.closed = False

    def json(self):
        return self.payload

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


class FakeAuthSession:
    """Plays the ChatGPT token endpoint."""

    def __init__(self):
        self.posts = []
        self.responses = []

    def post(self, url, data=None, headers=None, timeout=None, json=None, stream=None):
        self.posts.append({"url": url, "data": data})
        return self.responses.pop(0)

    def get(self, url, timeout=None):
        return FakeResp({"revocation_endpoint": "https://auth.openai.com/oauth/revoke"})


def token_payload(client_id="oaiapp_123", nonce="", scope="openid email offline_access resource.invoke " + PLAN_SCOPE):
    return {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "id_token": jwt({"iss": "https://auth.openai.com", "aud": client_id, "nonce": nonce, "exp": NOW + 3600, "sub": "user-1", "email": "me@example.com"}),
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": scope,
    }


def sign_in(tmp_path, scope=None):
    session = FakeAuthSession()
    auth = ChatGPTAuth(tmp_path / "chatgpt-auth.json", session=session, clock=lambda: NOW)
    url = auth.start("http://127.0.0.1:8765/auth/callback")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    payload = token_payload(nonce=q["nonce"]) if scope is None else token_payload(nonce=q["nonce"], scope=scope)
    session.responses.append(FakeResp(payload))
    auth.finish({"code": "code-1", "state": q["state"], "client_id": "oaiapp_123"})
    return auth, session, q


# Sign-in ---------------------------------------------------------------------------


def test_authorize_url_follows_the_documented_parameters(tmp_path):
    auth = ChatGPTAuth(tmp_path / "chatgpt-auth.json", session=FakeAuthSession(), clock=lambda: NOW)
    url = auth.start("http://127.0.0.1:8765/auth/callback")
    assert url.startswith(AUTHORIZE_URL + "?")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["client_id"] == DYNAMIC_CLIENT and q["agent_name_hint"] == "portfolio-manager"
    assert q["redirect_uri"] == "http://127.0.0.1:8765/auth/callback"
    assert q["scope"].split() == ["openid", "profile", "email", "offline_access", "resource.invoke", PLAN_SCOPE]
    assert q["resource"] == RESOURCE and q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["ext_agent_host_id"].startswith("urn:uuid:")
    assert q["state"] != q["nonce"]


def test_finish_exchanges_the_code_and_stores_tokens_privately(tmp_path):
    auth, session, q = sign_in(tmp_path)
    form = session.posts[0]
    assert form["url"] == TOKEN_URL
    assert form["data"]["grant_type"] == "authorization_code"
    assert form["data"]["client_id"] == "oaiapp_123"  # the issued id, never dynamic_agent_client
    assert form["data"]["redirect_uri"] == "http://127.0.0.1:8765/auth/callback"
    assert form["data"]["resource"] == RESOURCE and form["data"]["code_verifier"]
    assert auth.plan_ready and auth.status()["email"] == "me@example.com"
    path = tmp_path / "chatgpt-auth.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["client_id"] == "oaiapp_123" and saved["host_id"] == q["ext_agent_host_id"]
    # A second sign-in reuses the issued client id and the same host id.
    q2 = {k: v[0] for k, v in parse_qs(urlparse(auth.start("http://127.0.0.1:8765/auth/callback")).query).items()}
    assert q2["client_id"] == "oaiapp_123" and "agent_name_hint" not in q2
    assert q2["ext_agent_host_id"] == q["ext_agent_host_id"] and q2["id_token_hint"]


def test_unknown_or_replayed_state_is_refused(tmp_path):
    auth, session, q = sign_in(tmp_path)
    with pytest.raises(ChatGPTAuthError, match="찾을 수 없어요"):
        auth.finish({"code": "x", "state": q["state"]})  # already used
    with pytest.raises(ChatGPTAuthError):
        auth.finish({"code": "x", "state": "forged"})


def test_declined_sign_in_and_missing_registration(tmp_path):
    session = FakeAuthSession()
    auth = ChatGPTAuth(tmp_path / "a.json", session=session, clock=lambda: NOW)
    q = {k: v[0] for k, v in parse_qs(urlparse(auth.start("http://127.0.0.1:1/auth/callback")).query).items()}
    with pytest.raises(ChatGPTAuthError, match="취소"):
        auth.finish({"error": "access_denied", "state": q["state"]})
    q = {k: v[0] for k, v in parse_qs(urlparse(auth.start("http://127.0.0.1:1/auth/callback")).query).items()}
    with pytest.raises(ChatGPTAuthError, match="등록"):
        auth.finish({"code": "c", "state": q["state"]})  # no issued client_id
    assert session.posts == []  # no code was exchanged


def test_id_token_nonce_mismatch_is_refused(tmp_path):
    session = FakeAuthSession()
    auth = ChatGPTAuth(tmp_path / "a.json", session=session, clock=lambda: NOW)
    q = {k: v[0] for k, v in parse_qs(urlparse(auth.start("http://127.0.0.1:1/auth/callback")).query).items()}
    session.responses.append(FakeResp(token_payload(nonce="something-else")))
    with pytest.raises(ChatGPTAuthError, match="nonce"):
        auth.finish({"code": "c", "state": q["state"], "client_id": "oaiapp_123"})
    assert not auth.signed_in


def test_sign_in_without_plan_scope_keeps_chat_off(tmp_path):
    auth, _, _ = sign_in(tmp_path, scope="openid email offline_access")
    assert auth.signed_in and not auth.plan_ready
    assert "플랜 사용 권한" in auth.status()["reason"]


def test_access_token_refreshes_near_expiry_and_rotates(tmp_path):
    clock = {"t": NOW}
    auth, session, _ = sign_in(tmp_path)
    auth.clock = lambda: clock["t"]
    assert auth.access_token() == "at-1"
    clock["t"] = NOW + 3590  # inside the refresh margin
    session.responses.append(FakeResp({"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}))
    assert auth.access_token() == "at-2"
    form = session.posts[-1]["data"]
    assert form == {"grant_type": "refresh_token", "client_id": "oaiapp_123", "refresh_token": "rt-1", "resource": RESOURCE}
    assert json.loads((tmp_path / "chatgpt-auth.json").read_text())["refresh_token"] == "rt-2"


def test_rejected_refresh_after_expiry_asks_for_a_new_sign_in(tmp_path):
    clock = {"t": NOW}
    auth, session, _ = sign_in(tmp_path)
    auth.clock = lambda: clock["t"]
    clock["t"] = NOW + 4000  # expired
    session.responses.append(FakeResp({"error": "invalid_grant"}, status=400))
    with pytest.raises(ChatGPTAuthError, match="다시"):
        auth.access_token()
    assert not auth.signed_in
    saved = json.loads((tmp_path / "chatgpt-auth.json").read_text())
    assert "refresh_token" not in saved and saved["client_id"] == "oaiapp_123"  # kept for the next sign-in


def test_sign_out_revokes_and_forgets(tmp_path):
    auth, session, _ = sign_in(tmp_path)
    session.responses.append(FakeResp(status=200))
    assert auth.sign_out() is True
    assert session.posts[-1]["data"] == {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": "oaiapp_123"}
    assert not auth.signed_in


# The plan provider ---------------------------------------------------------------------


def sse(*events):
    lines = []
    for e in events:
        lines += [f"event: {e['type']}", f"data: {json.dumps(e)}", ""]
    return lines


class FakeApiSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json=None, headers=None, stream=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "stream": stream})
        return self.responses.pop(0)


def test_plan_request_shape_and_tool_call(tmp_path):
    auth, _, _ = sign_in(tmp_path)
    output = [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "function_call", "id": "fc_1", "status": "completed", "call_id": "call_1", "namespace": "portfolio", "name": "get_portfolio", "arguments": "{}"},
    ]
    api = FakeApiSession(
        FakeResp(lines=sse({"type": "response.created"}, {"type": "response.completed", "response": {"status": "completed", "output": output}}))
    )
    provider = ChatGPTPlanProvider(auth, "gpt-6.1-sol", session=api)
    reply = provider.complete("규칙", [{"role": "user", "text": "안녕"}], TOOLS)
    post = api.posts[0]
    assert post["url"] == "https://api.openai.com/v1/responses" and post["stream"] is True
    assert post["headers"]["Authorization"] == "Bearer at-1"
    body = post["json"]
    assert body["store"] is False and body["stream"] is True and body["instructions"] == "규칙"
    assert "max_output_tokens" not in body and "temperature" not in body
    assert isinstance(body["input"], list) and body["input"][0]["role"] == "user"
    ns = body["tools"][0]
    assert ns["type"] == "namespace" and ns["name"] == "portfolio" and ns["description"]
    assert {t["name"] for t in ns["tools"]} == {t["name"] for t in TOOLS}
    assert [(c.id, c.name) for c in reply.calls] == [("call_1", "get_portfolio")]
    # Replayed without server ids or reasoning items (nothing is stored with store=false).
    assert reply.raw == [{"type": "function_call", "call_id": "call_1", "name": "get_portfolio", "arguments": "{}", "namespace": "portfolio"}]


def test_plan_history_replay_order():
    history = [
        {"role": "user", "text": "안녕"},
        {"role": "assistant", "text": "", "calls": [ToolCall("call_1", "get_portfolio", {})], "raw": None},
        {"role": "tool", "results": [{"call_id": "call_1", "name": "get_portfolio", "output": "{}", "is_error": False}]},
    ]
    items = plan_input(history)
    assert [i["type"] for i in items] == ["message", "function_call", "function_call_output"]
    assert all("id" not in i for i in items)


def test_plan_usage_limit_is_explained(tmp_path):
    auth, _, _ = sign_in(tmp_path)
    failed = {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}
    provider = ChatGPTPlanProvider(auth, "m", session=FakeApiSession(FakeResp(lines=sse(failed))))
    with pytest.raises(ProviderError, match="한도"):
        provider.complete("s", [{"role": "user", "text": "x"}], TOOLS)


def test_plan_http_errors_use_the_error_code(tmp_path):
    auth, _, _ = sign_in(tmp_path)
    unavailable = FakeResp({"error": {"code": "subscription_sharing_usage_unavailable", "message": "x"}}, status=503)
    provider = ChatGPTPlanProvider(auth, "m", session=FakeApiSession(unavailable))
    with pytest.raises(ProviderError, match="잠시 뒤"):
        provider.complete("s", [{"role": "user", "text": "x"}], TOOLS)
    limited = FakeResp({"error": {"code": "subscription_sharing_usage_limit_exceeded"}}, status=429)
    provider = ChatGPTPlanProvider(auth, "m", session=FakeApiSession(limited))
    with pytest.raises(ProviderError, match="Usage"):
        provider.complete("s", [{"role": "user", "text": "x"}], TOOLS)
    assert auth.signed_in  # neither error signs the user out


def test_expired_token_is_refreshed_next_time(tmp_path):
    auth, _, _ = sign_in(tmp_path)
    provider = ChatGPTPlanProvider(auth, "m", session=FakeApiSession(FakeResp(status=401)))
    with pytest.raises(ProviderError, match="로그인"):
        provider.complete("s", [{"role": "user", "text": "x"}], TOOLS)
    assert auth.data["expires_at"] == 0


def test_namespaced_tool_names_are_understood():
    assert known_tool_name("portfolio.set_allocation") == "set_allocation"
    assert known_tool_name("portfolio__preview_orders") == "preview_orders"
    assert known_tool_name("get_portfolio") == "get_portfolio"
    assert known_tool_name("send_orders") == "send_orders"  # unknown stays unknown


def test_make_provider_for_chatgpt_needs_no_key(tmp_path):
    auth = ChatGPTAuth(tmp_path / "a.json", session=FakeAuthSession(), clock=lambda: NOW)
    provider, reason = make_provider(AssistantSettings(provider="chatgpt"), environ={}, auth=auth)
    assert reason == "" and provider.label == "ChatGPT" and provider.model == "gpt-6.1-sol"
    assert provider.ready is False


# The web screen ----------------------------------------------------------------------------


def test_login_callback_and_chat_over_http(tmp_path):
    for name in ("config.yaml", "portfolio.yaml"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    session = FakeAuthSession()
    auth = ChatGPTAuth(tmp_path / "chatgpt-auth.json", session=session, clock=lambda: NOW)
    provider = ChatGPTPlanProvider(auth, "gpt-6.1-sol")
    ctx = App(tmp_path / "config.yaml", mock_file=str(tmp_path / "examples" / "month14.yaml"), today=date(2028, 2, 3), provider=provider, port=8765)
    client = TestClient(create_app(ctx, {"testserver", "127.0.0.1:8765"}))
    client.headers.update({"X-Session-Token": ctx.token})

    chat = client.get("/api/state").json()["chat"]
    assert chat["enabled"] is False and chat["auth"]["signed_in"] is False
    assert chat["callback"] == "http://127.0.0.1:8765/auth/callback"
    assert client.post("/api/chat", json={"message": "안녕"}).status_code == 503

    url = client.post("/api/chatgpt/login").json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    session.responses.append(FakeResp(token_payload(nonce=q["nonce"])))
    bare = TestClient(create_app(ctx, {"127.0.0.1:8765"}), base_url="http://127.0.0.1:8765")
    res = bare.get("/auth/callback", params={"code": "c", "state": q["state"], "client_id": "oaiapp_123"}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/?chatgpt=ok"
    chat = client.get("/api/state").json()["chat"]
    assert chat["enabled"] is True and chat["auth"]["email"] == "me@example.com"

    bad = bare.get("/auth/callback", params={"code": "c", "state": "forged"})
    assert bad.status_code == 400 and "로그인" in bad.text
