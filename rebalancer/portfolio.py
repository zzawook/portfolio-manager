"""The part of the setup a person changes: instruments and target weights.

The web UI works on a draft copy (Draft) and writes it back to portfolio.yaml.
Every save keeps the previous file under history/ next to it, so nothing is lost.
"""

from __future__ import annotations

import copy
import math
import os
import re
from dataclasses import replace
from datetime import datetime
from typing import Any, Optional

import yaml

from .config import Config, ConfigError, load_config, parse_allocations, parse_instruments, portfolio_problems
from .models import SLEEVES

SLEEVE_LABELS = {"core": "코어", "satellite": "위성"}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
NOTE_LIMIT = 500
LABEL_LIMIT = 60

HEADER = """\
# 목표 비중과 종목 목록이에요. 웹 화면에서 저장해서 새로 쓴 파일이에요.
# 직접 고쳐도 돼요. 이전 버전은 history/ 폴더에 남아 있어요.
# 목표가 없는 종목을 instruments에 남겨 두면 '정리 대상'이 돼요. 보유 중이면 리밸런싱 때 팔아요.

"""


class DraftError(ValueError):
    """A draft edit that cannot be applied as asked."""


class DraftConflict(DraftError):
    """The draft changed since the caller last saw it."""


def fmt_pct(x: float) -> str:
    return f"{x:g}%"


def _symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise DraftError(f"티커 형식이 잘못됐어요: '{value}'. 영문 대문자와 숫자로 12자 이내여야 해요.")
    return symbol


