import json
import time
from datetime import date
from pathlib import Path

import pytest

from rebalancer.brokers.mock import MockBroker
from rebalancer.jobs import BUY_PLAN_READY, DONE, FAILED, QUESTION, SELLS_DONE, JobError, OrderJob
from rebalancer.planner import REBALANCE_BUYS, build_plan

from helpers import make_config

ROOT = Path(__file__).resolve().parents[1]


def wait_for(job, *statuses, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if job.status in statuses:
            return
        time.sleep(0.01)
    raise AssertionError(f"job stayed {job.status}, wanted {statuses}")


def answer_all(job, decide, timeout=5.0):
    """Answer each new question with decide(index) until the job stops asking."""
    seen = []
    end = time.time() + timeout
    while time.time() < end and job.status not in (DONE, FAILED, SELLS_DONE):
        q = job.to_dict()["question"]
        if q and q["id"] not in seen:
            try:
                job.answer(q["id"], decide(len(seen)))
            except JobError:
                pass  # answered already (the worker has not picked it up yet)
            seen.append(q["id"])
        time.sleep(0.01)
    job.wait()
    return len(seen)


class AskingBroker:
    """Asks for a confirmation on every order, like IBKR's price-check warnings."""

    name = "asking"

    def __init__(self, inner):
        self.inner = inner
        self.answers = []

    def snapshot(self):
        return self.inner.snapshot()

    def place_order(self, order, coid, confirm):
        approved = confirm(["The order price is more than 3% away from the market."])
        self.answers.append(approved)
        if not approved:
            return {"status": "declined", "detail": "not approved"}
        return self.inner.place_order(order, coid, confirm)


class BrokenBroker:
    name = "broken"

    def __init__(self):
        self.calls = 0

    def place_order(self, order, coid, confirm):
        self.calls += 1
        raise RuntimeError("gateway went away")


def contribution_job(tmp_path, broker=None, **kwargs):
    cfg = make_config()
    inner = MockBroker(ROOT / "examples" / "month01.yaml", cfg)
    plan = build_plan(cfg, inner.snapshot(), date(2027, 1, 5))
    job = OrderJob(cfg, broker or inner, tmp_path / "state.json", date(2027, 1, 5), plan, **kwargs)
    return job, inner


def test_contribution_orders_go_out_and_the_month_is_recorded(tmp_path):
    job, broker = contribution_job(tmp_path)
    job.start()
    job.wait()
    assert job.status == DONE
    view = job.to_dict()
    assert view["submitted"] == len(view["orders"]) > 0
    assert len({o["coid"] for o in view["orders"]}) == len(view["orders"])  # unique order ids
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "2027-01" in saved["executions"]


def test_a_broker_warning_waits_for_the_person(tmp_path):
    cfg = make_config()
    inner = MockBroker(ROOT / "examples" / "month01.yaml", cfg)
    asking = AskingBroker(inner)
    job, _ = contribution_job(tmp_path, broker=asking)
    job.start()
    wait_for(job, QUESTION)
    q = job.to_dict()["question"]
    assert q["messages"] == ["The order price is more than 3% away from the market."]
    assert inner.submitted == []  # nothing goes out while the question is open
    job.answer(q["id"], approve=False)
    with pytest.raises(JobError, match="이미"):
        job.answer(q["id"], approve=True)  # a second click cannot flip the answer
    answer_all(job, lambda i: True)
    assert asking.answers[0] is False
    statuses = [o["status"] for o in job.to_dict()["orders"]]
    assert statuses[0] == "declined"
    assert all(s == "submitted" for s in statuses[1:])


def test_an_unanswered_question_counts_as_no(tmp_path):
    cfg = make_config()
    asking = AskingBroker(MockBroker(ROOT / "examples" / "month01.yaml", cfg))
    job, _ = contribution_job(tmp_path, broker=asking, question_timeout=0.05)
    job.start()
    job.wait()
    assert job.status == DONE
    assert all(o["status"] == "declined" for o in job.to_dict()["orders"])
    assert not (tmp_path / "state.json").exists()  # nothing submitted, nothing recorded


def test_answering_a_stale_question_is_refused(tmp_path):
    job, _ = contribution_job(tmp_path)
    with pytest.raises(JobError):
        job.answer("nope", True)


def test_broker_failure_stops_the_rest(tmp_path):
    job, _ = contribution_job(tmp_path, broker=BrokenBroker())
    job.start()
    job.wait()
    assert job.status == FAILED
    statuses = [o["status"] for o in job.to_dict()["orders"]]
    assert statuses[0] == "error" and set(statuses[1:]) <= {"skipped"}
    assert "gateway went away" in job.error


def test_rebalance_sends_sells_then_replanned_buys(tmp_path):
    cfg = make_config()
    broker = MockBroker(ROOT / "examples" / "month14.yaml", cfg)
    today = date(2028, 2, 3)
    plan = build_plan(cfg, broker.snapshot(), today)
    assert plan.sells and plan.buys
    job = OrderJob(cfg, broker, tmp_path / "state.json", today, plan)
    job.start()
    job.wait()
    assert job.status == SELLS_DONE
    assert all(o["side"] == "SELL" for o in job.to_dict()["orders"])
    with pytest.raises(JobError):
        job.send_buys()  # buys need a fresh plan first

    snap = broker.snapshot()
    buy_plan = build_plan(cfg, snap, today, force_mode=REBALANCE_BUYS)
    job.set_buy_plan(buy_plan, snap, [])
    assert job.status == BUY_PLAN_READY
    job.send_buys()
    job.wait()
    assert job.status == DONE
    sides = [o["side"] for o in job.to_dict()["orders"]]
    assert sides[0] == "SELL" and "BUY" in sides
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    month = saved["executions"]["2028-02"]
    assert [o["side"] for o in month["orders"]] == sides  # one record for both steps
    assert saved["last_rebalance_year"] == 2028
    assert broker.cash >= 0


def test_finish_is_refused_while_sending(tmp_path):
    cfg = make_config()
    asking = AskingBroker(MockBroker(ROOT / "examples" / "month01.yaml", cfg))
    job, _ = contribution_job(tmp_path, broker=asking)
    job.start()
    wait_for(job, QUESTION)
    with pytest.raises(JobError):
        job.finish()
    answer_all(job, lambda i: False)
    assert job.status == DONE
