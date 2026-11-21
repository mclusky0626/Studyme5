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
from .backends import make_backend
from .tools import ToolContext, run_tool, schemas
from .usage import tracker

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

FOLLOWUP_PROMPT = """너는 디스코드 봇 '{bot_name}'이다. 방금 사용자에게 무언가를 하겠다고 말했지만 아직 하지 않았다.
지금 그 일을 실제로 끝내고, 사용자에게 바로 보낼 후속 메시지를 쓴다. 사용자가 다시 부르지 않아도 너가 먼저 이어서 말하는 상황이다.

[상황]
{goal}

[규칙]
- 필요한 도구(web_search, fetch_url, calculate 등)를 지금 호출해 정보를 모은 뒤 결과를 전한다. 다시 '찾아볼게' 같은 약속은 하지 마라.
- 디스코드 채팅 말투로, 결과 위주로 간결하게 (목록 사용 가능, 표는 쓰지 마라). 첫 문장을 '찾아왔어!'처럼 자연스럽게 시작해도 좋다.
- 직전 답변에서 이미 할 말을 다 했거나 실제로 할 일이 없으면, 다른 말 없이 정확히 NOOP 한 단어만 출력하라.
- 최대 {max_steps}단계 안에 끝낸다.

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
    mode: str = "report",
) -> AgentResult:
    started = time.monotonic()
    tools = [s for s in schemas(ctx) if s["name"] not in AGENT_EXCLUDED_TOOLS]
    system = (FOLLOWUP_PROMPT if mode == "followup" else AGENT_PROMPT).format(
        bot_name=bot_name, goal=goal, max_steps=limits.max_steps, persona=persona,
        now=datetime.now(KST).strftime("%Y-%m-%d (%a) %H:%M KST"),
    )
    # 제공자(CHAT_PROVIDER)에 맞는 어댑터. Gemini 는 작업용 모델·사고 수준을 따로 쓴다.
    # 지금 채팅에 쓰는 제공자·모델(/model 로 바뀌었을 수 있음)을 따른다.
    # 기본 Gemini 모델 그대로면 작업용 모델(TASK_MODEL)을 쓴다.
    cur = chat.backend
    use_model = model if (cur.provider == "gemini" and cur.model == chat.s.chat_model) else cur.model
    be = make_backend(chat.s, chat.limiter, provider=cur.provider, model=use_model, thinking_level="low")
    conv = be.start(system, f"작업을 시작하라: {goal}")
    result = AgentResult(text="")
    category = "chat" if mode == "followup" else "task"

    async def generate(allow_tools: bool):
        step = await be.step(conv, tools, allow_tools=allow_tools, temperature=0.4,
                             max_tokens=chat.s.chat_max_output_tokens, category=category)
        result.tokens += step.tokens
        return step

    stop_reason: str | None = None
    step = None
    try:
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
                be.add_user(conv, FINALIZE.format(reason=stop_reason))
                step = await generate(allow_tools=False)
                result.stopped_by = stop_reason
                break

            step = await generate(allow_tools=True)
            if not step.calls:
                break
            result.steps += 1
            be.add_model_turn(conv, step)
            desc = ", ".join(_describe(c) for c in step.calls)
            if on_progress is not None:
                try:
                    await on_progress(result.steps, desc)
                except Exception:  # noqa: BLE001 - 진행 표시 실패는 무시
                    pass
            outputs = await asyncio.gather(*(run_tool(c.name, c.args, ctx) for c in step.calls))
            be.add_tool_results(conv, list(zip(step.calls, outputs)))
    finally:
        await be.aclose()

    resp = step
    if not result.cancelled:
        result.text = (resp.text if resp is not None else "") \
            or "작업을 끝냈지만 보고서를 만들지 못했어요. 다시 시도해 주세요."
    result.tools = list(ctx.used)
    result.seconds = time.monotonic() - started
    log.info("🧭 agent 완료: %d단계 %d토큰 %.0fs (한도: %s) 도구=%s", result.steps, result.tokens,
             result.seconds, result.stopped_by or "-", ",".join(result.tools))
    return result