def _text(value: Any, limit: int = NOTE_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _target(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise DraftError(f"목표 비중은 숫자여야 해요: {value}") from e
    if not math.isfinite(number):
        raise DraftError(f"목표 비중은 숫자여야 해요: {value}")
    return round(number, 2)


def _conid(value: Any) -> Optional[int]:
    if value in (None, "", 0):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise DraftError(f"conid는 숫자여야 해요: {value}") from e
    if number <= 0:
        raise DraftError(f"conid는 양수여야 해요: {value}")
    return number


def portfolio_from_config(cfg: Config) -> dict:
    return {
        "instruments": [
            {"symbol": i.symbol, "name": i.name, "exchange": i.exchange, "currency": i.currency, "conid": i.conid}
            for i in cfg.instruments.values()
        ],
        "allocations": [
            {
                "sleeve": a.sleeve,
                "symbol": a.symbol,
                "target": a.target_pct,
                "label": a.label,
                "thesis": a.thesis,
                "invalidation": a.invalidation,
            }
            for a in cfg.allocations
        ],
    }


def normalize_portfolio(data: Any, base_currency: str) -> dict:
    """Clean up a portfolio that came from the browser or the assistant."""
    if not isinstance(data, dict):
        raise DraftError("포트폴리오 형식이 잘못됐어요.")
    instruments: list[dict] = []
    seen: set[str] = set()
    for item in data.get("instruments") or []:
        if not isinstance(item, dict):
            raise DraftError("종목 정보 형식이 잘못됐어요.")
        symbol = _symbol(item.get("symbol"))
        if symbol in seen:
            raise DraftError(f"{symbol}: 종목 목록에 두 번 들어 있어요.")
        seen.add(symbol)
        instruments.append(
            {
                "symbol": symbol,
                "name": _text(item.get("name"), 120) or symbol,
                "exchange": _text(item.get("exchange"), 20).upper() or "LSE",
                "currency": (_text(item.get("currency"), 3) or base_currency).upper(),
                "conid": _conid(item.get("conid")),
            }
        )
    allocations: list[dict] = []
    for item in data.get("allocations") or []:
        if not isinstance(item, dict):
            raise DraftError("목표 비중 형식이 잘못됐어요.")
        sleeve = item.get("sleeve")
        if sleeve not in SLEEVES:
            raise DraftError(f"칸은 core 또는 satellite여야 해요: {sleeve}")
        allocations.append(
            {
                "sleeve": sleeve,
                "symbol": _symbol(item.get("symbol")),
                "target": _target(item.get("target")),
                "label": _text(item.get("label"), LABEL_LIMIT),
                "thesis": _text(item.get("thesis")),
                "invalidation": _text(item.get("invalidation")),
            }
        )
    # Same order as the file: core first, then satellite (stable within a sleeve).
    allocations.sort(key=lambda a: SLEEVES.index(a["sleeve"]))
    return {"instruments": instruments, "allocations": allocations}


def _plain_number(x: float) -> float | int:
    return int(x) if float(x).is_integer() else x


def portfolio_to_raw(portfolio: dict) -> dict:
    """The YAML shape of portfolio.yaml."""
    instruments = {
        i["symbol"]: {"name": i["name"], "exchange": i["exchange"], "currency": i["currency"], "conid": i["conid"]}
        for i in portfolio["instruments"]
    }
    allocations: dict[str, list[dict]] = {s: [] for s in SLEEVES}
    for a in portfolio["allocations"]:
        item: dict[str, Any] = {"symbol": a["symbol"], "target": _plain_number(a["target"])}
        for key in ("label", "thesis", "invalidation"):
            if a.get(key):
                item[key] = a[key]
        allocations.setdefault(a["sleeve"], []).append(item)
    return {"instruments": instruments, "allocations": allocations}


def config_from_portfolio(cfg: Config, portfolio: dict) -> Config:
    """The same rules and broker settings with the given instruments and targets (not validated)."""
    raw = portfolio_to_raw(portfolio)
    return replace(
        cfg,
        instruments=parse_instruments(raw["instruments"], cfg.base_currency),
        allocations=parse_allocations(raw["allocations"]),
    )


def evaluate(cfg: Config, portfolio: dict) -> tuple[Optional[Config], list[str]]:
    try:
        new_cfg = config_from_portfolio(cfg, portfolio)
    except ConfigError as e:
        return None, [str(e)]
    return new_cfg, portfolio_problems(new_cfg)


def save_portfolio(cfg: Config, portfolio: dict, now: Optional[datetime] = None) -> Config:
    """Write portfolio.yaml after checking it, keeping the old file in history/. Returns the reloaded config."""
    _, problems = evaluate(cfg, portfolio)
    if problems:
        raise ConfigError("\n".join(problems))
    path = cfg.portfolio_path
    if path is None or cfg.path is None:
        raise ConfigError(
            "config.yaml에 portfolio_file이 없어서 화면에서 저장할 수 없어요. "
            "instruments와 allocations를 portfolio.yaml로 옮기고 portfolio_file: portfolio.yaml을 적어 주세요."
        )
    body = yaml.safe_dump(portfolio_to_raw(portfolio), sort_keys=False, allow_unicode=True, width=200)
    if path.exists():
        history = path.parent / "history"
        history.mkdir(exist_ok=True)
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup = history / f"{path.stem}-{stamp}{path.suffix}"
        n = 1
        while backup.exists():
            backup = history / f"{path.stem}-{stamp}-{n}{path.suffix}"
            n += 1
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(HEADER + body, encoding="utf-8")
    os.replace(tmp, path)
    return load_config(cfg.path)


def portfolio_changes(old: dict, new: dict) -> list[dict]:
    """What a draft changes compared with the saved portfolio, in plain words."""
    changes: list[dict] = []
    old_a = {(a["sleeve"], a["symbol"]): a for a in old["allocations"]}
    new_a = {(a["sleeve"], a["symbol"]): a for a in new["allocations"]}
    for (sleeve, sym), a in new_a.items():
        key = f"{sleeve}:{sym}"
        label = SLEEVE_LABELS[sleeve]
        before = old_a.get((sleeve, sym))
        if before is None:
            changes.append({"key": key, "kind": "added", "text": f"{sym} {label} {fmt_pct(a['target'])} 추가"})
            continue
        if abs(a["target"] - before["target"]) > 1e-9:
            changes.append(
                {"key": key, "kind": "target", "text": f"{sym} {label} {fmt_pct(a['target'])} (기존 {fmt_pct(before['target'])})"}
            )
        if any(a.get(k, "") != before.get(k, "") for k in ("label", "thesis", "invalidation")):
            changes.append({"key": key, "kind": "notes", "text": f"{sym} {label} 메모 수정"})
    for (sleeve, sym), before in old_a.items():
        if (sleeve, sym) not in new_a:
            changes.append(
                {
                    "key": f"{sleeve}:{sym}",
                    "kind": "removed",
                    "text": f"{sym} {SLEEVE_LABELS[sleeve]}에서 뺌 (기존 {fmt_pct(before['target'])})",
                }
            )

    old_i = {i["symbol"]: i for i in old["instruments"]}
    new_i = {i["symbol"]: i for i in new["instruments"]}
    allocated_new = {sym for _, sym in new_a}
    for sym, inst in new_i.items():
        before = old_i.get(sym)
        if before is None:
            if sym not in allocated_new:
                changes.append({"key": f"instrument:{sym}", "kind": "instrument", "text": f"{sym} 종목 목록에 추가"})
            continue
        if inst["conid"] != before["conid"]:
            text = f"{sym} conid {inst['conid']}" if inst["conid"] else f"{sym} conid 지움"
            changes.append({"key": f"instrument:{sym}", "kind": "instrument", "text": text})
        elif (inst["name"], inst["exchange"], inst["currency"]) != (before["name"], before["exchange"], before["currency"]):
            changes.append({"key": f"instrument:{sym}", "kind": "instrument", "text": f"{sym} 종목 정보 수정"})
    for sym in old_i:
        if sym not in new_i:
            changes.append({"key": f"instrument:{sym}", "kind": "instrument", "text": f"{sym} 종목 목록에서 지움"})
    return changes


class Draft:
    """An editable copy of the saved portfolio. Every change bumps `version`."""

    def __init__(self, cfg: Config):
        self.version = 0
        self.reset(cfg)

    def reset(self, cfg: Config) -> None:
        self.cfg = cfg
        self.saved = normalize_portfolio(portfolio_from_config(cfg), cfg.base_currency)
        self.portfolio = copy.deepcopy(self.saved)
        self.version += 1

    # Reading ---------------------------------------------------------------

    def changes(self) -> list[dict]:
        return portfolio_changes(self.saved, self.portfolio)

    @property
    def dirty(self) -> bool:
        return bool(self.changes())

    def evaluate(self) -> tuple[Optional[Config], list[str]]:
        return evaluate(self.cfg, self.portfolio)

    def view(self) -> dict:
        _, problems = self.evaluate()
        changes = self.changes()
        return {
            "version": self.version,
            "portfolio": copy.deepcopy(self.portfolio),
            "problems": problems,
            "changes": changes,
            "dirty": bool(changes),
        }

    def _allocation(self, portfolio: dict, sleeve: str, symbol: str) -> Optional[dict]:
        return next((a for a in portfolio["allocations"] if a["sleeve"] == sleeve and a["symbol"] == symbol), None)

    def _instrument(self, portfolio: dict, symbol: str) -> Optional[dict]:
        return next((i for i in portfolio["instruments"] if i["symbol"] == symbol), None)

    # Writing ---------------------------------------------------------------

    def _commit(self, portfolio: dict) -> None:
        self.portfolio = normalize_portfolio(portfolio, self.cfg.base_currency)
        self.version += 1

    def replace(self, data: Any, expected_version: Optional[int] = None) -> None:
        if expected_version is not None and expected_version != self.version:
            raise DraftConflict("초안이 다른 곳에서 바뀌었어요. 화면을 다시 불러올게요.")
        self._commit(data)

    def set_allocation(
        self,
        sleeve: str,
        symbol: str,
        target: Optional[float] = None,
        label: Optional[str] = None,
        thesis: Optional[str] = None,
        invalidation: Optional[str] = None,
        instrument: Optional[dict] = None,
    ) -> str:
        if sleeve not in SLEEVES:
            raise DraftError("칸(sleeve)은 core 또는 satellite여야 해요.")
        symbol = _symbol(symbol)
        sleeve_label = SLEEVE_LABELS[sleeve]
        portfolio = copy.deepcopy(self.portfolio)

        inst = self._instrument(portfolio, symbol)
        if instrument:
            spec = {
                "symbol": symbol,
                "name": instrument.get("name") or (inst or {}).get("name") or symbol,
                "exchange": instrument.get("exchange") or (inst or {}).get("exchange") or "LSE",
                "currency": instrument.get("currency") or (inst or {}).get("currency") or self.cfg.base_currency,
                "conid": instrument.get("conid", (inst or {}).get("conid")),
            }
            if inst is None:
                portfolio["instruments"].append(spec)
            else:
                inst.update(spec)
        elif inst is None:
            raise DraftError(
                f"{symbol}: 처음 보는 종목이에요. instrument(이름, 거래소, 통화)를 같이 넣어 주세요."
            )

        alloc = self._allocation(portfolio, sleeve, symbol)
        if target is not None:
            target = _target(target)
            if target <= 0:
                raise DraftError("목표 비중은 0보다 커야 해요. 빼려면 remove_allocation을 쓰세요.")
            if target > 100:
                raise DraftError("목표 비중은 100%를 넘을 수 없어요.")

        if alloc is None:
            if target is None:
                raise DraftError(f"{symbol}: {sleeve_label}에 새로 넣으려면 목표 비중(target)이 필요해요.")
            portfolio["allocations"].append(
                {
                    "sleeve": sleeve,
                    "symbol": symbol,
                    "target": target,
                    "label": label or "",
                    "thesis": thesis or "",
                    "invalidation": invalidation or "",
                }
            )
            text = f"{symbol} {sleeve_label} {fmt_pct(target)} 추가"
        else:
            parts = []
            if target is not None and abs(target - alloc["target"]) > 1e-9:
                parts.append(f"{fmt_pct(target)} (기존 {fmt_pct(alloc['target'])})")
                alloc["target"] = target
            notes_changed = False
            for key, value in (("label", label), ("thesis", thesis), ("invalidation", invalidation)):
                if value is not None and value != alloc.get(key, ""):
                    alloc[key] = value
                    notes_changed = True
            if notes_changed:
                parts.append("메모 수정")
            if instrument:
                parts.append("종목 정보 수정")
            text = f"{symbol} {sleeve_label} " + (", ".join(parts) if parts else "바뀐 것 없음")
        self._commit(portfolio)
        return text

    def remove_allocation(self, sleeve: str, symbol: str, held: bool) -> str:
        symbol = _symbol(symbol)
        if sleeve not in SLEEVES:
            raise DraftError("칸(sleeve)은 core 또는 satellite여야 해요.")
        portfolio = copy.deepcopy(self.portfolio)
        alloc = self._allocation(portfolio, sleeve, symbol)
        if alloc is None:
            raise DraftError(f"{symbol}: {SLEEVE_LABELS[sleeve]}에 없는 종목이에요.")
        portfolio["allocations"].remove(alloc)
        text = f"{symbol} {SLEEVE_LABELS[sleeve]}에서 뺌 (기존 {fmt_pct(alloc['target'])})"
        still_allocated = any(a["symbol"] == symbol for a in portfolio["allocations"])
        if not still_allocated:
            if held:
                text += ". 보유 중이라 정리 대상으로 남겨 뒀어요. 리밸런싱 때 팔아요."
            else:
                portfolio["instruments"] = [i for i in portfolio["instruments"] if i["symbol"] != symbol]
        self._commit(portfolio)
        return text

    def set_conid(self, symbol: str, conid: Optional[int]) -> str:
        symbol = _symbol(symbol)
        portfolio = copy.deepcopy(self.portfolio)
        inst = self._instrument(portfolio, symbol)
        if inst is None:
            raise DraftError(f"{symbol}: 종목 목록에 없어요.")
        inst["conid"] = _conid(conid)
        self._commit(portfolio)
        return f"{symbol} conid {inst['conid'] or '지움'}"

