from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .models import SATELLITE, SLEEVES, Allocation, Instrument


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
class FeeSettings:
    """Estimated cost per order: a commission (rate with a per-order minimum and an
    optional cap), GST on that commission, and fixed third-party fees per order.

    Defaults follow IBKR Pro Fixed pricing for USD orders on the LSE, checked October
    2026: 0.05% of trade value, at least USD 4.00, no cap, exchange and clearing fees
    included. Tiered is 0.05%, at least USD 1.70, at most USD 39.00, plus LSE exchange
    (0.0045%, at least GBP 0.10) and clearing (GBP 0.06) fees, about USD 0.21 per order
    at these sizes. Singapore residents pay GST (9%) on IBKR commissions.
    """

    rate_pct: float = 0.05
    min_per_order: float = 4.0
    max_per_order: float = 0.0  # 0 means no cap
    extra_per_order: float = 0.0  # exchange and clearing fees if you use Tiered pricing
    gst_pct: float = 9.0  # GST on the commission (Singapore residents)
    warn_pct: float = 1.0  # warn when the fee is this share of an order or more
    fx_rate_bp: float = 0.2  # IBKR spot FX: 0.2 basis points of trade value
    fx_min: float = 2.0  # at least USD 2.00 per conversion

    def estimate(self, notional: float) -> float:
        if notional <= 0:
            return 0.0
        commission = max(notional * self.rate_pct / 100, self.min_per_order)
        if self.max_per_order > 0:
            commission = min(commission, self.max_per_order)
        commission *= 1 + self.gst_pct / 100
        return round(commission + self.extra_per_order, 2)

    def fx_estimate(self, amount: float) -> float:
        if amount <= 0:
            return 0.0
        return round(max(amount * self.fx_rate_bp / 10000, self.fx_min), 2)


PROVIDERS = ("anthropic", "openai", "openai-compatible")
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-6.1-sol"}
DEFAULT_KEY_ENVS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


@dataclass(frozen=True)
class AssistantSettings:
    """Which chat model the web screen uses. The API key is read from an environment variable."""

    provider: str = "anthropic"
    model: str = ""
    api_key_env: str = ""
    base_url: str = ""
    max_tokens: int = 4096
    reasoning_effort: str = ""

    @property
    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, "")

    @property
    def key_env(self) -> str:
        return self.api_key_env or DEFAULT_KEY_ENVS.get(self.provider, "")


TOP_LEVEL_KEYS = {
    "base_currency",
    "broker",
    "rules",
    "fees",
    "assistant",
    "portfolio_file",
    "instruments",
    "allocations",
}
PORTFOLIO_KEYS = {"instruments", "allocations"}
INSTRUMENT_KEYS = {"name", "exchange", "currency", "conid"}
ALLOCATION_KEYS = {"symbol", "target", "label", "thesis", "invalidation"}


@dataclass(frozen=True)
class Config:
    base_currency: str
    rules: Rules
    broker: BrokerSettings
    instruments: dict[str, Instrument]
    allocations: tuple[Allocation, ...]
    path: Path | None = None
    assistant: AssistantSettings = field(default_factory=AssistantSettings)
    portfolio_path: Path | None = None
    fees: FeeSettings = field(default_factory=FeeSettings)

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
    def exit_symbols(self) -> list[str]:
        """Instruments kept in the list without a target: sold at the next rebalance."""
        allocated = {a.symbol for a in self.allocations}
        return [s for s in self.instruments if s not in allocated]

    @property
    def allowlist(self) -> set[str]:
        return set(self.instruments)

    def resolve_path(self, p: str) -> Path:
        """Resolve a path from the config relative to the config file's folder."""
        candidate = Path(p)
        if candidate.is_absolute() or self.path is None:
            return candidate
        return self.path.parent / candidate


def _read_yaml(p: Path, what: str) -> dict[str, Any]:
    if not p.exists():
        raise ConfigError(f"{what}을 찾을 수 없어요: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{what}을 읽을 수 없어요: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{what} 형식이 잘못됐어요: {p}")
    return raw


def load_config(path: str | Path) -> Config:
    """Read config.yaml and, when it names one, the portfolio file next to it."""
    p = Path(path)
    raw = _read_yaml(p, "설정 파일")
    portfolio_path = None
    if raw.get("portfolio_file"):
        portfolio_path = p.parent / str(raw["portfolio_file"])
        part = _read_yaml(portfolio_path, "포트폴리오 파일")
        unknown = sorted(set(part) - PORTFOLIO_KEYS)
        if unknown:
            raise ConfigError(f"포트폴리오 파일에 알 수 없는 항목이 있어요: {', '.join(unknown)}")
        for key in PORTFOLIO_KEYS:
            if key in raw:
                raise ConfigError(f"{key}: config.yaml과 포트폴리오 파일 두 곳에 있어요. 한 곳에만 두세요.")
        raw = {**raw, **part}
    cfg = parse_config(raw, p, portfolio_path)
    validate(cfg)
    return cfg


def _dataclass_from(cls, data: dict[str, Any] | None, section: str):
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{section} 형식이 잘못됐어요.")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"{section}에 알 수 없는 항목이 있어요: {', '.join(unknown)}")
    return cls(**data)


