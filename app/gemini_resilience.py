"""Gemini 호출 안정화: 공용 RPM 제한기 + 429/5xx 지수 백오프 재시도.

Graphiti 의 기본 GeminiClient 는 429(RateLimitError)를 만나면 재시도 없이 즉시 실패하고,
그러면 에피소드 처리 도중 이미 사용한 토큰이 전부 버려진다.
그래서 LLM / 임베더 / 리랭커 클라이언트를 서브클래싱해 "호출 단위"로 재시도한다.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from google.genai import errors as genai_errors
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.embedder.gemini import GeminiEmbedder
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.llm_client.errors import RateLimitError
from graphiti_core.llm_client.gemini_client import GeminiClient

from .usage import tracker

log = logging.getLogger(__name__)
T = TypeVar("T")

_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}
_RETRYABLE_TEXT = ("resource_exhausted", "rate limit", "quota", "unavailable", "overloaded", "deadline")


class RateLimiter:
    """분당 요청 수(RPM) 제한. rpm <= 0 이면 제한 없음. 봇 전체 Gemini 호출이 공유한다."""

    def __init__(self, rpm: int) -> None:
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self.interval
        if wait > 0:
            await asyncio.sleep(wait)


def is_retryable(exc: BaseException | None) -> bool:
    """예외 체인을 따라가며 429 / 일시적 서버 오류인지 판별한다."""
    seen = 0
    while exc is not None and seen < 6:
        if isinstance(exc, RateLimitError):
            return True
        if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) in _RETRYABLE_HTTP:
            return True
        if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
            return True
        text = str(exc).lower()
        if "429" in text or any(t in text for t in _RETRYABLE_TEXT):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


async def call_with_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    limiter: RateLimiter | None,
    max_retries: int,
    what: str,
    base_delay: float = 4.0,
    max_delay: float = 90.0,
) -> T:
    """factory() 를 실행하되, 재시도 가능한 오류면 지수 백오프(+지터)로 다시 시도한다."""
    attempt = 0
    while True:
        if limiter is not None:
            await limiter.acquire()
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 - 판별 후 재전파
            if attempt >= max_retries or not is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2**attempt)) + random.uniform(0, 1.5)
            attempt += 1
            log.warning("Gemini %s 일시 오류(%s) → %.1fs 후 재시도 (%d/%d)",
                        what, type(exc).__name__, delay, attempt, max_retries)
            await asyncio.sleep(delay)


class ResilientGeminiClient(GeminiClient):
    """Graphiti LLM 클라이언트: 호출마다 RPM 제한 + 429 백오프."""

    def __init__(self, *args: Any, limiter: RateLimiter, max_retries: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._limiter = limiter
        self._rl_retries = max_retries

    async def _generate_response(  # type: ignore[override]
        self,
        messages,
        response_model=None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        *,
        model: str | None = None,
    ):
        parent = super()._generate_response
        out = await call_with_retry(
            lambda: parent(messages, response_model, max_tokens, model_size, model=model),
            limiter=self._limiter,
            max_retries=self._rl_retries,
            what="LLM",
        )
        try:  # (응답, 입력 토큰, 출력 토큰)
            await tracker.record("memory", model or self.model or "gemini", input=out[1], output=out[2])
        except Exception:  # noqa: BLE001
            pass
        return out


class ResilientGeminiEmbedder(GeminiEmbedder):
    def __init__(self, *args: Any, limiter: RateLimiter, max_retries: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._limiter = limiter
        self._rl_retries = max_retries

    async def create(self, input_data):  # type: ignore[override]
        parent = super().create
        out = await call_with_retry(lambda: parent(input_data), limiter=self._limiter,
                                    max_retries=self._rl_retries, what="embedding")
        await self._track([input_data] if isinstance(input_data, str) else [])
        return out

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        parent = super().create_batch
        out = await call_with_retry(lambda: parent(input_data_list), limiter=self._limiter,
                                    max_retries=self._rl_retries, what="embedding-batch")
        await self._track(input_data_list)
        return out

    async def _track(self, texts: list) -> None:
        # 임베딩 응답에는 토큰 수가 없어 글자 수로 근사 (한국어 약 1.5자 = 1토큰)
        chars = sum(len(t) for t in texts if isinstance(t, str))
        await tracker.record("embedding", self.config.embedding_model, input=int(chars / 1.5) + 1)


class ResilientGeminiReranker(GeminiRerankerClient):
    def __init__(self, *args: Any, limiter: RateLimiter, max_retries: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._limiter = limiter
        self._rl_retries = max_retries

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        parent = super().rank
        return await call_with_retry(lambda: parent(query, passages), limiter=self._limiter,
                                     max_retries=self._rl_retries, what="rerank")
