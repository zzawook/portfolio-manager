"""In-app chat that edits the draft portfolio only.

The chat model gets tools to read the draft, change or remove target weights,
preview the month's orders, and look up IBKR contracts. There is deliberately no
tool that sends orders: orders go out only when the person presses the button in
the plan dialog and confirms.

Which model answers is a setting (see llm.py): the user's ChatGPT plan (default),
OpenAI or Anthropic with an API key, or any OpenAI-compatible server.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Protocol

from .llm import ChatProvider, describe_error

log = logging.getLogger(__name__)

MAX_STEPS = 8
MAX_HISTORY_TURNS = 40
MAX_TRANSCRIPT = 200
MAX_MESSAGE_CHARS = 4000

SLEEVE_ENUM = ["core", "satellite"]

TOOLS: list[dict] = [
    {
        "name": "get_portfolio",
        "description": (
            "지금 초안(종목, 칸별 목표 비중, 위성 메모, 검사 결과, 저장 안 한 변경)과 최근 보유 비중을 돌려줘요. "
            "바꾸기 전에 현재 상태를 다시 확인할 때 쓰세요."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_allocation",
        "description": (
            "초안에서 한 칸(core 또는 satellite)에 있는 종목의 목표 비중이나 메모를 바꾸거나, 종목을 새로 넣어요. "
            "저장도 주문도 하지 않아요. 합계가 100%가 되도록 다른 종목도 함께 조정하세요. "
            "처음 보는 종목이면 instrument(이름, 거래소, 통화)를 꼭 넣고, 위성이면 thesis와 invalidation도 넣으세요. "
            "적지 않은 항목은 그대로 둬요."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sleeve": {"type": "string", "enum": SLEEVE_ENUM, "description": "core(코어) 또는 satellite(위성)"},
                "symbol": {"type": "string", "description": "티커. 예: VWRA"},
                "target": {
                    "type": "number",
                    "description": "전체 포트폴리오 대비 목표 비중(%). 0보다 크고 100 이하.",
                },
                "label": {"type": "string", "description": "짧은 설명. 예: 전세계 주식"},
                "thesis": {"type": "string", "description": "위성 종목을 담는 이유"},
                "invalidation": {"type": "string", "description": "이 아이디어가 틀렸다고 볼 조건"},
                "instrument": {
                    "type": "object",
                    "description": "새 종목이거나 종목 정보를 고칠 때만 넣어요.",
                    "properties": {
                        "name": {"type": "string", "description": "상품 정식 이름"},
                        "exchange": {"type": "string", "description": "상장 거래소. 예: LSE"},
                        "currency": {"type": "string", "description": "거래 통화. 예: USD"},
                        "conid": {"type": "integer", "description": "IBKR 종목 번호 (find_contracts로 확인한 경우만)"},
                    },
                    "required": ["name", "exchange", "currency"],
                    "additionalProperties": False,
                },
            },
            "required": ["sleeve", "symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remove_allocation",
        "description": (
            "초안의 한 칸에서 종목을 빼요. 저장도 주문도 하지 않아요. 보유 중인 종목은 '정리 대상'으로 남아서 "
            "리밸런싱 때 팔려요. 뺀 만큼 다른 종목 비중을 올려서 합계를 100%로 맞추세요."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sleeve": {"type": "string", "enum": SLEEVE_ENUM},
                "symbol": {"type": "string"},
            },
            "required": ["sleeve", "symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "preview_orders",
        "description": (
            "지금 초안과 현재 잔고로 이번 달 주문 계획과 예상 수수료를 계산해서 보여 줘요. 계산만 하고 주문은 보내지 않아요. "
            "초안에 문제가 있으면 계산하지 않아요. force_rebalance가 true면 매도까지 포함한 리밸런싱으로 계산해요."
        ),
        "parameters": {
            "type": "object",
            "properties": {"force_rebalance": {"type": "boolean"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "find_contracts",
        "description": (
            "IBKR에서 티커로 상품 후보(conid, 거래소, 통화, 이름)를 찾아요. IBKR Gateway에 로그인돼 있을 때만 돼요. "
            "recommended가 true인 후보가 원하는 거래소와 통화에 맞는 상품이에요."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "exchange": {"type": "string", "description": "원하는 거래소. 기본 LSE"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
]

SYSTEM_PROMPT = """\
이 앱 사용자의 ETF 포트폴리오 목표 비중 초안을 함께 고치는 도우미예요. 사용자는 싱가포르에 살고, IBKR에서 매달 적립식으로 투자해요.

