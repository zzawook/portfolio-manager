"""File-backed broker for dry runs and practice. Orders fill instantly at the limit price, in memory only.

The estimated commission is charged on each fill, so practice runs show the cash that would really be left."""

from __future__ import annotations

from pathlib import Path

import yaml

from ..config import Config, ConfigError
from ..models import BUY, AccountSnapshot, Order, Quote
from . import ConfirmFn


class MockBroker:
    name = "mock"

    def __init__(self, path: str | Path, cfg: Config):
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"모의 계좌 파일을 찾을 수 없어요: {p}")
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        self.cash = float(raw.get("cash", 0))
        self.positions = {str(k).upper(): float(v) for k, v in (raw.get("positions") or {}).items()}
        self.quotes = {str(k).upper(): Quote(**(v or {})) for k, v in (raw.get("quotes") or {}).items()}
        # Optional: cash in other currencies and their rates to the base currency,
        # e.g. other_cash: {SGD: 4000} and fx_rates: {SGD: 0.78}.
        self.other_cash = {str(k).upper(): float(v) for k, v in (raw.get("other_cash") or {}).items()}
        self.fx_rates = {str(k).upper(): float(v) for k, v in (raw.get("fx_rates") or {}).items()}
        self.fx_markup = cfg.fees.auto_fx_pct / 100
        self.allow = cfg.allowlist
        self.submitted: list[tuple[str, Order]] = []

    def reconfigure(self, cfg: Config) -> None:
        """Follow a changed instrument list without losing the practice fills made so far."""
        self.allow = cfg.allowlist

    def snapshot(self) -> AccountSnapshot:
        known = {s: q for s, q in self.positions.items() if s in self.allow and q}
        unknown = [s for s, q in self.positions.items() if s not in self.allow and q]
        return AccountSnapshot(
            cash=self.cash,
            positions=known,
            quotes=dict(self.quotes),
            unknown_positions=unknown,
            other_cash=dict(self.other_cash),
            fx_rates=dict(self.fx_rates),
        )

    def place_order(self, order: Order, client_order_id: str, confirm: ConfirmFn) -> dict:
        self.cash += order.cash_effect  # the estimated fee is charged as if it were the real one
        # Like IBKR's automatic conversion: cover a shortfall from other currencies.
        for cur in list(self.other_cash):
            if self.cash >= 0:
                break
            rate = self.fx_rates.get(cur)
            if not rate:
                continue
            need = -self.cash * (1 + self.fx_markup) / rate  # in the other currency
            used = min(need, self.other_cash[cur])
            self.other_cash[cur] -= used
            self.cash += used * rate / (1 + self.fx_markup)
        if order.side == BUY:
            self.positions[order.symbol] = self.positions.get(order.symbol, 0) + order.quantity
        else:
            self.positions[order.symbol] = self.positions.get(order.symbol, 0) - order.quantity
        self.submitted.append((client_order_id, order))
        return {"status": "submitted", "order_id": f"MOCK-{len(self.submitted)}", "detail": "모의 체결"}
