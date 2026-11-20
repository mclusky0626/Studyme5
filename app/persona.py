"""봇 이름·페르소나 관리.

우선순위 (위가 이김)
  1. 관리 웹에서 저장한 값  (FalkorDB/Redis 키, 재시작 없이 즉시 반영)
  2. persona.md 파일        (메모장으로 수정, 저장 후 몇 초 내 반영)
  3. .env 의 BOT_PERSONA / BOT_NAME
  4. 코드 기본값
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

PERSONA_KEY = "memorybot:persona"
NAME_KEY = "memorybot:bot_name"
MAX_PERSONA_CHARS = 4000
MAX_NAME_CHARS = 40

DEFAULT_PERSONA = (
    "너는 친근하고 센스 있는 디스코드 친구다. 사람들과 나눈 대화를 오래 기억하고, "
    "적절한 순간에 자연스럽게 떠올려 대화를 이어간다. 기본적으로 편안한 존댓말을 쓰되, "
    "상대가 반말을 쓰면 맞춰도 좋다."
)
DEFAULT_NAME = "라스페치아"


def _s(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    v = str(v).strip()
    return v or None


def read_persona_file(path: Path) -> str | None:
    """persona.md 읽기. `#` 으로 시작하는 줄은 메모(주석)로 보고 무시한다."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(lines).strip()
    return body[:MAX_PERSONA_CHARS] or None


class PersonaStore:
    def __init__(self, settings: Settings, redis_getter: Callable[[], Any | None]) -> None:
        self.s = settings
        self._redis_getter = redis_getter
        self._cache: tuple[float, str, str] | None = None
        self._file_cache: tuple[float, str | None] | None = None
        self.ttl = 5.0

    # ------------------------------------------------------------------ 내부
    def _redis(self):
        try:
            return self._redis_getter()
        except Exception:  # noqa: BLE001
            return None

    def _file_persona(self) -> str | None:
        path = self.s.persona_file
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if self._file_cache and self._file_cache[0] == mtime:
            return self._file_cache[1]
        value = read_persona_file(path)
        self._file_cache = (mtime, value)
        return value

    async def _stored(self) -> tuple[str | None, str | None]:
        r = self._redis()
        if r is None:
            return None, None
        try:
            name, persona = await r.mget(NAME_KEY, PERSONA_KEY)
            return _s(name), _s(persona)
        except Exception as exc:  # noqa: BLE001
            log.debug("페르소나 조회 실패: %s", exc)
            return None, None

    def _fallback_name(self) -> str:
        return self.s.bot_name or DEFAULT_NAME

    def _fallback_persona(self) -> tuple[str, str]:
        file_value = self._file_persona()
        if file_value:
            return file_value, "file"
        if self.s.bot_persona:
            return self.s.bot_persona, "env"
        return DEFAULT_PERSONA, "default"

    # ------------------------------------------------------------------ 공개 API
    async def get(self) -> tuple[str, str]:
        """(봇 이름, 페르소나) — 답변마다 호출해도 되도록 5초 캐시."""
        now = time.monotonic()
        if self._cache and now - self._cache[0] < self.ttl:
            return self._cache[1], self._cache[2]
        name, persona = await self._stored()
        name = name or self._fallback_name()
        persona = persona or self._fallback_persona()[0]
        self._cache = (now, name, persona)
        return name, persona

    async def info(self) -> dict[str, Any]:
        name, persona = await self._stored()
        fb_persona, fb_source = self._fallback_persona()
        return {
            "name": name or self._fallback_name(),
            "name_source": "web" if name else ("env" if self.s.bot_name else "default"),
            "persona": persona or fb_persona,
            "persona_source": "web" if persona else fb_source,
            "file_persona": self._file_persona(),
            "file_path": str(self.s.persona_file),
            "default_persona": DEFAULT_PERSONA,
            "max_persona_chars": MAX_PERSONA_CHARS,
        }

    async def save(self, name: str | None, persona: str | None) -> None:
        r = self._redis()
        if r is None:
            raise RuntimeError("FalkorDB 에 연결되어 있지 않아 저장할 수 없습니다")
        name = (name or "").strip()[:MAX_NAME_CHARS]
        persona = (persona or "").strip()[:MAX_PERSONA_CHARS]
        if name:
            await r.set(NAME_KEY, name)
        else:
            await r.delete(NAME_KEY)
        if persona:
            await r.set(PERSONA_KEY, persona)
        else:
            await r.delete(PERSONA_KEY)
        self._cache = None

    async def reset(self) -> None:
        """웹에서 저장한 값을 지우고 persona.md / .env 값으로 돌아간다."""
        r = self._redis()
        if r is not None:
            await r.delete(NAME_KEY, PERSONA_KEY)
        self._cache = None
