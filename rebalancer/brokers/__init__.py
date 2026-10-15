from __future__ import annotations

from typing import Callable, Protocol

from ..models import AccountSnapshot, Order

# Called with the broker's warning messages; returns True only if the person approved them.
ConfirmFn = Callable[[list[str]], bool]


class Broker(Protocol):
    name: str

    def snapshot(self) -> AccountSnapshot: ...

    def place_order(self, order: Order, client_order_id: str, confirm: ConfirmFn) -> dict: ...
