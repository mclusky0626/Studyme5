"""외부 API 없이 동작하는 기본 도구: 계산, 시간, 날짜 계산, 랜덤."""

from __future__ import annotations

import ast
import math
import operator
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ToolContext, ToolError, tool

KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

# ------------------------------------------------------------------ 안전한 계산기 (eval 사용 안 함)
_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x), "abs": abs, "round": round,
    "floor": math.floor, "ceil": math.ceil, "log": math.log, "log10": math.log10, "log2": math.log2,
    "ln": math.log, "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "radians": math.radians,
    "degrees": math.degrees, "factorial": math.factorial, "gcd": math.gcd, "lcm": math.lcm,
    "min": min, "max": max, "sum": lambda *a: sum(a), "avg": lambda *a: sum(a) / len(a),
    "comb": math.comb, "perm": math.perm,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ToolError("지수가 너무 큽니다")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        args = [_eval(a) for a in node.args]
        if node.func.id == "factorial" and args and args[0] > 1000:
            raise ToolError("팩토리얼 입력이 너무 큽니다")
        return _FUNCS[node.func.id](*args)
    raise ToolError("지원하지 않는 수식입니다")


def calculate_expression(expression: str):
    expr = (expression.replace("×", "*").replace("÷", "/").replace("^", "**")
            .replace(",", "").replace("−", "-"))
    if len(expr) > 300:
        raise ToolError("수식이 너무 깁니다")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"수식을 해석할 수 없습니다: {expression}") from exc
    try:
        value = _eval(tree)
    except ZeroDivisionError as exc:
        raise ToolError("0으로 나눌 수 없습니다") from exc
    except (ValueError, OverflowError, TypeError) as exc:
        raise ToolError(f"계산 오류: {exc}") from exc
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            value = int(value)
        else:
            value = round(value, 10)
    return value


@tool(
    "calculate",
    "수식을 정확히 계산한다. 사칙연산, 거듭제곱(**), 나머지(%), sqrt, log, sin, factorial, comb, avg 등 지원. "
    "암산하지 말고 숫자 계산이 필요하면 항상 이 도구를 쓴다. 퍼센트는 0.15처럼 소수로 바꿔 넣는다.",
    {"expression": {"type": "string", "description": "파이썬식 수식. 예: (1200*3)*0.9, sqrt(2)*10, 37**2"}},
    ["expression"],
)
async def calculate(args, ctx: ToolContext):
    expression = str(args.get("expression", ""))
    return {"expression": expression, "result": calculate_expression(expression)}


# ------------------------------------------------------------------ 시간 / 날짜
def _tz(name: str | None):
    if not name:
        return KST
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ToolError(f"알 수 없는 시간대: {name}") from exc


@tool(
    "get_current_time",
    "특정 시간대의 현재 날짜와 시각을 알려준다. 해외 시각이 궁금할 때 사용 (한국 시각은 이미 알고 있음).",
    {"timezone": {"type": "string", "description": "IANA 시간대. 예: America/New_York, Europe/London, Asia/Tokyo"}},
)
async def get_current_time(args, ctx: ToolContext):
    tz = _tz(args.get("timezone"))
    now = datetime.now(tz)
    return {"timezone": str(tz), "datetime": now.strftime("%Y-%m-%d %H:%M"),
            "weekday": WEEKDAYS[now.weekday()], "utc_offset": now.strftime("%z")}


def _parse_date(value: str | None) -> date:
    if not value:
        return datetime.now(KST).date()
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise ToolError(f"날짜 형식은 YYYY-MM-DD 이어야 합니다: {value}") from exc


@tool(
    "date_calc",
    "날짜 계산: 두 날짜 사이 일수(D-day, 며칠 남았는지/지났는지) 또는 날짜에 일수를 더하거나 뺀 날짜와 요일을 구한다.",
    {
        "start_date": {"type": "string", "description": "YYYY-MM-DD. 생략하면 오늘(한국)"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD. 일수 차이를 구할 때"},
        "add_days": {"type": "integer", "description": "start_date 에 더할 일수 (음수면 뺌)"},
    },
)
async def date_calc(args, ctx: ToolContext):
    start = _parse_date(args.get("start_date"))
    out = {"start_date": start.isoformat(), "start_weekday": WEEKDAYS[start.weekday()]}
    if args.get("end_date"):
        end = _parse_date(args["end_date"])
        diff = (end - start).days
        out.update(end_date=end.isoformat(), end_weekday=WEEKDAYS[end.weekday()], days_between=diff,
                   d_day=f"D-{diff}" if diff > 0 else ("D-Day" if diff == 0 else f"D+{-diff}"),
                   weeks=round(diff / 7, 1))
    if args.get("add_days") is not None:
        days = int(args["add_days"])
        if abs(days) > 365 * 200:
            raise ToolError("범위가 너무 큽니다")
        result = start + timedelta(days=days)
        out.update(add_days=days, result_date=result.isoformat(), result_weekday=WEEKDAYS[result.weekday()])
    return out


# ------------------------------------------------------------------ 랜덤
_rng = random.SystemRandom()


@tool(
    "random_pick",
    "공정한 무작위 뽑기. 메뉴 골라주기/제비뽑기(choice), 주사위(dice), 동전 던지기(coin), 범위 내 숫자(number), 순서 섞기(shuffle).",
    {
        "mode": {"type": "string", "enum": ["choice", "dice", "coin", "number", "shuffle"]},
        "options": {"type": "array", "items": {"type": "string"}, "description": "choice/shuffle 대상 목록"},
        "count": {"type": "integer", "description": "뽑을 개수 또는 주사위 개수 (기본 1)"},
        "sides": {"type": "integer", "description": "주사위 면 수 (기본 6)"},
        "min": {"type": "integer"},
        "max": {"type": "integer"},
    },
    ["mode"],
)
async def random_pick(args, ctx: ToolContext):
    mode = args.get("mode")
    count = max(1, min(int(args.get("count") or 1), 100))
    if mode == "choice":
        options = [str(o) for o in args.get("options") or [] if str(o).strip()]
        if not options:
            raise ToolError("고를 후보가 없습니다")
        picked = _rng.sample(options, min(count, len(options)))
        return {"picked": picked, "from": options}
    if mode == "shuffle":
        options = [str(o) for o in args.get("options") or []]
        _rng.shuffle(options)
        return {"order": options}
    if mode == "dice":
        sides = max(2, min(int(args.get("sides") or 6), 1000))
        rolls = [_rng.randint(1, sides) for _ in range(count)]
        return {"sides": sides, "rolls": rolls, "total": sum(rolls)}
    if mode == "coin":
        flips = [_rng.choice(["앞면", "뒷면"]) for _ in range(count)]
        return {"flips": flips}
    if mode == "number":
        lo, hi = int(args.get("min", 1)), int(args.get("max", 100))
        if lo > hi:
            lo, hi = hi, lo
        return {"min": lo, "max": hi, "numbers": [_rng.randint(lo, hi) for _ in range(count)]}
    raise ToolError(f"알 수 없는 mode: {mode}")
