import shutil
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from rebalancer.config import ConfigError, load_config
from rebalancer.models import SELL
from rebalancer.planner import CONTRIBUTION, REBALANCE, build_plan
from rebalancer.portfolio import Draft, DraftConflict, DraftError, portfolio_from_config, save_portfolio

from helpers import make_config, raw_config, snapshot

ROOT = Path(__file__).resolve().parents[1]


def project(tmp_path: Path) -> Path:
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "portfolio.yaml", tmp_path / "portfolio.yaml")
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    return tmp_path / "config.yaml"


# Draft edits -----------------------------------------------------------------------


def test_changing_a_target_is_reported_in_plain_words():
    d = Draft(make_config())
    v = d.version
    text = d.set_allocation("core", "VWRA", target=35)
    assert text == "VWRA 코어 35% (기존 40%)"
    assert d.version == v + 1
    assert [c["text"] for c in d.changes()] == ["VWRA 코어 35% (기존 40%)"]
    _, problems = d.evaluate()
    assert any("합계" in p for p in problems)  # 95% now


def test_new_symbol_needs_instrument_details():
    d = Draft(make_config())
    with pytest.raises(DraftError, match="instrument"):
        d.set_allocation("satellite", "CSPX", target=5)
    text = d.set_allocation(
        "satellite",
        "CSPX",
        target=5,
        thesis="t",
        invalidation="i",
        instrument={"name": "iShares Core S&P 500", "exchange": "LSE", "currency": "USD"},
    )
    assert "추가" in text
    assert any(i["symbol"] == "CSPX" for i in d.portfolio["instruments"])


def test_satellite_without_notes_is_a_problem_not_an_error():
    d = Draft(make_config())
    d.set_allocation("satellite", "SMH", thesis="")
    _, problems = d.evaluate()
    assert any("SMH (위성)" in p for p in problems)


def test_removing_a_held_symbol_keeps_it_as_an_exit():
    d = Draft(make_config())
    d.remove_allocation("satellite", "ZAPP", held=True)
    d.set_allocation("core", "VWRA", target=45)
    cfg, problems = d.evaluate()
    assert problems == []
    assert cfg.exit_symbols == ["ZAPP"]


def test_removing_an_unheld_symbol_drops_the_instrument():
    d = Draft(make_config())
    d.remove_allocation("satellite", "ZAPP", held=False)
    assert all(i["symbol"] != "ZAPP" for i in d.portfolio["instruments"])


def test_symbol_in_two_sleeves_keeps_its_instrument():
    d = Draft(make_config())
    d.remove_allocation("satellite", "IGLN", held=False)
    assert any(i["symbol"] == "IGLN" for i in d.portfolio["instruments"])


def test_stale_version_is_refused():
    d = Draft(make_config())
    data = d.view()["portfolio"]
    with pytest.raises(DraftConflict):
        d.replace(data, expected_version=d.version - 1)


def test_bad_input_is_refused():
    d = Draft(make_config())
    for bad in (
        {"instruments": [], "allocations": [{"sleeve": "growth", "symbol": "X", "target": 1}]},
        {"instruments": [{"symbol": "bad symbol!"}], "allocations": []},
        {"instruments": [], "allocations": [{"sleeve": "core", "symbol": "VWRA", "target": "lots"}]},
    ):
        with pytest.raises(DraftError):
            d.replace(bad)


def test_conid_change_is_listed():
    d = Draft(make_config())
    d.set_conid("SMH", 123456)
    assert any("SMH conid 123456" == c["text"] for c in d.changes())


# Saving ----------------------------------------------------------------------------


def test_save_writes_the_file_and_keeps_a_backup(tmp_path):
    cfg = load_config(project(tmp_path))
    d = Draft(cfg)
    d.set_allocation("core", "VWRA", target=45)
    d.set_allocation("satellite", "WDGF", target=5)
    new = save_portfolio(cfg, d.portfolio, now=datetime(2027, 1, 5, 9, 30))
    assert new.symbol_targets()["VWRA"] == 45
    assert (tmp_path / "history" / "portfolio-20270105-093000.yaml").exists()
    data = yaml.safe_load((tmp_path / "portfolio.yaml").read_text(encoding="utf-8"))
    assert data["allocations"]["core"][0] == {"symbol": "VWRA", "target": 45, "label": "전세계 주식"}
    # Saving twice in the same second keeps both backups.
    save_portfolio(new, d.portfolio, now=datetime(2027, 1, 5, 9, 30))
    assert (tmp_path / "history" / "portfolio-20270105-093000-1.yaml").exists()


def test_save_refuses_a_broken_portfolio(tmp_path):
    cfg = load_config(project(tmp_path))
    d = Draft(cfg)
    d.set_allocation("core", "VWRA", target=50)  # 110%
    before = (tmp_path / "portfolio.yaml").read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="100%"):
        save_portfolio(cfg, d.portfolio)
    assert (tmp_path / "portfolio.yaml").read_text(encoding="utf-8") == before


def test_saved_file_round_trips(tmp_path):
    cfg = load_config(project(tmp_path))
    new = save_portfolio(cfg, portfolio_from_config(cfg))
    assert portfolio_from_config(new) == portfolio_from_config(cfg)


# Exits in the planner --------------------------------------------------------------


def exit_config():
    raw = raw_config()
    raw["allocations"]["satellite"] = [a for a in raw["allocations"]["satellite"] if a["symbol"] != "ZAPP"]
    raw["allocations"]["core"][0]["target"] = 45  # VWRA takes ZAPP's 5%
    return make_config(raw)


def test_held_exit_is_sold_in_full_at_rebalance():
    cfg = exit_config()
    positions = {"VWRA": 100, "AGGU": 1000, "IGLN": 60, "SMH": 90, "ZAPP": 70, "WDGF": 300}
    plan = build_plan(cfg, snapshot(500, positions), date(2028, 2, 3), force_mode=REBALANCE)
    zapp = [o for o in plan.orders if o.symbol == "ZAPP"]
    assert zapp and zapp[0].side == SELL and zapp[0].quantity == 70
    assert plan.target_weights["ZAPP"] == 0
    assert plan.projected_weights["ZAPP"] == pytest.approx(0)


def test_held_exit_is_never_bought_and_waits_for_a_rebalance():
    cfg = exit_config()
    positions = {"VWRA": 10, "ZAPP": 70}
    plan = build_plan(cfg, snapshot(3000, positions), date(2027, 2, 5))
    assert plan.mode == CONTRIBUTION
    assert all(o.symbol != "ZAPP" for o in plan.orders)
    assert any("ZAPP: 목표에서 뺀 종목" in w for w in plan.warnings)


def test_large_exit_triggers_a_rebalance_by_itself():
    cfg = exit_config()
    positions = {"VWRA": 100, "AGGU": 1000, "IGLN": 60, "SMH": 90, "ZAPP": 300, "WDGF": 300}
    plan = build_plan(cfg, snapshot(500, positions), date(2028, 2, 3))
    assert plan.mode == REBALANCE
    assert plan.reason.startswith("ZAPP: 목표에서 뺐는데")
