import os
import shutil
import stat
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rebalancer.brokers.ibkr import IbkrError
from rebalancer.cli import set_password
from rebalancer.server import App, create_app
from rebalancer.webauth import (
    CLIENT_FAILURES,
    COOKIE_NAME,
    IDLE_HOURS,
    LOCK_SECONDS,
    MAX_DAYS,
    TOTAL_FAILURES,
    LoginError,
    WebAuth,
    check_password,
    hash_password,
)

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct horse battery"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auth(tmp_path, clock):
    a = WebAuth(tmp_path / "web-auth.json", clock=clock)
    a.set_password(PASSWORD)
    return a


# The password store ----------------------------------------------------------------


def test_hash_round_trip_and_file_mode(tmp_path, auth):
    record = hash_password("abcdefghijk")
    assert check_password("abcdefghijk", record) and not check_password("abcdefghijx", record)
    assert stat.S_IMODE(os.stat(auth.path).st_mode) == 0o600
    assert PASSWORD not in auth.path.read_text()


def test_short_password_is_refused(tmp_path):
    with pytest.raises(LoginError):
        WebAuth(tmp_path / "w.json").set_password("short")


def test_login_and_session_lifetime(auth, clock):
    sid = auth.login(PASSWORD, "1.2.3.4")
    assert auth.check(sid)
    clock.now += IDLE_HOURS * 3600 - 10
    assert auth.check(sid)  # used in time: still valid, and the idle timer restarts
    clock.now += IDLE_HOURS * 3600 + 10
    assert not auth.check(sid)  # unused too long


