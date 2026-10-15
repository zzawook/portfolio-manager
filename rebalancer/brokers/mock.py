"""File-backed broker for dry runs and practice. Orders fill instantly at the limit price, in memory only."""

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
        self.allow = cfg.allowlist
        self.submitted: list[tuple[str, Order]] = []

    def snapshot(self) -> AccountSnapshot:
        known = {s: q for s, q in self.positions.items() if s in self.allow and q}
        unknown = [s for s, q in self.positions.items() if s not in self.allow and q]
        return AccountSnapshot(cash=self.cash, positions=known, quotes=dict(self.quotes), unknown_positions=unknown)

    def place_order(self, order: Order, client_order_id: str, confirm: ConfirmFn) -> dict:
        if order.side == BUY:
            self.cash -= order.notional
            self.positions[order.symbol] = self.positions.get(order.symbol, 0) + order.quantity
        else:
            self.cash += order.notional
            self.positions[order.symbol] = self.positions.get(order.symbol, 0) - order.quantity
        self.submitted.append((client_order_id, order))
        return {"status": "submitted", "order_id": f"MOCK-{len(self.submitted)}", "detail": "모의 체결"}
