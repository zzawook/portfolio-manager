from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .models import CORE, SATELLITE, SLEEVES, Allocation, Instrument


class ConfigError(ValueError):
    """Raised when the configuration breaks one of the plan's rules."""


@dataclass(frozen=True)
class Rules:
    monthly_contribution: float = 3000.0
    max_buy_orders: int = 3
    min_order_value: float = 100.0
    max_order_value: float = 5000.0
    cash_reserve: float = 20.0
    drift_threshold_pp: float = 5.0
    annual_rebalance_month: int = 1
    min_value_for_rebalance: float = 20000.0
    limit_buffer_pct: float = 0.3
    max_satellite_idea_pct: float = 15.0


@dataclass(frozen=True)
class IbkrSettings:
    base_url: str = "https://localhost:5000/v1/api"
    account_id: str = ""
    verify_ssl: bool = False


@dataclass(frozen=True)
class BrokerSettings:
    type: str = "mock"
    mock_file: str = "examples/month01.yaml"
    ibkr: IbkrSettings = field(default_factory=IbkrSettings)


@dataclass(frozen=True)
class Config:
    base_currency: str
    rules: Rules
    broker: BrokerSettings
    instruments: dict[str, Instrument]
    allocations: tuple[Allocation, ...]
    path: Path | None = None

    def symbol_targets(self) -> dict[str, float]:
        """Target weight per symbol in percent, summed across sleeves (IGLN core + satellite)."""
        out: dict[str, float] = {}
        for a in self.allocations:
            out[a.symbol] = out.get(a.symbol, 0.0) + a.target_pct
        return out

    def sleeve_targets(self) -> dict[str, float]:
        out = {s: 0.0 for s in SLEEVES}
        for a in self.allocations:
            out[a.sleeve] += a.target_pct
        return out

    def satellite_allocations(self) -> list[Allocation]:
        return [a for a in self.allocations if a.sleeve == SATELLITE]

    @property
    def allowlist(self) -> set[str]:
        return set(self.instruments)

    def resolve_path(self, p: str) -> Path:
        """Resolve a path from the config relative to the config file's folder."""
        candidate = Path(p)
        if candidate.is_absolute() or self.path is None:
            return candidate
        return self.path.parent / candidate


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"설정 파일을 찾을 수 없어요: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"설정 파일을 읽을 수 없어요: {e}") from e
    cfg = parse_config(raw, p)
    validate(cfg)
    return cfg


def _dataclass_from(cls, data: dict[str, Any] | None, section: str):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"{section}에 알 수 없는 항목이 있어요: {', '.join(unknown)}")
    return cls(**data)


def parse_config(raw: dict[str, Any], path: Path | None = None) -> Config:
    base = str(raw.get("base_currency", "USD")).upper()
    rules = _dataclass_from(Rules, raw.get("rules"), "rules")

    b = dict(raw.get("broker") or {})
    ibkr = _dataclass_from(IbkrSettings, b.pop("ibkr", None), "broker.ibkr")
    broker = _dataclass_from(BrokerSettings, {**b, "ibkr": ibkr}, "broker")

    instruments: dict[str, Instrument] = {}
    for sym, spec in (raw.get("instruments") or {}).items():
        spec = spec or {}
        conid = spec.get("conid")
        symbol = str(sym).upper()
        instruments[symbol] = Instrument(
            symbol=symbol,
            name=str(spec.get("name", symbol)),
            exchange=str(spec.get("exchange", "LSE")),
            currency=str(spec.get("currency", base)).upper(),
            conid=int(conid) if conid else None,
        )

    allocs: list[Allocation] = []
    raw_allocs = raw.get("allocations") or {}
    unknown_sleeves = sorted(set(raw_allocs) - set(SLEEVES))
    if unknown_sleeves:
        raise ConfigError(f"allocations에는 core와 satellite만 쓸 수 있어요: {', '.join(unknown_sleeves)}")
    for sleeve in SLEEVES:
        for item in raw_allocs.get(sleeve) or []:
            try:
                allocs.append(
                    Allocation(
                        symbol=str(item["symbol"]).upper(),
                        sleeve=sleeve,
                        target_pct=float(item["target"]),
                        label=str(item.get("label", "")),
                        thesis=str(item.get("thesis") or "").strip(),
                        invalidation=str(item.get("invalidation") or "").strip(),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                raise ConfigError(f"allocations.{sleeve} 항목 형식이 잘못됐어요 ({item}): {e}") from e

    return Config(
        base_currency=base,
        rules=rules,
        broker=broker,
        instruments=instruments,
        allocations=tuple(allocs),
        path=path,
    )


def validate(cfg: Config) -> None:
    errors: list[str] = []
    r = cfg.rules

    if not cfg.allocations:
        errors.append("allocations가 비어 있어요.")

    total = sum(a.target_pct for a in cfg.allocations)
    if abs(total - 100) > 0.01:
        errors.append(f"목표 비중 합계가 {total:g}%예요. 100%가 돼야 해요.")

    seen: set[tuple[str, str]] = set()
    for a in cfg.allocations:
        key = (a.sleeve, a.symbol)
        if key in seen:
            errors.append(f"{a.symbol}: {a.sleeve}에 두 번 들어 있어요.")
        seen.add(key)
        if a.target_pct <= 0:
            errors.append(f"{a.symbol}: 목표 비중은 0보다 커야 해요.")
        if a.symbol not in cfg.instruments:
            errors.append(f"{a.symbol}: instruments에 없는 종목이에요.")
        if a.sleeve == SATELLITE:
            if not a.thesis or not a.invalidation:
                errors.append(
                    f"{a.symbol} (위성): thesis(이유)와 invalidation(틀렸다고 볼 조건)을 둘 다 적어야 해요."
                )
            if a.target_pct > r.max_satellite_idea_pct + 1e-9:
                errors.append(
                    f"{a.symbol} (위성): {a.target_pct:g}%로 아이디어당 상한 {r.max_satellite_idea_pct:g}%를 넘어요."
                )

    unused = sorted(set(cfg.instruments) - {a.symbol for a in cfg.allocations})
    if unused:
        errors.append(f"instruments에만 있고 비중이 없는 종목이 있어요: {', '.join(unused)}")

    for inst in cfg.instruments.values():
        if inst.currency != cfg.base_currency:
            errors.append(
                f"{inst.symbol}: 통화가 {inst.currency}예요. 지금 버전은 {cfg.base_currency} 한 통화만 지원해요."
            )

    if r.max_buy_orders < 1:
        errors.append("rules.max_buy_orders는 1 이상이어야 해요.")
    if r.min_order_value < 0:
        errors.append("rules.min_order_value는 0 이상이어야 해요.")
    if r.max_order_value <= r.min_order_value:
        errors.append("rules.max_order_value는 min_order_value보다 커야 해요.")
    if r.cash_reserve < 0:
        errors.append("rules.cash_reserve는 0 이상이어야 해요.")
    if r.drift_threshold_pp <= 0:
        errors.append("rules.drift_threshold_pp는 0보다 커야 해요.")
    if not 1 <= r.annual_rebalance_month <= 12:
        errors.append("rules.annual_rebalance_month는 1~12 사이여야 해요.")
    if not 0 <= r.limit_buffer_pct <= 5:
        errors.append("rules.limit_buffer_pct는 0~5 사이여야 해요.")

    if cfg.broker.type not in ("mock", "ibkr"):
        errors.append("broker.type은 mock 또는 ibkr여야 해요.")

    if errors:
        raise ConfigError("\n".join(errors))
