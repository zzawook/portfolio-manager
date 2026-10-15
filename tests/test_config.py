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


def test_instrument_without_target_is_rejected():
    raw = raw_config()
    raw["instruments"]["CMOD"] = {"name": "CMOD"}
    with pytest.raises(ConfigError, match="CMOD"):
        make_config(raw)
