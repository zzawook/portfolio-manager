from pathlib import Path

import pytest

from rebalancer.config import ConfigError, load_config

from helpers import make_config, raw_config

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_config_is_valid():
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.symbol_targets() == {"VWRA": 40, "AGGU": 15, "IGLN": 15, "SMH": 15, "ZAPP": 5, "WDGF": 10}
    assert cfg.sleeve_targets() == {"core": 65, "satellite": 35}


def test_targets_must_sum_to_100():
    raw = raw_config()
    raw["allocations"]["core"][0]["target"] = 41
    with pytest.raises(ConfigError, match="100%"):
        make_config(raw)


def test_satellite_needs_thesis_and_invalidation():
    raw = raw_config()
    raw["allocations"]["satellite"][0]["invalidation"] = ""
    with pytest.raises(ConfigError, match="invalidation"):
        make_config(raw)


def test_satellite_idea_cap():
    raw = raw_config()
    raw["allocations"]["satellite"][0]["target"] = 20  # SMH 20
    raw["allocations"]["core"][0]["target"] = 35  # keep the total at 100
    with pytest.raises(ConfigError, match="상한"):
        make_config(raw)


def test_unknown_rule_key_is_rejected():
    raw = raw_config()
    raw["rules"] = {"max_buy_order": 3}
    with pytest.raises(ConfigError, match="max_buy_order"):
        make_config(raw)


def test_single_currency_only():
    raw = raw_config()
    raw["instruments"]["WDGF"]["currency"] = "EUR"
    with pytest.raises(ConfigError, match="EUR"):
        make_config(raw)


def test_instrument_without_target_is_an_exit():
    raw = raw_config()
    raw["instruments"]["CMOD"] = {"name": "CMOD"}
    cfg = make_config(raw)
    assert cfg.exit_symbols == ["CMOD"]
    assert "CMOD" not in cfg.symbol_targets()


def test_unknown_top_level_key_is_rejected():
    raw = raw_config()
    raw["rule"] = {"max_buy_orders": 3}
    with pytest.raises(ConfigError, match="rule"):
        make_config(raw)


def test_unknown_allocation_key_is_rejected():
    raw = raw_config()
    raw["allocations"]["satellite"][0]["thesys"] = "typo"
    with pytest.raises(ConfigError, match="thesys"):
        make_config(raw)


def test_portfolio_file_is_merged(tmp_path):
    (tmp_path / "config.yaml").write_text("base_currency: USD\nportfolio_file: p.yaml\n", encoding="utf-8")
    (tmp_path / "p.yaml").write_text(
        "instruments:\n  VWRA: {name: VWRA}\nallocations:\n  core:\n    - {symbol: VWRA, target: 100}\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.symbol_targets() == {"VWRA": 100}
    assert cfg.portfolio_path == tmp_path / "p.yaml"


def test_portfolio_in_two_places_is_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "portfolio_file: p.yaml\ninstruments:\n  VWRA: {name: VWRA}\n", encoding="utf-8"
    )
    (tmp_path / "p.yaml").write_text("instruments:\n  VWRA: {name: VWRA}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="두 곳"):
        load_config(tmp_path / "config.yaml")