def parse_instruments(raw_instruments: dict[str, Any] | None, base_currency: str) -> dict[str, Instrument]:
    instruments: dict[str, Instrument] = {}
    for sym, spec in (raw_instruments or {}).items():
        spec = spec or {}
        symbol = str(sym).strip().upper()
        if not isinstance(spec, dict):
            raise ConfigError(f"instruments.{symbol} 형식이 잘못됐어요.")
        unknown = sorted(set(spec) - INSTRUMENT_KEYS)
        if unknown:
            raise ConfigError(f"instruments.{symbol}에 알 수 없는 항목이 있어요: {', '.join(unknown)}")
        conid = spec.get("conid")
        try:
            conid = int(conid) if conid not in (None, "", 0) else None
        except (TypeError, ValueError) as e:
            raise ConfigError(f"instruments.{symbol}.conid는 숫자여야 해요: {conid}") from e
        instruments[symbol] = Instrument(
            symbol=symbol,
            name=str(spec.get("name") or symbol),
            exchange=str(spec.get("exchange") or "LSE"),
            currency=str(spec.get("currency") or base_currency).upper(),
            conid=conid,
        )
    return instruments


def parse_allocations(raw_allocs: dict[str, Any] | None) -> tuple[Allocation, ...]:
    raw_allocs = raw_allocs or {}
    if not isinstance(raw_allocs, dict):
        raise ConfigError("allocations 형식이 잘못됐어요.")
    unknown_sleeves = sorted(set(raw_allocs) - set(SLEEVES))
    if unknown_sleeves:
        raise ConfigError(f"allocations에는 core와 satellite만 쓸 수 있어요: {', '.join(unknown_sleeves)}")
    allocs: list[Allocation] = []
    for sleeve in SLEEVES:
        for item in raw_allocs.get(sleeve) or []:
            if not isinstance(item, dict):
                raise ConfigError(f"allocations.{sleeve} 항목 형식이 잘못됐어요: {item}")
            unknown = sorted(set(item) - ALLOCATION_KEYS)
            if unknown:
                raise ConfigError(f"allocations.{sleeve}의 {item.get('symbol')}에 알 수 없는 항목이 있어요: {', '.join(unknown)}")
            try:
                allocs.append(
                    Allocation(
                        symbol=str(item["symbol"]).strip().upper(),
                        sleeve=sleeve,
                        target_pct=float(item["target"]),
                        label=str(item.get("label") or "").strip(),
                        thesis=str(item.get("thesis") or "").strip(),
                        invalidation=str(item.get("invalidation") or "").strip(),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                raise ConfigError(f"allocations.{sleeve} 항목 형식이 잘못됐어요 ({item}): {e}") from e
    return tuple(allocs)


def parse_config(raw: dict[str, Any], path: Path | None = None, portfolio_path: Path | None = None) -> Config:
    unknown = sorted(set(raw) - TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"설정에 알 수 없는 항목이 있어요: {', '.join(unknown)}")
    base = str(raw.get("base_currency", "USD")).upper()
    rules = _dataclass_from(Rules, raw.get("rules"), "rules")
    fees = _dataclass_from(FeeSettings, raw.get("fees"), "fees")
    assistant = _dataclass_from(AssistantSettings, raw.get("assistant"), "assistant")

    b = dict(raw.get("broker") or {})
    ibkr = _dataclass_from(IbkrSettings, b.pop("ibkr", None), "broker.ibkr")
    broker = _dataclass_from(BrokerSettings, {**b, "ibkr": ibkr}, "broker")

    return Config(
        base_currency=base,
        rules=rules,
        broker=broker,
        instruments=parse_instruments(raw.get("instruments"), base),
        allocations=parse_allocations(raw.get("allocations")),
        path=path,
        assistant=assistant,
        portfolio_path=portfolio_path,
        fees=fees,
    )


def portfolio_problems(cfg: Config) -> list[str]:
    """Problems with the targets and instruments: the part a person edits month to month."""
    errors: list[str] = []
    r = cfg.rules

    if not cfg.allocations:
        errors.append("목표 비중이 비어 있어요.")

    total = sum(a.target_pct for a in cfg.allocations)
    if abs(total - 100) > 0.01:
        gap = total - 100
        direction = "넘쳐요" if gap > 0 else "모자라요"
        errors.append(f"목표 비중 합계가 {total:g}%예요. 100%가 돼야 해요 ({abs(gap):g}%p {direction}).")

    seen: set[tuple[str, str]] = set()
    for a in cfg.allocations:
        key = (a.sleeve, a.symbol)
        if key in seen:
            errors.append(f"{a.symbol}: {a.sleeve}에 두 번 들어 있어요.")
        seen.add(key)
        if a.target_pct <= 0:
            errors.append(f"{a.symbol}: 목표 비중은 0보다 커야 해요. 빼려면 목록에서 지우세요.")
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

    for inst in cfg.instruments.values():
        if inst.currency != cfg.base_currency:
            errors.append(
                f"{inst.symbol}: 통화가 {inst.currency}예요. 지금 버전은 {cfg.base_currency} 한 통화만 지원해요."
            )
    return errors


def config_problems(cfg: Config) -> list[str]:
    errors = portfolio_problems(cfg)
    r = cfg.rules
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
    f = cfg.fees
    if not 0 <= f.rate_pct <= 5:
        errors.append("fees.rate_pct는 0~5 사이여야 해요.")
    if min(f.min_per_order, f.max_per_order, f.extra_per_order, f.fx_rate_bp, f.fx_min) < 0:
        errors.append("fees의 값은 0 이상이어야 해요.")
    if not 0 <= f.gst_pct <= 30:
        errors.append("fees.gst_pct는 0~30 사이여야 해요.")
    if 0 < f.max_per_order < f.min_per_order:
        errors.append("fees.max_per_order는 0(상한 없음)이거나 min_per_order보다 커야 해요.")
    if f.warn_pct <= 0:
        errors.append("fees.warn_pct는 0보다 커야 해요.")
    # Chat settings are checked when the web screen starts (llm.settings_problem),
    # so a typo there never blocks the CLI.
    return errors


def validate(cfg: Config) -> None:
    errors = config_problems(cfg)
    if errors:
        raise ConfigError("\n".join(errors))

