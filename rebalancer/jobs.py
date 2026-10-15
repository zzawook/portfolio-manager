"""Order jobs for the web screen.

A job sends the orders of a plan the person confirmed with a button. It runs in a
worker thread so the page stays responsive. When IBKR asks for a confirmation
(price checks, missing market data, size warnings), the job stops and waits for
the person to approve or decline that order in the browser. Nothing is approved
automatically, and a question left unanswered counts as a decline.

A rebalance sends the sells first. After the person has checked the fills, the
buys are planned again from the cash actually there and confirmed again.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from .brokers import Broker
from .config import Config
from .execute import client_order_id, order_stamp
from .models import AccountSnapshot, Order, Plan
from .planner import MODE_LABELS, REBALANCE
from .state import State, month_key

RUNNING = "running"
QUESTION = "question"
SELLS_DONE = "sells_done"
BUY_PLAN_READY = "buy_plan_ready"
DONE = "done"
FAILED = "failed"
ACTIVE = {RUNNING, QUESTION, SELLS_DONE, BUY_PLAN_READY}

QUESTION_TIMEOUT = 15 * 60
SIDE_LABELS = {"BUY": "매수", "SELL": "매도"}


class JobError(RuntimeError):
    """An action that does not fit the job's current step."""


class OrderJob:
    def __init__(
        self,
        cfg: Config,
        broker: Broker,
        state_path: str | Path,
        today: date,
        plan: Plan,
        *,
        question_timeout: float = QUESTION_TIMEOUT,
    ):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.broker = broker
        self.state_path = Path(state_path)
        self.today = today
        self.key = month_key(today)
        self.plan = plan
        self.question_timeout = question_timeout
        self.status = RUNNING
        self.orders: list[dict] = []
        self.question: Optional[dict] = None
        self.buy_plan: Optional[Plan] = None
        self.buy_snapshot: Optional[AccountSnapshot] = None
        self.buy_problems: list[str] = []
        self.error: Optional[str] = None
        self.recorded = False
        self._sequence = 0
        self._stamp = order_stamp()
        self._lock = threading.Lock()
        self._answer_event = threading.Event()
        self._answer: Optional[bool] = None
        self._thread: Optional[threading.Thread] = None

    # Status ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def two_step(self) -> bool:
        """A rebalance with sells: buys are planned again after the sells fill."""
        return self.plan.mode == REBALANCE and bool(self.plan.sells)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "active": self.status in ACTIVE,
                "mode": self.plan.mode,
                "mode_label": MODE_LABELS.get(self.plan.mode, self.plan.mode),
                "two_step": self.two_step,
                "month": self.key,
                "orders": [dict(e, side_label=SIDE_LABELS[e["side"]]) for e in self.orders],
                "question": dict(self.question) if self.question else None,
                "error": self.error,
                "submitted": sum(1 for e in self.orders if e["status"] == "submitted"),
            }

    def wait(self, timeout: float = 10.0) -> None:
        """Wait for the worker thread (tests and shutdown)."""
        if self._thread is not None:
            self._thread.join(timeout)

    # Sending -----------------------------------------------------------------

    def start(self) -> None:
        first = self.plan.sells if self.plan.sells else self.plan.buys
        self._launch(first, first_batch=True)

    def _launch(self, orders: list[Order], first_batch: bool) -> None:
        entries = [self._entry(o) for o in orders]
        with self._lock:
            self.orders.extend(entries)
            self.status = RUNNING
        self._thread = threading.Thread(
            target=self._run, args=(orders, entries, first_batch), daemon=True, name=f"order-job-{self.id}"
        )
        self._thread.start()

    @staticmethod
    def _entry(o: Order) -> dict:
        return {
            "symbol": o.symbol,
            "side": o.side,
            "quantity": o.quantity,
            "limit_price": o.limit_price,
            "notional": o.notional,
            "fee": o.fee,
            "status": "pending",
            "detail": "",
            "order_id": "",
            "coid": "",
        }

    def _run(self, orders: list[Order], entries: list[dict], first_batch: bool) -> None:
        results: list[dict] = []
        try:
            for order, entry in zip(orders, entries):
                coid = client_order_id(self.key, self._stamp, self._sequence)
                self._sequence += 1
                with self._lock:
                    entry["status"] = "sending"
                    entry["coid"] = coid
                res = self.broker.place_order(order, coid, self._confirm_for(entry))
                with self._lock:
                    entry["status"] = str(res.get("status") or "rejected")
                    entry["order_id"] = str(res.get("order_id") or "")
                    if not entry["detail"]:
                        entry["detail"] = str(res.get("detail") or "")
                results.append(
                    {
                        "symbol": order.symbol,
                        "side": order.side,
                        "quantity": order.quantity,
                        "limit": order.limit_price,
                        "coid": coid,
                        **res,
                    }
                )
        except Exception as e:  # a broker or network failure: stop, never guess
            with self._lock:
                self.error = f"주문을 보내다가 멈췄어요: {e}"
                for entry in entries:
                    if entry["status"] == "sending":
                        entry["status"] = "error"
                        entry["detail"] = "응답을 받지 못했어요. IBKR에서 주문 내역을 직접 확인하세요."
                    elif entry["status"] == "pending":
                        entry["status"] = "skipped"
                        entry["detail"] = "앞 주문에서 멈춰서 보내지 않았어요."
        finally:
            self._record(results)
            with self._lock:
                if self.error:
                    self.status = FAILED
                elif first_batch and self.two_step:
                    self.status = SELLS_DONE
                else:
                    self.status = DONE

    def _confirm_for(self, entry: dict):
        def confirm(messages: list[str]) -> bool:
            question_id = uuid.uuid4().hex[:8]
            with self._lock:
                self._answer = None
                self._answer_event.clear()
                self.question = {
                    "id": question_id,
                    "symbol": entry["symbol"],
                    "side": entry["side"],
                    "side_label": SIDE_LABELS[entry["side"]],
                    "quantity": entry["quantity"],
                    "limit_price": entry["limit_price"],
                    "messages": [str(m) for m in messages],
                }
                self.status = QUESTION
            answered = self._answer_event.wait(self.question_timeout)
            with self._lock:
                approved = bool(self._answer) if answered else False
                if not answered:
                    entry["detail"] = "확인 요청에 답이 없어서 보내지 않았어요."
                self.question = None
                self.status = RUNNING
            return approved

        return confirm

    def answer(self, question_id: str, approve: bool) -> None:
        with self._lock:
            if self.question is None or self.question["id"] != question_id:
                raise JobError("지금 답할 확인 요청이 없어요.")
            if self._answer is not None:
                raise JobError("이미 답한 확인 요청이에요.")  # a second click never flips the first answer
            self._answer = bool(approve)
            self._answer_event.set()

    def _record(self, results: list[dict]) -> None:
        submitted = [r for r in results if r.get("status") == "submitted"]
        if not submitted and not self.recorded:
            return
        try:
            state = State(self.state_path)
            if not self.recorded:
                rebalanced_year = self.today.year if self.plan.mode == REBALANCE else None
                state.record_execution(self.key, self.plan.mode, self.plan.reason, results, rebalanced_year)
                state.record_targets(self.cfg, self.today)
                self.recorded = True
            else:
                state.extend_execution(self.key, results)
            state.save()
        except Exception as e:  # keep the orders' status visible even if the log can't be written
            with self._lock:
                note = f"실행 기록을 저장하지 못했어요: {e}"
                self.error = f"{self.error} {note}" if self.error else note

    # The buy step of a rebalance ---------------------------------------------

    def set_buy_plan(self, plan: Plan, snap: AccountSnapshot, problems: list[str]) -> None:
        with self._lock:
            if self.status not in (SELLS_DONE, BUY_PLAN_READY):
                raise JobError("지금은 매수 계획을 계산할 단계가 아니에요.")
            self.buy_plan, self.buy_snapshot, self.buy_problems = plan, snap, list(problems)
            self.status = BUY_PLAN_READY

    def send_buys(self) -> None:
        with self._lock:
            if self.status != BUY_PLAN_READY or self.buy_plan is None:
                raise JobError("먼저 매수 계획을 계산하세요.")
            if self.buy_problems:
                raise JobError("안전 점검에 걸려서 보낼 수 없어요: " + " / ".join(self.buy_problems))
            buys = list(self.buy_plan.buys)
            if not buys:
                self.status = DONE
                return
        self._launch(buys, first_batch=False)

    def finish(self) -> None:
        with self._lock:
            if self.status in (RUNNING, QUESTION):
                raise JobError("주문을 보내는 중이라 아직 끝낼 수 없어요.")
            self.status = DONE
