"""백그라운드 작업 · 예약 작업 도구."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import ToolContext, ToolError, tool

KST = ZoneInfo("Asia/Seoul")


def _ok(ctx: ToolContext) -> bool:
    return (ctx.tasks is not None and ctx.channel_id is not None and ctx.user_id is not None
            and ctx.settings.tasks_enabled)


def _common(ctx: ToolContext) -> dict:
    return dict(user_id=ctx.user_id, user_name=ctx.user_name or str(ctx.user_id), channel_id=ctx.channel_id,
                group_id=ctx.group_id or "", is_dm=ctx.is_dm, origin_message_id=ctx.message_id)


def _summary(t: dict) -> dict:
    from ..tasks import describe_schedule

    out = {"id": t["id"], "status": t["status"], "goal": t["goal"][:120]}
    if t.get("kind") == "scheduled":
        out["schedule"] = describe_schedule(t["schedule"])
        if t.get("next_run"):
            out["next_run_kst"] = datetime.fromtimestamp(t["next_run"], KST).strftime("%m/%d %H:%M")
    if t["status"] == "running":
        out["progress"] = f"{t.get('steps', 0)}단계 · {t.get('progress', '')}"
    return out


@tool(
    "start_background_task",
    "여러 번 검색하고 페이지를 읽어야 하는 큰 조사·정리 작업을 백그라운드로 맡긴다. 끝나면 이 채널에 결과를 따로 올린다. "
    "사용자가 '조사해서 알려줘', '자세히 찾아봐', '정리해서 나중에 줘' 처럼 시간이 걸려도 되는 깊은 작업을 원할 때 사용. "
    "간단한 질문은 쓰지 말고 바로 답한다. 호출 후에는 '작업을 시작했다'고만 짧게 답한다.",
    {"goal": {"type": "string", "description": "작업 목표와 원하는 결과 형식을 구체적으로. 예: 'OO 비교표와 예문 10개'"}},
    ["goal"],
    available=_ok,
)
async def start_background_task(args, ctx: ToolContext):
    goal = str(args.get("goal", "")).strip()
    if len(goal) < 4:
        raise ToolError("작업 목표가 너무 짧습니다")
    task = await ctx.tasks.start(goal=goal, **_common(ctx))  # type: ignore[union-attr]
    s = ctx.settings
    return {"task_id": task["id"], "status": "queued",
            "limits": f"최대 {s.task_max_steps}단계 · {s.task_max_seconds // 60}분",
            "note": "결과는 끝나는 대로 이 채널에 답장으로 올라간다"}


@tool(
    "schedule_task",
    "정해진 시각에 실행할 작업을 예약한다 (예: 매일 아침 8시 날씨·뉴스 요약, 금요일 저녁 주말 영화 추천). "
    "단순히 문구만 알려 주는 건 set_reminder, 그때 조사·정리가 필요하면 이 도구.",
    {
        "goal": {"type": "string", "description": "그 시각에 할 일. 예: 서울 날씨와 주요 뉴스 3개 요약"},
        "repeat": {"type": "string", "enum": ["once", "daily", "weekly"]},
        "time": {"type": "string", "description": "한국 시각 HH:MM (daily/weekly)"},
        "date": {"type": "string", "description": "once 일 때 YYYY-MM-DD (생략하면 오늘, 지났으면 내일)"},
        "weekdays": {"type": "array", "items": {"type": "string"}, "description": "weekly 일 때 요일. 예: ['월','수','금']"},
    },
    ["goal", "repeat", "time"],
    available=_ok,
)
async def schedule_task(args, ctx: ToolContext):
    from ..tasks import describe_schedule

    repeat = str(args.get("repeat", "once"))
    hhmm = str(args.get("time", "")).strip()
    try:
        hh, mm = (int(x) for x in hhmm.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception as exc:  # noqa: BLE001
        raise ToolError("time 은 HH:MM 형식이어야 합니다") from exc
    schedule: dict = {"repeat": repeat, "time": f"{hh:02d}:{mm:02d}"}
    if repeat == "once":
        now = datetime.now(KST)
        day = datetime.fromisoformat(args["date"]).date() if args.get("date") else now.date()
        at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=KST)
        if not args.get("date") and at <= now:
            at = at + timedelta(days=1)
        schedule["at"] = at.timestamp()
    elif repeat == "weekly":
        from ..tasks import parse_weekdays

        schedule["weekdays"] = parse_weekdays(args.get("weekdays"))
        if not schedule["weekdays"]:
            raise ToolError("weekly 는 요일(weekdays)이 필요합니다")
    task = await ctx.tasks.schedule(goal=str(args.get("goal", "")), schedule=schedule, **_common(ctx))  # type: ignore[union-attr]
    return {"task_id": task["id"], "schedule": describe_schedule(schedule),
            "next_run_kst": datetime.fromtimestamp(task["next_run"], KST).strftime("%Y-%m-%d %H:%M")}


@tool("list_tasks", "이 사용자의 백그라운드·예약 작업 목록(진행 중, 예약, 최근 완료)을 보여준다.", available=_ok)
async def list_tasks(args, ctx: ToolContext):
    tasks = await ctx.tasks.list_for(ctx.user_id)  # type: ignore[union-attr,arg-type]
    active = [t for t in tasks if t["status"] in {"queued", "running", "scheduled", "cancel_requested"}]
    recent = [t for t in tasks if t not in active][:5]
    return {"active": [_summary(t) for t in active], "recent": [_summary(t) for t in recent]}


@tool(
    "cancel_task",
    "백그라운드 작업이나 예약 작업을 취소한다. id 는 list_tasks 로 확인.",
    {"task_id": {"type": "string"}},
    ["task_id"],
    available=_ok,
)
async def cancel_task(args, ctx: ToolContext):
    ok = await ctx.tasks.cancel(str(args.get("task_id", "")).strip(), user_id=ctx.user_id)  # type: ignore[union-attr]
    return {"cancelled": ok}
