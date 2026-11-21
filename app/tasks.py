"""백그라운드 작업 · 예약 작업 관리.

- 작업 기록은 FalkorDB(Redis) 해시에 저장 → 관리 웹에서 보이고, 봇 재시작 후에도 예약이 유지된다.
- 실행은 이 노트북(봇 컨테이너)의 asyncio 워커가 맡고, 생각은 Gemini(flash-lite)가 한다.
- 조사 한도: 작업당 단계·시간·토큰, 동시 실행 수, 사용자별 하루 횟수, 사용자별 예약 개수.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .agent import AgentLimits, run_agent
from .config import Settings
from .tools import ToolError

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
TASKS_KEY = "memorybot:tasks"
COUNT_KEY = "memorybot:taskcount:{user}:{day}"
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
ACTIVE = {"queued", "running"}
MAX_PER_USER_ACTIVE = 2
KEEP_FINISHED = 200


# ------------------------------------------------------------------ 예약 시각 계산
def parse_weekdays(value: Any) -> list[int]:
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).replace(",", " ").split()
    out = []
    for v in items:
        v = str(v).strip()
        if v.isdigit() and 0 <= int(v) <= 6:
            out.append(int(v))
        elif v[:1] in WEEKDAYS:
            out.append(WEEKDAYS.index(v[:1]))
    return sorted(set(out))


def next_run(schedule: dict[str, Any], after: float | None = None) -> float | None:
    """다음 실행 시각(epoch). 1회성이고 이미 지났으면 None."""
    after = after if after is not None else time.time()
    kind = schedule.get("repeat", "once")
    if kind == "once":
        at = float(schedule["at"])
        return at if at > after else None
    hh, mm = (int(x) for x in str(schedule["time"]).split(":"))
    base = datetime.fromtimestamp(after, KST)
    days = parse_weekdays(schedule.get("weekdays")) if kind == "weekly" else list(range(7))
    for offset in range(0, 8):
        day = base + timedelta(days=offset)
        cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand.timestamp() > after and cand.weekday() in days:
            return cand.timestamp()
    return None


def describe_schedule(schedule: dict[str, Any]) -> str:
    kind = schedule.get("repeat", "once")
    if kind == "once":
        return datetime.fromtimestamp(float(schedule["at"]), KST).strftime("%m/%d %H:%M 1회")
    if kind == "daily":
        return f"매일 {schedule['time']}"
    days = "·".join(WEEKDAYS[d] for d in parse_weekdays(schedule.get("weekdays")))
    return f"매주 {days} {schedule['time']}"


# ------------------------------------------------------------------ 저장소
class TaskStore:
    def __init__(self, redis_getter: Callable[[], Any | None]) -> None:
        self._redis_getter = redis_getter
        self._local: dict[str, dict] = {}
        self._local_counts: dict[str, int] = {}

    def _redis(self):
        try:
            return self._redis_getter()
        except Exception:  # noqa: BLE001
            return None

    async def save(self, task: dict) -> None:
        r = self._redis()
        if r is None:
            self._local[task["id"]] = dict(task)
        else:
            await r.hset(TASKS_KEY, task["id"], json.dumps(task, ensure_ascii=False))

    async def get(self, task_id: str) -> dict | None:
        r = self._redis()
        if r is None:
            t = self._local.get(task_id)
            return dict(t) if t else None
        raw = await r.hget(TASKS_KEY, task_id)
        return json.loads(raw) if raw else None

    async def all(self) -> list[dict]:
        r = self._redis()
        if r is None:
            return [dict(t) for t in self._local.values()]
        return [json.loads(v) for v in (await r.hgetall(TASKS_KEY)).values()]

    async def delete(self, task_id: str) -> None:
        r = self._redis()
        if r is None:
            self._local.pop(task_id, None)
        else:
            await r.hdel(TASKS_KEY, task_id)

    async def incr_daily(self, user_id: int) -> int:
        day = datetime.now(KST).strftime("%Y%m%d")
        key = COUNT_KEY.format(user=user_id, day=day)
        r = self._redis()
        if r is None:
            self._local_counts[key] = self._local_counts.get(key, 0) + 1
            return self._local_counts[key]
        n = await r.incr(key)
        await r.expire(key, 2 * 86400)
        return int(n)

    async def daily_count(self, user_id: int) -> int:
        key = COUNT_KEY.format(user=user_id, day=datetime.now(KST).strftime("%Y%m%d"))
        r = self._redis()
        if r is None:
            return self._local_counts.get(key, 0)
        return int(await r.get(key) or 0)

    async def prune(self) -> None:
        """끝난 작업 기록이 너무 많아지면 오래된 것부터 정리."""
        done = sorted((t for t in await self.all() if t["status"] in {"done", "failed", "cancelled"}),
                      key=lambda t: t.get("finished") or 0)
        for t in done[:-KEEP_FINISHED] if len(done) > KEEP_FINISHED else []:
            await self.delete(t["id"])


# ------------------------------------------------------------------ 관리자
@dataclass
class Messenger:
    """디스코드 전송 함수 묶음 (테스트에서는 가짜로 대체)."""
    send: Callable[..., Awaitable[int | None]]          # (channel_id, text, reply_to=None, user_id=None) -> message_id
    edit: Callable[..., Awaitable[None]]                # (channel_id, message_id, text)


class TaskManager:
    def __init__(self, settings: Settings, store: TaskStore, messenger: Messenger,
                 agent_runner: Callable[[dict], Awaitable[Any]]) -> None:
        self.s = settings
        self.store = store
        self.messenger = messenger
        self._agent_runner = agent_runner          # task -> AgentResult
        self._sem = asyncio.Semaphore(max(1, settings.task_max_concurrent))
        self._running: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None

    # -------------------------------------------------------------- 생성
    async def _check_limits(self, user_id: int) -> None:
        if not self.s.tasks_enabled:
            raise ToolError("백그라운드 작업 기능이 꺼져 있습니다 (TASKS_ENABLED=false)")
        tasks = await self.store.all()
        active = [t for t in tasks if t["user_id"] == user_id and t["status"] in ACTIVE]
        if len(active) >= MAX_PER_USER_ACTIVE:
            raise ToolError(f"진행 중인 작업이 이미 {len(active)}개 있습니다. 끝난 뒤에 다시 맡겨 주세요.")
        if await self.store.daily_count(user_id) >= self.s.task_daily_limit:
            raise ToolError(f"오늘 작업 한도({self.s.task_daily_limit}회)를 다 썼습니다. 내일 다시 맡겨 주세요.")

    def _base(self, *, goal: str, user_id: int, user_name: str, channel_id: int, group_id: str,
              is_dm: bool, origin_message_id: int | None) -> dict:
        return {
            "id": uuid.uuid4().hex[:6], "goal": goal.strip()[:1000], "user_id": user_id,
            "user_name": user_name, "channel_id": channel_id, "group_id": group_id, "is_dm": is_dm,
            "origin_message_id": origin_message_id, "created": time.time(), "steps": 0, "tokens": 0,
            "runs": 0, "progress": "", "result_preview": "", "error": None,
        }

    async def start(self, **kw) -> dict:
        await self._check_limits(kw["user_id"])
        task = self._base(**kw) | {"kind": "background", "status": "queued"}
        await self.store.save(task)
        await self.store.incr_daily(task["user_id"])
        self._spawn(task)
        log.info("📋 작업 #%s 접수 (%s): %s", task["id"], task["user_name"], task["goal"][:80])
        return task

    async def schedule(self, *, schedule: dict[str, Any], **kw) -> dict:
        if not self.s.tasks_enabled:
            raise ToolError("예약 작업 기능이 꺼져 있습니다 (TASKS_ENABLED=false)")
        mine = [t for t in await self.store.all()
                if t["user_id"] == kw["user_id"] and t["status"] == "scheduled"]
        if len(mine) >= self.s.task_max_scheduled:
            raise ToolError(f"예약 작업은 한 사람당 {self.s.task_max_scheduled}개까지입니다")
        nr = next_run(schedule)
        if nr is None:
            raise ToolError("이미 지난 시각입니다")
        task = self._base(**kw) | {"kind": "scheduled", "status": "scheduled", "schedule": schedule,
                                    "next_run": nr}
        await self.store.save(task)
        log.info("🗓️ 예약 작업 #%s (%s): %s", task["id"], describe_schedule(schedule), task["goal"][:80])
        return task

    async def cancel(self, task_id: str, *, user_id: int | None = None) -> bool:
        task = await self.store.get(task_id)
        if not task or (user_id is not None and task["user_id"] != user_id):
            return False
        if task["status"] in {"done", "failed", "cancelled"}:
            return False
        task["status"] = "cancelled" if task["status"] in {"queued", "scheduled"} else "cancel_requested"
        task["finished"] = time.time() if task["status"] == "cancelled" else None
        await self.store.save(task)
        return True

    async def list_for(self, user_id: int) -> list[dict]:
        tasks = [t for t in await self.store.all() if t["user_id"] == user_id]
        return sorted(tasks, key=lambda t: t.get("created", 0), reverse=True)

    # -------------------------------------------------------------- 실행
    def _spawn(self, task: dict) -> None:
        t = asyncio.get_running_loop().create_task(self._execute(task["id"]))
        self._running.add(t)
        t.add_done_callback(self._running.discard)

    async def _is_cancelled(self, task_id: str) -> bool:
        t = await self.store.get(task_id)
        return not t or t["status"] in {"cancel_requested", "cancelled"}

    async def _update(self, task_id: str, **fields) -> dict | None:
        """최신 기록을 다시 읽어 필드만 갱신 (그 사이 들어온 취소·상태 변경을 덮어쓰지 않음)."""
        task = await self.store.get(task_id)
        if task is None:
            return None
        task.update(fields)
        await self.store.save(task)
        return task

    async def _execute(self, task_id: str, *, run_of: dict | None = None) -> None:
        """task_id 작업 실행. 예약 작업이면 run_of 에 그 시점의 예약 기록이 들어온다."""
        async with self._sem:
            task = run_of or await self.store.get(task_id)
            if not task or (run_of is None and task["status"] != "queued"):
                return
            label = f"작업 #{task['id']}"
            if run_of is None:
                task = await self._update(task_id, status="running", started=time.time()) or task
            status_msg = await self._safe_send(task, f"🔄 **{label}** 시작 — {task['goal'][:120]}")
            last_edit = 0.0

            async def progress(step: int, desc: str) -> None:
                nonlocal last_edit
                await self._update(task_id, steps=step, progress=desc)
                if status_msg and time.monotonic() - last_edit > 3:
                    last_edit = time.monotonic()
                    await self._safe_edit(task, status_msg,
                                          f"🔄 **{label}** {step}/{self.s.task_max_steps}단계 · {desc}")

            try:
                result = await self._agent_runner(task, progress, lambda: self._is_cancelled(task_id))
            except Exception as exc:  # noqa: BLE001
                log.exception("%s 실패", label)
                await self._finish(task, status_msg, error=f"{type(exc).__name__}: {str(exc)[:200]}")
                return
            await self._finish(task, status_msg, result=result)

    async def _finish(self, task: dict, status_msg: int | None, *, result=None, error: str | None = None) -> None:
        label = f"작업 #{task['id']}"
        scheduled = task.get("kind") == "scheduled"
        latest = await self.store.get(task["id"]) or task
        common = {"runs": latest.get("runs", 0) + 1, "last_run": time.time()}
        cancelled = result is not None and result.cancelled
        if error or cancelled:
            fields = dict(common, error=error)
            if not scheduled:
                fields.update(status="cancelled" if cancelled else "failed", finished=time.time())
            await self._update(task["id"], **fields)
            await self._safe_edit(task, status_msg, f"{'⛔ 취소됨' if cancelled else '⚠️ 실패'} — **{label}**")
            if error:
                await self._safe_send(task, f"<@{task['user_id']}> **{label}**을(를) 끝내지 못했어요: {error}",
                                      mention=True)
            return
        fields = dict(common, steps=result.steps, tokens=latest.get("tokens", 0) + result.tokens,
                      result_preview=result.text[:1500], error=None)
        if not scheduled:
            fields.update(status="done", finished=time.time())
        await self._update(task["id"], **fields)
        limit_note = f" · 한도({result.stopped_by})로 마무리" if result.stopped_by else ""
        await self._safe_edit(task, status_msg,
                              f"✅ **{label}** 완료 · {result.steps}단계 · {result.tokens:,}토큰 · "
                              f"{result.seconds:.0f}초{limit_note}")
        header = f"<@{task['user_id']}> 📄 **{label}** 결과 — {task['goal'][:80]}\n\n"
        await self._safe_send(task, header + result.text, mention=True)
        await self.store.prune()

    async def _safe_send(self, task: dict, text: str, *, mention: bool = False) -> int | None:
        try:
            return await self.messenger.send(task["channel_id"], text,
                                             reply_to=task.get("origin_message_id"),
                                             user_id=task["user_id"] if mention else None)
        except Exception as exc:  # noqa: BLE001
            log.warning("작업 메시지 전송 실패 #%s: %s", task["id"], exc)
            return None

    async def _safe_edit(self, task: dict, message_id: int | None, text: str) -> None:
        if not message_id:
            return
        try:
            await self.messenger.edit(task["channel_id"], message_id, text)
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------- 예약 루프 / 복구
    async def recover(self) -> None:
        """재시작 시: 실행 중이던 작업은 실패 처리 후 알림, 대기 중이던 작업은 다시 실행."""
        for task in await self.store.all():
            if task["status"] in {"running", "cancel_requested"}:
                task.update(status="failed", error="봇 재시작으로 중단됨", finished=time.time())
                await self.store.save(task)
                await self._safe_send(task, f"<@{task['user_id']}> 봇이 재시작되어 **작업 #{task['id']}**이 "
                                            f"중단됐어요. 필요하면 다시 맡겨 주세요.", mention=True)
            elif task["status"] == "queued":
                self._spawn(task)
            elif task["status"] == "scheduled" and task.get("next_run", 0) < time.time():
                nr = next_run(task["schedule"])
                if nr is None:
                    task.update(status="failed", error="예약 시각이 봇이 꺼져 있는 동안 지나감", finished=time.time())
                else:
                    task["next_run"] = nr
                await self.store.save(task)

    async def tick(self, now: float | None = None) -> int:
        """예약 시각이 된 작업을 실행. 실행한 개수 반환."""
        now = now or time.time()
        fired = 0
        for task in await self.store.all():
            if task["status"] != "scheduled" or task.get("next_run", 0) > now:
                continue
            nr = next_run(task["schedule"], after=now)
            if nr is None:
                task["status"] = "done"
                task["finished"] = now
            task["next_run"] = nr
            await self.store.save(task)
            run = dict(task)
            t = asyncio.get_running_loop().create_task(self._execute(task["id"], run_of=run))
            self._running.add(t)
            t.add_done_callback(self._running.discard)
            fired += 1
        return fired

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("예약 작업 루프 오류: %s", exc)
            await asyncio.sleep(30)

    def agent_limits(self) -> AgentLimits:
        return AgentLimits(max_steps=self.s.task_max_steps, max_seconds=self.s.task_max_seconds,
                           max_tokens=self.s.task_max_tokens)


__all__ = ["TaskManager", "TaskStore", "Messenger", "next_run", "describe_schedule", "parse_weekdays", "run_agent"]
