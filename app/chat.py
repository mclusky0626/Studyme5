"""답변 생성: 단기 기억 + 장기 기억 + (필요 시) 함수 호출 도구 → Gemini."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from google import genai
from google.genai import types

from .config import Settings
from .gemini_resilience import RateLimiter, call_with_retry
from .memory import MemoryContext
from .persona import DEFAULT_PERSONA
from .tools import ToolContext, declarations, run_tool
from .usage import tracker

log = logging.getLogger(__name__)

# "잠시만요, 찾아볼게요" 처럼 나중을 약속하고 끝내는 답변 감지 (봇은 한 번 답하면 후속 메시지를 못 보냄)
PROMISE_RE = re.compile(
    r"(잠시만|잠깐만|기다려\s*주|기다려줘|찾아\s*(볼|보고|올)|검색해\s*(볼|보고|올)|알아\s*(볼|보고|올)|"
    r"확인해\s*(볼|보고|올)|조사해\s*(볼|보고|올)|정리해\s*서?\s*(보여|알려|드릴|줄)|준비해\s*서?\s*(보여|드릴)|"
    r"곧\s*(보여|알려|정리)|금방\s*(보여|알려|정리|가져))"
)
DEFERRING_TOOLS = {"start_background_task", "schedule_task", "set_reminder"}  # 실제로 나중 처리가 예약된 경우
NUDGE = (
    "[시스템] 방금 답변은 '잠시만요/찾아볼게요/정리해서 보여드릴게요' 같은 약속으로 끝났다. "
    "너는 이 답변 이후에 메시지를 보낼 수 없다. 정보가 더 필요하면 지금 도구를 호출하고, "
    "아니면 지금 가진 정보로 요청된 내용을 이 답변 안에서 끝까지 완성하라. 약속하는 문장은 쓰지 마라."
)

TOOL_GUIDE = """[도구 사용]
- 필요할 때만 함수를 호출하고, 결과를 바탕으로 자연스럽게 답한다. 도구 이름이나 JSON 을 그대로 보여 주지 마라.
- 한 번의 검색으로 부족하면 검색어를 바꿔 여러 번 호출해도 된다. 정리·예문·비교처럼 자세한 자료가 필요하면 web_search 의 detail="detailed".
- 숫자 계산은 암산하지 말고 calculate. 날씨는 get_weather(영문 도시명). 환율은 convert_currency.
- 뉴스·시세·경기 결과·영업시간처럼 최신 정보가 필요하거나 확실하지 않은 사실은 web_search 로 확인한다.
- "~분 뒤/~시에 알려줘" 는 set_reminder. 고르기·주사위·제비뽑기는 random_pick. D-day·요일은 date_calc.
- 검색 결과를 쓸 때는 끝에 (출처: 사이트명) 처럼 짧게 밝힌다.
- 검색·페이지 읽기를 여러 번 해야 하는 깊은 조사나 "조사해서 나중에 알려줘"는 start_background_task 로 맡기고,
  "작업 #id 로 시작했다, 끝나면 여기에 올리겠다"고 짧게 답한다. 이 경우만 나중에 결과가 자동으로 올라간다.
