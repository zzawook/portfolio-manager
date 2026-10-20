"""Local state: which months already ran, the last annual rebalance, and a journal of target changes."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config


def month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def state_path_for(config_path: str | Path, broker_kind: str, override: str | Path | None = None) -> Path:
    """Practice runs on the mock broker keep their own state so they never block a real run."""
    if override:
        return Path(override)
    name = "state.json" if broker_kind == "ibkr" else "state.mock.json"
    return Path(config_path).resolve().parent / name


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "executions": {},
            "last_rebalance_year": None,
            "targets": {},
            "journal": [],
        }
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    @property
    def last_rebalance_year(self) -> Optional[int]:
        return self.data.get("last_rebalance_year")

    def executed(self, key: str) -> bool:
        return key in self.data["executions"]

    def record_execution(self, key: str, mode: str, reason: str, results: list[dict], rebalanced_year: Optional[int]):
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "reason": reason,
            "orders": results,
        }
        # A forced second run in the same month keeps the earlier record too.
        previous = self.data["executions"].get(key)
        if previous:
            entry["previous"] = previous
        self.data["executions"][key] = entry
        if rebalanced_year is not None:
            self.data["last_rebalance_year"] = rebalanced_year

    def extend_execution(self, key: str, results: list[dict]) -> None:
        """Add the buy step of a two-step rebalance to the month's record."""
        self.data["executions"][key]["orders"].extend(results)

    def runs(self) -> list[tuple[str, dict]]:
        """Every recorded run, newest first, including earlier runs of a repeated month."""
        out = []
        for key, rec in self.data["executions"].items():
            while rec:
                out.append((key, rec))
                rec = rec.get("previous")
        return sorted(out, key=lambda kr: kr[1].get("at", ""), reverse=True)

    def update_orders(self, details: dict[str, dict]) -> bool:
        """Store what became of orders (by client order id). Returns True if anything changed."""
        changed = False
        for _, rec in self.runs():
            for o in rec.get("orders") or []:
                d = details.get(str(o.get("coid") or ""))
                if d and any(o.get(k) != v for k, v in d.items()):
                    o.update(d)
                    changed = True
        return changed

    def execution(self, key: str) -> Optional[dict]:
        return self.data["executions"].get(key)

    def target_changes(self, cfg: Config) -> list[dict]:
        """Differences between the config targets and the ones recorded at the last execution."""
        old = self.data.get("targets") or {}
        if not old:
            return []
        changes = []
        current = {f"{a.sleeve}:{a.symbol}": a for a in cfg.allocations}
        for key, a in current.items():
            before = old.get(key)
            if before != a.target_pct:
                changes.append(
                    {
                        "key": key,
                        "from": before,
                        "to": a.target_pct,
                        "thesis": a.thesis,
                        "invalidation": a.invalidation,
                    }
                )
        for key, before in old.items():
            if key not in current:
                changes.append({"key": key, "from": before, "to": None, "thesis": "", "invalidation": ""})
        return changes

    def record_targets(self, cfg: Config, today: date) -> list[dict]:
        changes = self.target_changes(cfg)
        for c in changes:
            self.data["journal"].append({"date": today.isoformat(), **c})
        self.data["targets"] = {f"{a.sleeve}:{a.symbol}": a.target_pct for a in cfg.allocations}
        return changes

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
