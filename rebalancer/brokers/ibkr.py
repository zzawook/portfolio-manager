"""IBKR Client Portal Gateway (Web API v1) client and broker.

The gateway is a small Java program you run locally and log in to through the
browser. This module only talks to it over https://localhost:5000/v1/api.
It never handles your IBKR username or password.

Endpoints used:
  POST iserver/auth/status                       session check
  GET  iserver/accounts                          required before placing orders
  GET  portfolio/accounts                        required before other portfolio calls
  GET  portfolio/{acct}/positions/{page}         holdings
  GET  portfolio/{acct}/ledger                   cash by currency
  GET  iserver/marketdata/snapshot               31=last, 84=bid, 86=ask (first call only subscribes)
  GET  trsrv/stocks?symbols=...                  contract search (resolve command)
  GET  iserver/contract/{conid}/info             contract currency/exchange (resolve command)
  POST iserver/account/{acct}/orders             body {"orders": [...]}
  POST iserver/reply/{id}                        answer an order warning
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Optional

import requests
import urllib3

from ..config import Config
from ..models import AccountSnapshot, Order, Quote
from . import ConfirmFn

FIELD_LAST, FIELD_BID, FIELD_ASK = "31", "84", "86"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
MAX_REPLY_ROUNDS = 5


class IbkrError(RuntimeError):
    def __init__(self, message: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.status = status  # HTTP status from the Gateway, 0 if it could not be reached


ACCOUNT_ENV = "IBKR_ACCOUNT_ID"


def account_id_for(cfg: Config) -> str:
    """The account number from config, or from IBKR_ACCOUNT_ID so it can stay out of git."""
    return cfg.broker.ibkr.account_id or os.environ.get(ACCOUNT_ENV, "")


def parse_price(value: Any) -> Optional[float]:
    """Snapshot values can be strings with prefixes such as 'C' (prior close) or 'H' (halted)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    match = _NUMBER.search(str(value).replace(",", ""))
    if not match:
        return None
    number = float(match.group())
    return number if number > 0 else None


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        account_id: str,
        verify_ssl: bool | str = False,  # True, False, or the path of the Gateway's certificate
        timeout: float = 15,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base = base_url.rstrip("/") + "/"
        self.account_id = account_id
        self.verify = verify_ssl
        self.timeout = timeout
        self.session = session or requests.Session()
        self.sleep = sleep
        if not verify_ssl:
            # The local gateway uses a self-signed certificate.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = self.base + path
        try:
            resp = self.session.request(method, url, verify=self.verify, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise IbkrError(
                f"Gateway에 연결할 수 없어요 ({e}). Client Portal Gateway를 켜고 브라우저로 로그인했는지 확인하세요.",
                status=0,
            ) from e
        if resp.status_code == 401:
            raise IbkrError("IBKR 로그인이 필요해요. 화면의 'IBKR 로그인'을 눌러 다시 로그인하세요.", status=401)
        if resp.status_code >= 400:
            raise IbkrError(f"{method} {path} 실패: HTTP {resp.status_code} {resp.text[:300]}", status=resp.status_code)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise IbkrError(f"{method} {path}: JSON이 아닌 응답이에요: {resp.text[:300]}") from e

    def auth_status(self) -> dict:
        return self._request("POST", "iserver/auth/status") or {}

    def tickle(self) -> dict:
        """Keep the Gateway's brokerage session from timing out."""
        return self._request("POST", "tickle") or {}

    def reauthenticate(self) -> dict:
        """Open the brokerage session again while the IBKR login (SSO) is still valid.

        IBKR requires compete=true here ("Force compete capability must be used together
        with compete flag"). Callers only do this when no other session is open
        (auth status competing=false), so nothing is taken over."""
        return self._request("POST", "iserver/auth/ssodh/init", json={"publish": True, "compete": True}) or {}

    def exchange_rate(self, source: str, target: str) -> Optional[float]:
        """How much `target` one unit of `source` buys (SGD -> USD: about 0.78)."""
        data = self._request("GET", "iserver/exchangerate", params={"source": source, "target": target}) or {}
        try:
            rate = float(data.get("rate"))
        except (TypeError, ValueError):
            return None
        return rate if rate > 0 else None

    def iserver_accounts(self) -> dict:
        return self._request("GET", "iserver/accounts") or {}

    def portfolio_accounts(self) -> list:
        return self._request("GET", "portfolio/accounts") or []

    def positions(self, max_pages: int = 20) -> list[dict]:
        # Keyed by conid so a repeated page can never double-count a holding.
        seen: dict[Any, dict] = {}
        for page in range(max_pages):
            batch = self._request("GET", f"portfolio/{self.account_id}/positions/{page}") or []
            new = [p for p in batch if p.get("conid") not in seen]
            if not new:
                break
            for p in new:
                seen[p.get("conid")] = p
        return list(seen.values())

    def ledger(self) -> dict:
        return self._request("GET", f"portfolio/{self.account_id}/ledger") or {}

    def snapshot(self, conids: list[int], tries: int = 5) -> dict[int, Quote]:
        wanted = {int(c) for c in conids}
        params = {"conids": ",".join(str(c) for c in sorted(wanted)), "fields": f"{FIELD_LAST},{FIELD_BID},{FIELD_ASK}"}
        result: dict[int, Quote] = {}
        for _ in range(tries):
            rows = self._request("GET", "iserver/marketdata/snapshot", params=params) or []
            for row in rows:
                try:
                    conid = int(row.get("conid"))
                except (TypeError, ValueError):
                    continue
                quote = Quote(
                    bid=parse_price(row.get(FIELD_BID)),
                    ask=parse_price(row.get(FIELD_ASK)),
                    last=parse_price(row.get(FIELD_LAST)),
                )
                if quote.mark:
                    result[conid] = quote
            if wanted <= set(result):
                break
            # The first request only starts the market data stream.
            self.sleep(1.0)
        return result

    def search_stock(self, symbol: str) -> dict:
        return self._request("GET", "trsrv/stocks", params={"symbols": symbol}) or {}

    def contract_info(self, conid: int) -> dict:
        return self._request("GET", f"iserver/contract/{conid}/info") or {}

    def place_order(self, order_body: dict) -> Any:
        return self._request("POST", f"iserver/account/{self.account_id}/orders", json={"orders": [order_body]})

    def reply(self, reply_id: str, confirmed: bool) -> Any:
        return self._request("POST", f"iserver/reply/{reply_id}", json={"confirmed": confirmed})


class IbkrBroker:
    name = "ibkr"

    def __init__(self, cfg: Config, client: Optional[GatewayClient] = None):
        settings = cfg.broker.ibkr
        account_id = account_id_for(cfg)
        if not account_id:
            raise IbkrError(
                f"계좌번호가 없어요. 환경 변수 {ACCOUNT_ENV}를 설정하거나 config.yaml의 broker.ibkr.account_id를 채워 주세요."
                " 모의계좌는 DU로 시작해요."
            )
        missing = [s for s, inst in cfg.instruments.items() if not inst.conid]
        if missing:
            raise IbkrError(
                f"conid가 없는 종목이 있어요: {', '.join(missing)}. 웹 화면의 'IBKR에서 conid 찾기'나 resolve 명령으로 찾아서 portfolio.yaml에 채워 주세요."
            )
        self.cfg = cfg
        self.client = client or GatewayClient(settings.base_url, account_id, settings.verify)
        self.symbol_by_conid = {inst.conid: sym for sym, inst in cfg.instruments.items()}
        self._ready = False

    def ensure_ready(self) -> None:
        if self._ready:
            return
        status = self.client.auth_status()
        if not (status.get("authenticated") and status.get("connected")):
            raise IbkrError("IBKR 로그인이 필요해요. 화면의 'IBKR 로그인'을 눌러 다시 로그인하세요.", status=401)
        accounts = self.client.iserver_accounts().get("accounts", [])
        if self.client.account_id not in accounts:
            raise IbkrError(f"계좌({self.client.account_id})를 찾을 수 없어요. 이 세션에서 보이는 계좌: {accounts}")
        self.client.portfolio_accounts()
        self._ready = True

    def snapshot(self) -> AccountSnapshot:
        self.ensure_ready()
        positions: dict[str, float] = {}
        unknown: list[str] = []
        for p in self.client.positions():
            qty = float(p.get("position") or 0)
            if qty == 0:
                continue
            try:
                conid = int(p.get("conid"))
            except (TypeError, ValueError):
                conid = None
            sym = self.symbol_by_conid.get(conid)
            if sym is None:
                unknown.append(str(p.get("contractDesc") or p.get("ticker") or conid))
                continue
            positions[sym] = positions.get(sym, 0.0) + qty

        ledger = self.client.ledger()
        ccy = self.cfg.base_currency
        # No line for the base currency just means none is held yet (e.g. only SGD deposited).
        cash = float((ledger.get(ccy) or {}).get("cashbalance") or 0.0)
        other_cash = {}
        for cur, row in ledger.items():
            if cur in (ccy, "BASE") or not isinstance(row, dict):
                continue
            amount = float(row.get("cashbalance") or 0.0)
            if abs(amount) >= 0.01:
                other_cash[cur] = amount

        fx_rates: dict[str, float] = {}
        for cur, amount in other_cash.items():
            if amount <= 0:
                continue
            try:
                rate = self.client.exchange_rate(cur, ccy)
            except IbkrError:
                rate = None
            if rate:
                fx_rates[cur] = rate

        quotes_by_conid = self.client.snapshot(list(self.symbol_by_conid))
        quotes = {self.symbol_by_conid[c]: q for c, q in quotes_by_conid.items() if c in self.symbol_by_conid}
        return AccountSnapshot(
            cash=cash, positions=positions, quotes=quotes, unknown_positions=unknown, other_cash=other_cash, fx_rates=fx_rates
        )

    def place_order(self, order: Order, client_order_id: str, confirm: ConfirmFn) -> dict:
        self.ensure_ready()
        inst = self.cfg.instruments[order.symbol]
        body = {
            "conid": inst.conid,
            "orderType": "LMT",
            "price": order.limit_price,
            "side": order.side,
            "quantity": order.quantity,
            "tif": "DAY",
            "cOID": client_order_id,
        }
        resp = self.client.place_order(body)
        for _ in range(MAX_REPLY_ROUNDS):
            item = resp[0] if isinstance(resp, list) and resp else resp
            if not isinstance(item, dict):
                return {"status": "rejected", "detail": f"알 수 없는 응답: {resp}"}
            if "error" in item:
                return {"status": "rejected", "detail": str(item["error"])}
            if "order_id" in item:
                return {
                    "status": "submitted",
                    "order_id": str(item["order_id"]),
                    "detail": str(item.get("order_status", "")),
                }
            if "id" in item and "message" in item:
                approved = confirm([str(m) for m in item.get("message") or []])
                if not approved:
                    try:
                        self.client.reply(item["id"], False)
                    except IbkrError:
                        pass
                    return {"status": "declined", "detail": "경고를 승인하지 않아서 주문을 보내지 않았어요."}
                resp = self.client.reply(item["id"], True)
                continue
            return {"status": "rejected", "detail": f"알 수 없는 응답: {item}"}
        return {"status": "rejected", "detail": "주문 확인 메시지가 너무 많이 반복돼서 멈췄어요."}


def _require_session(client: GatewayClient) -> None:
    status = client.auth_status()
    if not (status.get("authenticated") and status.get("connected")):
        raise IbkrError("IBKR 로그인이 필요해요. 화면의 'IBKR 로그인'을 눌러 다시 로그인하세요.", status=401)


def find_contracts(client: GatewayClient, symbol: str, currency: str, exchange: str = "LSE") -> list[dict]:
    """IBKR contracts for a ticker. `recommended` marks the listing on the wanted exchange and currency."""
    sym = symbol.strip().upper()
    data = client.search_stock(sym)
    entries = data.get(sym) or data.get(sym.lower()) or []
    found: list[dict] = []
    for entry in entries:
        for contract in entry.get("contracts", []):
            conid = contract.get("conid")
            if not conid:
                continue
            listing = str(contract.get("exchange", "?"))
            info = client.contract_info(conid)
            ccy = str(info.get("currency", "?")).upper()
            found.append(
                {
                    "conid": int(conid),
                    "exchange": listing,
                    "currency": ccy,
                    "name": str(entry.get("name", "")),
                    "recommended": exchange.upper() in listing.upper() and ccy == currency.upper(),
                }
            )
    return found


def resolve_candidates(cfg: Config, client: GatewayClient) -> list[str]:
    """List IBKR contracts for each configured symbol so the person can pick the LSE USD line."""
    _require_session(client)
    lines: list[str] = []
    for sym, inst in cfg.instruments.items():
        lines.append(f"{sym} ({inst.name})")
        candidates = find_contracts(client, sym, inst.currency, inst.exchange)
        for c in candidates:
            hint = "  <- 후보" if c["recommended"] else ""
            lines.append(f"  conid={c['conid']} exchange={c['exchange']} currency={c['currency']} name={c['name']}{hint}")
        if not candidates:
            lines.append("  검색 결과가 없어요. IBKR 웹/앱에서 종목을 검색해 conid를 확인하세요.")
    return lines


def search_contracts(client: GatewayClient, symbol: str, currency: str, exchange: str = "LSE") -> list[dict]:
    _require_session(client)
    return find_contracts(client, symbol, currency, exchange)
