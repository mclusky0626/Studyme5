"""장기 기억을 모델이 직접 다시 찾아볼 수 있게 하는 도구.

답변마다 질문 기준으로 기억을 자동 주입하지만, 대화 도중 다른 주제의 기억이 필요할 때
(예: "내가 전에 말한 그 책 제목 뭐였지?") 모델이 검색어를 바꿔 한 번 더 찾을 수 있다.
"""

from __future__ import annotations

from . import ToolContext, tool


def _has_memory(ctx: ToolContext) -> bool:
    return ctx.memory is not None and ctx.group_id is not None and ctx.memory.available


@tool(
    "search_memory",
    "이 서버(또는 DM)의 장기 기억을 다른 검색어로 더 찾아본다. 시스템 프롬프트의 [장기 기억]에 원하는 내용이 없을 때만 사용.",
    {"query": {"type": "string", "description": "찾을 내용. 예: 건우가 읽고 있다던 책"}},
    ["query"],
    available=_has_memory,
)
async def search_memory(args, ctx: ToolContext):
    result = await ctx.memory.search(ctx.group_id, str(args.get("query", "")))  # type: ignore[union-attr,arg-type]
    return {"facts": result.facts, "entities": result.entities, "found": not result.is_empty()}
