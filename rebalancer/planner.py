"""Turns the account snapshot and the target weights into a list of limit orders.

Two modes:
- contribution: the monthly default. Buy the most underweight holdings with the
  cash on hand (at most `max_buy_orders`), never sell.
- rebalance: sell what is overweight, then buy what is underweight. Triggered when
  a holding is overweight by `drift_threshold_pp` or more, once a year in
  `annual_rebalance_month`, or on request. Skipped while the portfolio is smaller
  than `min_value_for_rebalance`, where contributions alone fix the weights.

Instruments that stay in the list without a target ("exits") count as a 0% target
while they are held: never bought, and sold in full at the next rebalance.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

from .config import Config
from .models import BUY, SELL, AccountSnapshot, Order, Plan, Quote, round_down_cents, round_up_cents

CONTRIBUTION = "contribution"
REBALANCE = "rebalance"
REBALANCE_BUYS = "rebalance-buys"

MODE_LABELS = {
    CONTRIBUTION: "정기 적립",
    REBALANCE: "리밸런싱",
    REBALANCE_BUYS: "리밸런싱 (매수 단계)",
}


class PlanError(RuntimeError):
    """Raised when a sensible plan cannot be built from the snapshot."""


def build_plan(
    cfg: Config,
    snap: AccountSnapshot,
    today: date,
    last_rebalance_year: Optional[int] = None,
    force_mode: Optional[str] = None,
) -> Plan:
    if force_mode not in (None, REBALANCE, REBALANCE_BUYS):
        raise ValueError(f"unknown force_mode: {force_mode}")

    r = cfg.rules
    targets = {s: pct / 100 for s, pct in cfg.symbol_targets().items()}
    held_exits = [s for s in cfg.exit_symbols if snap.positions.get(s, 0) > 0]
    for sym in held_exits:
        targets[sym] = 0.0
    warnings: list[str] = []

    marks: dict[str, float] = {}
    for sym in targets:
        q = snap.quotes.get(sym)
        mark = q.mark if q else None
        if mark is None or mark <= 0:
            if snap.positions.get(sym, 0) > 0:
                raise PlanError(f"{sym}: 보유 중인데 시세가 없어서 평가할 수 없어요.")
            warnings.append(f"{sym}: 시세가 없어서 이번 계획에서 빠졌어요.")
            continue
        marks[sym] = mark

    for desc in snap.unknown_positions:
        warnings.append(f"설정에 없는 종목을 보유 중이에요: {desc}. 계산에서 제외했어요.")

    values = {s: snap.positions.get(s, 0) * marks[s] for s in marks}
    invested = sum(values.values())
    total = invested + snap.cash
    if total <= 0:
        raise PlanError("평가할 자산과 현금이 없어요.")

    current = {s: values.get(s, 0.0) / total for s in targets}

    if force_mode == REBALANCE_BUYS:
        mode, reason = REBALANCE_BUYS, "매도 체결 후 남은 현금으로 모자란 종목을 사요."
    else:
        mode, reason = _choose_mode(cfg, current, targets, invested, today, last_rebalance_year, force_mode)

    if mode != REBALANCE:
        for sym in held_exits:
            warnings.append(
                f"{sym}: 목표에서 뺀 종목이에요. 매도는 리밸런싱 때 해요. 지금 정리하려면 매도 포함 리밸런싱으로 계산하세요."
            )

    cash_available = max(0.0, snap.cash - r.cash_reserve)
    if mode == REBALANCE:
        orders = _rebalance_orders(cfg, snap, targets, marks, values, invested + cash_available, warnings)
    else:
        max_orders = r.max_buy_orders if mode == CONTRIBUTION else len(targets)
        orders, _ = _buy_orders(
            cfg, targets, marks, values, snap.quotes, invested + cash_available, cash_available, max_orders, warnings
        )

    for o in orders:
        share = o.fee / o.notional * 100 if o.notional else 0.0
        if share >= cfg.fees.warn_pct:
            warnings.append(
                f"{o.symbol}: 주문 금액 대비 예상 수수료 비율이 {share:.1f}%예요 (주문 {o.notional:,.2f}, 수수료 약 {o.fee:,.2f}). "
                "작은 주문은 최소 수수료 때문에 비율이 커져요."
            )

    if mode == CONTRIBUTION and snap.cash < r.monthly_contribution * 0.5:
        warnings.append(
            f"현금({snap.cash:,.2f})이 월 적립액({r.monthly_contribution:,.0f})의 절반보다 적어요. 입금이 됐는지 확인하세요."
        )

    projected, projected_cash = _project(targets, marks, values, snap.cash, orders)
    return Plan(
        mode=mode,
        reason=reason,
        total_value=total,
        cash=snap.cash,
        orders=orders,
        current_weights=current,
        target_weights=targets,
        projected_weights=projected,
        projected_cash=projected_cash,
        warnings=warnings,
    )


def _choose_mode(
    cfg: Config,
    current: dict[str, float],
    targets: dict[str, float],
    invested: float,
    today: date,
    last_rebalance_year: Optional[int],
    force_mode: Optional[str],
) -> tuple[str, str]:
    r = cfg.rules
    if force_mode == REBALANCE:
        return REBALANCE, "직접 요청한 리밸런싱이에요."
    if invested < r.min_value_for_rebalance:
        return CONTRIBUTION, (
            f"투자 금액이 {r.min_value_for_rebalance:,.0f} 미만이라 매도 없이 모자란 종목부터 사요."
        )
    drift_pp, sym = max(((current[s] - targets[s]) * 100, s) for s in targets)
    if drift_pp >= r.drift_threshold_pp:
        if targets[sym] == 0:
            return REBALANCE, (
                f"{sym}: 목표에서 뺐는데 {drift_pp:.1f}%p가 남아 있어요 (기준 {r.drift_threshold_pp:g}%p)."
            )
        return REBALANCE, f"{sym}: 목표보다 {drift_pp:.1f}%p 많아요 (기준 {r.drift_threshold_pp:g}%p)."
    if today.month == r.annual_rebalance_month and last_rebalance_year != today.year:
        return REBALANCE, f"{today.year}년 연간 리밸런싱이에요."
    return CONTRIBUTION, "정기 적립이에요. 모자란 종목부터 사요."


def _buy_orders(
    cfg: Config,
    targets: dict[str, float],
    marks: dict[str, float],
    values: dict[str, float],
    quotes: dict[str, Quote],
    total_for_targets: float,
    cash_available: float,
    max_orders: int,
    warnings: list[str],
) -> tuple[list[Order], float]:
    """Fill the largest shortfalls first, in whole shares, within the cash on hand."""
    r = cfg.rules
    shortfalls = []
    for sym, weight in targets.items():
        if sym not in marks:
            continue
        gap = weight * total_for_targets - values.get(sym, 0.0)
        if gap >= r.min_order_value:
            shortfalls.append((gap, sym))
    shortfalls.sort(reverse=True)

    orders: list[Order] = []
    remaining = cash_available
    for gap, sym in shortfalls:
        if len(orders) >= max_orders:
            break
        ref = quotes[sym].buy_reference
        if not ref:
            continue
        limit = round_up_cents(ref * (1 + r.limit_buffer_pct / 100))
        budget = min(gap, remaining, r.max_order_value)
        qty = math.floor(budget / limit)
        fee = 0.0
        while qty > 0:  # the estimated commission comes out of the same cash
            fee = cfg.fees.estimate(qty * limit)
            if qty * limit + fee <= remaining + 1e-9:
                break
            qty -= 1
        if qty <= 0:
            continue
        notional = qty * limit
        if notional < r.min_order_value:
            continue
        if gap > r.max_order_value and budget == r.max_order_value:
            warnings.append(f"{sym}: 1회 주문 한도 {r.max_order_value:,.0f}에 걸려서 일부만 사요.")
        orders.append(Order(sym, BUY, qty, limit, fee))
        remaining -= notional + fee
    return orders, remaining


def _rebalance_orders(
    cfg: Config,
    snap: AccountSnapshot,
    targets: dict[str, float],
    marks: dict[str, float],
    values: dict[str, float],
    investable_total: float,
    warnings: list[str],
) -> list[Order]:
    r = cfg.rules
    sells: list[Order] = []
    proceeds = 0.0
    for sym, weight in targets.items():
        if sym not in marks:
            continue
        excess = values.get(sym, 0.0) - weight * investable_total
        if excess < r.min_order_value:
            continue
        ref = snap.quotes[sym].sell_reference
        if not ref:
            continue
        held = math.floor(snap.positions.get(sym, 0))
        qty = min(math.floor(min(excess, r.max_order_value) / ref), held)
        if qty <= 0:
            continue
        if excess > r.max_order_value:
            warnings.append(f"{sym}: 1회 주문 한도 {r.max_order_value:,.0f}에 걸려서 일부만 팔아요.")
        limit = round_down_cents(ref * (1 - r.limit_buffer_pct / 100))
        fee = cfg.fees.estimate(qty * limit)
        sells.append(Order(sym, SELL, qty, limit, fee))
        proceeds += qty * limit - fee

    post_values = dict(values)
    for o in sells:
        post_values[o.symbol] -= o.quantity * marks[o.symbol]
    cash_after = max(0.0, snap.cash - r.cash_reserve) + proceeds
    buys, _ = _buy_orders(
        cfg, targets, marks, post_values, snap.quotes, investable_total, cash_after, len(targets), warnings
    )
    return sells + buys


def _project(
    targets: dict[str, float],
    marks: dict[str, float],
    values: dict[str, float],
    cash: float,
    orders: list[Order],
) -> tuple[dict[str, float], float]:
    v = dict(values)
    c = cash
    for o in orders:
        moved = o.quantity * marks[o.symbol]
        if o.side == BUY:
            v[o.symbol] = v.get(o.symbol, 0.0) + moved
        else:
            v[o.symbol] = v.get(o.symbol, 0.0) - moved
        c += o.cash_effect
    total = sum(v.values()) + c
    return {s: v.get(s, 0.0) / total for s in targets}, c
