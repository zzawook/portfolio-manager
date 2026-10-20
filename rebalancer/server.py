"""Web screen, served on 127.0.0.1. From outside it is reached through Caddy (HTTPS).

Security model
- The server listens on 127.0.0.1. Every request must carry an expected Host
  header, which blocks DNS-rebinding pages from reading it.
- When a password is set (web-auth.json, see webauth.py), every page and API call
  needs a signed-in session cookie. Requests that come through Caddy (they carry
  X-Forwarded-For) are refused until a password is set.
- Caddy asks /auth/check before it passes anything to the IBKR Gateway's login
  pages, so only signed-in people can reach the Gateway.
- Every API call needs the per-run session token embedded in the page, so other
  websites open in the same browser cannot call the API.
- The page refuses to be framed, so it cannot be overlaid to trick a click.
- Orders are sent only by POST /api/orders, which the page calls when the person
  presses the send button in the plan dialog. The chat has no way to call it.
"""

from __future__ import annotations

import copy
import logging
import os
import secrets
import socket
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from .assistant import Assistant, AssistantBusy
from .chatgpt_auth import CALLBACK_PATH, ChatGPTAuth, ChatGPTAuthError
from .webauth import COOKIE_NAME, MAX_DAYS, LoginError, WebAuth
from .brokers.ibkr import GatewayClient, IbkrBroker, IbkrError, account_id_for, search_contracts
from .brokers.mock import MockBroker
from .config import Config, ConfigError, load_config
from .envfile import load_env_file
from .execute import safety_problems
from .jobs import BUY_PLAN_READY, JobError, OrderJob
from .llm import ChatProvider, make_provider
from .models import CORE, SATELLITE, AccountSnapshot, Plan
from .planner import CONTRIBUTION, MODE_LABELS, REBALANCE, REBALANCE_BUYS, PlanError, build_plan
from .portfolio import Draft, DraftConflict, DraftError, save_portfolio
from .report import fee_rule, sleeve_weights
from .state import State, month_key, state_path_for

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
PLAN_TTL_SECONDS = 10 * 60
HOLDINGS_TTL_SECONDS = 60
SIDE_LABELS = {"BUY": "매수", "SELL": "매도"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


@dataclass
class PlanRecord:
    id: str
    cfg: Config
    snap: AccountSnapshot
    plan: Plan
    problems: list[str]
    portfolio: dict
    dirty: bool
    draft_version: int
    saved_version: int
    created_at: float = field(default_factory=time.time)
    used: bool = False


# Views ------------------------------------------------------------------------


def plan_view(cfg: Config, plan: Plan, problems: list[str]) -> dict:
    total = plan.total_value or 1.0
    weights = [
        {
            "symbol": sym,
            "target": target * 100,
            "now": plan.current_weights.get(sym, 0.0) * 100,
            "after": plan.projected_weights.get(sym, 0.0) * 100,
            "exit": target == 0,
        }
        for sym, target in plan.target_weights.items()
    ]
    now_sleeves = sleeve_weights(cfg, plan.current_weights)
    after_sleeves = sleeve_weights(cfg, plan.projected_weights)
    targets = cfg.sleeve_targets()
    two_step = plan.mode == REBALANCE and bool(plan.sells)
    sending = plan.sells if plan.sells else plan.buys
    buy_total = sum(o.notional for o in plan.buys)
    sell_total = sum(o.notional for o in plan.sells)
    traded = buy_total + sell_total
    leftover = None
    if plan.mode == CONTRIBUTION and plan.orders and plan.projected_cash > max(cfg.rules.min_order_value, 0.1 * total):
        leftover = f"남는 현금은 다음 실행 때 모자란 종목을 사는 데 써요 (한 번에 최대 {cfg.rules.max_buy_orders}종목)."
    contribution = cfg.rules.monthly_contribution
    return {
        "mode": plan.mode,
        "mode_label": MODE_LABELS.get(plan.mode, plan.mode),
        "reason": plan.reason,
        "total_value": plan.total_value,
        "cash": plan.cash,
        "projected_cash": plan.projected_cash,
        "cash_now": plan.cash / total * 100,
        "cash_after": plan.projected_cash / total * 100,
        "orders": [
            {
                "symbol": o.symbol,
                "side": o.side,
                "side_label": SIDE_LABELS[o.side],
                "quantity": o.quantity,
                "limit_price": o.limit_price,
                "notional": o.notional,
                "fee": o.fee,
                "sent_now": o in sending,
            }
            for o in plan.orders
        ],
        "buy_total": buy_total,
        "sell_total": sell_total,
        "fee_total": plan.fee_total,
        "fee_share": plan.fee_total / traded * 100 if traded else 0.0,
        "fee_rule": fee_rule(cfg),
        "fx": {"amount": contribution, "fee": cfg.fees.fx_estimate(contribution)},
        "two_step": two_step,
        "send_count": len(sending),
        "weights": weights,
        "sleeves": [
            {"sleeve": s, "target": targets[s], "now": now_sleeves[s] * 100, "after": after_sleeves[s] * 100}
            for s in (CORE, SATELLITE)
        ],
        "warnings": list(plan.warnings),
        "problems": list(problems),
        "leftover_note": leftover,
    }


def holdings_view(cfg: Config, snap: AccountSnapshot, missing_conids: list[str]) -> dict:
    positions = []
    invested = 0.0
    for sym, qty in snap.positions.items():
        quote = snap.quotes.get(sym)
        price = quote.mark if quote else None
        value = qty * price if price else None
        if value:
            invested += value
        positions.append({"symbol": sym, "quantity": qty, "price": price, "value": value})
    prices = {sym: q.mark for sym, q in snap.quotes.items() if q.mark and sym in cfg.instruments}
    return {
        "cash": snap.cash,
        "invested": invested,
        "total": invested + snap.cash,
        "positions": positions,
        "prices": prices,
        "unknown": list(snap.unknown_positions),
        "other_cash": dict(snap.other_cash),
        "missing_conids": missing_conids,
        "unpriced": [p["symbol"] for p in positions if p["value"] is None],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


# The app ----------------------------------------------------------------------


class App:
    def __init__(
        self,
        config_path: str | Path,
        broker_kind: Optional[str] = None,
        mock_file: Optional[str] = None,
        today: Optional[date] = None,
        provider: Optional[ChatProvider] = None,
        chat_reason: str = "",
        gateway: Optional[GatewayClient] = None,
        question_timeout: Optional[float] = None,
        port: int = 8765,
        keepalive: bool = False,
    ):
        self.config_path = Path(config_path).resolve()
        self.saved = load_config(self.config_path)
        self.kind = broker_kind or self.saved.broker.type
        if self.kind not in ("mock", "ibkr"):
            raise ConfigError("broker는 mock 또는 ibkr여야 해요.")
        self.state_path = state_path_for(self.config_path, self.kind)
        self.today_override = today
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.broker_lock = threading.Lock()
        self.draft = Draft(self.saved)
        self.saved_version = 1
        self.plans: dict[str, PlanRecord] = {}
        self.job: Optional[OrderJob] = None
        self.question_timeout = question_timeout
        self.port = port
        self._holdings: Optional[dict] = None
        self.webauth: Optional[WebAuth] = None  # set by serve() / create_app()
        self._last_reauth = 0.0
        self._dropped_since: Optional[float] = None  # when a dropped brokerage session was first seen

        self.mock: Optional[MockBroker] = None
        self.gateway: Optional[GatewayClient] = None
        if self.kind == "mock":
            path = Path(mock_file) if mock_file else self.saved.resolve_path(self.saved.broker.mock_file)
            self.mock = MockBroker(path, self.saved)
            self.mock_label = path.name
        else:
            settings = self.saved.broker.ibkr
            self.gateway = gateway or GatewayClient(settings.base_url, account_id_for(self.saved), settings.verify)

        if keepalive and self.gateway is not None:
            threading.Thread(target=self._keep_gateway_alive, daemon=True, name="ibkr-tickle").start()

        self.chat_reason = chat_reason
        self.provider = provider
        self.assistant: Optional[Assistant] = None
        if provider is not None:
            self.assistant = Assistant(
                provider,
                host=self,
                rules=self.saved.rules,
                currency=self.saved.base_currency,
                fee_rule=fee_rule(self.saved),
            )

    # Basics ------------------------------------------------------------------

    def today(self) -> date:
        return self.today_override or date.today()

    def broker_info(self) -> dict:
        if self.kind == "mock":
            return {"kind": "mock", "label": "모의 계좌", "live": False, "detail": f"예시 파일 {self.mock_label}"}
        account = self.gateway.account_id if self.gateway else ""
        paper = account.upper().startswith("D")
        masked = f"{account[:2]}{'*' * max(0, len(account) - 5)}{account[-3:]}" if len(account) > 5 else account
        return {
            "kind": "ibkr",
            "label": "IBKR 모의계좌" if paper else "IBKR 실계좌",
            "live": not paper,
            "detail": f"계좌 {masked}" if account else "계좌번호 없음",
        }

    def _keep_gateway_alive(self, every: float = 60.0) -> None:
        """Ping the Gateway so a logged-in IBKR session does not time out while the screen
        runs, and reopen the brokerage session when that works without a new login."""
        while True:
            time.sleep(every)
            try:
                self.gateway.tickle()
            except Exception:  # not logged in yet, or the Gateway is down: try again later
                pass
            try:
                self.ibkr_status(reauth=True)
            except Exception:
                pass

    def ibkr_status(self, reauth: bool = False) -> dict:
        """Where the IBKR login stands. With reauth, a brokerage session that dropped while
        the IBKR login is still valid is opened again (never taking over another session)."""
        if self.gateway is None:
            return {"kind": self.kind}
        view = {"kind": "ibkr", "reachable": True, "authenticated": False, "needs_login": False, "message": ""}
        try:
            st = self.gateway.auth_status()
        except IbkrError as e:
            self._dropped_since = None
            if e.status == 0:
                view.update(reachable=False, message="IBKR Gateway에 연결할 수 없어요. 서버에서 ibkr-gateway 서비스가 켜져 있는지 확인하세요.")
            else:
                view.update(needs_login=True, message="IBKR 로그인이 필요해요. 세션이 끝났거나 아직 로그인하지 않았어요.")
            return view
        if st.get("authenticated") and st.get("connected"):
            self._dropped_since = None
            view["authenticated"] = True
            return view
        if st.get("competing"):
            self._dropped_since = None
            view.update(
                needs_login=True,
                message="다른 곳(TWS, 휴대폰 앱, IBKR 웹)에서 IBKR에 로그인돼 있어요. 그쪽에서 로그아웃하고 다시 로그인하세요.",
            )
            return view
        now = time.monotonic()
        if self._dropped_since is None:
            self._dropped_since = now
        # Right after a login the Gateway opens the brokerage session itself (a few
        # seconds later); only step in when the session has stayed closed for a while.
        if reauth and now - self._dropped_since >= REAUTH_AFTER and now - self._last_reauth > 30:
            self._last_reauth = now
            try:
                self.gateway.reauthenticate()
                view["message"] = "IBKR 연결을 다시 여는 중이에요. 잠시 뒤 자동으로 이어져요."
                return view
            except IbkrError:
                pass
        if st.get("connected"):
            view["message"] = "IBKR 연결을 여는 중이에요. 잠시 뒤 자동으로 이어져요."
            return view
        view.update(needs_login=True, message="IBKR 로그인이 필요해요.")
        return view

    def broker_for(self, cfg: Config):
        if self.kind == "mock":
            self.mock.reconfigure(cfg)
            return self.mock
        try:
            return IbkrBroker(cfg, client=self.gateway)
        except IbkrError as e:
            raise ApiError(400, str(e)) from e

    def _job_active(self) -> bool:
        return self.job is not None and self.job.active

    def state_view(self) -> dict:
        state = State(self.state_path)
        today = self.today()
        key = month_key(today)
        done = state.execution(key)
        with self.lock:
            return {
                "today": today.isoformat(),
                "base_currency": self.saved.base_currency,
                "broker": self.broker_info(),
                "rules": asdict(self.saved.rules),
                "saved": copy.deepcopy(self.draft.saved),
                "draft": self.draft.view(),
                "chat": self.chat_view(),
                "month": {
                    "key": key,
                    "executed_at": done["at"] if done else None,
                    "mode_label": MODE_LABELS.get(done["mode"], done["mode"]) if done else None,
                },
                "job": self.job_view(self.job) if self.job else None,
                "web": {"password": bool(self.webauth and self.webauth.configured)},
            }

    def _chat_ready(self) -> bool:
        return self.assistant is not None and bool(getattr(self.provider, "ready", True))

    def chat_view(self) -> dict:
        settings = self.saved.assistant
        auth = self.provider.status() if self.provider is not None and hasattr(self.provider, "status") else None
        reason = self.chat_reason
        if auth and not self._chat_ready():
            reason = auth["reason"]
        return {
            "enabled": self._chat_ready(),
            "provider": self.provider.label if self.provider else settings.provider,
            "model": self.provider.model if self.provider else settings.resolved_model,
            "reason": reason,
            "auth": auth,
            "callback": f"http://127.0.0.1:{self.port}{CALLBACK_PATH}",
            "busy": bool(self.assistant and self.assistant.busy),
            "transcript": list(self.assistant.transcript) if self.assistant else [],
        }

    # Holdings ----------------------------------------------------------------

    def _holdings_cfg(self) -> Config:
        cfg, _ = self.draft.evaluate()
        return cfg or self.saved

    def holdings(self, refresh: bool = False) -> dict:
        with self.lock:
            cached = self._holdings
            if cached and not refresh and time.time() - cached["ts"] < HOLDINGS_TTL_SECONDS:
                return cached["view"]
            cfg = self._holdings_cfg()
        missing = [s for s, i in cfg.instruments.items() if not i.conid] if self.kind == "ibkr" else []
        try:
            with self.broker_lock:
                if self.kind == "ibkr":
                    usable = replace(cfg, instruments={s: i for s, i in cfg.instruments.items() if i.conid})
                    if not usable.instruments:
                        raise ApiError(
                            400,
                            "conid가 있는 종목이 없어서 잔고를 불러올 수 없어요. 목록에서 종목을 펼쳐 IBKR conid를 찾아 넣고 저장하세요.",
                            {"missing_conids": missing},
                        )
                    snap = IbkrBroker(usable, client=self.gateway).snapshot()
                else:
                    snap = self.broker_for(cfg).snapshot()
        except IbkrError as e:
            raise ApiError(502, str(e)) from e
        view = holdings_view(cfg, snap, missing)
        with self.lock:
            self._holdings = {"ts": time.time(), "view": view, "snap": snap}
        return view

    def _is_held(self, symbol: str) -> bool:
        with self.lock:
            if not self._holdings:
                return True  # unknown: keep the instrument so a holding is never orphaned
            return self._holdings["snap"].positions.get(symbol, 0) > 0

    # Draft -------------------------------------------------------------------

    def _no_chat_running(self) -> None:
        if self.assistant and self.assistant.busy:
            raise ApiError(409, "채팅이 초안을 고치는 중이에요. 답이 온 뒤에 다시 해 주세요.")

    def put_draft(self, version: int, portfolio: dict) -> dict:
        self._no_chat_running()
        with self.lock:
            try:
                self.draft.replace(portfolio, expected_version=version)
            except DraftConflict as e:
                raise ApiError(409, str(e), {"draft": self.draft.view()}) from e
            except DraftError as e:
                raise ApiError(400, str(e)) from e
            return self.draft.view()

    def reset_draft(self) -> dict:
        self._no_chat_running()
        with self.lock:
            self.draft.reset(self.saved)
            return self.draft.view()

    def save_draft(self) -> dict:
        self._no_chat_running()
        with self.lock:
            self._save_draft_locked(self.draft.portfolio)
            return self.state_view()

    def _save_draft_locked(self, portfolio: dict) -> None:
        try:
            self.saved = save_portfolio(self.saved, portfolio)
        except ConfigError as e:
            raise ApiError(400, "저장할 수 없어요.", {"problems": str(e).split("\n")}) from e
        self.saved_version += 1
        self.draft.reset(self.saved)

    # Plans and orders --------------------------------------------------------

    def _compute_plan(self, force_rebalance: bool) -> PlanRecord:
        with self.lock:
            cfg, problems = self.draft.evaluate()
            if cfg is None or problems:
                raise ApiError(400, "초안에 고칠 점이 있어서 계획을 계산할 수 없어요.", {"problems": problems})
            portfolio = copy.deepcopy(self.draft.portfolio)
            dirty = self.draft.dirty
            draft_version = self.draft.version
            saved_version = self.saved_version
        try:
            with self.broker_lock:
                snap = self.broker_for(cfg).snapshot()
            state = State(self.state_path)
            plan = build_plan(cfg, snap, self.today(), state.last_rebalance_year, REBALANCE if force_rebalance else None)
        except IbkrError as e:
            raise ApiError(502, str(e)) from e
        except PlanError as e:
            raise ApiError(400, str(e)) from e
        return PlanRecord(
            id=uuid.uuid4().hex,
            cfg=cfg,
            snap=snap,
            plan=plan,
            problems=safety_problems(cfg, plan, snap),
            portfolio=portfolio,
            dirty=dirty,
            draft_version=draft_version,
            saved_version=saved_version,
        )

    def make_plan(self, force_rebalance: bool = False) -> dict:
        if self._job_active():
            raise ApiError(409, "진행 중인 주문이 있어요. 먼저 그 주문을 끝내 주세요.")
        rec = self._compute_plan(force_rebalance)
        with self.lock:
            self.plans = {k: v for k, v in self.plans.items() if time.time() - v.created_at < PLAN_TTL_SECONDS}
            self.plans[rec.id] = rec
        state = State(self.state_path)
        key = month_key(self.today())
        done = state.execution(key)
        return {
            "id": rec.id,
            **plan_view(rec.cfg, rec.plan, rec.problems),
            "dirty": rec.dirty,
            "force_rebalance": force_rebalance,
            "broker": self.broker_info(),
            "month": {"key": key, "executed_at": done["at"] if done else None},
        }

    def start_orders(self, plan_id: str, allow_repeat: bool = False) -> dict:
        with self.lock:
            if self._job_active():
                raise ApiError(409, "진행 중인 주문이 있어요. 먼저 그 주문을 끝내 주세요.")
            rec = self.plans.get(plan_id)
            if rec is None:
                raise ApiError(410, "이 계획은 더 이상 쓸 수 없어요. 계획을 다시 계산해 주세요.")
            if time.time() - rec.created_at > PLAN_TTL_SECONDS:
                raise ApiError(410, "계획을 만든 지 10분이 넘었어요. 시세가 바뀌었을 수 있으니 다시 계산해 주세요.")
            if rec.used:
                raise ApiError(409, "이미 보낸 계획이에요. 새로 계산해 주세요.")
            if rec.saved_version != self.saved_version or (rec.dirty and rec.draft_version != self.draft.version):
                raise ApiError(409, "계획을 만든 뒤 목표가 바뀌었어요. 계획을 다시 계산해 주세요.")
            if rec.problems:
                raise ApiError(400, "안전 점검에 걸려서 보낼 수 없어요.", {"problems": rec.problems})
            if not rec.plan.orders:
                raise ApiError(400, "보낼 주문이 없어요.")
            today = self.today()
            key = month_key(today)
            if State(self.state_path).executed(key) and not allow_repeat:
                raise ApiError(409, f"{key}에 이미 주문했어요. 다시 보내려면 '이번 달에 한 번 더 보낼게요'를 체크하세요.")
            if self.assistant and self.assistant.busy:
                raise ApiError(409, "채팅이 초안을 고치는 중이에요. 답이 온 뒤에 다시 해 주세요.")

            cfg = rec.cfg
            if rec.dirty:
                self._save_draft_locked(rec.portfolio)
                cfg = self.saved
            rec.used = True
            kwargs = {"question_timeout": self.question_timeout} if self.question_timeout else {}
            job = OrderJob(cfg, self.broker_for(cfg), self.state_path, today, rec.plan, **kwargs)
            self.job = job
        job.start()
        return self.job_view(job)

    def job_view(self, job: OrderJob) -> dict:
        view = job.to_dict()
        if job.status == BUY_PLAN_READY and job.buy_plan is not None:
            view["buy_plan"] = plan_view(job.cfg, job.buy_plan, job.buy_problems)
        else:
            view["buy_plan"] = None
        return view

    def _get_job(self, job_id: str) -> OrderJob:
        with self.lock:
            if self.job is None or self.job.id != job_id:
                raise ApiError(404, "주문 작업을 찾을 수 없어요.")
            return self.job

    def answer(self, job_id: str, question_id: str, approve: bool) -> dict:
        job = self._get_job(job_id)
        try:
            job.answer(question_id, approve)
        except JobError as e:
            raise ApiError(409, str(e)) from e
        time.sleep(0.05)  # let the worker pick the answer up so the reply shows it
        return self.job_view(job)

    def buy_plan(self, job_id: str) -> dict:
        job = self._get_job(job_id)
        try:
            with self.broker_lock:
                broker = self.broker_for(job.cfg) if self.kind == "mock" else job.broker
                snap = broker.snapshot()
            state = State(self.state_path)
            plan = build_plan(job.cfg, snap, self.today(), state.last_rebalance_year, REBALANCE_BUYS)
            job.set_buy_plan(plan, snap, safety_problems(job.cfg, plan, snap))
        except IbkrError as e:
            raise ApiError(502, str(e)) from e
        except (PlanError, JobError) as e:
            raise ApiError(409, str(e)) from e
        return self.job_view(job)

    def send_buys(self, job_id: str) -> dict:
        job = self._get_job(job_id)
        try:
            job.send_buys()
        except JobError as e:
            raise ApiError(409, str(e)) from e
        return self.job_view(job)

    def finish_job(self, job_id: str) -> dict:
        job = self._get_job(job_id)
        try:
            job.finish()
        except JobError as e:
            raise ApiError(409, str(e)) from e
        return self.job_view(job)

    # Contracts ---------------------------------------------------------------

    def find_contracts(self, symbol: str, exchange: str = "LSE") -> list[dict]:
        if self.kind != "ibkr" or self.gateway is None:
            raise ApiError(
                400, "모의 계좌로 실행 중이라 IBKR에서 찾을 수 없어요. IBKR로 실행한 뒤 화면에서 conid를 채우면 돼요."
            )
        if not symbol.strip():
            raise ApiError(400, "찾을 티커를 넣어 주세요.")
        try:
            with self.broker_lock:
                return search_contracts(self.gateway, symbol, self.saved.base_currency, exchange or "LSE")
        except IbkrError as e:
            raise ApiError(502, str(e)) from e

    # Chat --------------------------------------------------------------------

    def chat(self, message: str) -> dict:
        if not self._chat_ready():
            raise ApiError(503, self.chat_view()["reason"] or "채팅이 꺼져 있어요.")
        try:
            result = self.assistant.send(message)
        except AssistantBusy as e:
            raise ApiError(409, str(e)) from e
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        with self.lock:
            draft = self.draft.view()
        return {**result, "draft": draft, "chat": self.chat_view()}

    def reset_chat(self) -> dict:
        if self.assistant is not None:
            try:
                self.assistant.reset()
            except AssistantBusy as e:
                raise ApiError(409, str(e)) from e
        return self.chat_view()

    # Sign in with ChatGPT -------------------------------------------------------

    def _auth(self) -> ChatGPTAuth:
        auth = getattr(self.provider, "auth", None)
        if not isinstance(auth, ChatGPTAuth):
            raise ApiError(400, "config.yaml의 assistant.provider가 chatgpt일 때만 ChatGPT로 로그인할 수 있어요.")
        return auth

    def chatgpt_login(self) -> dict:
        url = self._auth().start(f"http://127.0.0.1:{self.port}{CALLBACK_PATH}")
        return {"url": url}

    def chatgpt_callback(self, params: dict) -> dict:
        return self._auth().finish(params)

    def chatgpt_logout(self) -> dict:
        confirmed = self._auth().sign_out()
        return {"revoked": confirmed, "chat": self.chat_view()}

    # What the assistant can see and do (AssistantHost) ---------------------------

    def draft_overview(self) -> dict:
        with self.lock:
            view = self.draft.view()
            holdings = self._holdings["view"] if self._holdings else None
        portfolio = view["portfolio"]
        allocated = {a["symbol"] for a in portfolio["allocations"]}
        sleeves = {CORE: 0.0, SATELLITE: 0.0}
        for a in portfolio["allocations"]:
            sleeves[a["sleeve"]] += a["target"]
        overview: dict[str, Any] = {
            "allocations": portfolio["allocations"],
            "sleeve_targets": sleeves,
            "sum": round(sum(sleeves.values()), 4),
            "exits": [i["symbol"] for i in portfolio["instruments"] if i["symbol"] not in allocated],
            "instruments": portfolio["instruments"],
            "problems": view["problems"],
            "unsaved_changes": [c["text"] for c in view["changes"]],
        }
        if holdings and holdings["total"] > 0:
            total = holdings["total"]
            overview["holdings"] = {
                "as_of": holdings["fetched_at"],
                "total_value": round(total, 2),
                "cash_pct": round(holdings["cash"] / total * 100, 2),
                "weights_pct": {
                    p["symbol"]: round(p["value"] / total * 100, 2) for p in holdings["positions"] if p["value"]
                },
            }
        else:
            overview["holdings"] = "잔고를 아직 불러오지 않았어요."
        return overview

    def assistant_context(self) -> dict:
        return {
            "today": self.today().isoformat(),
            "broker": self.broker_info()["label"],
            "portfolio": self.draft_overview(),
        }

    def set_allocation(self, **kwargs: Any) -> str:
        with self.lock:
            return self.draft.set_allocation(**kwargs)

    def remove_allocation(self, sleeve: str, symbol: str) -> str:
        held = self._is_held(symbol.strip().upper())
        with self.lock:
            return self.draft.remove_allocation(sleeve, symbol, held)

    def preview_orders(self, force_rebalance: bool) -> dict:
        try:
            rec = self._compute_plan(force_rebalance)
        except ApiError as e:
            return {"error": e.message, **e.extra}
        view = plan_view(rec.cfg, rec.plan, rec.problems)
        keep = (
            "mode_label",
            "reason",
            "total_value",
            "cash",
            "buy_total",
            "sell_total",
            "fee_total",
            "fee_rule",
            "projected_cash",
            "two_step",
        )
        summary = {k: view[k] for k in keep}
        order_keys = ("side_label", "symbol", "quantity", "limit_price", "notional", "fee")
        summary["orders"] = [{k: o[k] for k in order_keys} for o in view["orders"]]
        summary["warnings"] = view["warnings"]
        summary["problems"] = view["problems"]
        summary["note"] = "계산만 했어요. 주문은 사용자가 '주문 계획 보기'에서 직접 보내야 나가요."
        return summary


# HTTP layer -------------------------------------------------------------------


class DraftBody(BaseModel):
    version: int
    portfolio: dict


class PlanBody(BaseModel):
    force_rebalance: bool = False


class OrdersBody(BaseModel):
    plan_id: str
    allow_repeat: bool = False


class AnswerBody(BaseModel):
    question_id: str
    approve: bool


class ChatBody(BaseModel):
    message: str


class CallbackUrlBody(BaseModel):
    url: str


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


# Pages and files anyone may load: the login page itself, Caddy's check, and the
# ChatGPT sign-in return (protected by its one-time state value).
OPEN_PATHS = {"/login", "/auth/check", CALLBACK_PATH, "/static/login.css", "/static/favicon.svg"}
IBKR_LOGIN_PATH = "/sso/Login?forwardTo=22&RL=1&ip2loc=US"
REAUTH_AFTER = 20.0  # seconds a dropped brokerage session must stay closed before the app reopens it


def _via_proxy(request: Request) -> bool:
    """The request came through Caddy, i.e. from outside this machine."""
    return "x-forwarded-for" in request.headers


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "local"


def _safe_next(target: str) -> str:
    """Only paths on this site, so the login page can't be used to send people elsewhere."""
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def create_app(ctx: App, allowed_hosts: set[str], webauth: Optional[WebAuth] = None) -> FastAPI:
    app = FastAPI(title="portfolio-manager", docs_url=None, redoc_url=None, openapi_url=None)
    if webauth is not None:
        ctx.webauth = webauth

    def signed_in(request: Request) -> bool:
        return webauth is not None and webauth.check(request.cookies.get(COOKIE_NAME, ""))

    def password_set() -> bool:
        return webauth is not None and webauth.configured

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "허용되지 않은 주소로 들어왔어요."}, status_code=403)
        path = request.url.path
        if path not in OPEN_PATHS:
            if password_set():
                if not signed_in(request):
                    if path.startswith("/api/"):
                        return JSONResponse({"detail": "로그인이 필요해요.", "login": True}, status_code=401)
                    target = path + (f"?{request.url.query}" if request.url.query else "")
                    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
            elif _via_proxy(request):
                return HTMLResponse(_setup_page(), status_code=503, headers=PAGE_HEADERS)
        if path.startswith("/api/"):
            token = request.headers.get("x-session-token", "")
            if not secrets.compare_digest(token.encode(), ctx.token.encode()):
                return JSONResponse({"detail": "세션이 맞지 않아요. 페이지를 새로고침하세요."}, status_code=403)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            if k not in response.headers:  # a page may set its own policy (the login form)
                response.headers[k] = v
        return response

    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, exc: ApiError):
        return JSONResponse({"detail": exc.message, **exc.extra}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def on_bad_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "요청 형식이 잘못됐어요."}, status_code=422)

    @app.exception_handler(ConfigError)
    async def on_config_error(request: Request, exc: ConfigError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/") -> HTMLResponse:
        if password_set() and signed_in(request):
            return RedirectResponse(_safe_next(next), status_code=303)
        message = "" if password_set() else _NO_PASSWORD
        return HTMLResponse(_login_page(_safe_next(next), message), headers=LOGIN_HEADERS)

    @app.post("/login")
    async def login(request: Request):
        form = dict(parse_qsl((await request.body()).decode("utf-8", "replace"), keep_blank_values=True))
        target = _safe_next(form.get("next", "/"))
        if webauth is None or not webauth.configured:
            return HTMLResponse(_login_page(target, _NO_PASSWORD), status_code=503, headers=LOGIN_HEADERS)
        try:
            sid = webauth.login(form.get("password", ""), _client_key(request))
        except LoginError as e:
            status = 429 if "너무 많아요" in str(e) else 401
            return HTMLResponse(_login_page(target, str(e)), status_code=status, headers=LOGIN_HEADERS)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            sid,
            max_age=MAX_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=request.headers.get("x-forwarded-proto", "") == "https",
            path="/",
        )
        return response

    @app.post("/api/session/logout")
    def logout(request: Request):
        if webauth is not None:
            webauth.logout(request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/auth/check")
    def auth_check(request: Request):
        # Caddy asks this before passing a request on to the IBKR Gateway.
        if password_set() and signed_in(request):
            return Response(status_code=204)
        method = request.headers.get("x-forwarded-method", "GET")
        uri = request.headers.get("x-forwarded-uri", "/")
        if method == "GET" and not uri.startswith("/v1/api/"):
            return RedirectResponse(f"/login?next={quote(_safe_next(uri), safe='')}", status_code=303)
        return JSONResponse({"detail": "로그인이 필요해요."}, status_code=401)

    @app.get("/api/ibkr/status")
    def ibkr_status(request: Request):
        view = ctx.ibkr_status(reauth=True)
        if view.get("kind") == "ibkr":
            view["login_url"] = IBKR_LOGIN_PATH if _via_proxy(request) else "https://localhost:5000/"
        return view

    @app.post("/api/chatgpt/callback-url")
    def chatgpt_callback_url(body: CallbackUrlBody):
        # When the screen is opened from outside, ChatGPT's return address
        # (http://127.0.0.1:.../auth/callback) does not open in the browser. The person
        # pastes that address here and the server finishes the sign-in with it.
        parts = urlsplit(body.url.strip())
        if parts.path != CALLBACK_PATH or not parts.query:
            raise ApiError(400, "붙여 넣은 주소가 ChatGPT 로그인 뒤의 주소가 아니에요. 주소창의 주소를 통째로 복사해 주세요.")
        try:
            ctx.chatgpt_callback(dict(parse_qsl(parts.query)))
        except ChatGPTAuthError as e:
            raise ApiError(400, str(e)) from e
        return ctx.chat_view()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__SESSION_TOKEN__", ctx.token))

    @app.get("/static/{name}")
    def static(name: str):
        types = {
            "app.js": "text/javascript",
            "app.css": "text/css",
            "login.css": "text/css",
            "favicon.svg": "image/svg+xml",
        }
        if name not in types:
            return JSONResponse({"detail": "없는 파일이에요."}, status_code=404)
        return FileResponse(STATIC / name, media_type=types[name])

    @app.get("/api/state")
    def get_state():
        return ctx.state_view()

    @app.get("/api/holdings")
    def get_holdings(refresh: bool = False):
        return ctx.holdings(refresh)

    @app.put("/api/draft")
    def put_draft(body: DraftBody):
        return ctx.put_draft(body.version, body.portfolio)

    @app.post("/api/draft/reset")
    def reset_draft():
        return ctx.reset_draft()

    @app.post("/api/draft/save")
    def save_draft():
        return ctx.save_draft()

    @app.post("/api/plan")
    def make_plan(body: PlanBody):
        return ctx.make_plan(body.force_rebalance)

    @app.post("/api/orders")
    def start_orders(body: OrdersBody):
        return ctx.start_orders(body.plan_id, body.allow_repeat)

    @app.get("/api/jobs/current")
    def current_job():
        with ctx.lock:
            job = ctx.job
        return {"job": ctx.job_view(job) if job else None}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return ctx.job_view(ctx._get_job(job_id))

    @app.post("/api/jobs/{job_id}/answer")
    def answer(job_id: str, body: AnswerBody):
        return ctx.answer(job_id, body.question_id, body.approve)

    @app.post("/api/jobs/{job_id}/buy-plan")
    def buy_plan(job_id: str):
        return ctx.buy_plan(job_id)

    @app.post("/api/jobs/{job_id}/buys")
    def send_buys(job_id: str):
        return ctx.send_buys(job_id)

    @app.post("/api/jobs/{job_id}/finish")
    def finish(job_id: str):
        return ctx.finish_job(job_id)

    @app.get("/api/contracts")
    def contracts(symbol: str, exchange: str = "LSE"):
        return {"candidates": ctx.find_contracts(symbol, exchange)}

    @app.get("/api/chat")
    def get_chat():
        return ctx.chat_view()

    @app.post("/api/chat")
    def chat(body: ChatBody):
        return ctx.chat(body.message)

    @app.post("/api/chat/reset")
    def reset_chat():
        return ctx.reset_chat()

    @app.post("/api/chatgpt/login")
    def chatgpt_login():
        return ctx.chatgpt_login()

    @app.post("/api/chatgpt/logout")
    def chatgpt_logout():
        return ctx.chatgpt_logout()

    @app.get(CALLBACK_PATH)
    def chatgpt_callback(request: Request):
        # The browser lands here from ChatGPT. The one-time state value ties it to a
        # sign-in this server started, so it needs no session token.
        try:
            ctx.chatgpt_callback(dict(request.query_params))
        except ChatGPTAuthError as e:
            return HTMLResponse(_callback_page("ChatGPT 로그인이 끝나지 않았어요", str(e)), status_code=400)
        except ApiError as e:
            return HTMLResponse(_callback_page("ChatGPT 로그인이 끝나지 않았어요", e.message), status_code=e.status)
        return RedirectResponse("/?chatgpt=ok", status_code=303)

    return app


def _callback_page(title: str, message: str) -> str:
    from html import escape

    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title></head><body><main><h1>{escape(title)}</h1><p>{escape(message)}</p>"
        '<p><a href="/">포트폴리오 화면으로 돌아가기</a></p></main></body></html>'
    )


