import pytest

from app.backends import Step
from app.chat import ChatResponder
from app.config import settings
from app.gemini_resilience import RateLimiter
from app.modelswitch import ModelSwitcher, ModelSwitchError


class FakeRedis:
    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v):
        self.d[k] = v

    async def delete(self, *ks):
        for k in ks:
            self.d.pop(k, None)


@pytest.fixture
def ok_step(monkeypatch):
    async def step(self, conv, tools, **kw):
        return Step(text="pong", calls=[])
    monkeypatch.setattr("app.backends.GeminiBackend.step", step)


async def test_switch_persist_restore_reset(ok_step):
    r = FakeRedis()
    chat = ChatResponder(settings, RateLimiter(0))
    sw = ModelSwitcher(chat, lambda: r)
    default = chat.backend.model
    cur = await sw.set("gemini", "gemini-3.5-flash")
    assert cur["model"] == "gemini-3.5-flash" and cur["overridden"]
    # 재시작 흉내: 새 응답기 + 저장값 복원
    chat2 = ChatResponder(settings, RateLimiter(0))
    sw2 = ModelSwitcher(chat2, lambda: r)
    await sw2.restore()
    assert chat2.backend.model == "gemini-3.5-flash"
    back = await sw2.reset()
    assert back["model"] == default and not back["overridden"] and r.d == {}
    await chat.aclose()
    await chat2.aclose()


async def test_bad_model_is_rejected(monkeypatch):
    async def fail(self, conv, tools, **kw):
        raise RuntimeError("404 model not found")
    monkeypatch.setattr("app.backends.GeminiBackend.step", fail)
    chat = ChatResponder(settings, RateLimiter(0))
    sw = ModelSwitcher(chat, lambda: None)
    before = chat.backend
    with pytest.raises(ModelSwitchError):
        await sw.set("gemini", "gemini-오타")
    assert chat.backend is before  # 실패하면 그대로
    with pytest.raises(ModelSwitchError):
        await sw.set("claude", "x")
    await chat.aclose()
