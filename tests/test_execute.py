import json
from datetime import date
from pathlib import Path

import pytest

from rebalancer.brokers.mock import MockBroker
from rebalancer.execute import ExecutionAborted, run_execute, safety_problems
from rebalancer.models import BUY, AccountSnapshot, Order, Plan
from rebalancer.state import State

from helpers import make_config, quotes, raw_config

ROOT = Path(__file__).resolve().parents[1]


class Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else "no"


def broker(cfg, name="month01.yaml"):
    return MockBroker(ROOT / "examples" / name, cfg)


def test_nothing_is_sent_without_yes(tmp_path):
    cfg = make_config()
    b = broker(cfg)
    results = run_execute(cfg, b, State(tmp_path / "s.json"), date(2027, 1, 5), input_fn=Answers("no"), out=lambda s: None)
    assert results == []
    assert b.submitted == []
    assert not (tmp_path / "s.json").exists()


def test_execute_places_orders_and_records_the_month(tmp_path):
    cfg = make_config()
    b = broker(cfg)
    state = State(tmp_path / "s.json")
    results = run_execute(cfg, b, state, date(2027, 1, 5), input_fn=Answers("yes"), out=lambda s: None)
    assert results and all(r["status"] == "submitted" for r in results)
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert "2027-01" in saved["executions"]
    assert saved["targets"]["satellite:SMH"] == 15


def test_second_run_in_same_month_is_blocked_unless_forced(tmp_path):
    cfg = make_config()
    run_execute(cfg, broker(cfg), State(tmp_path / "s.json"), date(2027, 1, 5), input_fn=Answers("yes"), out=lambda s: None)
    with pytest.raises(ExecutionAborted, match="--force"):
        run_execute(cfg, broker(cfg), State(tmp_path / "s.json"), date(2027, 1, 20), input_fn=Answers("yes"), out=lambda s: None)
    again = run_execute(
        cfg, broker(cfg), State(tmp_path / "s.json"), date(2027, 1, 20), force=True, input_fn=Answers("yes"), out=lambda s: None
    )
    assert again


def test_rebalance_sells_first_then_replans_buys(tmp_path):
    cfg = make_config()
    b = broker(cfg, "month14.yaml")
    state = State(tmp_path / "s.json")
    answers = Answers("yes", "", "yes")
    results = run_execute(cfg, b, state, date(2028, 2, 3), input_fn=answers, out=lambda s: None)
    sides = [r["side"] for r in results]
    assert sides[0] == "SELL" and "BUY" in sides
    assert sides.index("BUY") > sides.index("SELL")
    assert b.cash >= 0
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["last_rebalance_year"] == 2028


def test_target_changes_go_into_the_journal(tmp_path):
    cfg = make_config()
    state_path = tmp_path / "s.json"
    run_execute(cfg, broker(cfg), State(state_path), date(2027, 1, 5), input_fn=Answers("yes"), out=lambda s: None)

    raw = raw_config()
    raw["allocations"]["core"][0]["target"] = 45  # VWRA 40 -> 45
    raw["allocations"]["satellite"][0]["target"] = 10  # SMH 15 -> 10
    cfg2 = make_config(raw)
    run_execute(cfg2, broker(cfg2), State(state_path), date(2027, 2, 5), input_fn=Answers("yes"), out=lambda s: None)

    journal = json.loads(state_path.read_text(encoding="utf-8"))["journal"]
    keys = {(j["key"], j["from"], j["to"]) for j in journal}
    assert ("core:VWRA", 40, 45) in keys
    assert ("satellite:SMH", 15, 10) in keys


def test_safety_check_catches_oversized_and_unknown_orders():
    cfg = make_config(max_order_value=1000)
    snap = AccountSnapshot(cash=10000, positions={}, quotes=quotes())
    plan = Plan(
        mode="contribution",
        reason="",
        total_value=10000,
        cash=10000,
        orders=[Order("VWRA", BUY, 10, 190.0), Order("TSLA", BUY, 1, 200.0)],
        current_weights={},
        target_weights={},
        projected_weights={},
        projected_cash=0,
    )
    problems = safety_problems(cfg, plan, snap)
    assert any("한도" in p for p in problems)
    assert any("TSLA" in p for p in problems)