_NO_PASSWORD = (
    "아직 비밀번호가 없어요. 서버에서 다음 명령으로 먼저 정해 주세요: "
    "cd ~/portfolio-manager && .venv/bin/python -m rebalancer set-password"
)

# The login page posts a plain form, so it may submit to this site (form-action 'self').
LOGIN_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}
PAGE_HEADERS = {"Cache-Control": "no-store"}


def _login_page(next_path: str, message: str = "") -> str:
    from html import escape

    error = f'<p class="error" role="alert">{escape(message)}</p>' if message else ""
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        "<title>로그인 - 포트폴리오</title>"
        '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/static/login.css"></head><body>'
        '<main class="card"><h1>포트폴리오</h1><p class="lead">비밀번호를 입력하세요.</p>'
        f"{error}"
        '<form method="post" action="/login">'
        f'<input type="hidden" name="next" value="{escape(next_path)}">'
        '<input type="text" name="username" value="portfolio" autocomplete="username" hidden>'
        '<label for="password">비밀번호</label>'
        '<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>'
        '<button type="submit">로그인</button></form></main></body></html>'
    )


def _setup_page() -> str:
    from html import escape

    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>비밀번호를 먼저 정해 주세요</title></head><body><main>"
        "<h1>비밀번호를 먼저 정해 주세요</h1>"
        f"<p>{escape(_NO_PASSWORD)}</p></main></body></html>"
    )


