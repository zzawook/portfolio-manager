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
