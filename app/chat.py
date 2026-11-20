"""답변 생성: 단기 기억(채널 최근 대화) + 장기 기억(Graphiti 검색 결과) → Gemini."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from .config import Settings
from .gemini_resilience import RateLimiter, call_with_retry
from .memory import MemoryContext

log = logging.getLogger(__name__)

DEFAULT_PERSONA = (
    "너는 친근하고 센스 있는 디스코드 친구다. 사람들과 나눈 대화를 오래 기억하고, "
    "적절한 순간에 자연스럽게 떠올려 대화를 이어간다. 기본적으로 한국어 반말 대신 "
    "편안한 존댓말을 쓰되, 상대가 반말을 쓰면 맞춰도 좋다."
)


@dataclass
class ShortLine:
    speaker: str
    text: str
    is_bot: bool = False


class ChatResponder:
    def __init__(self, settings: Settings, limiter: RateLimiter) -> None:
        self.s = settings
        self.limiter = limiter
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrent_chats))
        self._tz = ZoneInfo("Asia/Seoul")
        self._thinking = (
            types.ThinkingConfig(thinking_level=settings.chat_thinking_level)
            if settings.chat_thinking_level
            else None
        )

    def build_system_prompt(self, *, bot_name: str, place: str, memory: MemoryContext) -> str:
        now = datetime.now(self._tz).strftime("%Y-%m-%d (%a) %H:%M KST")
        parts = [
            f"너는 '{place}'에서 활동하는 AI '{bot_name}'이다.",
            self.s.bot_persona or DEFAULT_PERSONA,
            "",
            "규칙:",
            "- [장기 기억]은 과거 대화에서 추출한 사실이다. 지금 대화와 관련 있을 때만 자연스럽게 활용하라.",
            "- '데이터베이스에 따르면', '기억 목록에 의하면' 같은 기계적인 표현은 쓰지 마라.",
            "- 기억과 현재 대화가 충돌하면 현재 대화를 우선하고, '과거 사실'로 표시된 것은 지금은 유효하지 않을 수 있다.",
            "- 모르는 것은 지어내지 말고 솔직하게 물어봐라.",
            "- 디스코드 채팅이므로 보통 1~4문장으로 간결하게 답하고, 마크다운은 꼭 필요할 때만 쓴다.",
            f"- 현재 시각: {now}",
        ]
        if memory.facts:
            parts += ["", "[장기 기억 — 관련 사실]", *(f"- {f}" for f in memory.facts)]
        if memory.entities:
            parts += ["", "[장기 기억 — 관련 인물/대상 요약]", *(f"- {e}" for e in memory.entities)]
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

    async def reply(
        self,
        *,
        bot_name: str,
        place: str,
        history: list[ShortLine],
        current: ShortLine,
        memory: MemoryContext,
    ) -> str:
        system_prompt = self.build_system_prompt(bot_name=bot_name, place=place, memory=memory)
        user_content = self.build_user_content(history, current)
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=self.s.chat_temperature,
            max_output_tokens=2048,
            thinking_config=self._thinking,
        )

        async def _call():
            return await self.client.aio.models.generate_content(
                model=self.s.chat_model, contents=user_content, config=config
            )

        async with self._sem:
            # 채팅 응답은 사용자가 기다리고 있으므로 재시도를 짧게(최대 3회) 가져간다.
            response = await call_with_retry(
                _call, limiter=self.limiter, max_retries=min(3, self.s.gemini_max_retries),
                what="chat", base_delay=2.0, max_delay=15.0,
            )
        text = (getattr(response, "text", None) or "").strip()
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            log.debug("chat tokens in=%s out=%s think=%s", usage.prompt_token_count,
                      usage.candidates_token_count, getattr(usage, "thoughts_token_count", None))
        return text or "음... 지금은 뭐라고 답해야 할지 모르겠어요. 다시 한 번 말해 주실래요?"
