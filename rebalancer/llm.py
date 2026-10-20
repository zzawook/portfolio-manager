"""Chat model providers behind one small interface.

The assistant keeps the conversation in a neutral form, and each provider turns it
into its own API format and back:

- anthropic          Claude Messages API (anthropic SDK)
- openai             OpenAI Responses API (tool calls on the newest OpenAI models need it)
- chatgpt            the same Responses API, paid by the user's ChatGPT Plus or Pro plan
                     through Sign in with ChatGPT (store=false, streaming, namespaced tools)
- openai-compatible  Chat Completions API at any base_url: Gemini, OpenRouter, Ollama and others

Neutral conversation turns:
    {"role": "user", "text": str}
    {"role": "assistant", "text": str, "calls": [ToolCall], "raw": provider data or None}
    {"role": "tool", "results": [{"call_id", "name", "output", "is_error"}]}

Tools are {"name", "description", "parameters" (JSON schema)}.

API keys are read from environment variables and only ever sent to the provider.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

import requests

from .chatgpt_auth import RESOURCE as CHATGPT_RESOURCE
from .chatgpt_auth import ChatGPTAuth, ChatGPTAuthError
from .config import PROVIDERS, AssistantSettings

OPENAI_BASE_URL = "https://api.openai.com/v1"
REQUEST_TIMEOUT = 120


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    error: str = ""  # set when the model sent arguments that are not valid JSON


@dataclass
class Reply:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # provider-specific; passed back unchanged on the next request


class ProviderError(RuntimeError):
    """A provider call failed. The message is written for the person using the app."""


class ChatProvider(Protocol):
    name: str
    label: str
    model: str

    def complete(self, system: str, history: list[dict], tools: list[dict]) -> Reply: ...


def _tool_call(call_id: Any, name: Any, arguments: Any) -> ToolCall:
    call_id, name = str(call_id or ""), str(name or "")
    if isinstance(arguments, dict):
        return ToolCall(call_id, name, arguments)
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return ToolCall(call_id, name, {}, error="도구 인자가 올바른 JSON 객체가 아니에요. 다시 보내 주세요.")
    return ToolCall(call_id, name, parsed)


# Anthropic ----------------------------------------------------------------------


def anthropic_messages(history: list[dict]) -> list[dict]:
    out: list[dict] = []

    def push_user(blocks: list[dict]) -> None:
        if out and out[-1]["role"] == "user":
            previous = out[-1]["content"]
            if isinstance(previous, str):
                previous = [{"type": "text", "text": previous}]
            out[-1]["content"] = list(previous) + blocks  # tool results stay first, then text
        else:
            out.append({"role": "user", "content": blocks})

    for turn in history:
        if turn["role"] == "user":
            push_user([{"type": "text", "text": turn["text"]}])
        elif turn["role"] == "assistant":
            content = turn.get("raw")
            if content is None:
                content = [{"type": "text", "text": turn["text"]}] if turn["text"] else []
                content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in turn["calls"]]
            out.append({"role": "assistant", "content": content})
        elif turn["role"] == "tool":
            blocks = []
            for r in turn["results"]:
                block = {"type": "tool_result", "tool_use_id": r["call_id"], "content": r["output"]}
                if r.get("is_error"):
                    block["is_error"] = True
                blocks.append(block)
            push_user(blocks)
    return out


class AnthropicProvider:
    name = "anthropic"
    label = "Anthropic"

    def __init__(self, client: Any, model: str, max_tokens: int = 4096):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def complete(self, system: str, history: list[dict], tools: list[dict]) -> Reply:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools],
            messages=anthropic_messages(history),
        )
        content = list(response.content)
        if response.stop_reason == "tool_use":
            calls = [
                ToolCall(b.id, b.name, dict(b.input or {})) for b in content if getattr(b, "type", None) == "tool_use"
            ]
        else:
            calls = []
            # A tool call cut off by max_tokens has no result to pair with, so it is dropped.
            content = [b for b in content if getattr(b, "type", None) != "tool_use"]
        text = "\n".join(
            b.text for b in content if getattr(b, "type", None) == "text" and getattr(b, "text", "")
        ).strip()
        if response.stop_reason == "max_tokens" and not calls:
            text = (text + "\n\n" if text else "") + "답이 길어서 중간에 끊겼어요."
        if not content:
            content = [{"type": "text", "text": text or "(빈 답)"}]
        return Reply(text, calls, raw=content)


# OpenAI and OpenAI-compatible -------------------------------------------------------


class _HttpProvider:
    name = ""
    label = ""

    def __init__(self, api_key: str, model: str, base_url: str, max_tokens: int, key_env: str, session=None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.key_env = key_env
        self.session = session or requests.Session()

    def _post(self, path: str, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = self.session.post(self.base_url + path, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise ProviderError(f"{self.label}에 연결할 수 없어요. 인터넷 연결과 base_url을 확인하세요. ({type(e).__name__})") from e
        if resp.status_code in (401, 403):
            where = f" {self.key_env}를 확인하세요." if self.key_env else ""
            raise ProviderError(f"API 키가 거부됐어요 ({self.label}).{where}")
        if resp.status_code == 404:
            raise ProviderError("모델이나 주소를 찾을 수 없어요. config.yaml의 assistant.model과 base_url을 확인하세요.")
        if resp.status_code == 429:
            raise ProviderError("요청 한도에 걸렸어요. 크레딧이나 요청 한도를 확인하고 잠시 뒤에 다시 보내 주세요.")
        if resp.status_code >= 400:
            detail = ""
            try:
                err = resp.json().get("error")
                detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
            except ValueError:
                detail = resp.text
            raise ProviderError(f"{self.label} API 오류예요 (HTTP {resp.status_code}). {detail[:200]}".strip())
        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderError(f"{self.label}에서 JSON이 아닌 응답이 왔어요.") from e
        if not isinstance(data, dict):
            raise ProviderError(f"{self.label}에서 알 수 없는 응답이 왔어요.")
        return data


def responses_input(history: list[dict]) -> list[dict]:
    items: list[dict] = []
    for turn in history:
        if turn["role"] == "user":
            items.append({"role": "user", "content": turn["text"]})
        elif turn["role"] == "assistant":
            if turn.get("raw") is not None:
                items.extend(turn["raw"])  # every output item goes back as it came
                continue
            if turn["text"]:
                items.append({"role": "assistant", "content": turn["text"]})
            for c in turn["calls"]:
                items.append(
                    {"type": "function_call", "call_id": c.id, "name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}
                )
        elif turn["role"] == "tool":
            for r in turn["results"]:
                items.append({"type": "function_call_output", "call_id": r["call_id"], "output": r["output"]})
    return items


class OpenAIResponsesProvider(_HttpProvider):
    name = "openai"
    label = "OpenAI"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "",
        max_tokens: int = 4096,
        reasoning_effort: str = "",
        key_env: str = "OPENAI_API_KEY",
        session=None,
    ):
        super().__init__(api_key, model, base_url or OPENAI_BASE_URL, max_tokens, key_env, session)
        self.reasoning_effort = reasoning_effort

    def complete(self, system: str, history: list[dict], tools: list[dict]) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": responses_input(history),
            "tools": [
                {"type": "function", "name": t["name"], "description": t["description"], "parameters": t["parameters"]}
                for t in tools
            ],
            "max_output_tokens": self.max_tokens,
        }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        data = self._post("/responses", body)
        output = data.get("output") or []
        calls: list[ToolCall] = []
        texts: list[str] = []
        for item in output:
            kind = item.get("type")
            if kind == "function_call":
                calls.append(_tool_call(item.get("call_id") or item.get("id"), item.get("name"), item.get("arguments")))
            elif kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text" and part.get("text"):
                        texts.append(part["text"])
                    elif part.get("type") == "refusal" and part.get("refusal"):
                        texts.append(part["refusal"])
        text = "\n".join(texts).strip()
        if data.get("status") == "incomplete" and not calls:
            reason = (data.get("incomplete_details") or {}).get("reason", "")
            note = "답이 중간에 끊겼어요."
            if reason == "max_output_tokens":
                note += " config.yaml의 assistant.max_tokens를 늘려 보세요."
            text = (text + "\n\n" if text else "") + note
        return Reply(text, calls, raw=output)


def chat_messages(history: list[dict]) -> list[dict]:
    messages: list[dict] = []
    for turn in history:
        if turn["role"] == "user":
            messages.append({"role": "user", "content": turn["text"]})
        elif turn["role"] == "assistant":
            raw = turn.get("raw")
            if raw is not None:
                message = {"role": "assistant", "content": raw.get("content") or ""}
                if raw.get("tool_calls"):
                    message["tool_calls"] = raw["tool_calls"]  # kept whole, including provider extras
            else:
                message = {"role": "assistant", "content": turn["text"] or ""}
                if turn["calls"]:
                    message["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                        }
                        for c in turn["calls"]
                    ]
            messages.append(message)
        elif turn["role"] == "tool":
            for r in turn["results"]:
                messages.append({"role": "tool", "tool_call_id": r["call_id"], "content": r["output"]})
    return messages


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")).strip()
    return ""


class OpenAIChatProvider(_HttpProvider):
    name = "openai-compatible"

    def __init__(self, api_key: str, model: str, base_url: str, max_tokens: int = 4096, key_env: str = "", session=None):
        super().__init__(api_key, model, base_url, max_tokens, key_env, session)
        self.label = urlparse(base_url).hostname or "OpenAI 호환 서버"

    def complete(self, system: str, history: list[dict], tools: list[dict]) -> Reply:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + chat_messages(history),
            "tools": [
                {
                    "type": "function",
                    "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
                }
                for t in tools
            ],
            "max_tokens": self.max_tokens,
        }
        data = self._post("/chat/completions", body)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.label}에서 빈 응답이 왔어요.")
        message = choices[0].get("message") or {}
        calls = [
            _tool_call(tc.get("id"), (tc.get("function") or {}).get("name"), (tc.get("function") or {}).get("arguments"))
            for tc in message.get("tool_calls") or []
        ]
        text = _content_text(message.get("content"))
        if choices[0].get("finish_reason") == "length" and not calls:
            text = (text + "\n\n" if text else "") + "답이 중간에 끊겼어요. config.yaml의 assistant.max_tokens를 늘려 보세요."
        return Reply(text, calls, raw=message)


# ChatGPT plan (Sign in with ChatGPT) ---------------------------------------------------

TOOL_NAMESPACE = "portfolio"

PLAN_ERRORS = {
    "subscription_sharing_usage_limit_exceeded": (
        "ChatGPT 플랜 사용 한도에 걸렸어요. ChatGPT 설정의 사용량(Usage) 화면에서 이 앱의 한도를 확인하세요."
    ),
    "subscription_sharing_usage_unavailable": "ChatGPT가 지금 플랜 사용량을 확인하지 못했어요. 잠시 뒤 다시 보내 주세요.",
}


def _error_info(resp) -> tuple[str, str]:
    """(code, message) from an error response body, or ("", raw text)."""
    try:
        payload = resp.json()
    except ValueError:
        return "", str(getattr(resp, "text", "") or "")
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or ""), str(err.get("message") or "")
    return "", str(err or "")


def _replayable(output: list) -> list[dict]:
    """Output items that can go back as input with store=false.

    Server-side ids are dropped (nothing is stored to point at), and reasoning items are
    left out because, without stored state, they cannot be referenced again."""
    keep: list[dict] = []
    for item in output or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call":
            keep.append({k: item[k] for k in ("type", "call_id", "name", "arguments", "namespace") if k in item})
        elif kind == "message":
            parts = [
                {"type": "output_text", "text": p.get("text", "")}
                for p in item.get("content") or []
                if isinstance(p, dict) and p.get("type") == "output_text" and p.get("text")
            ]
            if parts:
                keep.append({"type": "message", "role": "assistant", "content": parts})
    return keep


def plan_input(history: list[dict]) -> list[dict]:
    items: list[dict] = []
    for turn in history:
        if turn["role"] == "user":
            items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": turn["text"]}]})
        elif turn["role"] == "assistant":
            if turn.get("raw") is not None:
                items.extend(turn["raw"])
                continue
            if turn["text"]:
                items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": turn["text"]}]})
            for c in turn["calls"]:
                items.append(
                    {"type": "function_call", "call_id": c.id, "name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}
                )
        elif turn["role"] == "tool":
            for r in turn["results"]:
                items.append({"type": "function_call_output", "call_id": r["call_id"], "output": r["output"]})
    return items


def _sse_events(resp) -> Any:
    """Parse a server-sent event stream into JSON objects."""
    data_lines: list[str] = []
    for raw in resp.iter_lines(decode_unicode=True):
        line = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                if payload.strip() == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except ValueError:
                    continue
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        try:
            yield json.loads("\n".join(data_lines))
        except ValueError:
            return


class ChatGPTPlanProvider:
    name = "chatgpt"
    label = "ChatGPT"

    def __init__(self, auth: ChatGPTAuth, model: str, reasoning_effort: str = "", session=None):
        self.auth = auth
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.session = session or requests.Session()

    @property
    def ready(self) -> bool:
        return self.auth.plan_ready

    def status(self) -> dict:
        return self.auth.status()

    def complete(self, system: str, history: list[dict], tools: list[dict]) -> Reply:
        try:
            token = self.auth.access_token()
        except ChatGPTAuthError as e:
            raise ProviderError(str(e)) from e
        body: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": plan_input(history),
            # The plan route takes function tools grouped in a namespace (which needs a description).
            "tools": [
                {
                    "type": "namespace",
                    "name": TOOL_NAMESPACE,
                    "description": "포트폴리오 초안을 읽고 고치는 도구예요. 주문은 보낼 수 없어요.",
                    "tools": [
                        {"type": "function", "name": t["name"], "description": t["description"], "parameters": t["parameters"]}
                        for t in tools
                    ],
                }
            ],
            "store": False,
            "stream": True,
        }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream"}
        try:
            resp = self.session.post(
                CHATGPT_RESOURCE + "/responses", json=body, headers=headers, stream=True, timeout=(15, 300)
            )
        except requests.RequestException as e:
            raise ProviderError(f"ChatGPT에 연결할 수 없어요. 인터넷 연결을 확인하세요. ({type(e).__name__})") from e
        try:
            if resp.status_code >= 400:
                code, detail = _error_info(resp)
                if resp.status_code == 401:
                    # The next request refreshes the token. If that fails, the sign-in button comes back.
                    self.auth.invalidate_access()
                    raise ProviderError("ChatGPT가 로그인 토큰을 받지 않았어요. 한 번 더 보내 보세요. 계속 안 되면 다시 로그인해 주세요.")
                if code in PLAN_ERRORS:
                    raise ProviderError(PLAN_ERRORS[code])
                if resp.status_code == 403:
                    raise ProviderError(
                        "ChatGPT가 요청을 거절했어요 (권한이나 지역 정책). Plus나 Pro 플랜 계정으로 로그인했는지 확인하세요."
                    )
                if resp.status_code == 429:
                    raise ProviderError(PLAN_ERRORS["subscription_sharing_usage_limit_exceeded"])
                raise ProviderError(f"ChatGPT 요청이 실패했어요 (HTTP {resp.status_code}). {detail[:200]}".strip())
            final = None
            streamed: list[dict] = []  # some models send the output only as items, not in the final event
            for event in _sse_events(resp):
                kind = event.get("type")
                if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
                    streamed.append(event["item"])
                elif kind in ("response.completed", "response.incomplete"):
                    final = event.get("response") or {}
                elif kind == "response.failed":
                    error = (event.get("response") or {}).get("error") or {}
                    code = error.get("code", "")
                    raise ProviderError(PLAN_ERRORS.get(code) or f"ChatGPT 답이 실패했어요 ({code or error.get('message', '')}).")
                elif kind == "error":
                    code = event.get("code") or (event.get("error") or {}).get("code", "")
                    raise ProviderError(PLAN_ERRORS.get(code) or f"ChatGPT 답이 실패했어요 ({code or event.get('message', '')}).")
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
        if final is None:
            raise ProviderError("ChatGPT 답이 중간에 끊겼어요. 다시 보내 주세요.")
        output = final.get("output") or streamed
        calls: list[ToolCall] = []
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                calls.append(_tool_call(item.get("call_id") or item.get("id"), item.get("name"), item.get("arguments")))
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "refusal"):
                        texts.append(part.get("text") or part.get("refusal") or "")
        text = "\n".join(t for t in texts if t).strip()
        if final.get("status") == "incomplete" and not calls:
            text = (text + "\n\n" if text else "") + "답이 중간에 끊겼어요."
        return Reply(text, calls, raw=_replayable(output))


# Choosing a provider --------------------------------------------------------------


def settings_problem(settings: AssistantSettings) -> str:
    if settings.provider not in PROVIDERS:
        return f"assistant.provider는 {', '.join(PROVIDERS)} 중 하나여야 해요."
    if not settings.resolved_model:
        return "assistant.model을 적어 주세요. openai-compatible은 기본 모델이 없어요."
    if settings.provider == "openai-compatible" and not settings.base_url:
        return "openai-compatible을 쓰려면 assistant.base_url에 서버 주소를 적어 주세요."
    if settings.reasoning_effort and settings.provider not in ("openai", "chatgpt"):
        return "assistant.reasoning_effort는 provider가 openai나 chatgpt일 때만 쓸 수 있어요."
    if settings.max_tokens < 256:
        return "assistant.max_tokens는 256 이상이어야 해요."
    return ""


def make_provider(
    settings: AssistantSettings, environ: Optional[dict] = None, auth: Optional[ChatGPTAuth] = None
) -> tuple[Optional[ChatProvider], str]:
    """The configured chat provider, or None with a reason the chat stays off.

    The chatgpt provider is returned even before sign-in; its `ready` says whether it can answer."""
    env = os.environ if environ is None else environ
    problem = settings_problem(settings)
    if problem:
        return None, problem
    if settings.provider == "chatgpt":
        if auth is None:
            return None, "ChatGPT 로그인 정보를 둘 곳이 없어요."
        return ChatGPTPlanProvider(auth, settings.resolved_model, settings.reasoning_effort), ""
    key_env = settings.key_env
    key = env.get(key_env, "") if key_env else ""
    model = settings.resolved_model

    if settings.provider == "anthropic":
        if not key:
            return None, (
                f"채팅을 쓰려면 {key_env} 환경 변수에 Anthropic API 키를 넣고 다시 실행하세요. "
                "OpenAI 키를 쓰려면 config.yaml의 assistant.provider를 openai로 바꾸세요."
            )
        try:
            import anthropic
        except ImportError:
            return None, "anthropic 패키지가 없어요. pip install -e . 로 다시 설치하세요."
        return AnthropicProvider(anthropic.Anthropic(api_key=key), model, settings.max_tokens), ""

    if settings.provider == "openai":
        if not key:
            return None, f"채팅을 쓰려면 {key_env} 환경 변수에 OpenAI API 키를 넣고 다시 실행하세요."
        return (
            OpenAIResponsesProvider(
                key, model, settings.base_url, settings.max_tokens, settings.reasoning_effort, key_env=key_env
            ),
            "",
        )

    # openai-compatible: a key is optional (a local server such as Ollama needs none).
    if settings.api_key_env and not key:
        return None, f"채팅을 쓰려면 {settings.api_key_env} 환경 변수에 API 키를 넣고 다시 실행하세요."
    return OpenAIChatProvider(key, model, settings.base_url, settings.max_tokens, key_env=settings.api_key_env), ""


def describe_error(e: Exception) -> str:
    """A message for the chat panel when a provider call fails."""
    if isinstance(e, ProviderError):
        return str(e)
    try:
        import anthropic
    except ImportError:  # pragma: no cover - anthropic is a dependency
        anthropic = None
    if anthropic is not None:
        if isinstance(e, anthropic.AuthenticationError):
            return "Anthropic이 API 키를 거부했어요. 환경 변수에 넣은 키를 확인하세요."
        if isinstance(e, anthropic.PermissionDeniedError):
            return "이 API 키로는 이 모델을 쓸 수 없어요. 키 권한이나 config.yaml의 assistant.model을 확인하세요."
        if isinstance(e, anthropic.NotFoundError):
            return "모델을 찾을 수 없어요. config.yaml의 assistant.model을 확인하세요."
        if isinstance(e, anthropic.RateLimitError):
            return "요청 한도에 걸렸어요. 잠시 뒤에 다시 보내 주세요."
        if isinstance(e, anthropic.APIConnectionError):
            return "Anthropic API에 연결할 수 없어요. 인터넷 연결을 확인하세요."
        if isinstance(e, anthropic.APIStatusError):
            return f"Anthropic API 오류예요 (HTTP {e.status_code}). 잠시 뒤에 다시 보내 주세요."
    return f"채팅 중 오류가 났어요: {e}"
