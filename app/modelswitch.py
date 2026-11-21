"""채팅 모델 실행 중 전환 (/model). 재시작 없이 바뀌고, FalkorDB 에 저장돼 재시작 후에도 유지된다.

바꾸는 범위: 채팅 답변 · 백그라운드/예약 작업 · 후속 처리 (함수 호출 포함).
기억(Graphiti) 모델은 바꾸면 기존 기억 벡터와 맞지 않아 .env 의 MEMORY_PROVIDER 로만 바꾼다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .backends import make_backend

if TYPE_CHECKING:  # pragma: no cover
    from .chat import ChatResponder

log = logging.getLogger(__name__)
KEY = "memorybot:model_override"
PROVIDERS = ("gemini", "openai")
# 채팅용이 아닌 모델 (에이전트 전용·이미지 생성 등)
NOT_CHAT = ("antigravity", "deep-research", "nano-banana", "image", "tts", "embedding", "imagen", "veo",
            "whisper", "dall-e", "moderation", "audio", "realtime", "transcribe")
RECOMMENDED = {
    "gemini-3.5-flash-lite": "기본 · 가장 저렴",
    "gemini-3.5-flash": "더 똑똑함 · 답변당 약 2배 · 자동 캐시 지원",
}
CACHE_SECONDS = 600


class ModelSwitchError(Exception):
    pass


class ModelSwitcher:
    def __init__(self, chat: "ChatResponder", redis_getter: Callable[[], Any | None]) -> None:
        self.chat = chat
        self._redis_getter = redis_getter
        self.default = (chat.backend.provider, chat.backend.model)

    def _redis(self):
        try:
            return self._redis_getter()
        except Exception:  # noqa: BLE001
            return None

    def current(self) -> dict[str, Any]:
        be = self.chat.backend
        return {"provider": be.provider, "model": be.model,
                "default_provider": self.default[0], "default_model": self.default[1],
                "overridden": (be.provider, be.model) != self.default}

    def _check_keys(self, provider: str) -> None:
        s = self.chat.s
        if provider == "gemini" and not s.gemini_api_key:
            raise ModelSwitchError("Gemini 키(GEMINI_API_KEY)가 없어 gemini 로 바꿀 수 없습니다.")
        if provider == "openai" and not (s.openai_api_key or s.openai_base_url):
            raise ModelSwitchError("OpenAI 키(OPENAI_API_KEY) 또는 OPENAI_BASE_URL 이 없어 openai 로 바꿀 수 없습니다.")

    async def set(self, provider: str, model: str, *, test: bool = True) -> dict[str, Any]:
        provider = provider.lower().strip()
        model = model.strip()
        if provider not in PROVIDERS:
            raise ModelSwitchError(f"제공자는 {', '.join(PROVIDERS)} 중 하나여야 합니다.")
        if not model or len(model) > 100:
            raise ModelSwitchError("모델 이름이 올바르지 않습니다.")
        self._check_keys(provider)
        new = make_backend(self.chat.s, self.chat.limiter, provider=provider, model=model)
        if test:  # 짧은 시험 호출로 이름 오타·권한 문제를 미리 잡는다
            try:
                conv = new.start("짧게 답하라.", "ping")
                await new.step(conv, [], max_tokens=32, category="chat")
            except Exception as exc:  # noqa: BLE001
                await new.aclose()
                raise ModelSwitchError(f"'{model}' 시험 호출 실패: {str(exc)[:200]}") from exc
        old = self.chat.backend
        self.chat.backend = new
        await old.aclose()
        r = self._redis()
        if r is not None:
            await r.set(KEY, json.dumps({"provider": provider, "model": model}))
        log.info("🔀 채팅 모델 전환: %s / %s", provider, model)
        return self.current()

    async def reset(self) -> dict[str, Any]:
        r = self._redis()
        if r is not None:
            await r.delete(KEY)
        provider, model = self.default
        new = make_backend(self.chat.s, self.chat.limiter, provider=provider, model=model)
        old, self.chat.backend = self.chat.backend, new
        await old.aclose()
        log.info("🔀 채팅 모델 기본값으로: %s / %s", provider, model)
        return self.current()

    async def restore(self) -> None:
        """봇 시작 시 저장된 전환을 다시 적용 (시험 호출 없이, 실패하면 기본값 유지)."""
        r = self._redis()
        if r is None:
            return
        raw = await r.get(KEY)
        if not raw:
            return
        try:
            data = json.loads(raw)
            self._check_keys(data["provider"])
            await self.set(data["provider"], data["model"], test=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("저장된 모델 전환을 적용하지 못해 기본값을 씁니다: %s", exc)

    async def list_models(self, provider: str) -> list[str]:
        """선택 가능한 채팅 모델 이름 (10분 캐시). API 가 목록을 못 주면 빈 목록."""
        import time

        cache = getattr(self, "_list_cache", {})
        self._list_cache = cache
        hit = cache.get(provider)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        names = [n for n in await self._fetch_models(provider) if not any(x in n for x in NOT_CHAT)]
        if names:
            cache[provider] = (time.monotonic(), names)
        return names

    async def _fetch_models(self, provider: str) -> list[str]:
        try:
            if provider == "gemini":
                names = []
                async for m in await self.chat.client.aio.models.list():
                    actions = getattr(m, "supported_actions", None) or []
                    if "generateContent" in actions and "image" not in m.name and "tts" not in m.name:
                        names.append(m.name.removeprefix("models/"))
                return sorted(names)
            if provider == "openai" and self.chat.openai_client is not None:
                page = await self.chat.openai_client.models.list()
                return sorted(m.id for m in page.data
                              if not any(x in m.id for x in ("embedding", "whisper", "tts", "dall-e", "image",
                                                              "moderation", "audio", "realtime", "transcribe")))
        except Exception as exc:  # noqa: BLE001
            log.warning("모델 목록 조회 실패(%s): %s", provider, exc)
        return []
