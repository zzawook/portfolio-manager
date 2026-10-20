import pytest

from rebalancer.brokers.ibkr import GatewayClient, IbkrBroker, IbkrError, parse_price
from rebalancer.models import BUY, Order, Quote

from helpers import make_config, raw_config


def ibkr_config():
    raw = raw_config()
    raw["broker"] = {"type": "ibkr", "ibkr": {"account_id": "DU123"}}
    return make_config(raw)


@pytest.mark.parametrize(
    "value, expected",
    [("C190.5", 190.5), ("1,234.50", 1234.5), (95, 95.0), ("", None), (None, None), (0, None), ("H-", None)],
)
def test_parse_price(value, expected):
    assert parse_price(value) == expected


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.payload)


def test_order_body_is_wrapped_in_orders_list():
    session = FakeSession([{"order_id": "1", "order_status": "Submitted"}])
    client = GatewayClient("https://localhost:5000/v1/api", "DU123", session=session)
    client.place_order({"conid": 1, "side": "BUY"})
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == "https://localhost:5000/v1/api/iserver/account/DU123/orders"
    assert kwargs["json"] == {"orders": [{"conid": 1, "side": "BUY"}]}
    assert kwargs["verify"] is False


def test_positions_are_not_double_counted_if_pages_repeat():
    session = FakeSession([{"conid": 1, "position": 10}, {"conid": 4, "position": 5}])  # same page every time
    client = GatewayClient("https://x/v1/api", "DU123", session=session)
    assert client.positions() == [{"conid": 1, "position": 10}, {"conid": 4, "position": 5}]
    assert len(session.calls) == 2


def test_snapshot_retries_after_preflight():
    calls = {"n": 0}

    class Session:
        def request(self, method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse([{"conid": 1, "conidEx": "1"}])
            return FakeResponse([{"conid": 1, "31": "190.0", "84": "189.9", "86": "190.1"}])

    slept = []
    client = GatewayClient("https://x/v1/api", "DU123", session=Session(), sleep=slept.append)
    result = client.snapshot([1])
    assert result[1] == Quote(bid=189.9, ask=190.1, last=190.0)
    assert slept == [1.0]


class FakeClient:
    account_id = "DU123"

    def __init__(self, order_responses=None, authenticated=True):
        self.order_responses = list(order_responses or [])
        self.replies = []
        self.authenticated = authenticated

    def auth_status(self):
        return {"authenticated": self.authenticated, "connected": True}

    def iserver_accounts(self):
        return {"accounts": ["DU123"]}

    def portfolio_accounts(self):
        return [{"id": "DU123"}]

    def positions(self):
        return [
            {"conid": 1, "position": 10, "contractDesc": "VWRA"},
            {"conid": 4, "position": 5, "contractDesc": "SMH"},
            {"conid": 999, "position": 3, "contractDesc": "TSLA"},
            {"conid": 2, "position": 0, "contractDesc": "AGGU"},
        ]

    def ledger(self):
        return {"USD": {"cashbalance": 3000.5}, "BASE": {"cashbalance": 4000}}

    def snapshot(self, conids):
        return {c: Quote(bid=10, ask=10.1, last=10.05) for c in conids}

    def place_order(self, body):
        self.last_body = body
        return self.order_responses.pop(0)

    def reply(self, reply_id, confirmed):
        self.replies.append((reply_id, confirmed))
        return self.order_responses.pop(0) if self.order_responses else {}


def test_snapshot_maps_conids_and_flags_unknown_positions():
    b = IbkrBroker(ibkr_config(), client=FakeClient())
    snap = b.snapshot()
    assert snap.positions == {"VWRA": 10, "SMH": 5}
    assert snap.unknown_positions == ["TSLA"]
    assert snap.cash == 3000.5
    assert set(snap.quotes) == {"VWRA", "AGGU", "IGLN", "SMH", "ZAPP", "WDGF"}


def test_unauthenticated_session_is_refused():
    b = IbkrBroker(ibkr_config(), client=FakeClient(authenticated=False))
    with pytest.raises(IbkrError, match="로그인"):
        b.snapshot()


def test_warning_is_sent_back_only_after_approval():
    client = FakeClient(
        order_responses=[
            [{"id": "abc", "message": ["price exceeds 3%"], "isSuppressed": False, "messageIds": ["o163"]}],
            [{"order_id": "777", "order_status": "Submitted"}],
        ]
    )
    b = IbkrBroker(ibkr_config(), client=client)
    seen = []
    res = b.place_order(Order("VWRA", BUY, 2, 190.5), "rb1", lambda msgs: seen.append(msgs) or True)
    assert res == {"status": "submitted", "order_id": "777", "detail": "Submitted"}
    assert client.replies == [("abc", True)]
    assert seen == [["price exceeds 3%"]]
    assert client.last_body == {
        "conid": 1,
        "orderType": "LMT",
        "price": 190.5,
        "side": "BUY",
        "quantity": 2,
        "tif": "DAY",
        "cOID": "rb1",
    }


def test_declined_warning_cancels_the_order():
    client = FakeClient(order_responses=[[{"id": "abc", "message": ["are you sure?"]}]])
    b = IbkrBroker(ibkr_config(), client=client)
    res = b.place_order(Order("VWRA", BUY, 2, 190.5), "rb1", lambda msgs: False)
    assert res["status"] == "declined"
    assert client.replies == [("abc", False)]


def test_rejection_is_reported():
    client = FakeClient(order_responses=[{"error": "insufficient funds"}])
    b = IbkrBroker(ibkr_config(), client=client)
    res = b.place_order(Order("VWRA", BUY, 2, 190.5), "rb1", lambda msgs: True)
    assert res == {"status": "rejected", "detail": "insufficient funds"}


def test_account_id_can_come_from_environment(monkeypatch):
    raw = raw_config()
    raw["broker"] = {"type": "ibkr", "ibkr": {"account_id": ""}}
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DU999")
    b = IbkrBroker(make_config(raw))
    assert b.client.account_id == "DU999"


def test_broker_needs_account_and_conids(monkeypatch):
    monkeypatch.delenv("IBKR_ACCOUNT_ID", raising=False)
    raw = raw_config()
    raw["broker"] = {"type": "ibkr", "ibkr": {"account_id": ""}}
    with pytest.raises(IbkrError, match="account_id"):
        IbkrBroker(make_config(raw), client=FakeClient())
    raw = raw_config()
    raw["broker"] = {"type": "ibkr", "ibkr": {"account_id": "DU123"}}
    raw["instruments"]["SMH"]["conid"] = None
    with pytest.raises(IbkrError, match="SMH"):
        IbkrBroker(make_config(raw), client=FakeClient())


def test_snapshot_without_usd_cash_reports_other_currencies():
    class SgdOnly(FakeClient):
        def ledger(self):
            return {"BASE": {"cashbalance": 1550.0}, "SGD": {"cashbalance": 1550.0}}

    snap = IbkrBroker(ibkr_config(), client=SgdOnly()).snapshot()
    assert snap.cash == 0.0 and snap.other_cash == {"SGD": 1550.0}