- "매일/매주/특정 시각에 ~해줘"(그때 조사·요약이 필요한 일)는 schedule_task. 단순 알림 문구는 set_reminder."""


@dataclass
class ShortLine:
    speaker: str
    text: str
    is_bot: bool = False


class ChatResponder:
    def __init__(self, settings: Settings, limiter: RateLimiter) -> None:
        self.s = settings
        self.limiter = limiter
        self.client = genai.Client(api_key=settings.gemini_api_key or "missing-api-key")
        self.http = httpx.AsyncClient(timeout=12.0, headers={"User-Agent": "discord-memory-bot/1.0"})
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrent_chats))
        self._tz = ZoneInfo("Asia/Seoul")
        self._thinking = (
            types.ThinkingConfig(thinking_level=settings.chat_thinking_level)
            if settings.chat_thinking_level
            else None
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def tool_context(self, **kwargs: Any) -> ToolContext:
        """메시지별 도구 컨텍스트 (공용 서비스 + 채널/사용자 정보)."""
        return ToolContext(settings=self.s, genai_client=self.client, limiter=self.limiter,
                           http=self.http, **kwargs)

    def build_system_prompt(self, *, bot_name: str, place: str, memory: MemoryContext,
                            persona: str | None = None, with_tools: bool = False) -> str:
        now = datetime.now(self._tz).strftime("%Y-%m-%d (%a) %H:%M KST")
        parts = [
            f"너는 '{place}'에서 활동하는 AI '{bot_name}'이다.",
            "",
            "[페르소나 — 말투와 성격은 이것을 따른다]",
            persona or DEFAULT_PERSONA,
            "",
            "규칙:",
            "- [장기 기억]은 과거 대화에서 추출한 사실이다. 지금 대화와 관련 있을 때만 자연스럽게 활용하라.",
            "- '데이터베이스에 따르면', '기억 목록에 의하면' 같은 기계적인 표현은 쓰지 마라.",
            "- 기억과 현재 대화가 충돌하면 현재 대화를 우선하고, '과거 사실'로 표시된 것은 지금은 유효하지 않을 수 있다.",
            "- 모르는 것은 지어내지 말고 솔직하게 물어봐라.",
            "- 너는 메시지당 답장을 한 번만 보낼 수 있다. '잠시만요', '찾아볼게요', '정리해서 보여드릴게요'처럼 "
            "나중을 약속하는 말로 끝내지 말고, 필요한 도구는 지금 호출해서 이 답변 안에서 완성하라.",
            "- 일상 대화는 1~4문장으로 짧게. 단, 정리·목록·예문·비교·자세한 설명을 요청받으면 요청한 분량을 "
            "끝까지 채워서 답하라 (긴 답변은 자동으로 나눠 전송된다). 디스코드 마크다운(굵게, 목록, 제목)을 써도 된다. "
            "단, 디스코드는 표(| 칸 |)를 지원하지 않으므로 표 대신 목록으로 정리하라.",
            f"- 현재 시각: {now}",
        ]
        if with_tools:
            parts += ["", TOOL_GUIDE]
        if memory.facts:
            parts += ["", "[장기 기억 — 관련 사실]", *(f"- {f}" for f in memory.facts)]
        if memory.entities:
            parts += ["", "[장기 기억 — 관련 인물/대상 요약]", *(f"- {e}" for e in memory.entities)]
        if memory.personal_facts or memory.personal_entities:
            parts += ["", "[개인 기억 — 지금 말하는 사람에 대해 서버·DM을 통틀어 기억하는 것]",
                      *(f"- {f}" for f in memory.personal_facts),
                      *(f"- {e}" for e in memory.personal_entities),
                      "(다른 사람도 보는 서버에서는 건강·연애·주소·금전 같은 민감한 개인 기억을 먼저 꺼내지 마라.)"]
        if memory.is_empty():
            parts += ["", "[장기 기억] (이 질문과 관련된 기억 없음)"]
        return "\n".join(parts)

    @staticmethod
    def build_user_content(history: list[ShortLine], current: ShortLine) -> str:
        lines: list[str] = []
        if history:
            lines.append("[최근 대화]")
            lines += [f"{h.speaker}{' (너)' if h.is_bot else ''}: {h.text}" for h in history]
            lines.append("")
        lines.append("[방금 받은 메시지 — 여기에 답하라]")
        lines.append(f"{current.speaker}: {current.text}")
        return "\n".join(lines)

    async def _generate(self, contents, config):
        async def _call():
            return await self.client.aio.models.generate_content(
                model=self.s.chat_model, contents=contents, config=config)

        # 채팅 응답은 사용자가 기다리고 있으므로 재시도를 짧게(최대 3회) 가져간다.
        resp = await call_with_retry(_call, limiter=self.limiter,
                                     max_retries=min(3, self.s.gemini_max_retries),
                                     what="chat", base_delay=2.0, max_delay=15.0)
        await tracker.record_response("chat", self.s.chat_model, resp)
        return resp

    async def reply(
        self,
        *,
        bot_name: str,
        place: str,
        history: list[ShortLine],
        current: ShortLine,
        memory: MemoryContext,
        persona: str | None = None,
        tool_ctx: ToolContext | None = None,
    ) -> str:
        decls = declarations(tool_ctx) if tool_ctx is not None else []
        system_prompt = self.build_system_prompt(bot_name=bot_name, place=place, memory=memory,
                                                 persona=persona, with_tools=bool(decls))
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=self.build_user_content(history, current))])
        ]
        base = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=self.s.chat_temperature,
            max_output_tokens=self.s.chat_max_output_tokens,
            thinking_config=self._thinking,
        )
        with_tools = base.model_copy(update={
            "tools": [types.Tool(function_declarations=decls)] if decls else None,
        })
        no_tools = base.model_copy(update={
            "tools": [types.Tool(function_declarations=decls)] if decls else None,
            "tool_config": types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="NONE")) if decls else None,
        })

        async with self._sem:
            response = None
            max_steps = max(0, self.s.chat_max_tool_steps) if decls else 0
            nudged = False
            step = 0
            while True:
                last = step >= max_steps
                response = await self._generate(contents, no_tools if last else with_tools)
                calls = response.function_calls or []
                if not calls:
                    draft = (getattr(response, "text", None) or "").strip()
                    # 약속만 하고 끝난 답변이면 한 번 더 기회를 준다 (마지막 단계여도 도구 없이 완성하게)
                    deferred = tool_ctx is not None and any(n in DEFERRING_TOOLS for n in tool_ctx.used)
                    if not nudged and not deferred and draft and PROMISE_RE.search(draft[-300:]) and response.candidates:
                        log.info("약속형 답변 감지 → 완성 요청: %s", draft[-120:].replace("\n", " "))
                        nudged = True
                        contents.append(response.candidates[0].content)
                        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=NUDGE)]))
                        step += 1
                        continue
                    break
                if last:
                    break
                step += 1
                # 모델의 함수 호출 턴(사고 서명 포함)을 그대로 대화에 붙인 뒤 결과를 돌려준다.
                contents.append(response.candidates[0].content)
                results = await asyncio.gather(*(run_tool(fc.name, fc.args, tool_ctx) for fc in calls))
                contents.append(types.Content(role="user", parts=[
                    types.Part(function_response=types.FunctionResponse(id=fc.id, name=fc.name, response=res))
                    for fc, res in zip(calls, results, strict=True)
                ]))

        text = (getattr(response, "text", None) or "").strip() if response is not None else ""
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            log.debug("chat tokens in=%s out=%s think=%s", usage.prompt_token_count,
                      usage.candidates_token_count, getattr(usage, "thoughts_token_count", None))
        return text or "음... 지금은 뭐라고 답해야 할지 모르겠어요. 다시 한 번 말해 주실래요?"
