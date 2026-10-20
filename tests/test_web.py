import shutil
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rebalancer.llm import Reply, ToolCall
from rebalancer.server import App, create_app

ROOT = Path(__file__).resolve().parents[1]


class ScriptedProvider:
    name = "scripted"
    label = "Test"
    model = "test-model"

    def __init__(self, *steps):
        self.steps = list(steps)

    def complete(self, system, history, tools):
        return self.steps.pop(0)


@pytest.fixture
def project(tmp_path):
    for name in ("config.yaml", "portfolio.yaml"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    return tmp_path


def client_for(project, example="month14.yaml", provider=None, today=date(2028, 2, 3)):
    ctx = App(project / "config.yaml", mock_file=str(project / "examples" / example), today=today, provider=provider)
    client = TestClient(create_app(ctx, {"testserver"}))
    client.headers.update({"X-Session-Token": ctx.token})
    return ctx, client


def wait_job(client, job_id, *statuses):
    for _ in range(300):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.01)
    raise AssertionError(job["status"])


def test_api_needs_the_session_token(project):
    ctx, client = client_for(project)
    assert client.get("/api/state").status_code == 200
    bare = TestClient(create_app(ctx, {"testserver"}))
    assert bare.get("/api/state").status_code == 403
    assert bare.get("/api/state", headers={"X-Session-Token": "wrong"}).status_code == 403


def test_unexpected_host_is_refused(project):
    ctx, _ = client_for(project)
    other = TestClient(create_app(ctx, {"127.0.0.1:8765"}))
    assert other.get("/").status_code == 403  # e.g. a DNS-rebinding page
    assert other.get("/api/state", headers={"X-Session-Token": ctx.token}).status_code == 403


def test_page_carries_the_token_and_refuses_framing(project):
    ctx, client = client_for(project)
    page = client.get("/")
    assert page.status_code == 200 and ctx.token in page.text
    assert page.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../config.yaml").status_code == 404


def test_holdings_and_state(project):
    _, client = client_for(project)
    state = client.get("/api/state").json()
    assert state["broker"]["label"] == "모의 계좌"
    assert state["draft"]["dirty"] is False and state["chat"]["enabled"] is False
    holdings = client.get("/api/holdings").json()
    assert holdings["total"] == pytest.approx(43200)
    assert {p["symbol"] for p in holdings["positions"]} == {"VWRA", "AGGU", "IGLN", "SMH", "ZAPP", "WDGF"}


def test_draft_edit_conflict_and_save(project):
    _, client = client_for(project)
    draft = client.get("/api/state").json()["draft"]
    portfolio = draft["portfolio"]
    portfolio["allocations"][0]["target"] = 45  # VWRA 45, sum 105
    view = client.put("/api/draft", json={"version": draft["version"], "portfolio": portfolio}).json()
    assert view["dirty"] and any("합계" in p for p in view["problems"])
    assert client.post("/api/draft/save").status_code == 400  # not while the sum is off
    stale = client.put("/api/draft", json={"version": draft["version"], "portfolio": portfolio})
    assert stale.status_code == 409 and "draft" in stale.json()

    portfolio["allocations"][1]["target"] = 10  # AGGU 15 -> 10, back to 100
    view = client.put("/api/draft", json={"version": view["version"], "portfolio": portfolio}).json()
    assert view["problems"] == []
    saved = client.post("/api/draft/save").json()
    assert saved["draft"]["dirty"] is False
    assert "target: 45" in (project / "portfolio.yaml").read_text(encoding="utf-8")
    assert list((project / "history").glob("portfolio-*.yaml"))


def test_contribution_flow_and_monthly_lock(project):
    ctx, client = client_for(project, "month01.yaml", today=date(2027, 1, 5))
    plan = client.post("/api/plan", json={}).json()
    assert plan["mode"] == "contribution" and plan["orders"] and plan["fee_total"] > 0
    job = client.post("/api/orders", json={"plan_id": plan["id"]}).json()
    job = wait_job(client, job["id"], "done")
    assert job["submitted"] == len(plan["orders"])
    assert client.post("/api/orders", json={"plan_id": plan["id"]}).status_code == 409  # used

    again = client.post("/api/plan", json={}).json()
    assert again["month"]["executed_at"]
    blocked = client.post("/api/orders", json={"plan_id": again["id"]})
    assert blocked.status_code == 409 and "이미 주문했어요" in blocked.json()["detail"]


