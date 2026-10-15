"""Order execution with the safety rails: one run per month, allowlist, per-order cap,
explicit 'yes' before sending, and every broker warning approved by a person."""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable

from .brokers import Broker
from .config import Config
from .models import SELL, AccountSnapshot, Order, Plan
from .planner import REBALANCE, REBALANCE_BUYS, build_plan
from .report import format_plan
from .state import State, month_key

InputFn = Callable[[str], str]
OutFn = Callable[[str], None]


class ExecutionAborted(RuntimeError):
    pass


def safety_problems(cfg: Config, plan: Plan, snap: AccountSnapshot) -> list[str]:
    problems: list[str] = []
    for o in plan.orders:
        if o.symbol not in cfg.allowlist:
            problems.append(f"{o.symbol}: 허용 목록에 없는 종목이에요.")
        if o.quantity <= 0:
            problems.append(f"{o.symbol}: 수량이 0 이하예요.")
        if o.notional > cfg.rules.max_order_value + 0.01:
            problems.append(f"{o.symbol}: 주문 금액 {o.notional:,.2f}가 1회 한도 {cfg.rules.max_order_value:,.0f}를 넘어요.")
        if o.side == SELL and o.quantity > snap.positions.get(o.symbol, 0):
            problems.append(f"{o.symbol}: 보유 수량보다 많이 팔려고 해요.")
    buy_total = sum(o.notional for o in plan.buys)
    sell_total = sum(o.notional for o in plan.sells)
    if buy_total > snap.cash + sell_total + 0.01:
        problems.append(f"매수 합계 {buy_total:,.2f}가 쓸 수 있는 현금보다 많아요.")
    return problems


def _ask_yes(input_fn: InputFn, prompt: str) -> bool:
    return input_fn(prompt).strip().lower() == "yes"


def _confirm_warning(input_fn: InputFn, out: OutFn) -> Callable[[list[str]], bool]:
    def confirm(messages: list[str]) -> bool:
        out("증권사가 주문 전에 확인을 요청했어요:")
        for m in messages:
            out(f"  > {m}")
        return _ask_yes(input_fn, "이 경고를 승인하고 주문을 보낼까요? 보내려면 yes: ")

    return confirm


def _place(
    broker: Broker, orders: list[Order], key: str, input_fn: InputFn, out: OutFn, start: int = 0
) -> list[dict]:
    results = []
    stamp = datetime.now().strftime("%d%H%M%S")
    for i, o in enumerate(orders, start=start):
        coid = f"rb{key.replace('-', '')}{stamp}{i:02d}"
        res = broker.place_order(o, coid, _confirm_warning(input_fn, out))
        out(
            f"  {o.side} {o.symbol} {o.quantity}주 @ {o.limit_price:,.2f}: "
            f"{res.get('status')} {res.get('order_id', '')} {res.get('detail', '')}".rstrip()
        )
        results.append(
            {"symbol": o.symbol, "side": o.side, "quantity": o.quantity, "limit": o.limit_price, "coid": coid, **res}
        )
    return results


def _confirmed_plan(cfg: Config, broker: Broker, snap: AccountSnapshot, plan: Plan, input_fn: InputFn, out: OutFn) -> bool:
    out(format_plan(cfg, snap, plan))
    if not plan.orders:
        return False
    problems = safety_problems(cfg, plan, snap)
    if problems:
        raise ExecutionAborted("안전 점검에 걸려서 멈췄어요:\n" + "\n".join(f"  - {p}" for p in problems))
    out("")
    if plan.sells and plan.buys:
        out("매도 주문만 먼저 보내요. 체결된 뒤 현금 기준으로 매수 계획을 다시 계산해서 한 번 더 확인받아요.")
        return _ask_yes(input_fn, f"[{broker.name}] 위 매도 주문을 보낼까요? 보내려면 yes: ")
    return _ask_yes(input_fn, f"[{broker.name}] 위 주문을 보낼까요? 보내려면 yes: ")


def run_execute(
    cfg: Config,
    broker: Broker,
    state: State,
    today: date,
    *,
    force: bool = False,
    force_rebalance: bool = False,
    input_fn: InputFn | None = None,
    out: OutFn | None = None,
) -> list[dict]:
    input_fn = input_fn or (lambda prompt: input(prompt))
    out = out or print
    key = month_key(today)
    if state.executed(key) and not force:
        raise ExecutionAborted(f"{key}에 이미 실행했어요. 정말 다시 보내려면 --force를 붙이세요.")

    snap = broker.snapshot()
    plan = build_plan(cfg, snap, today, state.last_rebalance_year, REBALANCE if force_rebalance else None)
    if not _confirmed_plan(cfg, broker, snap, plan, input_fn, out):
        out("주문을 보내지 않았어요.")
        return []

    first_batch = plan.sells if plan.sells else plan.buys
    results = _place(broker, first_batch, key, input_fn, out)

    if plan.mode == REBALANCE and plan.sells:
        out("")
        out("매도 주문을 보냈어요. 체결된 걸 확인한 뒤 Enter를 누르면, 그때 현금으로 매수 계획을 다시 계산해요.")
        if input_fn("Enter로 계속, 그만하려면 q: ").strip().lower() != "q":
            snap2 = broker.snapshot()
            buy_plan = build_plan(cfg, snap2, today, state.last_rebalance_year, REBALANCE_BUYS)
            if _confirmed_plan(cfg, broker, snap2, buy_plan, input_fn, out):
                results += _place(broker, buy_plan.buys, key, input_fn, out, start=len(results))

    submitted = [r for r in results if r.get("status") == "submitted"]
    if submitted:
        rebalanced_year = today.year if plan.mode == REBALANCE else None
        state.record_execution(key, plan.mode, plan.reason, results, rebalanced_year)
        changes = state.record_targets(cfg, today)
        if changes:
            out(f"목표 비중 변경 {len(changes)}건을 기록했어요.")
        state.save()
        out(f"{len(submitted)}건을 보냈어요. 기록: {state.path}")
    else:
        out("접수된 주문이 없어서 이번 달 실행으로 기록하지 않았어요.")
    return results
