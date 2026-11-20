"""리마인더: '30분 뒤에 빨래 알려줘' → 정해진 시각에 디스코드 채널(또는 DM)로 멘션.

FalkorDB(Redis) 정렬 집합에 저장하므로 봇이 재시작돼도 유지된다. DB 미연결 시엔 메모리에 보관.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import ToolContext, ToolError, tool

KEY = "memorybot:reminders"
KST = ZoneInfo("Asia/Seoul")
MAX_DAYS = 60
MAX_PER_USER = 25


class ReminderStore:
    def __init__(self, redis_getter: Callable[[], Any | None]) -> None:
        self._redis_getter = redis_getter
        self._local: dict[str, tuple[float, str]] = {}  # id -> (due, json) — DB 미연결 시

    def _redis(self):
        try:
            return self._redis_getter()
        except Exception:  # noqa: BLE001
            return None

    async def _all(self) -> list[tuple[str, float]]:
        r = self._redis()
        if r is None:
            return [(raw, due) for due, raw in self._local.values()]
        items = await r.zrange(KEY, 0, -1, withscores=True)
        return [(raw.decode() if isinstance(raw, bytes) else raw, float(score)) for raw, score in items]

    async def add(self, *, channel_id: int, user_id: int, is_dm: bool, text: str, due: float) -> dict:
        mine = [x for x in await self.list(user_id)]
        if len(mine) >= MAX_PER_USER:
            raise ToolError(f"리마인더는 한 사람당 최대 {MAX_PER_USER}개까지 가능합니다")
        item = {"id": uuid.uuid4().hex[:6], "channel_id": channel_id, "user_id": user_id,
                "is_dm": is_dm, "text": text[:300], "due": due, "created": time.time()}
        raw = json.dumps(item, ensure_ascii=False)
        r = self._redis()
        if r is None:
            self._local[item["id"]] = (due, raw)
        else:
            await r.zadd(KEY, {raw: due})
        return item

    async def list(self, user_id: int) -> list[dict]:
        out = []
        for raw, _ in await self._all():
            item = json.loads(raw)
            if item["user_id"] == user_id:
                out.append(item)
        return sorted(out, key=lambda x: x["due"])

    async def cancel(self, user_id: int, reminder_id: str) -> bool:
        for raw, _ in await self._all():
            item = json.loads(raw)
            if item["id"] == reminder_id and item["user_id"] == user_id:
                r = self._redis()
                if r is None:
                    self._local.pop(reminder_id, None)
                else:
                    await r.zrem(KEY, raw)
                return True
        return False

    async def pop_due(self, now: float | None = None) -> list[dict]:
        """기한이 된 리마인더를 꺼낸다 (꺼낸 것만 반환하므로 중복 발송 없음)."""
        now = now or time.time()
        r = self._redis()
        due: list[dict] = []
        if r is None:
            for rid, (ts, raw) in list(self._local.items()):
                if ts <= now:
                    self._local.pop(rid, None)
                    due.append(json.loads(raw))
            return due
        for raw in await r.zrangebyscore(KEY, "-inf", now):
            if await r.zrem(KEY, raw):
                due.append(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
        return due


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d %H:%M")


def _has_store(ctx: ToolContext) -> bool:
    return ctx.reminders is not None and ctx.channel_id is not None and ctx.user_id is not None


@tool(
    "set_reminder",
    "사용자가 나중에 알려 달라고 할 때 리마인더를 예약한다. 시각이 되면 이 채널에서 그 사용자를 멘션해 알린다. "
    "상대 시간이면 minutes_from_now, 특정 시각이면 at(한국 시각) 중 하나를 쓴다.",
    {
        "message": {"type": "string", "description": "알려줄 내용. 예: 빨래 꺼내기"},
        "minutes_from_now": {"type": "number", "description": "지금부터 몇 분 뒤 (예: 30, 1시간이면 60)"},
        "at": {"type": "string", "description": "한국 시각 'YYYY-MM-DD HH:MM'"},
    },
    ["message"],
    available=_has_store,
)
async def set_reminder(args, ctx: ToolContext):
    now = time.time()
    if args.get("minutes_from_now") is not None:
        due = now + float(args["minutes_from_now"]) * 60
    elif args.get("at"):
        try:
            due = datetime.fromisoformat(str(args["at"]).strip().replace("T", " ")).replace(tzinfo=KST).timestamp()
        except ValueError as exc:
            raise ToolError("at 은 'YYYY-MM-DD HH:MM' 형식이어야 합니다") from exc
    else:
        raise ToolError("minutes_from_now 또는 at 중 하나가 필요합니다")
    if due < now + 30:
        raise ToolError("이미 지났거나 너무 가까운 시각입니다")
    if due > now + MAX_DAYS * 86400:
        raise ToolError(f"최대 {MAX_DAYS}일 뒤까지만 예약할 수 있습니다")
    item = await ctx.reminders.add(channel_id=ctx.channel_id, user_id=ctx.user_id, is_dm=ctx.is_dm,  # type: ignore[union-attr]
                                   text=str(args.get("message", "")).strip() or "알림", due=due)
    return {"ok": True, "id": item["id"], "when_kst": _fmt(due), "message": item["text"]}


@tool("list_reminders", "이 사용자가 예약해 둔 리마인더 목록을 보여준다.", available=_has_store)
async def list_reminders(args, ctx: ToolContext):
    items = await ctx.reminders.list(ctx.user_id)  # type: ignore[union-attr,arg-type]
    return {"reminders": [{"id": x["id"], "when_kst": _fmt(x["due"]), "message": x["text"]} for x in items]}


@tool(
    "cancel_reminder",
    "예약된 리마인더를 취소한다. id 는 list_reminders 로 확인.",
    {"reminder_id": {"type": "string"}},
    ["reminder_id"],
    available=_has_store,
)
async def cancel_reminder(args, ctx: ToolContext):
    ok = await ctx.reminders.cancel(ctx.user_id, str(args.get("reminder_id", "")).strip())  # type: ignore[union-attr,arg-type]
    return {"cancelled": ok}
