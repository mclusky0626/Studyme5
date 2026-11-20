import asyncio
import dataclasses

import pytest

from app.batcher import EpisodeBatcher, core_length, should_store
from app.config import settings as base_settings
from app.gemini_resilience import RateLimiter, call_with_retry, is_retryable
from graphiti_core.llm_client.errors import RateLimitError


def make_settings(tmp_path, **overrides):
    defaults = dict(batch_idle_seconds=0.2, batch_max_messages=4, batch_max_chars=10_000,
                    min_message_chars=10, episode_max_retries=2, max_concurrent_episodes=1,
                    data_dir=tmp_path)
    defaults.update(overrides)
    return dataclasses.replace(base_settings, **defaults)


def test_filters():
    assert core_length("ㅋㅋㅋㅋ") == 0
    assert core_length("ㅇㅇ!!") == 0
    assert not should_store("ㅋㅋㅋ", direct=True, is_bot=False, min_chars=10)
    assert not should_store("ㄹㅇ 맞아", direct=False, is_bot=False, min_chars=10)
    # 봇에게 직접 한 짧은 사실은 보존
    assert should_store("내 이름은 건우야", direct=True, is_bot=False, min_chars=10)
    # 일반 채널 대화는 10자 규칙 적용
    assert not should_store("나 고양이 키워", direct=False, is_bot=False, min_chars=10)
    assert should_store("나 다음 주에 제주도로 여행 가", direct=False, is_bot=False, min_chars=10)


def _add(b, text, key="c1", speaker="건우", **kw):
    return b.add(key=key, group_id="guild-1", speaker=speaker, text=text,
                 channel_label="#general", source_description="test", **kw)


@pytest.mark.asyncio
async def test_size_flush(tmp_path):
    saved = []

    async def saver(ep):
        saved.append(ep)

    b = EpisodeBatcher(make_settings(tmp_path), saver)
    for i in range(4):
        _add(b, f"이건 테스트 메시지 번호 {i} 입니다")
    await asyncio.sleep(0.05)
    assert len(saved) == 1
    assert saved[0].message_count == 4
    assert saved[0].body.startswith("건우: 이건 테스트")
    assert b.stats()["buffers"] == 0


@pytest.mark.asyncio
async def test_debounce_flush(tmp_path):
    saved = []

    async def saver(ep):
        saved.append(ep)

    b = EpisodeBatcher(make_settings(tmp_path), saver)
    _add(b, "첫 번째 메시지는 충분히 깁니다")
    await asyncio.sleep(0.12)
    _add(b, "두 번째 메시지가 타이머를 리셋합니다")
    await asyncio.sleep(0.12)
    assert saved == []  # 아직 0.2초 무응답이 아님
    await asyncio.sleep(0.2)
    assert len(saved) == 1 and saved[0].message_count == 2


@pytest.mark.asyncio
async def test_retry_then_dead_letter_and_replay(tmp_path, monkeypatch):
    calls = 0

    async def failing(ep):
        nonlocal calls
        calls += 1
        raise RateLimitError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr("app.batcher.random.uniform", lambda a, b: 0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr("app.batcher.asyncio.sleep", lambda s: real_sleep(0))

    b = EpisodeBatcher(make_settings(tmp_path), failing)
    _add(b, "저장이 계속 실패하는 메시지입니다")
    b.flush("c1")
    for _ in range(50):
        await real_sleep(0.01)
        if b.failed_count:
            break
    assert calls == 3  # 최초 1회 + 재시도 2회
    failed_file = tmp_path / "failed_episodes.jsonl"
    assert failed_file.exists()

    saved = []

    async def ok(ep):
        saved.append(ep)

    b2 = EpisodeBatcher(make_settings(tmp_path), ok)
    assert await b2.replay_failed() == 1
    await real_sleep(0.05)
    assert len(saved) == 1 and not failed_file.exists()


@pytest.mark.asyncio
async def test_saver_exception_does_not_crash(tmp_path):
    async def boom(ep):
        raise ValueError("not retryable")

    b = EpisodeBatcher(make_settings(tmp_path, episode_max_retries=0), boom)
    _add(b, "예외가 나도 봇은 살아 있어야 합니다")
    b.flush("c1")
    await asyncio.sleep(0.05)
    assert b.failed_count == 1


@pytest.mark.asyncio
async def test_shutdown_flushes_buffers(tmp_path):
    saved = []

    async def saver(ep):
        saved.append(ep)

    b = EpisodeBatcher(make_settings(tmp_path, batch_idle_seconds=60), saver)
    _add(b, "종료 직전에 남아 있던 대화입니다", key="a")
    _add(b, "다른 채널에 남아 있던 대화입니다", key="b")
    await b.shutdown(timeout=1)
    assert len(saved) == 2


def test_is_retryable():
    assert is_retryable(RateLimitError())
    try:
        try:
            raise RateLimitError("quota")
        except RateLimitError as inner:
            raise Exception("wrapped") from inner
    except Exception as outer:
        assert is_retryable(outer)
    assert not is_retryable(ValueError("bad json"))


@pytest.mark.asyncio
async def test_call_with_retry(monkeypatch):
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RateLimitError("429")
        return "ok"

    real_sleep = asyncio.sleep
    monkeypatch.setattr("app.gemini_resilience.asyncio.sleep", lambda s: real_sleep(0))
    assert await call_with_retry(flaky, limiter=RateLimiter(0), max_retries=5, what="t") == "ok"
    assert attempts == 3
