from __future__ import annotations

from .config import Config
from .models import BUY, SLEEVES, AccountSnapshot, Plan
from .planner import CONTRIBUTION, MODE_LABELS

SLEEVE_LABELS = {"core": "코어", "satellite": "위성"}
SIDE_LABELS = {"BUY": "매수", "SELL": "매도"}


def _pct(x: float) -> str:
    return f"{x * 100:6.1f}%"


def sleeve_weights(cfg: Config, weights: dict[str, float]) -> dict[str, float]:
    """Split a symbol held in both sleeves (e.g. IGLN) in proportion to its targets."""
    sym_targets = cfg.symbol_targets()
    out = {s: 0.0 for s in SLEEVES}
    for a in cfg.allocations:
        share = a.target_pct / sym_targets[a.symbol]
        out[a.sleeve] += weights.get(a.symbol, 0.0) * share
    return out


def format_plan(cfg: Config, snap: AccountSnapshot, plan: Plan) -> str:
    ccy = cfg.base_currency
    invested = plan.total_value - plan.cash
    lines = [
        f"== 계획: {MODE_LABELS.get(plan.mode, plan.mode)} ==",
        f"이유: {plan.reason}",
        f"총자산 {plan.total_value:,.2f} {ccy} (투자 {invested:,.2f} + 현금 {plan.cash:,.2f})",
        "",
        f"{'Symbol':<8}{'Target':>8}{'Now':>8}{'Diff(pp)':>10}{'After':>8}",
    ]
    for sym, target in plan.target_weights.items():
        now = plan.current_weights.get(sym, 0.0)
        after = plan.projected_weights.get(sym, 0.0)
        diff_pp = (now - target) * 100
        lines.append(f"{sym:<8}{_pct(target):>8}{_pct(now):>8}{diff_pp:>+10.1f}{_pct(after):>8}")
    cash_now = plan.cash / plan.total_value
    cash_after = plan.projected_cash / plan.total_value
    lines.append(f"{'CASH':<8}{'':>8}{_pct(cash_now):>8}{'':>10}{_pct(cash_after):>8}")

    now_sleeves = sleeve_weights(cfg, plan.current_weights)
    after_sleeves = sleeve_weights(cfg, plan.projected_weights)
    target_sleeves = cfg.sleeve_targets()
    lines.append("")
    for s in SLEEVES:
        lines.append(
            f"{SLEEVE_LABELS[s]}: 목표 {target_sleeves[s]:.0f}%, 현재 {now_sleeves[s] * 100:.1f}%, "
            f"실행 후 {after_sleeves[s] * 100:.1f}%"
        )

    lines.append("")
    if plan.orders:
        lines.append("주문 (지정가, 당일 유효)")
        for o in plan.orders:
            lines.append(
                f"  {SIDE_LABELS[o.side]} {o.symbol:<6}{o.quantity:>6}주 @ {o.limit_price:>9,.2f} = {o.notional:>11,.2f} {ccy}"
            )
        buy_total = sum(o.notional for o in plan.orders if o.side == BUY)
        sell_total = sum(o.notional for o in plan.orders if o.side != BUY)
        lines.append(
            f"매수 합계 {buy_total:,.2f} / 매도 합계 {sell_total:,.2f} / 실행 후 현금 약 {plan.projected_cash:,.2f} {ccy}"
        )
        if plan.mode == CONTRIBUTION and plan.projected_cash > max(cfg.rules.min_order_value, 0.1 * plan.total_value):
            lines.append(
                f"남는 현금은 다음 실행 때 모자란 종목을 사는 데 써요 (한 번에 최대 {cfg.rules.max_buy_orders}종목)."
            )
    else:
        lines.append("이번에 보낼 주문이 없어요.")

    if plan.warnings:
        lines.append("")
        lines.append("확인할 점")
        lines.extend(f"  - {w}" for w in plan.warnings)
    return "\n".join(lines)


def format_check(cfg: Config) -> str:
    lines = ["설정이 규칙에 맞아요.", ""]
    st = cfg.sleeve_targets()
    lines.append(f"코어 {st['core']:g}% / 위성 {st['satellite']:g}%")
    lines.append("")
    lines.append("종목별 목표 (두 칸에 걸친 종목은 합산)")
    for sym, pct in cfg.symbol_targets().items():
        inst = cfg.instruments[sym]
        conid = inst.conid if inst.conid else "미입력"
        lines.append(f"  {sym:<6}{pct:>6g}%  {inst.name} (conid: {conid})")
    lines.append("")
    lines.append("위성 아이디어")
    for a in cfg.satellite_allocations():
        lines.append(f"  {a.symbol} {a.target_pct:g}% {a.label}")
        lines.append(f"    이유: {a.thesis}")
        lines.append(f"    틀렸다고 볼 조건: {a.invalidation}")
    missing = [s for s, i in cfg.instruments.items() if not i.conid]
    if missing:
        lines.append("")
        lines.append(f"IBKR로 실행하려면 conid를 채워야 해요: {', '.join(missing)} (resolve 명령으로 찾을 수 있어요)")
    return "\n".join(lines)
