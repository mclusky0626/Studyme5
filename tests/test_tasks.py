import asyncio
import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent import AgentResult
from app.config import settings as base
from app.memory import MemoryContext
from app.tasks import Messenger, TaskManager, TaskStore, next_run, parse_weekdays
from app.tools import ToolError

KST = ZoneInfo("Asia/Seoul")


def ts(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=KST).timestamp()


def test_next_run_daily_weekly_once():
    now = ts(2026, 11, 21, 9, 0)  # 토요일 09:00
    assert next_run({"repeat": "daily", "time": "08:00"}, after=now) == ts(2026, 11, 22, 8, 0)
    assert next_run({"repeat": "daily", "time": "10:30"}, after=now) == ts(2026, 11, 21, 10, 30)
    assert next_run({"repeat": "weekly", "time": "08:00", "weekdays": ["월", "수"]}, after=now) == ts(2026, 11, 23, 8, 0)
    assert next_run({"repeat": "once", "at": now - 10}, after=now) is None
    assert parse_weekdays(["월요일", "금", 6]) == [0, 4, 6]


class FakeMessenger:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, channel_id, text, reply_to=None, user_id=None):
        self.sent.append(text)
        return len(self.sent)

    async def edit(self, channel_id, message_id, text):
        self.edits.append(text)


def make(runner, **over):
    s = dataclasses.replace(base, tasks_enabled=True, task_daily_limit=3, task_max_scheduled=2, **over)
    fm = FakeMessenger()
    return TaskManager(s, TaskStore(lambda: None), Messenger(send=fm.send, edit=fm.edit), runner), fm


KW = dict(user_id=1, user_name="건우", channel_id=10, group_id="guild-1", is_dm=False, origin_message_id=99)


async def test_background_task_runs_and_reports():
    async def runner(task, progress, is_cancelled):
        await progress(1, "web_search(카스미)")
        return AgentResult(text="보고서 본문", steps=1, tokens=1234, seconds=2)

    tm, fm = make(runner)
    task = await tm.start(goal="카스미 대사 조사", **KW)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if (await tm.store.get(task["id"]))["status"] == "done":
            break
    done = await tm.store.get(task["id"])
    assert done["status"] == "done" and done["tokens"] == 1234
    assert any("보고서 본문" in s for s in fm.sent)
    assert any("완료" in e for e in fm.edits)


async def test_limits_daily_and_active():
    gate = asyncio.Event()

    async def runner(task, progress, is_cancelled):
        await gate.wait()
        return AgentResult(text="ok")

    tm, _ = make(runner)
    await tm.start(goal="첫 번째 작업", **KW)
    await tm.start(goal="두 번째 작업", **KW)
    with pytest.raises(ToolError):  # 동시에 2개까지
        await tm.start(goal="세 번째 작업", **KW)
    gate.set()
    await asyncio.sleep(0.05)
    await tm.start(goal="세 번째 작업", **KW)  # 하루 3회까지
    await asyncio.sleep(0.05)
    with pytest.raises(ToolError):
        await tm.start(goal="네 번째 작업", **KW)


async def test_cancel_running_task_is_respected():
    started = asyncio.Event()

    async def runner(task, progress, is_cancelled):
        started.set()
        for _ in range(100):
            await progress(1, "x")  # 진행 저장이 취소 상태를 덮어쓰면 안 된다
            if await is_cancelled():
                return AgentResult(text="", cancelled=True)
            await asyncio.sleep(0.01)
        return AgentResult(text="끝까지 실행됨")

    tm, fm = make(runner)
    task = await tm.start(goal="오래 걸리는 작업", **KW)
    await started.wait()
    assert await tm.cancel(task["id"], user_id=1)
    await asyncio.sleep(0.1)
    assert (await tm.store.get(task["id"]))["status"] == "cancelled"
    assert not any("끝까지 실행됨" in s for s in fm.sent)


async def test_scheduled_task_fires_and_reschedules():
    runs = []

    async def runner(task, progress, is_cancelled):
        runs.append(task["goal"])
        return AgentResult(text="아침 브리핑")

    tm, fm = make(runner)
    task = await tm.schedule(goal="아침 브리핑", schedule={"repeat": "daily", "time": "08:00"}, **KW)
    first = task["next_run"]
    assert await tm.tick(now=first + 1) == 1
    await asyncio.sleep(0.05)
    t = await tm.store.get(task["id"])
    assert runs == ["아침 브리핑"] and t["status"] == "scheduled" and t["next_run"] > first
    assert t["runs"] == 1
    assert await tm.tick(now=first + 2) == 0  # 같은 회차 중복 실행 없음
    with pytest.raises(ToolError):
        for i in range(3):
            await tm.schedule(goal=f"추가 {i}", schedule={"repeat": "daily", "time": "09:00"}, **KW)


def test_memory_merge_personal():
    server = MemoryContext(facts=["A", "B"], entities=["x"])
    personal = MemoryContext(facts=["B", "C"], entities=["y"])
    server.merge_personal(personal)
    assert server.personal_facts == ["C"] and server.personal_entities == ["y"]
