"""함수 호출 루프: 약속형 답변 재촉, OpenAI 함수 호출 (모델 호출은 가짜)."""

import json
from types import SimpleNamespace

from app.backends import OpenAIBackend, Step, ToolCall
from app.chat import ChatResponder, ShortLine
from app.config import settings
from app.gemini_resilience import RateLimiter
from app.memory import MemoryContext


async def _reply(chat, ctx=None, text="정리해줘"):
    return await chat.reply(bot_name="봇", place="테스트", history=[], current=ShortLine("유저", text),
                            memory=MemoryContext(), tool_ctx=ctx)


def fake_steps(chat, texts):
    """backend.step 을 가짜로: 호출마다 texts 를 차례로 돌려준다."""
    calls = []

    async def step(conv, tools, **kw):
        calls.append(len(conv.messages))
        return Step(text=texts[min(len(calls), len(texts)) - 1], calls=[], raw=None)

    chat.backend.step = step
    chat.backend.add_model_turn = lambda conv, s: conv.messages.append("model")
    return calls


async def test_promise_reply_is_completed():
    chat = ChatResponder(settings, RateLimiter(0))
    seen = fake_steps(chat, ["데이터가 많으니 조금만 기다려 주세요!", "1. 첫째 예문 / 2. 둘째 예문"])
    out = await _reply(chat, chat.tool_context())
    assert out.startswith("1. 첫째")
    assert seen == [1, 3]  # 모델 답 + 시스템 재촉이 붙어서 한 번 더 호출
    await chat.aclose()


async def test_normal_reply_not_nudged():
    chat = ChatResponder(settings, RateLimiter(0))
    seen = fake_steps(chat, ["크리스마스까지 34일 남았어요!"])
    assert "34일" in await _reply(chat, chat.tool_context())
    assert len(seen) == 1
    await chat.aclose()


async def test_nudge_only_once():
    chat = ChatResponder(settings, RateLimiter(0))
    seen = fake_steps(chat, ["잠시만요, 찾아볼게요", "잠시만요, 찾아볼게요"])
    await _reply(chat, chat.tool_context())
    assert len(seen) == 2  # 무한 루프 없이 한 번만 재촉
    await chat.aclose()


class FakeOpenAI:
    """chat.completions.create 를 흉내: 처음엔 calculate 도구 호출, 다음엔 최종 답."""

    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10, total_tokens=110,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=0))
        if len(self.requests) == 1:
            tc = SimpleNamespace(id="call_1", type="function",
                                 function=SimpleNamespace(name="calculate", arguments='{"expression": "37*12"}'))
            msg = SimpleNamespace(content=None, tool_calls=[tc])
        else:
            msg = SimpleNamespace(content="37 곱하기 12는 444야!", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    async def close(self):
        pass


async def test_openai_function_calling_roundtrip():
    chat = ChatResponder(settings, RateLimiter(0))
    be = OpenAIBackend(settings, RateLimiter(0))
    be.client = FakeOpenAI()
    chat.backend = be
    out = await _reply(chat, chat.tool_context(), text="37*12 계산해줘")
    assert out == "37 곱하기 12는 444야!"
    first, second = be.client.requests
    assert first["tool_choice"] == "auto" and any(t["function"]["name"] == "calculate" for t in first["tools"])
    # 두 번째 요청: assistant(tool_calls) → tool(결과) 메시지가 붙어 있어야 한다
    roles = [m["role"] for m in second["messages"]]
    assert roles[-2:] == ["assistant", "tool"]
    assert json.loads(second["messages"][-1]["content"])["result"] == 444
    assert second["messages"][-1]["tool_call_id"] == "call_1"
    await chat.aclose()


def test_promise_detection_cases():
    from app.chat import is_unfulfilled_promise as p

    promises = [
        "아, '요즘 뉴스'를 그냥 찾아보라는 거였구나! 오타인 줄 알았어. 오늘 주요 뉴스를 몇 가지 살펴볼게.",
        "원하시는 대로 싹 정리해서 보여드릴게요. (잠시만요, 데이터 양이 꽤 많으니 조금만 기다려 주세요!)",
        "좋아, 바로 알아봐 줄게!",
        "그거 확인해 보고 올게요",
        "최신 가격 금방 찾아올게~",
    ]
    normal = [
        "응, 찾아봤지! 오늘 주요 뉴스 몇 가지 전해줄게.\n\n• 경부선 화물열차 탈선: 세종시 소정리역 인근에서 화물열차가 탈선해 일부 구간 운행이 조정됐어.\n"
        "• 대법원장 탄핵소추: 국회에서 탄핵소추안이 당론으로 채택됐어.\n• 수능 부정행위 의혹: 정부가 수사를 의뢰할 방침이래.",
        "크리스마스까지 34일 남았어요!",
        "다음에 또 얘기해요. 좋은 하루 보내!",
    ]
    for t in promises:
        assert p(t), t
    for t in normal:
        assert not p(t), t
    # 리마인더·백그라운드 작업처럼 실제로 예약했으면 약속이 아니다
    assert not p("40분 뒤에 알려줄게! 곧 알려줄게", ["set_reminder"])
