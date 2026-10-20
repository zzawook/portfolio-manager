from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

CORE = "core"
SATELLITE = "satellite"
SLEEVES = (CORE, SATELLITE)

BUY = "BUY"
SELL = "SELL"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    exchange: str
    currency: str
    conid: Optional[int] = None


@dataclass(frozen=True)
class Allocation:
    symbol: str
    sleeve: str
    target_pct: float
    label: str = ""
    thesis: str = ""
    invalidation: str = ""


@dataclass(frozen=True)
class Quote:
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None

    @property
    def mark(self) -> Optional[float]:
        """Price used to value a holding: mid when both sides exist, otherwise last."""
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last or self.ask or self.bid

    @property
    def buy_reference(self) -> Optional[float]:
        return self.ask or self.last

    @property
    def sell_reference(self) -> Optional[float]:
        return self.bid or self.last


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str
    quantity: int
    limit_price: float
    fee: float = 0.0  # estimated commission

    @property
    def notional(self) -> float:
        return round(self.quantity * self.limit_price, 2)

    @property
    def cash_effect(self) -> float:
        """Cash after the fill, fee included: negative for a buy, positive for a sell."""
        if self.side == BUY:
            return -(self.notional + self.fee)
        return self.notional - self.fee


@dataclass
class PendingOrder:
    """An order sent to the broker that has not fully filled yet."""

    symbol: str
    side: str
    quantity: float  # still open
    price: float  # limit price
    status: str = ""


@dataclass
class AccountSnapshot:
    cash: float
    positions: dict[str, float]
    quotes: dict[str, Quote]
    unknown_positions: list[str] = field(default_factory=list)
    other_cash: dict[str, float] = field(default_factory=dict)  # cash in other currencies (e.g. SGD)
    fx_rates: dict[str, float] = field(default_factory=dict)  # other currency -> base currency (1 SGD = 0.78 USD)
    pending: list["PendingOrder"] = field(default_factory=list)  # sent but not yet filled
    tick_rules: dict[str, list[tuple[float, float]]] = field(default_factory=dict)  # symbol -> [(from price, tick)]


@dataclass
class Plan:
    mode: str
    reason: str
    total_value: float
    cash: float
    orders: list[Order]
    current_weights: dict[str, float]
    target_weights: dict[str, float]
    projected_weights: dict[str, float]
    projected_cash: float
    warnings: list[str] = field(default_factory=list)
    # Cash in other currencies that IBKR converts automatically when a buy settles.
    fx_cash: float = 0.0  # base-currency amount counted in `cash` from other currencies (after the safety margin)
    fx_needed: float = 0.0  # how much of this plan's buys IBKR is expected to convert
    fx_cost: float = 0.0  # estimated cost of that conversion

    @property
    def buys(self) -> list[Order]:
        return [o for o in self.orders if o.side == BUY]

    @property
    def sells(self) -> list[Order]:
        return [o for o in self.orders if o.side == SELL]

    @property
    def fee_total(self) -> float:
        return round(sum(o.fee for o in self.orders), 2)


def round_up_cents(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def round_down_cents(x: float) -> float:
    return math.floor(round(x * 100, 6)) / 100


def round_to_tick(price: float, rules: list[tuple[float, float]] | None, up: bool) -> float:
    """Round a limit price to the instrument's price step (up for buys, down for sells).

    rules: [(lower edge, increment)], e.g. LSE ETFs use 0.01 from 50 and 0.02 from 100.
    Without rules, whole cents."""
    tick = 0.01
    for edge, inc in sorted(rules or []):
        if price >= edge:
            tick = inc
    steps = round(price / tick, 6)
    n = math.ceil(steps) if up else math.floor(steps)
    decimals = max(2, len(f"{tick:.10f}".rstrip("0").split(".")[1]))
    return round(n * tick, decimals)