def _port_free(port: int) -> bool:
    """True when nothing is listening on the port.

    Connections left over from a previous run (FIN_WAIT, TIME_WAIT, e.g. a browser that
    stayed connected through an SSH tunnel) don't count: uvicorn binds with SO_REUSEADDR
    on Unix too, so they don't stop it. On Windows SO_REUSEADDR would let the bind steal
    a port in use, so it is left off there."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def serve(
    config_path: str,
    broker_kind: Optional[str] = None,
    mock_file: Optional[str] = None,
    today: Optional[date] = None,
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    import uvicorn

    if not _port_free(port):
        raise ConfigError(f"{port}번 포트를 이미 쓰고 있어요. --port로 다른 번호를 주세요.")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    base = Path(config_path).resolve().parent
    load_env_file(base / ".env")
    cfg = load_config(config_path)
    settings = cfg.assistant
    auth = ChatGPTAuth(base / "chatgpt-auth.json") if settings.provider == "chatgpt" else None
    webauth = WebAuth(base / "web-auth.json", sessions_path=base / "web-sessions.json")
    provider, reason = make_provider(settings, auth=auth)
    ctx = App(
        config_path, broker_kind, mock_file, today, provider=provider, chat_reason=reason, port=port, keepalive=True
    )
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    public = cfg.web.public_host.strip()
    if public:
        hosts |= {public, f"{public}:443"}
    app = create_app(ctx, hosts, webauth)
    url = f"http://127.0.0.1:{port}/"
    print(f"웹 화면: {url}" + (f" (밖에서는 https://{public})" if public else ""))
    if webauth.configured:
        print("로그인: 비밀번호를 물어봐요.")
    elif public:
        print("로그인: 비밀번호가 아직 없어서 밖에서는 열리지 않아요. set-password로 정해 주세요.")
    print(f"브로커: {ctx.broker_info()['label']} ({ctx.broker_info()['detail']})")
    if provider is None:
        print(f"채팅: 꺼짐. {reason}")
    elif not getattr(provider, "ready", True):
        print(f"채팅: {provider.label} 로그인 전이에요. 화면의 채팅 창에서 'ChatGPT로 로그인'을 누르세요.")
    else:
        print(f"채팅: 켜짐 ({provider.label}, {provider.model})")
    print("끄려면 Ctrl+C")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