def test_two_step_rebalance_over_http(project):
    _, client = client_for(project)
    plan = client.post("/api/plan", json={}).json()
    assert plan["two_step"] and plan["send_count"] == 1
    job = client.post("/api/orders", json={"plan_id": plan["id"]}).json()
    job = wait_job(client, job["id"], "sells_done")
    job = client.post(f"/api/jobs/{job['id']}/buy-plan").json()
    assert job["status"] == "buy_plan_ready" and job["buy_plan"]["orders"]
    job = client.post(f"/api/jobs/{job['id']}/buys").json()
    job = wait_job(client, job["id"], "done")
    assert job["submitted"] > 1


def test_plan_with_unsaved_draft_saves_it_when_sent(project):
    _, client = client_for(project, "month01.yaml", today=date(2027, 1, 5))
    draft = client.get("/api/state").json()["draft"]
    portfolio = draft["portfolio"]
    portfolio["allocations"][0]["target"] = 45
    portfolio["allocations"][1]["target"] = 10
    client.put("/api/draft", json={"version": draft["version"], "portfolio": portfolio})
    plan = client.post("/api/plan", json={}).json()
    assert plan["dirty"] is True
    job = client.post("/api/orders", json={"plan_id": plan["id"]}).json()
    wait_job(client, job["id"], "done")
    assert "target: 45" in (project / "portfolio.yaml").read_text(encoding="utf-8")


def test_plan_goes_stale_when_the_draft_changes(project):
    _, client = client_for(project, "month01.yaml", today=date(2027, 1, 5))
    draft = client.get("/api/state").json()["draft"]
    portfolio = draft["portfolio"]
    portfolio["allocations"][0]["target"] = 45
    portfolio["allocations"][1]["target"] = 10
    view = client.put("/api/draft", json={"version": draft["version"], "portfolio": portfolio}).json()
    plan = client.post("/api/plan", json={}).json()
    portfolio["allocations"][1]["target"] = 9
    portfolio["allocations"][2]["target"] = 11
    client.put("/api/draft", json={"version": view["version"], "portfolio": portfolio})
    stale = client.post("/api/orders", json={"plan_id": plan["id"]})
    assert stale.status_code == 409 and "다시 계산" in stale.json()["detail"]


def test_broken_draft_cannot_be_planned(project):
    _, client = client_for(project)
    draft = client.get("/api/state").json()["draft"]
    portfolio = draft["portfolio"]
    portfolio["allocations"][0]["target"] = 50
    client.put("/api/draft", json={"version": draft["version"], "portfolio": portfolio})
    res = client.post("/api/plan", json={})
    assert res.status_code == 400 and res.json()["problems"]


def test_chat_is_off_without_a_provider(project):
    _, client = client_for(project)
    res = client.post("/api/chat", json={"message": "안녕"})
    assert res.status_code == 503


def test_chat_changes_only_the_draft(project):
    provider = ScriptedProvider(
        Reply("", [ToolCall("a", "set_allocation", {"sleeve": "core", "symbol": "VWRA", "target": 45}),
                   ToolCall("b", "set_allocation", {"sleeve": "core", "symbol": "AGGU", "target": 10})]),
        Reply("VWRA 45%, AGGU 10%로 바꿨어요."),
    )
    ctx, client = client_for(project, provider=provider)
    res = client.post("/api/chat", json={"message": "VWRA 늘리고 채권 줄여 줘"}).json()
    assert res["ok"] and len(res["changes"]) == 2
    assert res["draft"]["dirty"] and res["chat"]["transcript"][-1]["role"] == "assistant"
    assert ctx.mock.submitted == []  # no order without the button
    assert client.get("/api/jobs/current").json()["job"] is None
    assert client.post("/api/chat/reset").json()["transcript"] == []


def test_contract_search_needs_ibkr(project):
    _, client = client_for(project)
    res = client.get("/api/contracts", params={"symbol": "VWRA"})
    assert res.status_code == 400 and "모의 계좌" in res.json()["detail"]


# IBKR mode with a fake Gateway ------------------------------------------------------


