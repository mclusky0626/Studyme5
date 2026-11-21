"""백그라운드 조사 에이전트: 목표가 끝날 때까지 '생각 → 도구 → 결과 확인'을 반복한다.

채팅 답변(최대 몇 단계, 수 초)과 달리 수 분 동안 여러 번 검색·페이지 읽기를 하고
최종 보고서를 만든다. 단계·시간·토큰 한도를 넘으면 지금까지 모은 정보로 마무리한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from google.genai import types

from .gemini_resilience import call_with_retry
from .tools import ToolContext, declarations, run_tool

if TYPE_CHECKING:  # pragma: no cover
    from .chat import ChatResponder

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 백그라운드 작업 안에서는 쓰지 않는 도구 (작업이 작업을 만드는 재귀 방지 등)
AGENT_EXCLUDED_TOOLS = {
    "set_reminder", "list_reminders", "cancel_reminder",
    "start_background_task", "schedule_task", "list_tasks", "cancel_task",
}

AGENT_PROMPT = """너는 디스코드 봇 '{bot_name}'의 백그라운드 조사 담당이다. 사용자가 맡긴 작업을 혼자 끝까지 처리한다.

[작업]
{goal}

[진행 방법]
- 먼저 무엇을 찾아야 할지 짧게 계획하고, 도구로 정보를 모은다. 검색어를 바꿔 여러 번 찾아도 된다.
- 검색 요약이 부족하면 fetch_url 로 원문 페이지를 읽는다 (일부 사이트는 자동 접근을 막아 실패할 수 있다).
- 계산은 calculate, 날씨는 get_weather 처럼 맞는 도구를 쓴다.
- 최대 {max_steps}단계 안에 끝내야 한다. 충분히 모였으면 더 찾지 말고 바로 보고서를 쓴다.

[최종 보고서]
- 한국어, 디스코드 마크다운(제목·목록·굵게). 요청한 형식과 분량을 지킨다.
- 디스코드는 표(| 칸 |)를 지원하지 않는다. '표로 정리'를 요청받아도 항목별 목록(예: **제목** — 개봉일 · 소개)으로 쓴다.
- 확인한 사실과 추측을 구분하고, 끝까지 찾지 못한 부분은 "찾지 못함"이라고 솔직히 적는다. 원문 인용·대사를 지어내지 마라.
- 마지막 줄에 참고한 출처(사이트 이름 또는 URL)를 짧게 적는다.
- 말투는 아래 페르소나를 따르되, 보고서 내용이 우선이다.

[페르소나]
{persona}

현재 시각: {now}"""

FINALIZE = ("[시스템] 작업 한도({reason})에 도달했다. 더 이상 도구를 쓰지 말고, "
            "지금까지 모은 정보만으로 최종 보고서를 작성하라. 부족한 부분은 부족하다고 밝혀라.")


@dataclass
class AgentLimits:
    max_steps: int
    max_seconds: int
    max_tokens: int


@dataclass
class AgentResult:
    text: str
    steps: int = 0
    tokens: int = 0
    seconds: float = 0.0
    tools: list[str] = field(default_factory=list)
    stopped_by: str | None = None  # 한도에 걸려 마무리했으면 그 이유
    cancelled: bool = False


ProgressCb = Callable[[int, str], Awaitable[None]]
CancelCb = Callable[[], Awaitable[bool]]


def _usage_tokens(resp) -> int:
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return 0
    return sum(int(getattr(u, k, 0) or 0) for k in
               ("prompt_token_count", "candidates_token_count", "thoughts_token_count"))


def _describe(fc) -> str:
    args = dict(fc.args or {})
    hint = args.get("query") or args.get("url") or args.get("expression") or args.get("location") or ""
    return f"{fc.name}({str(hint)[:60]})" if hint else fc.name


async def run_agent(
    chat: "ChatResponder",
    goal: str,
    *,
    ctx: ToolContext,
    limits: AgentLimits,
    bot_name: str,
    persona: str,
    model: str,
    on_progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> AgentResult:
    started = time.monotonic()
    decls = [d for d in declarations(ctx) if d.name not in AGENT_EXCLUDED_TOOLS]
    system = AGENT_PROMPT.format(
        bot_name=bot_name, goal=goal, max_steps=limits.max_steps, persona=persona,
        now=datetime.now(KST).strftime("%Y-%m-%d (%a) %H:%M KST"),
    )
    base = types.GenerateContentConfig(
        system_instruction=system, temperature=0.4, max_output_tokens=chat.s.chat_max_output_tokens,
        thinking_config=types.ThinkingConfig(thinking_level="low"),
        tools=[types.Tool(function_declarations=decls)] if decls else None,
    )
    final_cfg = base.model_copy(update={"tool_config": types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(mode="NONE"))} if decls else {})
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=f"작업을 시작하라: {goal}")])]
    result = AgentResult(text="")

    async def generate(cfg):
        async def _call():
            return await chat.client.aio.models.generate_content(model=model, contents=contents, config=cfg)
        resp = await call_with_retry(_call, limiter=chat.limiter, max_retries=chat.s.gemini_max_retries,
                                     what="agent", base_delay=4.0, max_delay=60.0)
        result.tokens += _usage_tokens(resp)
        return resp

    stop_reason: str | None = None
    resp = None
    while True:
        if is_cancelled is not None and await is_cancelled():
            result.cancelled = True
            result.text = "작업이 취소되었습니다."
            break
        elapsed = time.monotonic() - started
        if result.steps >= limits.max_steps:
            stop_reason = f"{limits.max_steps}단계"
        elif elapsed >= limits.max_seconds:
            stop_reason = f"{limits.max_seconds}초"
        elif result.tokens >= limits.max_tokens:
            stop_reason = f"{limits.max_tokens:,}토큰"
        if stop_reason:
            contents.append(types.Content(role="user", parts=[
                types.Part.from_text(text=FINALIZE.format(reason=stop_reason))]))
            resp = await generate(final_cfg)
            result.stopped_by = stop_reason
            break

        resp = await generate(base)
        calls = resp.function_calls or []
        if not calls:
            break
        result.steps += 1
        contents.append(resp.candidates[0].content)
        desc = ", ".join(_describe(fc) for fc in calls)
        if on_progress is not None:
            try:
                await on_progress(result.steps, desc)
            except Exception:  # noqa: BLE001 - 진행 표시 실패는 무시
                pass
        outputs = await asyncio.gather(*(run_tool(fc.name, fc.args, ctx) for fc in calls))
        contents.append(types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(id=fc.id, name=fc.name, response=out))
            for fc, out in zip(calls, outputs, strict=True)
        ]))

    if not result.cancelled:
        result.text = ((getattr(resp, "text", None) or "").strip() if resp is not None else "") \
            or "작업을 끝냈지만 보고서를 만들지 못했어요. 다시 시도해 주세요."
    result.tools = list(ctx.used)
    result.seconds = time.monotonic() - started
    log.info("🧭 agent 완료: %d단계 %d토큰 %.0fs (한도: %s) 도구=%s", result.steps, result.tokens,
             result.seconds, result.stopped_by or "-", ",".join(result.tools))
    return result