할 수 있는 일과 없는 일
- 도구로 초안(종목, 목표 비중, 위성 아이디어 메모)을 바꾸고, 이번 달 주문 계획과 예상 수수료를 미리 계산해 볼 수 있어요.
- 주문은 보낼 수 없어요. 주문은 사용자가 화면 아래 '주문 계획 보기'를 누르고 내용을 확인한 뒤 직접 보내야 나가요. 주문을 보냈다거나 보내겠다고 말하지 마세요.
- 초안은 아직 저장되지 않은 상태예요. 사용자가 '목표 저장'을 누르거나 주문을 보낼 때 저장돼요.

포트폴리오 규칙
- 코어(core)는 예측 없이 오래 유지하는 자리예요. 시장 전망 때문에 코어를 바꾸자는 요청이면, 바꾸기 전에 코어 원칙과 어긋난다는 점을 한 줄로 짚고 위성으로 같은 효과를 내는 방법도 함께 제안하세요. 그래도 원하면 바꿔 주세요.
- 위성(satellite)은 매크로 판단을 담는 자리예요. 위성 종목마다 thesis(담는 이유)와 invalidation(틀렸다고 볼 조건)이 꼭 있어야 하고, 한 아이디어는 최대 {max_idea}%예요. 사용자가 이유나 조건을 말하지 않았으면 문구를 제안해서 넣고, 본인 판단으로 고쳐 쓰라고 알려 주세요.
- 목표 비중 합계는 정확히 100%여야 해요. 한 종목을 바꾸면 어디서 덜거나 채울지도 함께 바꿔서 합계를 맞추세요. 사용자가 말하지 않았으면 가장 자연스러운 곳을 골라 맞추고, 그렇게 했다고 알려 주세요.
- 한 종목이 두 칸에 동시에 있을 수 있어요. 예를 들어 IGLN은 코어와 위성에 나뉘어 있을 수 있어요.
- 통화는 {currency}만 써요. 지금까지는 미국 배당 원천징수(15%)와 미국 상속세 때문에 런던거래소(LSE)에 상장된 아일랜드 UCITS ETF의 달러 라인을 써 왔어요. 새 종목도 같은 기준을 따르는 게 좋아요.
- 새 종목은 instrument(이름, 거래소, 통화)를 함께 넣어야 해요. 티커나 상품이 실제로 있는지 확실하지 않으면 지어내지 말고 사용자에게 확인하세요. IBKR에 연결돼 있으면 find_contracts로 확인할 수 있어요.
- 보유 중인 종목을 목표에서 빼면 '정리 대상'으로 남고, 실제 매도는 리밸런싱 때 이뤄져요.
- 수수료는 주문마다 따로 붙어요 ({fee_rule}). 작은 주문을 여러 개 만드는 변경이면 수수료 비율이 커진다는 점을 알려 주세요.

답하는 방식
- 한국어 존댓말로 짧고 분명하게 답하세요. 긴 대시(—)는 쓰지 마세요.
- 매수나 매도를 확신하며 권하지 말고, 판단에 필요한 사실과 트레이드오프를 알려 주세요. 최신 가격이나 뉴스는 알 수 없다고 말하세요.
- 초안을 바꿨으면 바뀐 내용을 한두 줄로 정리하고, 저장이나 주문은 사용자가 버튼으로 한다고 덧붙이세요.

규칙 값: 위성 아이디어 상한 {max_idea}%, 비중 이탈 리밸런싱 기준 {drift}%p, 리밸런싱을 하는 최소 투자 금액 {min_value:,.0f} {currency}, 정기 적립 때 한 번에 최대 {max_buys}종목 매수, 1회 주문 한도 {max_order:,.0f} {currency}.

