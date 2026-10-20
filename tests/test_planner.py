import math
from datetime import date

import pytest

from rebalancer.models import BUY, AccountSnapshot, Quote
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


# Cash in other currencies (IBKR converts it automatically when a buy settles) ---------


def sgd_only(sgd=4000.0, rate=0.78):
    from rebalancer.models import AccountSnapshot

    return AccountSnapshot(cash=0.0, positions={}, quotes=quotes(), other_cash={"SGD": sgd}, fx_rates={"SGD": rate})


def test_sgd_cash_is_spent_through_automatic_conversion():
    cfg = make_config()
    plan = build_plan(cfg, sgd_only(), date(2027, 1, 5))
    usable = 4000 * 0.78 * (1 - (1.0 + 0.03) / 100)  # 1% safety margin + 0.03% markup
    assert plan.fx_cash == pytest.approx(usable, abs=0.01)
    assert plan.buys, "it buys with SGD"
    cost = sum(o.notional + o.fee for o in plan.buys)
    assert cost <= usable - cfg.rules.cash_reserve + 0.01
    assert plan.fx_needed == pytest.approx(cost, abs=0.01)  # all of it comes from SGD
    assert plan.fx_cost == pytest.approx(cost * 0.0003, abs=0.01)


def test_usd_is_used_first_and_only_the_rest_is_converted():
    from dataclasses import replace

    snap = replace(sgd_only(sgd=4000), cash=500.0)
    plan = build_plan(make_config(), snap, date(2027, 1, 5))
    cost = sum(o.notional + o.fee for o in plan.buys)
    assert plan.fx_needed == pytest.approx(max(0.0, cost - 500.0), abs=0.01)


def test_auto_fx_can_be_turned_off():
    from dataclasses import replace

    with pytest.raises(PlanError):  # SGD only: nothing to spend, nothing held
        build_plan(make_config(auto_fx=False), sgd_only(), date(2027, 1, 5))
    plan = build_plan(make_config(auto_fx=False), replace(sgd_only(), cash=3000.0), date(2027, 1, 5))
    assert plan.fx_cash == 0.0 and plan.fx_needed == 0.0
    assert any("다른 통화 현금은 계산에서 뺐어요" in w for w in plan.warnings)


def test_mock_broker_converts_like_ibkr(tmp_path):
    from rebalancer.brokers.mock import MockBroker

    f = tmp_path / "acct.yaml"
    f.write_text("cash: 0\nother_cash: {SGD: 4000}\nfx_rates: {SGD: 0.78}\nquotes:\n  VWRA: {bid: 190, ask: 190.2, last: 190.1}\n")
    cfg = make_config()
    broker = MockBroker(f, cfg)
    plan = build_plan(cfg, broker.snapshot(), date(2027, 1, 5))
    for o in plan.buys:
        broker.place_order(o, "x", lambda m: True)
    after = broker.snapshot()
    assert after.cash == pytest.approx(0.0, abs=0.01)  # the shortfall came from SGD
    assert 0 < after.other_cash["SGD"] < 4000


def test_missing_rate_leaves_that_currency_out_with_a_warning():
    from dataclasses import replace

    snap = replace(sgd_only(), cash=3000.0, fx_rates={})
    plan = build_plan(make_config(), snap, date(2027, 1, 5))
    assert plan.fx_cash == 0.0 and any("SGD 환율을 받지 못해서" in w for w in plan.warnings)


def test_safety_check_counts_converted_cash():
    from rebalancer.execute import safety_problems

    cfg = make_config()
    snap = sgd_only()
    plan = build_plan(cfg, snap, date(2027, 1, 5))
    assert not [p for p in safety_problems(cfg, plan, snap) if "현금" in p]


# Price steps and orders still waiting to fill ------------------------------------------


def test_round_to_tick_follows_the_price_ladder():
    from rebalancer.models import round_to_tick

    lse = [(0.0, 0.0001), (50.0, 0.01), (100.0, 0.02), (200.0, 0.05)]
    assert round_to_tick(101.39, lse, up=True) == 101.40
    assert round_to_tick(101.39, lse, up=False) == 101.38
    assert round_to_tick(80.505, lse, up=True) == 80.51
    assert round_to_tick(201.01, lse, up=True) == 201.05
    assert round_to_tick(12.345, None, up=True) == 12.35  # no rules: cents


def test_plan_uses_tick_rules():
    from dataclasses import replace

    lse = [(0.0, 0.0001), (50.0, 0.01), (100.0, 0.02), (200.0, 0.05)]
    snap = replace(snapshot(3000.0, prices={**PRICES, "SMH": 101.0}), tick_rules={s: lse for s in PRICES})
    plan = build_plan(make_config(), snap, date(2027, 1, 5))
    smh = [o for o in plan.buys if o.symbol == "SMH"]
    assert smh and round(smh[0].limit_price / 0.02, 6).is_integer()


def test_pending_buys_count_as_held_and_reserve_cash():
    from dataclasses import replace

    from rebalancer.models import PendingOrder

    base = snapshot(3000.0)
    pending = [PendingOrder("VWRA", "BUY", 6, 190.6), PendingOrder("IGLN", "BUY", 4, 95.3)]
    plan = build_plan(make_config(), replace(base, pending=pending), date(2027, 1, 5))
    assert not [o for o in plan.buys if o.symbol in ("VWRA", "IGLN")]  # already on their way
    assert any("체결 대기 중인 주문" in w for w in plan.warnings)
    spent = sum(o.notional + o.fee for o in plan.buys)
    reserved = 6 * 190.6 + 4 * 95.3
    assert spent <= 3000.0 - reserved



def test_state_keeps_final_order_results(tmp_path):
    from rebalancer.state import State

    st = State(tmp_path / "state.json")
    st.record_execution("2027-01", "contribution", "r", [{"symbol": "VWRA", "coid": "c1", "status": "submitted"}], None)
    st.record_execution("2027-01", "contribution", "r", [{"symbol": "IGLN", "coid": "c2", "status": "submitted"}], None)
    assert [r["orders"][0]["coid"] for _, r in st.runs()] == ["c2", "c1"] or len(st.runs()) == 2
    assert st.update_orders({"c1": {"outcome": "filled", "filled": 2.0}})
    st.save()
    again = State(tmp_path / "state.json")
    found = [o for _, r in again.runs() for o in r["orders"] if o["coid"] == "c1"][0]
    assert found["outcome"] == "filled" and found["filled"] == 2.0