def test_session_has_a_hard_limit(auth, clock):
    sid = auth.login(PASSWORD, "c")
    for _ in range(MAX_DAYS * 24 // (IDLE_HOURS - 1) + 2):
        clock.now += (IDLE_HOURS - 1) * 3600
        auth.check(sid)
    assert not auth.check(sid)


def test_new_password_signs_everyone_out(auth):
    sid = auth.login(PASSWORD, "c")
    auth.set_password("another long password")
    assert not auth.check(sid)
    with pytest.raises(LoginError):
        auth.login(PASSWORD, "c")


def test_password_changed_by_another_process_signs_out(auth, tmp_path, clock):
    sid = auth.login(PASSWORD, "c")
    WebAuth(auth.path, clock=clock).set_password("changed elsewhere 123")
    assert not auth.check(sid)


def test_one_client_is_locked_after_repeated_failures(auth, clock):
    for _ in range(CLIENT_FAILURES):
        with pytest.raises(LoginError, match="맞지 않아요"):
            auth.login("wrong password", "attacker")
    with pytest.raises(LoginError, match="너무 많아요"):
        auth.login(PASSWORD, "attacker")  # even the right password waits
    assert auth.login(PASSWORD, "someone else")  # other clients are not affected
    clock.now += LOCK_SECONDS + 1
    assert auth.login(PASSWORD, "attacker")


def test_everyone_is_locked_after_many_failures(auth):
    for i in range(TOTAL_FAILURES):
        with pytest.raises(LoginError):
            auth.login("wrong password", f"client-{i % 4}-{i}")
    with pytest.raises(LoginError, match="너무 많아요"):
        auth.login(PASSWORD, "new client")


def test_set_password_command(tmp_path, capsys):
    answers = iter(["short", "a long enough one", "different one!!", "a long enough one", "a long enough one"])
    assert set_password(str(tmp_path / "config.yaml"), ask=lambda prompt: next(answers)) == 0
    auth = WebAuth(tmp_path / "web-auth.json")
    assert auth.login("a long enough one", "c")
    out = capsys.readouterr().out
    assert "10자 이상" in out and "달라요" in out


# The web screen with a password ---------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    for name in ("config.yaml", "portfolio.yaml"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    return tmp_path


def web(project, auth=None, gateway=None, monkeypatch=None):
    if gateway is not None:
        monkeypatch.setenv("IBKR_ACCOUNT_ID", "U123")
        ctx = App(project / "config.yaml", broker_kind="ibkr", today=date(2027, 1, 5), gateway=gateway)
    else:
        ctx = App(project / "config.yaml", mock_file=str(project / "examples" / "month01.yaml"), today=date(2027, 1, 5))
    client = TestClient(create_app(ctx, {"testserver", "1.2.3.4"}, auth), follow_redirects=False)
    client.headers.update({"X-Session-Token": ctx.token})
    return ctx, client


def sign_in(client):
    res = client.post("/login", content="password=correct+horse+battery&next=%2F", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert res.status_code == 303 and res.headers["location"] == "/"
    return res


def test_everything_needs_a_login_once_a_password_is_set(project, auth):
    _, client = web(project, auth)
    page = client.get("/")
    assert page.status_code == 303 and page.headers["location"] == "/login?next=%2F"
    api = client.get("/api/state")
    assert api.status_code == 401 and api.json()["login"] is True
    assert client.get("/static/app.js").status_code == 303
    login = client.get("/login")
    assert login.status_code == 200 and 'type="password"' in login.text
    assert "form-action 'self'" in login.headers["Content-Security-Policy"]
    assert client.get("/static/login.css").status_code == 200

    sign_in(client)
    assert client.get("/").status_code == 200
    state = client.get("/api/state").json()
    assert state["web"]["password"] is True

    client.post("/api/session/logout")
    assert client.get("/api/state").status_code == 401


def test_wrong_password_and_safe_redirect(project, auth):
    _, client = web(project, auth)
    bad = client.post("/login", content="password=nope&next=%2F", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert bad.status_code == 401 and "맞지 않아요" in bad.text and COOKIE_NAME not in bad.cookies
    evil = client.post(
        "/login",
        content="password=correct+horse+battery&next=%2F%2Fevil.example",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert evil.status_code == 303 and evil.headers["location"] == "/"


def test_cookie_is_secure_behind_https(project, auth):
    _, client = web(project, auth)
    res = client.post(
        "/login",
        content="password=correct+horse+battery&next=%2F",
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Forwarded-Proto": "https", "X-Forwarded-For": "9.9.9.9"},
    )
    cookie = res.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()


def test_outside_access_is_refused_until_a_password_exists(project, tmp_path):
    _, client = web(project, WebAuth(tmp_path / "none.json"))
    outside = client.get("/", headers={"X-Forwarded-For": "9.9.9.9"})
    assert outside.status_code == 503 and "set-password" in outside.text
    assert client.get("/auth/check", headers={"X-Forwarded-For": "9.9.9.9"}).status_code in (303, 401)
    assert client.get("/").status_code == 200  # on this machine (or a tunnel) it still opens


def test_caddy_check_guards_the_ibkr_login_pages(project, auth):
    _, client = web(project, auth)
    page = client.get("/auth/check", headers={"X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/sso/Login?forwardTo=22"})
    assert page.status_code == 303 and page.headers["location"].startswith("/login?next=%2Fsso%2FLogin")
    api = client.get("/auth/check", headers={"X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/v1/api/iserver/accounts"})
    assert api.status_code == 401
    sign_in(client)
    assert client.get("/auth/check", headers={"X-Forwarded-Uri": "/v1/api/tickle"}).status_code == 204


def test_public_host_is_allowed(project, auth):
    _, client = web(project, auth)
    assert client.get("/login", headers={"Host": "1.2.3.4"}).status_code == 200
    assert client.get("/login", headers={"Host": "5.6.7.8"}).status_code == 403


# IBKR login status -------------------------------------------------------------------


class StatusGateway:
    account_id = "U123"

    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error
        self.reauths = 0

    def auth_status(self):
        if self.error:
            raise self.error
        return self.status

    def reauthenticate(self):
        self.reauths += 1
        return {}


def test_ibkr_status_asks_for_a_login(project, auth, monkeypatch):
    gw = StatusGateway(error=IbkrError("not logged in", status=401))
    _, client = web(project, auth, gw, monkeypatch)
    sign_in(client)
    outside = client.get("/api/ibkr/status", headers={"X-Forwarded-For": "9.9.9.9"}).json()
    assert outside["needs_login"] is True and outside["login_url"].startswith("/sso/Login")
    local = client.get("/api/ibkr/status").json()
    assert local["login_url"] == "https://localhost:5000/"


def test_ibkr_status_reopens_a_dropped_session(project, auth, monkeypatch):
    gw = StatusGateway(status={"authenticated": False, "connected": True, "competing": False})
    _, client = web(project, auth, gw, monkeypatch)
    sign_in(client)
    first = client.get("/api/ibkr/status").json()
    assert gw.reauths == 1 and first["needs_login"] is False and "다시 여는 중" in first["message"]
    client.get("/api/ibkr/status")
    assert gw.reauths == 1  # not again within 30 seconds


def test_ibkr_status_never_takes_over_another_session(project, auth, monkeypatch):
    gw = StatusGateway(status={"authenticated": False, "connected": True, "competing": True})
    _, client = web(project, auth, gw, monkeypatch)
    sign_in(client)
    st = client.get("/api/ibkr/status").json()
    assert gw.reauths == 0 and st["needs_login"] is True and "다른 곳" in st["message"]


def test_ibkr_status_when_connected_and_when_gateway_is_down(project, auth, monkeypatch):
    gw = StatusGateway(status={"authenticated": True, "connected": True})
    _, client = web(project, auth, gw, monkeypatch)
    sign_in(client)
    assert client.get("/api/ibkr/status").json()["authenticated"] is True
    gw.error = IbkrError("down", status=0)
    st = client.get("/api/ibkr/status").json()
    assert st["reachable"] is False and st["needs_login"] is False


# ChatGPT sign-in when the screen is opened from outside ---------------------------------


def test_pasted_chatgpt_return_address_is_checked(project, auth):
    ctx, client = web(project, auth)
    sign_in(client)
    seen = {}

    def finish(params):
        seen.update(params)
        return {}

    ctx.chatgpt_callback = finish
    bad = client.post("/api/chatgpt/callback-url", json={"url": "https://example.com/other?code=1"})
    assert bad.status_code == 400
    ok = client.post("/api/chatgpt/callback-url", json={"url": " http://127.0.0.1:8765/auth/callback?code=abc&state=xyz "})
    assert ok.status_code == 200 and seen == {"code": "abc", "state": "xyz"}
