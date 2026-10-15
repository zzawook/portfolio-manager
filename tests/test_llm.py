import json
from types import SimpleNamespace as NS

import pytest

from rebalancer.config import AssistantSettings
from rebalancer.llm import (
    AnthropicProvider,
    OpenAIChatProvider,
    OpenAIResponsesProvider,
    ProviderError,
    ToolCall,
    anthropic_messages,
    make_provider,
)

TOOLS = [{"name": "get_portfolio", "description": "d", "parameters": {"type": "object", "properties": {}}}]


def history_with_tool_turn(raw=None):
    return [
        {"role": "user", "text": "VWRA를 45%로"},
        {"role": "assistant", "text": "", "calls": [ToolCall("t1", "get_portfolio", {})], "raw": raw},
        {"role": "tool", "results": [{"call_id": "t1", "name": "get_portfolio", "output": "{}", "is_error": False}]},
    ]


# Anthropic -------------------------------------------------------------------------


def test_anthropic_history_puts_tool_results_first_and_merges_user_text():
    history = history_with_tool_turn() + [{"role": "user", "text": "계속해 줘"}]
    msgs = anthropic_messages(history)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    last = msgs[-1]["content"]
    assert last[0] == {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}
    assert last[1] == {"type": "text", "text": "계속해 줘"}
    assert msgs[1]["content"] == [{"type": "tool_use", "id": "t1", "name": "get_portfolio", "input": {}}]


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_anthropic_provider_reads_tool_calls_and_keeps_raw_blocks():
    blocks = [NS(type="thinking", thinking="..."), NS(type="tool_use", id="t1", name="get_portfolio", input={})]
    client = NS(messages=FakeMessages([NS(content=blocks, stop_reason="tool_use")]))
    provider = AnthropicProvider(client, "claude-sonnet-5-5", 2048)
    reply = provider.complete("system", [{"role": "user", "text": "안녕"}], TOOLS)
    assert [(c.id, c.name) for c in reply.calls] == [("t1", "get_portfolio")]
    assert reply.raw == blocks  # passed back unchanged, thinking included
    sent = client.messages.calls[0]
    assert sent["tools"][0]["input_schema"] == TOOLS[0]["parameters"]
    assert sent["system"] == "system" and sent["max_tokens"] == 2048


def test_anthropic_tool_call_cut_off_by_max_tokens_is_dropped():
    blocks = [NS(type="text", text="생각 중"), NS(type="tool_use", id="t1", name="get_portfolio", input={})]
    client = NS(messages=FakeMessages([NS(content=blocks, stop_reason="max_tokens")]))
    reply = AnthropicProvider(client, "m").complete("s", [{"role": "user", "text": "x"}], TOOLS)
    assert reply.calls == []
    assert all(getattr(b, "type", None) != "tool_use" for b in reply.raw)
    assert "끊겼어요" in reply.text


# OpenAI and compatible servers -------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def test_openai_responses_request_shape_and_round_trip():
    output = [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "get_portfolio", "arguments": "{}"},
    ]
    session = FakeSession(FakeResponse({"output": output, "status": "completed"}))
    provider = OpenAIResponsesProvider("sk-test", "gpt-6.1-sol", max_tokens=3000, session=session)
    reply = provider.complete("규칙", [{"role": "user", "text": "안녕"}], TOOLS)
    post = session.posts[0]
    assert post["url"] == "https://api.openai.com/v1/responses"
    assert post["headers"]["Authorization"] == "Bearer sk-test"
    body = post["json"]
    assert body["instructions"] == "규칙"
    assert body["input"] == [{"role": "user", "content": "안녕"}]
    assert body["tools"][0] == {"type": "function", "name": "get_portfolio", "description": "d", "parameters": TOOLS[0]["parameters"]}
    assert body["max_output_tokens"] == 3000
    assert [(c.id, c.name) for c in reply.calls] == [("call_1", "get_portfolio")]

    # The next request carries every output item back, then the tool output.
    history = [
        {"role": "user", "text": "안녕"},
        {"role": "assistant", "text": "", "calls": reply.calls, "raw": reply.raw},
        {"role": "tool", "results": [{"call_id": "call_1", "name": "get_portfolio", "output": "{}", "is_error": False}]},
    ]
    session.responses.append(FakeResponse({"output": [{"type": "message", "content": [{"type": "output_text", "text": "끝"}]}]}))
    final = provider.complete("규칙", history, TOOLS)
    items = session.posts[1]["json"]["input"]
    assert items[1:3] == output
    assert items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "{}"}
    assert final.text == "끝" and final.calls == []


