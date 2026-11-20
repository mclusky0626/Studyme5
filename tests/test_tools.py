import time

import pytest

from app.config import settings
from app.gemini_resilience import RateLimiter
from app.tools import ToolContext, ToolError, run_tool
from app.tools.basic import calculate_expression
from app.tools.reminders import ReminderStore


def ctx(**kw):
    return ToolContext(settings=settings, genai_client=None, limiter=RateLimiter(0), http=None, **kw)


def test_calculator_math():
    assert calculate_expression("(18500*7)*1.1") == 142450
    assert calculate_expression("2^10") == 1024
    assert calculate_expression("sqrt(16) + 1,000") == 1004
    assert calculate_expression("10 ÷ 4") == 2.5
    assert calculate_expression("factorial(5)") == 120


@pytest.mark.parametrize("bad", ["__import__('os')", "open('x')", "(1).__class__", "9**99999", "a+1", "1/0"])
def test_calculator_rejects_unsafe(bad):
    with pytest.raises(ToolError):
        calculate_expression(bad)


async def test_run_tool_never_raises():
    res = await run_tool("calculate", {"expression": "import os"}, ctx())
    assert "error" in res
    res = await run_tool("no_such_tool", {}, ctx())
    assert "error" in res


async def test_date_calc_and_random():
    res = await run_tool("date_calc", {"start_date": "2026-11-21", "end_date": "2026-12-25"}, ctx())
    assert res["days_between"] == 34 and res["d_day"] == "D-34"
    res = await run_tool("date_calc", {"start_date": "2026-01-01", "add_days": 100}, ctx())
    assert res["result_date"] == "2026-04-11"
    res = await run_tool("random_pick", {"mode": "choice", "options": ["a", "b"]}, ctx())
    assert res["picked"][0] in {"a", "b"}
    res = await run_tool("random_pick", {"mode": "dice", "count": 3}, ctx())
    assert len(res["rolls"]) == 3 and all(1 <= r <= 6 for r in res["rolls"])


async def test_reminders_roundtrip():
    store = ReminderStore(lambda: None)
    c = ctx(reminders=store, channel_id=10, user_id=20)
    res = await run_tool("set_reminder", {"message": "빨래", "minutes_from_now": 5}, c)
    assert res["ok"]
    listed = await run_tool("list_reminders", {}, c)
    assert listed["reminders"][0]["message"] == "빨래"
    assert await store.pop_due() == []
    due = await store.pop_due(now=time.time() + 600)
    assert len(due) == 1 and due[0]["channel_id"] == 10
    assert await store.pop_due(now=time.time() + 600) == []  # 중복 발송 없음


async def test_reminder_tools_hidden_without_channel():
    res = await run_tool("set_reminder", {"message": "x", "minutes_from_now": 5}, ctx())
    assert "error" in res
