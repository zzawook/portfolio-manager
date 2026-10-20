from __future__ import annotations

from .config import Config
from .models import BUY, SLEEVES, AccountSnapshot, Plan
from .planner import CONTRIBUTION, MODE_LABELS

SLEEVE_LABELS = {"core": "코어", "satellite": "위성"}
SIDE_LABELS = {"BUY": "매수", "SELL": "매도"}


def fee_rule(cfg: Config) -> str:
    f = cfg.fees
    text = f"거래 금액의 {f.rate_pct:g}%, 주문당 최소 {f.min_per_order:,.2f}"
    if f.max_per_order > 0:
        text += f", 최대 {f.max_per_order:,.2f}"
    if f.gst_pct > 0:
        text += f", 수수료에 GST {f.gst_pct:g}%"
    if f.extra_per_order > 0:
        text += f", 거래소와 청산 수수료 {f.extra_per_order:,.2f}"
    return text


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


def _other_cash(snap: AccountSnapshot, names_only: bool = False) -> str:
    held = {c: a for c, a in snap.other_cash.items() if a > 0}
    if names_only:
        return ", ".join(held) or "다른 통화"
    return ", ".join(f"{c} {a:,.2f}" for c, a in held.items())


def format_plan(cfg: Config, snap: AccountSnapshot, plan: Plan) -> str:
    ccy = cfg.base_currency
    invested = plan.total_value - plan.cash
    lines = [
        f"== 계획: {MODE_LABELS.get(plan.mode, plan.mode)} ==",
        f"이유: {plan.reason}",
        f"총자산 {plan.total_value:,.2f} {ccy} (투자 {invested:,.2f} + 현금 {plan.cash:,.2f})",
        *(
            [f"  현금 중 {plan.fx_cash:,.2f} {ccy}는 다른 통화 현금이에요 ({_other_cash(snap)}). 살 때 IBKR이 자동으로 바꿔요."]
            if plan.fx_cash
            else []
        ),
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
                f"  (수수료 약 {o.fee:,.2f})"
            )
        buy_total = sum(o.notional for o in plan.orders if o.side == BUY)
        sell_total = sum(o.notional for o in plan.orders if o.side != BUY)
        traded = buy_total + sell_total
        fee_share = plan.fee_total / traded * 100 if traded else 0.0
        lines.append(
            f"매수 합계 {buy_total:,.2f} / 매도 합계 {sell_total:,.2f} / 실행 후 현금 약 {plan.projected_cash:,.2f} {ccy}"
        )
        lines.append(f"예상 수수료 합계 {plan.fee_total:,.2f} {ccy} (주문 금액의 {fee_share:.2f}%)")
        lines.append(f"  수수료 기준: {fee_rule(cfg)}. 실제 금액은 IBKR 체결 내역에서 확인하세요.")
        if plan.fx_needed:
            lines.append(
                f"자동 환전 예상: 약 {plan.fx_needed:,.2f} {ccy}어치를 {_other_cash(snap, names_only=True)}에서 바꿔요 "
                f"(환율에 {cfg.fees.auto_fx_pct:g}% 반영, 약 {plan.fx_cost:,.2f} {ccy})."
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
    if cfg.exit_symbols:
        lines.append("")
        lines.append(f"정리 대상: {', '.join(cfg.exit_symbols)} (목표에서 뺀 종목이에요. 보유 중이면 리밸런싱 때 팔아요.)")
    missing = [s for s, i in cfg.instruments.items() if not i.conid]
    if missing:
        lines.append("")
        lines.append(f"IBKR로 실행하려면 conid를 채워야 해요: {', '.join(missing)} (웹 화면의 'IBKR에서 conid 찾기'나 resolve 명령으로 찾을 수 있어요)")
    return "\n".join(lines)