def test_openai_compatible_chat_completions_shape():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_portfolio", "arguments": "{}"}}],
    }
    session = FakeSession(FakeResponse({"choices": [{"message": message, "finish_reason": "tool_calls"}]}))
    provider = OpenAIChatProvider("", "llama3.1", "http://localhost:11434/v1", 2048, session=session)
    reply = provider.complete("규칙", [{"role": "user", "text": "안녕"}], TOOLS)
    post = session.posts[0]
    assert post["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in post["headers"]  # local servers need no key
    body = post["json"]
    assert body["messages"][0] == {"role": "system", "content": "규칙"}
    assert body["tools"][0]["function"]["name"] == "get_portfolio"
    assert [(c.id, c.name) for c in reply.calls] == [("c1", "get_portfolio")]

    history = history_with_tool_turn(raw=message)
    session.responses.append(FakeResponse({"choices": [{"message": {"role": "assistant", "content": "끝"}}]}))
    provider.complete("규칙", history, TOOLS)
    msgs = session.posts[1]["json"]["messages"]
    assert msgs[2]["tool_calls"] == message["tool_calls"]
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": "{}"}


def test_invalid_tool_arguments_are_flagged_not_run():
    message = {"tool_calls": [{"id": "c1", "function": {"name": "set_allocation", "arguments": "{not json"}}]}
    session = FakeSession(FakeResponse({"choices": [{"message": message}]}))
    reply = OpenAIChatProvider("", "m", "http://x/v1", session=session).complete("s", [{"role": "user", "text": "x"}], TOOLS)
    assert reply.calls[0].error and reply.calls[0].arguments == {}


@pytest.mark.parametrize(
    "status, match",
    [(401, "OPENAI_API_KEY"), (404, "assistant.model"), (429, "한도"), (500, "HTTP 500")],
)
def test_http_errors_become_plain_messages(status, match):
    session = FakeSession(FakeResponse({"error": {"message": "boom"}}, status=status))
    provider = OpenAIResponsesProvider("sk", "m", session=session)
    with pytest.raises(ProviderError, match=match):
        provider.complete("s", [{"role": "user", "text": "x"}], TOOLS)


# Choosing a provider ----------------------------------------------------------------


def test_missing_key_turns_the_chat_off_with_a_reason():
    provider, reason = make_provider(AssistantSettings(provider="anthropic"), environ={})
    assert provider is None and "ANTHROPIC_API_KEY" in reason
    provider, reason = make_provider(AssistantSettings(provider="openai"), environ={})
    assert provider is None and "OPENAI_API_KEY" in reason


def test_openai_key_is_picked_up():
    provider, reason = make_provider(AssistantSettings(provider="openai"), environ={"OPENAI_API_KEY": "sk-x"})
    assert reason == "" and provider.label == "OpenAI" and provider.model


def test_compatible_server_needs_base_url_and_model_but_not_always_a_key():
    provider, reason = make_provider(AssistantSettings(provider="openai-compatible", model="llama3.1"), environ={})
    assert provider is None and "base_url" in reason
    provider, reason = make_provider(AssistantSettings(provider="openai-compatible", base_url="http://localhost:11434/v1"), environ={})
    assert provider is None and "model" in reason
    provider, reason = make_provider(
        AssistantSettings(provider="openai-compatible", base_url="http://localhost:11434/v1", model="llama3.1"), environ={}
    )
    assert reason == "" and provider.model == "llama3.1"
    provider, reason = make_provider(
        AssistantSettings(
            provider="openai-compatible",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-x",
            api_key_env="GEMINI_API_KEY",
        ),
        environ={},
    )
    assert provider is None and "GEMINI_API_KEY" in reason


def test_unknown_provider_is_reported():
    provider, reason = make_provider(AssistantSettings(provider="bard"), environ={})
    assert provider is None and "provider" in reason
