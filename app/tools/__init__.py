"""Gemini 함수 호출(Function Calling)용 도구 모음.

새 도구 추가 방법: 아무 모듈에서 `@tool(...)` 데코레이터로 async 함수를 등록하고
아래 `_load_modules()` 목록에 모듈을 넣으면 끝. 핸들러는 dict 를 반환한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from google.genai import types

if TYPE_CHECKING:  # pragma: no cover
    import httpx
    from google import genai

    from ..config import Settings
    from ..gemini_resilience import RateLimiter
    from ..memory import MemoryService
    from ..tasks import TaskManager
    from .reminders import ReminderStore

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]]
MAX_RESULT_CHARS = 8000
TOOL_TIMEOUT = 30.0


@dataclass
class ToolContext:
    """도구 실행에 필요한 공용 서비스 + 메시지별 정보."""

    settings: "Settings"
    genai_client: "genai.Client"
    limiter: "RateLimiter"
    http: "httpx.AsyncClient"
    memory: "MemoryService | None" = None
    reminders: "ReminderStore | None" = None
    tasks: "TaskManager | None" = None
    openai_client: Any = None   # SEARCH_PROVIDER=openai 일 때 web_search 용
    message_id: int | None = None
    group_id: str | None = None
    channel_id: int | None = None
    user_id: int | None = None
    user_name: str | None = None
    is_dm: bool = False
    used: list[str] = field(default_factory=list)  # 이번 답변에서 쓴 도구 이름


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    available: Callable[[ToolContext], bool] = lambda ctx: True


TOOLS: dict[str, ToolSpec] = {}


def tool(name: str, description: str, properties: dict[str, Any] | None = None,
         required: list[str] | None = None, *, available: Callable[[ToolContext], bool] | None = None):
    def deco(fn: Handler) -> Handler:
        TOOLS[name] = ToolSpec(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties or {}, "required": required or []},
            handler=fn,
            available=available or (lambda ctx: True),
        )
        return fn

    return deco


class ToolError(Exception):
    """사용자에게 그대로 보여 줘도 되는 도구 오류."""


def _load_modules() -> None:
    from . import basic, memory_tool, reminders, tasks_tool, web  # noqa: F401


def enabled_names(settings: "Settings") -> set[str]:
    raw = (settings.tools_enabled or "all").strip().lower()
    if raw in {"", "all", "*"}:
        return set(TOOLS)
    if raw in {"none", "off", "false", "0"}:
        return set()
    return {n.strip() for n in raw.split(",") if n.strip() in TOOLS}


def schemas(ctx: ToolContext) -> list[dict[str, Any]]:
    """제공자 중립 도구 정의 (name, description, JSON 스키마) — Gemini·OpenAI 모두 이걸로 변환."""
    _load_modules()
    names = enabled_names(ctx.settings)
    return [{"name": s.name, "description": s.description, "parameters": s.parameters}
            for s in TOOLS.values() if s.name in names and s.available(ctx)]


def declarations(ctx: ToolContext) -> list[types.FunctionDeclaration]:
    _load_modules()
    names = enabled_names(ctx.settings)
    decls = []
    for spec in TOOLS.values():
        if spec.name in names and spec.available(ctx):
            decls.append(types.FunctionDeclaration(
                name=spec.name, description=spec.description, parameters_json_schema=spec.parameters,
            ))
    return decls


async def run_tool(name: str, args: dict[str, Any] | None, ctx: ToolContext) -> dict[str, Any]:
    """도구 실행. 어떤 실패도 예외로 새지 않고 {'error': ...} 로 모델에게 돌려준다."""
    _load_modules()
    spec = TOOLS.get(name)
    if spec is None or name not in enabled_names(ctx.settings) or not spec.available(ctx):
        return {"error": f"사용할 수 없는 도구: {name}"}
    args = dict(args or {})
    ctx.used.append(name)
    try:
        result = await asyncio.wait_for(spec.handler(args, ctx), timeout=TOOL_TIMEOUT)
    except ToolError as exc:
        result = {"error": str(exc)}
    except asyncio.TimeoutError:
        result = {"error": "도구 응답 시간이 초과되었습니다"}
    except Exception as exc:  # noqa: BLE001
        log.warning("도구 %s 실패: %s", name, exc, exc_info=True)
        result = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        result = {"truncated": True, "partial": text[:MAX_RESULT_CHARS]}
    log.info("🔧 tool %s(%s) → %s", name, json.dumps(args, ensure_ascii=False)[:200], text[:200])
    return result