class FakeGateway:
    account_id = "DU123"

    def __init__(self):
        self.orders = []
        self.replies = []
        self.positions_data = [{"conid": 101, "position": 10, "contractDesc": "VWRA"}]

    def auth_status(self):
        return {"authenticated": True, "connected": True}

    def iserver_accounts(self):
        return {"accounts": ["DU123"]}

    def portfolio_accounts(self):
        return [{"id": "DU123"}]

    def positions(self):
        return self.positions_data

    def ledger(self):
        return {"USD": {"cashbalance": 3000.0}}

    def snapshot(self, conids):
        from rebalancer.models import Quote

        prices = {101: 190, 102: 5.85, 103: 95, 104: 65, 105: 26, 106: 11.5}
        return {c: Quote(bid=prices[c] - 0.1, ask=prices[c] + 0.1, last=prices[c]) for c in conids}

    def search_stock(self, symbol):
        return {symbol: [{"name": symbol, "contracts": [{"conid": 999, "exchange": "LSEETF"}]}]}

    def contract_info(self, conid):
        return {"currency": "USD"}

    def place_order(self, body):
        self.orders.append(body)
        return [{"id": f"r{len(self.orders)}", "message": ["Price exceeds the 3% constraint."]}]

    def reply(self, reply_id, confirmed):
        self.replies.append((reply_id, confirmed))
        return [{"order_id": f"o{len(self.replies)}", "order_status": "Submitted"}] if confirmed else {}


def test_ibkr_flow_with_conid_lookup_and_warning_approval(project, monkeypatch):
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DU123")
    gateway = FakeGateway()
    ctx = App(project / "config.yaml", broker_kind="ibkr", today=date(2027, 1, 5), gateway=gateway)
    client = TestClient(create_app(ctx, {"testserver"}))
    client.headers.update({"X-Session-Token": ctx.token})

    assert client.get("/api/state").json()["broker"]["label"] == "IBKR 모의계좌"
    res = client.get("/api/holdings")
    assert res.status_code == 400 and set(res.json()["missing_conids"]) == {"VWRA", "AGGU", "IGLN", "SMH", "ZAPP", "WDGF"}
    found = client.get("/api/contracts", params={"symbol": "VWRA"}).json()["candidates"]
    assert found == [{"conid": 999, "exchange": "LSEETF", "currency": "USD", "name": "VWRA", "recommended": True}]

    draft = client.get("/api/state").json()["draft"]
    portfolio = draft["portfolio"]
    for i, inst in enumerate(portfolio["instruments"]):
        inst["conid"] = 101 + i
    client.put("/api/draft", json={"version": draft["version"], "portfolio": portfolio})
    assert client.post("/api/draft/save").status_code == 200
    holdings = client.get("/api/holdings", params={"refresh": True}).json()
    assert holdings["positions"][0]["symbol"] == "VWRA" and holdings["cash"] == 3000

    plan = client.post("/api/plan", json={}).json()
    job = client.post("/api/orders", json={"plan_id": plan["id"]}).json()
    job = wait_job(client, job["id"], "question")
    assert job["question"]["messages"] == ["Price exceeds the 3% constraint."]
    assert gateway.replies == []  # waits for the person
    seen = {job["question"]["id"]}
    client.post(f"/api/jobs/{job['id']}/answer", json={"question_id": job["question"]["id"], "approve": True})
    deadline = time.time() + 5
    while time.time() < deadline:  # decline every later warning, once each
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] == "done":
            break
        q = job.get("question")
        if q and q["id"] not in seen:
            seen.add(q["id"])
            client.post(f"/api/jobs/{job['id']}/answer", json={"question_id": q["id"], "approve": False})
        time.sleep(0.01)
    assert job["status"] == "done"
    statuses = [o["status"] for o in job["orders"]]
    assert statuses[0] == "submitted" and set(statuses[1:]) == {"declined"}
    assert gateway.replies[0] == ("r1", True) and all(c is False for _, c in gateway.replies[1:])
    assert gateway.orders[0]["orderType"] == "LMT" and gateway.orders[0]["tif"] == "DAY"


def test_port_free_ignores_connections_left_from_a_previous_run():
    import socket

    from rebalancer.server import _port_free

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    assert not _port_free(port)  # something is listening: really in use
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()  # the old server closes first while the client (a tunnel) stays connected
    srv.close()
    try:
        assert _port_free(port)  # only a leftover connection remains: free to start again
    finally:
        client.close()
