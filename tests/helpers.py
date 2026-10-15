from __future__ import annotations

import copy

from rebalancer.config import Config, parse_config, validate
from rebalancer.models import AccountSnapshot, Quote

BASE_RAW = {
    "base_currency": "USD",
    "broker": {"type": "mock", "mock_file": "examples/month01.yaml"},
    "rules": {},
    "instruments": {
        "VWRA": {"name": "VWRA", "conid": 1},
        "AGGU": {"name": "AGGU", "conid": 2},
        "IGLN": {"name": "IGLN", "conid": 3},
        "SMH": {"name": "SMH", "conid": 4},
        "ZAPP": {"name": "ZAPP", "conid": 5},
        "WDGF": {"name": "WDGF", "conid": 6},
    },
    "allocations": {
        "core": [
            {"symbol": "VWRA", "target": 40},
            {"symbol": "AGGU", "target": 15},
            {"symbol": "IGLN", "target": 10},
        ],
        "satellite": [
            {"symbol": "SMH", "target": 15, "thesis": "t", "invalidation": "i"},
            {"symbol": "ZAPP", "target": 5, "thesis": "t", "invalidation": "i"},
            {"symbol": "WDGF", "target": 10, "thesis": "t", "invalidation": "i"},
            {"symbol": "IGLN", "target": 5, "thesis": "t", "invalidation": "i"},
        ],
    },
}

PRICES = {"VWRA": 190.0, "AGGU": 5.85, "IGLN": 95.0, "SMH": 65.0, "ZAPP": 26.0, "WDGF": 11.5}


def raw_config() -> dict:
    return copy.deepcopy(BASE_RAW)


def make_config(raw: dict | None = None, **rules) -> Config:
    raw = raw or raw_config()
    if rules:
        raw["rules"] = {**raw.get("rules", {}), **rules}
    cfg = parse_config(raw)
    validate(cfg)
    return cfg


def quotes(prices: dict[str, float] | None = None, spread: float = 0.0) -> dict[str, Quote]:
    prices = prices or PRICES
    return {s: Quote(bid=p - spread, ask=p + spread, last=p) for s, p in prices.items()}


def snapshot(cash: float, positions: dict[str, float] | None = None, prices: dict[str, float] | None = None) -> AccountSnapshot:
    return AccountSnapshot(cash=cash, positions=dict(positions or {}), quotes=quotes(prices))


def on_target_positions(total: float, prices: dict[str, float] | None = None) -> dict[str, float]:
    """Whole-share holdings close to the target weights for a given portfolio size."""
    prices = prices or PRICES
    targets = make_config().symbol_targets()
    return {s: int(total * pct / 100 / prices[s]) for s, pct in targets.items()}
