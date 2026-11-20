import dataclasses

from app.config import settings as base_settings
from app.persona import DEFAULT_NAME, DEFAULT_PERSONA, PersonaStore, read_persona_file


class FakeRedis:
    def __init__(self):
        self.d = {}

    async def mget(self, *keys):
        return [self.d.get(k) for k in keys]

    async def set(self, k, v):
        self.d[k] = v

    async def delete(self, *keys):
        for k in keys:
            self.d.pop(k, None)


def make(tmp_path, file_text=None, **kw):
    path = tmp_path / "persona.md"
    if file_text is not None:
        path.write_text(file_text, encoding="utf-8")
    s = dataclasses.replace(base_settings, persona_file=path, bot_name=None, bot_persona=None, **kw)
    r = FakeRedis()
    store = PersonaStore(s, lambda: r)
    store.ttl = 0
    return store, r


def test_file_comments_are_ignored(tmp_path):
    p = tmp_path / "p.md"
    p.write_text("# 메모\n너는 고양이다.\n  # 들여쓴 메모\n냥냥체를 쓴다.", encoding="utf-8")
    assert read_persona_file(p) == "너는 고양이다.\n냥냥체를 쓴다."


async def test_priority_web_over_file_over_default(tmp_path):
    store, r = make(tmp_path)
    assert await store.get() == (DEFAULT_NAME, DEFAULT_PERSONA)

    store, r = make(tmp_path, file_text="# c\n파일 페르소나")
    assert (await store.get())[1] == "파일 페르소나"
    assert (await store.info())["persona_source"] == "file"

    await store.save("페치", "웹 페르소나")
    assert await store.get() == ("페치", "웹 페르소나")
    assert (await store.info())["persona_source"] == "web"

    await store.reset()
    assert await store.get() == (DEFAULT_NAME, "파일 페르소나")


async def test_no_redis_falls_back(tmp_path):
    s = dataclasses.replace(base_settings, persona_file=tmp_path / "x.md", bot_name="환경이름",
                            bot_persona="환경 페르소나")
    store = PersonaStore(s, lambda: None)
    assert await store.get() == ("환경이름", "환경 페르소나")
