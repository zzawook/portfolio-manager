from datetime import date

import pytest

from rebalancer.config import FeeSettings
from rebalancer.models import BUY, SELL
from rebalancer.planner import REBALANCE, build_plan

from helpers import make_config, raw_config, snapshot


def fees_config(**fees):
    raw = raw_config()
    raw["fees"] = fees
    return make_config(raw)


def test_fixed_pricing_has_a_minimum_and_adds_gst():
    f = FeeSettings()  # IBKR Fixed, 0.05%, at least 4.00, GST 9%
    assert f.estimate(1000) == pytest.approx(4.36)
    assert f.estimate(20000) == pytest.approx(10.90)  # 0.05% of 20,000 = 10.00, plus GST
    assert f.estimate(0) == 0


def test_tiered_pricing_has_a_cap_and_third_party_fees():
    f = FeeSettings(min_per_order=1.70, max_per_order=39.0, extra_per_order=0.21)
    assert f.estimate(1000) == round(1.70 * 1.09 + 0.21, 2)
    assert f.estimate(200000) == round(39.0 * 1.09 + 0.21, 2)


def test_fx_estimate_uses_the_minimum():
    assert FeeSettings().fx_estimate(3000) == 2.0
    assert FeeSettings().fx_estimate(0) == 0


def test_buys_leave_room_for_their_fees():
    cfg = fees_config(min_per_order=50)  # an absurd minimum makes the effect easy to see
    plan = build_plan(cfg, snapshot(3000), date(2027, 1, 5))
    spent = sum(o.notional + o.fee for o in plan.buys)
    assert spent <= 3000 - cfg.rules.cash_reserve + 1e-9
    assert all(o.fee == pytest.approx(54.5) for o in plan.buys)
    assert plan.fee_total == pytest.approx(54.5 * len(plan.buys))


def test_small_orders_get_a_fee_warning():
    cfg = fees_config(warn_pct=1.0)
    plan = build_plan(cfg, snapshot(600), date(2027, 1, 5))
    assert plan.orders
    assert any("수수료 비율" in w for w in plan.warnings)


def test_sell_fees_reduce_the_cash_for_buys():
    cfg = make_config()
    positions = {"VWRA": 76, "AGGU": 1000, "IGLN": 60, "SMH": 134, "ZAPP": 70, "WDGF": 320}
    plan = build_plan(cfg, snapshot(3000, positions), date(2028, 2, 3), force_mode=REBALANCE)
    sells = [o for o in plan.orders if o.side == SELL]
    buys = [o for o in plan.orders if o.side == BUY]
    assert sells and buys
    cash_after = 3000 + sum(o.notional - o.fee for o in sells) - sum(o.notional + o.fee for o in buys)
    assert cash_after == pytest.approx(plan.projected_cash)
    assert cash_after >= cfg.rules.cash_reserve - 1e-6


def test_fee_settings_are_checked():
    raw = raw_config()
    raw["fees"] = {"gst_pct": 50}
    with pytest.raises(Exception, match="gst_pct"):
        make_config(raw)
