import math
from datetime import date

import pytest

from rebalancer.models import BUY, SELL, AccountSnapshot, Quote
from rebalancer.planner import CONTRIBUTION, REBALANCE, REBALANCE_BUYS, PlanError, build_plan

from helpers import PRICES, make_config, on_target_positions, quotes, snapshot

JAN = date(2027, 1, 5)
FEB = date(2027, 2, 5)


def test_first_month_buys_at_most_three_whole_share_orders_within_cash():
    cfg = make_config()
    plan = build_plan(cfg, snapshot(3000), JAN)
    assert plan.mode == CONTRIBUTION
    assert 1 <= len(plan.orders) <= 3
    assert all(o.side == BUY and isinstance(o.quantity, int) and o.quantity > 0 for o in plan.orders)
    assert all(o.notional >= cfg.rules.min_order_value for o in plan.orders)
    assert sum(o.notional for o in plan.orders) <= 3000 - cfg.rules.cash_reserve
    assert plan.orders[0].symbol == "VWRA"  # biggest gap first


def test_most_underweight_holding_is_bought_first():
    cfg = make_config()
    positions = on_target_positions(10000)
    positions["WDGF"] = 0  # 10% target, now empty
    plan = build_plan(cfg, snapshot(3000, positions), FEB)
    assert plan.orders[0].symbol == "WDGF"


def test_small_portfolio_never_sells_even_when_overweight():
    cfg = make_config()
    positions = on_target_positions(10000)
    positions["SMH"] *= 3  # far above target, but the portfolio is below min_value_for_rebalance
    plan = build_plan(cfg, snapshot(3000, positions), FEB)
    assert plan.mode == CONTRIBUTION
    assert not plan.sells


def test_overweight_drift_triggers_rebalance_with_sell():
    cfg = make_config()
    positions = {"VWRA": 76, "AGGU": 1000, "IGLN": 60, "SMH": 134, "ZAPP": 70, "WDGF": 320}
    plan = build_plan(cfg, snapshot(3000, positions), date(2028, 2, 3))
    assert plan.mode == REBALANCE
    sells = plan.sells
    assert [o.symbol for o in sells] == ["SMH"]
    assert sells[0].quantity <= positions["SMH"]
    assert abs(plan.projected_weights["SMH"] - 0.15) < 0.01


def test_annual_rebalance_runs_once_a_year():
    cfg = make_config()
    positions = on_target_positions(40000)
    plan = build_plan(cfg, snapshot(3000, positions), JAN, last_rebalance_year=2026)
    assert plan.mode == REBALANCE
    plan_again = build_plan(cfg, snapshot(3000, positions), JAN, last_rebalance_year=2027)
    assert plan_again.mode == CONTRIBUTION


def test_order_cap_limits_large_buys():
    cfg = make_config(max_order_value=1000)
    plan = build_plan(cfg, snapshot(20000), JAN)
    assert all(o.notional <= 1000 for o in plan.orders)
    assert any("한도" in w for w in plan.warnings)


def test_buy_limit_price_includes_buffer_and_rounds_up():
    cfg = make_config(limit_buffer_pct=0.3)
    snap = AccountSnapshot(cash=3000, positions={}, quotes={**quotes(), "VWRA": Quote(bid=189.9, ask=190.1, last=190.0)})
    plan = build_plan(cfg, snap, JAN)
    vwra = next(o for o in plan.orders if o.symbol == "VWRA")
    assert vwra.limit_price == math.ceil(190.1 * 1.003 * 100) / 100


def test_held_symbol_without_quote_is_an_error():
    cfg = make_config()
    prices = {k: v for k, v in PRICES.items() if k != "SMH"}
    snap = AccountSnapshot(cash=3000, positions={"SMH": 10}, quotes=quotes(prices))
    with pytest.raises(PlanError, match="SMH"):
        build_plan(cfg, snap, JAN)


def test_missing_quote_for_unheld_symbol_is_skipped_with_warning():
    cfg = make_config()
    prices = {k: v for k, v in PRICES.items() if k != "ZAPP"}
    snap = AccountSnapshot(cash=3000, positions={}, quotes=quotes(prices))
    plan = build_plan(cfg, snap, JAN)
    assert all(o.symbol != "ZAPP" for o in plan.orders)
    assert any("ZAPP" in w for w in plan.warnings)


def test_rebalance_buy_phase_is_not_limited_to_three_orders():
    cfg = make_config()
    positions = on_target_positions(40000)
    for s in positions:
        positions[s] = int(positions[s] * 0.8)  # everything underweight, lots of cash after a sell
    plan = build_plan(cfg, snapshot(9000, positions), FEB, force_mode=REBALANCE_BUYS)
    assert plan.mode == REBALANCE_BUYS
    assert len(plan.orders) > 3
    assert not plan.sells


def test_unknown_positions_are_reported():
    cfg = make_config()
    snap = AccountSnapshot(cash=3000, positions={}, quotes=quotes(), unknown_positions=["TSLA"])
    plan = build_plan(cfg, snap, JAN)
    assert any("TSLA" in w for w in plan.warnings)
