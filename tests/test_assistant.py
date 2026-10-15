import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from rebalancer.assistant import MAX_STEPS, TOOLS, Assistant, AssistantBusy
from rebalancer.llm import ProviderError, Reply, ToolCall
from rebalancer.server import App

ROOT = Path(__file__).resolve().parents[1]


class ScriptedProvider:
    """Plays back replies; each item is a Reply or a function(history) -> Reply."""

    name = "scripted"
    label = "Test"
    model = "test-model"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen = []

    def complete(self, system, history, tools):
        self.seen.append({"system": system, "history": [dict(t) for t in history], "tools": tools})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(history) if callable(step) else step


def app(tmp_path, provider=None):
    for name in ("config.yaml", "portfolio.yaml"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    return App(tmp_path / "config.yaml", mock_file=str(tmp_path / "examples" / "month14.yaml"), today=date(2028, 2, 3), provider=provider)


def test_tools_never_include_an_order_tool():
    names = {t["name"] for t in TOOLS}
    assert names == {"get_portfolio", "set_allocation", "remove_allocation", "preview_orders", "find_contracts"}


def test_chat_edits_the_draft_and_reports_changes(tmp_path):
    provider = ScriptedProvider(
        Reply(
            "",
            [
                ToolCall("a", "set_allocation", {"sleeve": "satellite", "symbol": "WDGF", "target": 5}),
                ToolCall("b", "set_allocation", {"sleeve": "core", "symbol": "VWRA", "target": 45}),
            ],
        ),
        Reply("WDGF 5%, VWRA 45%로 바꿨어요."),
    )
    a = app(tmp_path, provider)
    result = a.chat("방산을 5%로 줄이고 VWRA에 넣어 줘")
    assert result["ok"] and result["reply"] == "WDGF 5%, VWRA 45%로 바꿨어요."
    assert result["changes"] == ["WDGF 위성 5% (기존 10%)", "VWRA 코어 45% (기존 40%)"]
    assert result["draft"]["dirty"] and result["draft"]["problems"] == []
    # The draft changed, the saved file and the account did not.
    assert "target: 40" in (tmp_path / "portfolio.yaml").read_text(encoding="utf-8")
    assert a.mock.submitted == []
    # The model saw the tool results right after its calls.
    tool_turn = provider.seen[1]["history"][-1]
    assert tool_turn["role"] == "tool" and [r["call_id"] for r in tool_turn["results"]] == ["a", "b"]
    assert all(r["is_error"] is False for r in tool_turn["results"])


def test_tool_errors_go_back_to_the_model(tmp_path):
    provider = ScriptedProvider(
        Reply("", [ToolCall("a", "set_allocation", {"sleeve": "satellite", "symbol": "NEWX", "target": 5})]),
        Reply("새 종목 정보가 필요해요."),
    )
    a = app(tmp_path, provider)
    a.chat("NEWX 5% 넣어 줘")
    result = provider.seen[1]["history"][-1]["results"][0]
    assert result["is_error"] is True
    assert "instrument" in json.loads(result["output"])["error"]


def test_preview_is_read_only(tmp_path):
    provider = ScriptedProvider(Reply("", [ToolCall("a", "preview_orders", {})]), Reply("계획을 보여 드렸어요."))
    a = app(tmp_path, provider)
    a.chat("이번 달에 뭘 사?")
    output = json.loads(provider.seen[1]["history"][-1]["results"][0]["output"])
    assert output["orders"] and "주문은 사용자가" in output["note"]
    assert a.mock.submitted == [] and a.plans == {}  # nothing a button could send


def test_system_prompt_carries_rules_and_fees(tmp_path):
    provider = ScriptedProvider(Reply("네."))
    a = app(tmp_path, provider)
    a.chat("안녕")
    system = provider.seen[0]["system"]
    assert "아이디어 상한 15%" in system and "주문은 보낼 수 없어요" in system and "GST" in system


def test_failed_call_rolls_back_the_turn_but_keeps_draft_edits(tmp_path):
    provider = ScriptedProvider(
        Reply("", [ToolCall("a", "set_allocation", {"sleeve": "core", "symbol": "VWRA", "target": 45})]),
        ProviderError("요청 한도에 걸렸어요."),
    )
    a = app(tmp_path, provider)
    result = a.chat("VWRA 45%")
    assert result["ok"] is False and result["reply"] == "요청 한도에 걸렸어요."
    assert result["changes"] == ["VWRA 코어 45% (기존 40%)"]
    assert a.assistant.history == []  # the next turn starts clean
    assert a.assistant.transcript[-1]["role"] == "error"


def test_runaway_tool_loops_stop(tmp_path):
    loop = Reply("", [ToolCall("a", "get_portfolio", {})])
    provider = ScriptedProvider(*([loop] * MAX_STEPS))
    a = app(tmp_path, provider)
    result = a.chat("계속 확인해 줘")
    assert "멈췄어요" in result["reply"]
    # The next message still forms a valid conversation after the dangling tool results.
    provider.steps.append(Reply("네."))
    assert a.chat("이제 됐어")["ok"]


def test_one_message_at_a_time(tmp_path):
    a = app(tmp_path, ScriptedProvider(Reply("네.")))
    a.assistant._lock.acquire()
    try:
        with pytest.raises(AssistantBusy):
            a.assistant.send("두 번째")
    finally:
        a.assistant._lock.release()


def test_assistant_without_provider_is_off(tmp_path):
    a = app(tmp_path)
    assert a.assistant is None and a.chat_view()["enabled"] is False


def test_long_messages_are_refused():
    assistant = Assistant(ScriptedProvider(), host=None, rules=None)
    with pytest.raises(ValueError):
        assistant.send("가" * 5000)