현재 상태 (이번 요청 기준)
{context}
"""


TOOL_NAMES = {t["name"] for t in TOOLS}


def known_tool_name(name: str) -> str:
    """Tool names can come back prefixed with their namespace (e.g. portfolio.set_allocation)."""
    if name in TOOL_NAMES:
        return name
    for sep in (".", "/", ":", "__"):
        tail = name.rsplit(sep, 1)[-1]
        if tail in TOOL_NAMES:
            return tail
    return name


class AssistantHost(Protocol):
    """What the chat needs from the app: the draft and read-only views of the account."""

    def assistant_context(self) -> dict: ...

    def draft_overview(self) -> dict: ...

    def set_allocation(self, **kwargs: Any) -> str: ...

    def remove_allocation(self, sleeve: str, symbol: str) -> str: ...

    def preview_orders(self, force_rebalance: bool) -> dict: ...

    def find_contracts(self, symbol: str, exchange: str = "LSE") -> list[dict]: ...


class AssistantBusy(RuntimeError):
    pass


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Assistant:
    def __init__(
        self,
        provider: ChatProvider,
        host: AssistantHost,
        rules: Any,
        currency: str = "USD",
        fee_rule: str = "",
    ):
        self.provider = provider
        self.host = host
        self.rules = rules
        self.currency = currency
        self.fee_rule = fee_rule
        self.history: list[dict] = []
        self.transcript: list[dict] = []
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def reset(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise AssistantBusy("답하는 중에는 새 대화를 시작할 수 없어요.")
        try:
            self.history = []
            self.transcript = []
        finally:
            self._lock.release()

    def send(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("보낼 내용이 없어요.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError(f"메시지가 너무 길어요. {MAX_MESSAGE_CHARS:,}자 안으로 줄여 주세요.")
        if not self._lock.acquire(blocking=False):
            raise AssistantBusy("이전 메시지에 답하는 중이에요. 답이 온 뒤에 보내 주세요.")
        try:
            return self._turn(text)
        finally:
            self._lock.release()

    # One turn ----------------------------------------------------------------

    def _turn(self, text: str) -> dict:
        checkpoint = len(self.history)
        self.history.append({"role": "user", "text": text})
        self._log({"role": "user", "text": text})
        changes: list[str] = []
        try:
            reply = self._loop(changes)
        except Exception as e:  # the API failed: drop the half-finished turn, keep draft edits
            log.warning("chat turn failed: %s", e)
            del self.history[checkpoint:]
            message = describe_error(e)
            self._log({"role": "error", "text": message, "changes": changes})
            return {"ok": False, "reply": message, "changes": changes}
        self._log({"role": "assistant", "text": reply, "changes": changes})
        self._trim()
        return {"ok": True, "reply": reply, "changes": changes}

    def _loop(self, changes: list[str]) -> str:
        for _ in range(MAX_STEPS):
            reply = self.provider.complete(self._system(), self.history, TOOLS)
            self.history.append({"role": "assistant", "text": reply.text, "calls": reply.calls, "raw": reply.raw})
            if not reply.calls:
                return reply.text or "답을 만들지 못했어요. 다시 말씀해 주세요."
            results = []
            for call in reply.calls:
                if call.error:
                    output, is_error, change = _dumps({"error": call.error}), True, None
                else:
                    output, is_error, change = self._run_tool(call.name, call.arguments)
                if change:
                    changes.append(change)
                results.append({"call_id": call.id, "name": call.name, "output": output, "is_error": is_error})
            self.history.append({"role": "tool", "results": results})
        return "작업이 길어져서 여기서 멈췄어요. 지금까지 바뀐 초안을 확인해 주세요."

    def _run_tool(self, name: str, args: dict) -> tuple[str, bool, str | None]:
        name = known_tool_name(name)
        try:
            if name == "get_portfolio":
                return _dumps(self.host.draft_overview()), False, None
            if name == "set_allocation":
                allowed = ("sleeve", "symbol", "target", "label", "thesis", "invalidation", "instrument")
                change = self.host.set_allocation(**{k: v for k, v in args.items() if k in allowed})
                return _dumps({"ok": True, "change": change, "draft": self.host.draft_overview()}), False, change
            if name == "remove_allocation":
                change = self.host.remove_allocation(str(args.get("sleeve", "")), str(args.get("symbol", "")))
                return _dumps({"ok": True, "change": change, "draft": self.host.draft_overview()}), False, change
            if name == "preview_orders":
                return _dumps(self.host.preview_orders(bool(args.get("force_rebalance")))), False, None
            if name == "find_contracts":
                found = self.host.find_contracts(str(args.get("symbol", "")), str(args.get("exchange") or "LSE"))
                return _dumps({"candidates": found}), False, None
            return _dumps({"error": f"알 수 없는 도구예요: {name}"}), True, None
        except Exception as e:  # reported back to the model so it can correct itself
            message = getattr(e, "message", None) or str(e)
            return _dumps({"error": message}), True, None

    # Context and history -----------------------------------------------------

    def _system(self) -> str:
        r = self.rules
        return SYSTEM_PROMPT.format(
            max_idea=f"{r.max_satellite_idea_pct:g}",
            drift=f"{r.drift_threshold_pp:g}",
            min_value=r.min_value_for_rebalance,
            max_buys=r.max_buy_orders,
            max_order=r.max_order_value,
            currency=self.currency,
            fee_rule=self.fee_rule or "IBKR 요율",
            context=_dumps(self.host.assistant_context()),
        )

    def _log(self, entry: dict) -> None:
        self.transcript.append(entry)
        if len(self.transcript) > MAX_TRANSCRIPT:
            del self.transcript[: len(self.transcript) - MAX_TRANSCRIPT]

    def _trim(self) -> None:
        """Keep the conversation short by dropping whole old turns (starting at a user message)."""
        if len(self.history) <= MAX_HISTORY_TURNS:
            return
        for i in range(1, len(self.history)):
            if self.history[i]["role"] == "user" and len(self.history) - i <= MAX_HISTORY_TURNS:
                self.history = self.history[i:]
                return
