from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .brokers.ibkr import GatewayClient, IbkrBroker, IbkrError, account_id_for, resolve_candidates
from .brokers.mock import MockBroker
from .config import Config, ConfigError, load_config
from .envfile import load_env_file
from .execute import ExecutionAborted, run_execute
from .planner import REBALANCE, PlanError, build_plan
from .report import format_check, format_plan
from .state import State, state_path_for


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rebalancer", description="IBKR 목표 비중 리밸런서")
    p.add_argument("-c", "--config", default="config.yaml", help="설정 파일 (기본: config.yaml)")
    p.add_argument("--state", default=None, help="상태 파일 (기본: 설정 파일 옆 state.json)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="설정이 규칙에 맞는지 확인")

    for name, text in (("plan", "주문 계획만 계산해서 보여 주기 (드라이런)"), ("execute", "계획을 확인한 뒤 주문 보내기")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("--broker", choices=["mock", "ibkr"], help="설정의 broker.type 대신 쓸 브로커")
        sp.add_argument("--mock-file", help="모의 계좌 파일 (mock 브로커용)")
        sp.add_argument("--today", help="날짜 지정 (YYYY-MM-DD, 테스트용)")
        sp.add_argument("--force-rebalance", action="store_true", help="조건과 상관없이 리밸런싱으로 계산")
        if name == "execute":
            sp.add_argument("--force", action="store_true", help="이번 달에 이미 실행했어도 다시 실행")

    sub.add_parser("resolve", help="IBKR에서 종목별 conid 후보 찾기")

    sp = sub.add_parser("ui", help="로컬 웹 화면 열기 (비중 보기, 편집, AI 채팅, 주문)")
    sp.add_argument("--broker", choices=["mock", "ibkr"], help="설정의 broker.type 대신 쓸 브로커")
    sp.add_argument("--mock-file", help="모의 계좌 파일 (mock 브로커용)")
    sp.add_argument("--today", help="날짜 지정 (YYYY-MM-DD, 테스트용)")
    sp.add_argument("--port", type=int, default=8765, help="포트 (기본 8765)")
    sp.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않기")
    return p


def _broker_kind(cfg: Config, args) -> str:
    return getattr(args, "broker", None) or cfg.broker.type


def _broker(cfg: Config, args) -> object:
    kind = _broker_kind(cfg, args)
    if kind == "ibkr":
        return IbkrBroker(cfg)
    mock_file = getattr(args, "mock_file", None) or cfg.broker.mock_file
    path = Path(mock_file) if getattr(args, "mock_file", None) else cfg.resolve_path(mock_file)
    return MockBroker(path, cfg)


def _today(args) -> date:
    return date.fromisoformat(args.today) if getattr(args, "today", None) else date.today()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # API keys and the IBKR account number can live in .env next to config.yaml (kept out of git).
    load_env_file(Path(args.config).resolve().parent / ".env")
    try:
        cfg = load_config(args.config)
        kind = "ibkr" if args.command in ("check", "resolve") else _broker_kind(cfg, args)
        state = State(state_path_for(args.config, kind, args.state))

        if args.command == "check":
            print(format_check(cfg))
            changes = state.target_changes(cfg)
            if changes:
                print("")
                print("마지막 실행 이후 바뀐 목표 비중 (다음 실행 때 기록돼요)")
                for c in changes:
                    print(f"  {c['key']}: {c['from']} -> {c['to']}")
            return 0

        if args.command == "resolve":
            settings = cfg.broker.ibkr
            client = GatewayClient(settings.base_url, account_id_for(cfg), settings.verify_ssl)
            print("\n".join(resolve_candidates(cfg, client)))
            print("\nLSE 달러 라인의 conid를 portfolio.yaml의 instruments에 채우거나, 웹 화면에서 'IBKR에서 conid 찾기'로 고르세요.")
            return 0

        if args.command == "ui":
            from .server import serve

            serve(
                args.config,
                broker_kind=args.broker,
                mock_file=args.mock_file,
                today=_today(args) if args.today else None,
                port=args.port,
                open_browser=not args.no_browser,
            )
            return 0

        broker = _broker(cfg, args)
        today = _today(args)

        if args.command == "plan":
            snap = broker.snapshot()
            force_mode = REBALANCE if args.force_rebalance else None
            plan = build_plan(cfg, snap, today, state.last_rebalance_year, force_mode)
            print(f"[드라이런 · {broker.name} · {today.isoformat()}]")
            print(format_plan(cfg, snap, plan))
            return 0

        if args.command == "execute":
            run_execute(cfg, broker, state, today, force=args.force, force_rebalance=args.force_rebalance)
            return 0

    except (ConfigError, PlanError, IbkrError, ExecutionAborted) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n중단했어요.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
