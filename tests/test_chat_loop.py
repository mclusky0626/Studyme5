"""함수 호출 루프: 약속만 하고 끝나는 답변을 다시 완성시키는지 확인 (Gemini 호출은 가짜)."""

from types import SimpleNamespace

from google.genai import types

from app.chat import ChatResponder, ShortLine
from app.config import settings
from app.gemini_resilience import RateLimiter
from app.memory import MemoryContext


def fake_response(text=None, calls=None):
    content = types.Content(role="model", parts=[types.Part.from_text(text=text or "...")])
    return SimpleNamespace(text=text, function_calls=calls, candidates=[SimpleNamespace(content=content)],
                           usage_metadata=None)


async def _reply(chat, ctx=None):
    return await chat.reply(bot_name="봇", place="테스트", history=[], current=ShortLine("유저", "정리해줘"),
                            memory=MemoryContext(), tool_ctx=ctx)


async def test_promise_reply_is_completed():
    chat = ChatResponder(settings, RateLimiter(0))
    seen = []

    async def gen(contents, config):
        seen.append(len(contents))
        if len(seen) == 1:
            return fake_response("데이터가 많으니 조금만 기다려 주세요!")
        return fake_response("1. 첫째 예문\n2. 둘째 예문")

    chat._generate = gen
    out = await _reply(chat, chat.tool_context())
    assert out.startswith("1. 첫째")
    assert seen == [1, 3]  # 모델 답 + 시스템 재촉이 붙어서 한 번 더 호출
    await chat.aclose()


async def test_normal_reply_not_nudged():
    chat = ChatResponder(settings, RateLimiter(0))
    calls = 0

    async def gen(contents, config):
        nonlocal calls
        calls += 1
        return fake_response("크리스마스까지 34일 남았어요!")

    chat._generate = gen
    assert "34일" in await _reply(chat, chat.tool_context())
    assert calls == 1
    await chat.aclose()


async def test_nudge_only_once():
    chat = ChatResponder(settings, RateLimiter(0))
    calls = 0

    async def gen(contents, config):
        nonlocal calls
        calls += 1
        return fake_response("잠시만요, 찾아볼게요")

    chat._generate = gen
    await _reply(chat, chat.tool_context())
    assert calls == 2  # 무한 루프 없이 한 번만 재촉
    await chat.aclose()
