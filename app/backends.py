"""LLM 제공자 어댑터: Gemini / OpenAI 를 같은 방식으로 다룬다 (함수 호출 포함).

함수 호출 흐름은 두 회사가 같다:
  1) 도구(JSON 스키마)와 함께 요청 → 2) 모델이 "이 도구를 이 인자로 불러줘" 응답
  3) 우리가 도구를 실행해 결과를 대화에 붙여 다시 요청 → 4) 모델이 최종 답변
다른 건 메시지 모양뿐이라, 그 차이를 이 파일이 흡수한다.

  Gemini : tools=[Tool(function_declarations)] → response.function_calls → Part(function_response)
  OpenAI : tools=[{"type":"function",...}]     → message.tool_calls      → {"role":"tool","tool_call_id"}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import types

from .config import Settings
from .gemini_resilience import RateLimiter, call_with_retry
from .usage import tracker

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str | None
    name: str
    args: dict[str, Any]


@dataclass
class Step:
    text: str
    calls: list[ToolCall]
    raw: Any = None
    tokens: int = 0


@dataclass
class Conversation:
    system: str
    messages: list[Any] = field(default_factory=list)  # 제공자별 메시지 객체


class Backend:
    provider = "base"
    model: str

    def start(self, system: str, user_text: str) -> Conversation:
        raise NotImplementedError

    async def step(self, conv: Conversation, tools: list[dict], *, allow_tools: bool = True,
                   max_tokens: int = 8192, category: str = "chat", temperature: float | None = None) -> Step:
        raise NotImplementedError

    def add_model_turn(self, conv: Conversation, step: Step) -> None:
        raise NotImplementedError

    def add_tool_results(self, conv: Conversation, results: list[tuple[ToolCall, dict]]) -> None:
        raise NotImplementedError

    def add_user(self, conv: Conversation, text: str) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------------ Gemini
class GeminiBackend(Backend):
    provider = "gemini"

    def __init__(self, settings: Settings, limiter: RateLimiter, *, model: str | None = None,
                 thinking_level: str | None = None) -> None:
        self.s = settings
        self.limiter = limiter
        self.model = model or settings.chat_model
        self.client = genai.Client(api_key=settings.gemini_api_key or "missing-api-key")
        level = thinking_level if thinking_level is not None else settings.chat_thinking_level
        self.thinking = types.ThinkingConfig(thinking_level=level) if level else None

    def start(self, system: str, user_text: str) -> Conversation:
        return Conversation(system, [types.Content(role="user", parts=[types.Part.from_text(text=user_text)])])

    async def step(self, conv, tools, *, allow_tools=True, max_tokens=8192, category="chat", temperature=None):
        cfg = types.GenerateContentConfig(
            system_instruction=conv.system, max_output_tokens=max_tokens, thinking_config=self.thinking,
            temperature=self.s.chat_temperature if temperature is None else temperature,
        )
        if tools:
            cfg.tools = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["parameters"]) for t in tools])]
            if not allow_tools:
                cfg.tool_config = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))

        async def _call():
            return await self.client.aio.models.generate_content(model=self.model, contents=conv.messages, config=cfg)

        resp = await call_with_retry(_call, limiter=self.limiter, max_retries=min(3, self.s.gemini_max_retries),
                                     what=category, base_delay=2.0, max_delay=30.0)
        await tracker.record_response(category, self.model, resp)
        u = resp.usage_metadata
        tokens = sum(int(getattr(u, k, 0) or 0) for k in
                     ("prompt_token_count", "candidates_token_count", "thoughts_token_count")) if u else 0
        calls = [ToolCall(fc.id, fc.name, dict(fc.args or {})) for fc in (resp.function_calls or [])]
        # 도구 호출이 있으면 텍스트를 읽지 않는다 (SDK 경고 방지, 어차피 다음 턴에서 답함)
        text = "" if calls else (resp.text or "").strip()
        return Step(text=text, calls=calls, raw=resp, tokens=tokens)

    def add_model_turn(self, conv, step):
        # 사고 서명(thought signature)까지 그대로 보존해야 Gemini 3 가 다음 턴을 이어간다
        if step.raw is not None and step.raw.candidates:
            conv.messages.append(step.raw.candidates[0].content)

    def add_tool_results(self, conv, results):
        conv.messages.append(types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(id=c.id, name=c.name, response=r))
            for c, r in results]))

    def add_user(self, conv, text):
        conv.messages.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))


# ------------------------------------------------------------------ OpenAI (+ 호환 서버)
class OpenAIBackend(Backend):
    provider = "openai"

    def __init__(self, settings: Settings, limiter: RateLimiter, *, model: str | None = None) -> None:
        from openai import AsyncOpenAI

        self.s = settings
        self.limiter = limiter
        self.model = model or settings.openai_model
        self.client = AsyncOpenAI(api_key=settings.openai_api_key or "not-needed",
                                  base_url=settings.openai_base_url or None)
        # 공식 OpenAI 추론 모델에만 reasoning_effort 를 보낸다 (호환 서버는 보통 모름)
        self.reasoning = settings.openai_reasoning if not settings.openai_base_url else None

    def start(self, system: str, user_text: str) -> Conversation:
        # 시스템 → 사용자 순서. 앞부분(시스템·도구)이 같으면 OpenAI 가 자동 캐시(1,024토큰 이상)
        return Conversation(system, [{"role": "system", "content": system},
                                     {"role": "user", "content": user_text}])

    async def step(self, conv, tools, *, allow_tools=True, max_tokens=8192, category="chat", temperature=None):
        kwargs: dict[str, Any] = {"model": self.model, "messages": conv.messages,
                                  "max_completion_tokens": max_tokens}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
            kwargs["tool_choice"] = "auto" if allow_tools else "none"
        if self.reasoning:
            kwargs["reasoning_effort"] = self.reasoning

        async def _call():
            try:
                return await self.client.chat.completions.create(**kwargs)
            except Exception as exc:  # reasoning_effort 를 모르는 모델이면 빼고 다시
                if "reasoning_effort" in kwargs and "reasoning" in str(exc).lower():
                    kwargs.pop("reasoning_effort", None)
                    return await self.client.chat.completions.create(**kwargs)
                raise

        resp = await call_with_retry(_call, limiter=self.limiter, max_retries=min(3, self.s.gemini_max_retries),
                                     what=category, base_delay=2.0, max_delay=30.0)
        usage = getattr(resp, "usage", None)
        cached = 0
        tokens = 0
        if usage is not None:
            details = getattr(usage, "prompt_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
            await tracker.record(category, self.model, input=int(usage.prompt_tokens or 0),
                                 output=int(usage.completion_tokens or 0), cached=cached)
            tokens = int(usage.total_tokens or 0)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args if isinstance(args, dict) else {}))
        return Step(text=(msg.content or "").strip(), calls=calls, raw=msg, tokens=tokens)

    def add_model_turn(self, conv, step):
        m = step.raw
        entry: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
        if m.tool_calls:
            entry["tool_calls"] = [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                   for tc in m.tool_calls]
        conv.messages.append(entry)

    def add_tool_results(self, conv, results):
        for c, r in results:
            conv.messages.append({"role": "tool", "tool_call_id": c.id,
                                  "content": json.dumps(r, ensure_ascii=False, default=str)})

    def add_user(self, conv, text):
        conv.messages.append({"role": "user", "content": text})

    async def aclose(self) -> None:
        await self.client.close()


def make_backend(settings: Settings, limiter: RateLimiter, *, provider: str | None = None,
                 model: str | None = None, thinking_level: str | None = None) -> Backend:
    provider = provider or settings.chat_provider
    if provider == "openai":
        return OpenAIBackend(settings, limiter, model=model)
    return GeminiBackend(settings, limiter, model=model, thinking_level=thinking_level)
