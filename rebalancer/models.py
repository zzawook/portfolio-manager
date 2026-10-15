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
class AccountSnapshot:
    cash: float
    positions: dict[str, float]
    quotes: dict[str, Quote]
    unknown_positions: list[str] = field(default_factory=list)


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
