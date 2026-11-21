"""Gemini API 사용량 기록 · 추정 비용 계산.

Gemini API 는 API 키로 사용량/청구액을 조회하는 기능이 없어서, 봇이 모든 호출의 토큰을
직접 세어 FalkorDB(Redis)에 일별로 쌓는다. 비용은 .env 의 단가로 계산한 "추정치"다.
실제 청구액은 Google AI Studio 의 결제(Billing) 화면이 기준이다.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
KEY = "memorybot:usage:{day}"
KEEP_DAYS = 400

CATEGORY_LABEL = {
    "chat": "💬 채팅 답변",
    "memory": "🧠 기억 저장(추출)",
    "task": "📋 백그라운드 작업",
    "search": "🔎 웹 검색",
    "embedding": "🧬 임베딩",
}
FIELDS = ("req", "in", "out", "think", "cached", "grounded")


def day_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), KST).strftime("%Y%m%d")


def period_days(period: str, now: float | None = None) -> list[str]:
    today = datetime.fromtimestamp(now or time.time(), KST).date()
    if period == "today":
        start = today
    elif period == "week":
        start = today - timedelta(days=6)
    elif period == "month":
        start = today.replace(day=1)
    else:  # last30
        start = today - timedelta(days=29)
    n = (today - start).days + 1
    return [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


class UsageTracker:
    def __init__(self) -> None:
        self._redis_getter: Callable[[], Any | None] = lambda: None
        self._local: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def bind(self, redis_getter: Callable[[], Any | None]) -> None:
        self._redis_getter = redis_getter

    def _redis(self):
        try:
            return self._redis_getter()
        except Exception:  # noqa: BLE001
            return None

    async def record(self, category: str, model: str, *, input: int = 0, output: int = 0,
                     thinking: int = 0, cached: int = 0, requests: int = 1, grounded: int = 0) -> None:
        """호출 1건 기록. 실패해도 봇 동작에는 영향 없음."""
        values = {"req": requests, "in": input, "out": output, "think": thinking, "cached": cached,
                  "grounded": grounded}
        day = day_key()
        try:
            r = self._redis()
            if r is None:
                for f, v in values.items():
                    if v:
                        self._local[day][f"{category}|{model}|{f}"] += int(v)
                return
            key = KEY.format(day=day)
            pipe = r.pipeline()
            for f, v in values.items():
                if v:
                    pipe.hincrby(key, f"{category}|{model}|{f}", int(v))
            pipe.expire(key, KEEP_DAYS * 86400)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            log.debug("사용량 기록 실패: %s", exc)

    async def record_response(self, category: str, model: str, resp: Any, *, grounded: int = 0) -> None:
        u = getattr(resp, "usage_metadata", None)

        def g(name: str) -> int:
            return int(getattr(u, name, 0) or 0) if u is not None else 0

        await self.record(category, model, input=g("prompt_token_count"), output=g("candidates_token_count"),
                          thinking=g("thoughts_token_count"), cached=g("cached_content_token_count"),
                          grounded=grounded)

    async def raw(self, days: list[str]) -> dict[str, dict[str, int]]:
        """{day: {"category|model|field": n}}"""
        r = self._redis()
        out: dict[str, dict[str, int]] = {}
        for day in days:
            if r is None:
                out[day] = dict(self._local.get(day, {}))
                continue
            data = await r.hgetall(KEY.format(day=day))
            out[day] = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in (data or {}).items()}
        return out


tracker = UsageTracker()


# ------------------------------------------------------------------ 요약 / 비용
def _empty() -> dict[str, int]:
    return {f: 0 for f in FIELDS}


def summarize(raw: dict[str, dict[str, int]], prices: dict[str, float], *, month_grounded: int = 0) -> dict:
    """일별 원자료 → 카테고리/일자별 합계와 추정 비용(USD)."""
    by_cat: dict[str, dict[str, int]] = defaultdict(_empty)
    by_day: dict[str, dict[str, Any]] = {}
    for day, fields in sorted(raw.items()):
        day_tot = _empty()
        day_cost = 0.0
        for key, val in fields.items():
            try:
                cat, _model, f = key.split("|")
            except ValueError:
                continue
            by_cat[cat][f] += val
            day_tot[f] += val
        for cat, c in _per_cat(fields).items():
            day_cost += _cost(cat, c, prices)
        by_day[day] = {**day_tot, "cost_usd": round(day_cost, 6)}

    total = _empty()
    cost = 0.0
    cats = {}
    for cat, c in by_cat.items():
        cc = _cost(cat, c, prices)
        cats[cat] = {**c, "cost_usd": round(cc, 6), "label": CATEGORY_LABEL.get(cat, cat)}
        cost += cc
        for f in FIELDS:
            total[f] += c[f]
    # 검색 그라운딩: 월 무료 한도를 넘은 만큼만 과금 (월 단위 누적 기준)
    free = int(prices.get("search_free_per_month", 0))
    period_grounded = total["grounded"]
    billable = max(0, month_grounded - free) if month_grounded else max(0, period_grounded - free)
    billable = min(billable, period_grounded)
    search_cost = billable * prices.get("search_per_1k", 0) / 1000
    cost += search_cost
    return {
        "categories": cats, "days": by_day, "total": total,
        "search": {"grounded": period_grounded, "month_grounded": month_grounded, "free_per_month": free,
                   "billable": billable, "cost_usd": round(search_cost, 6)},
        "cost_usd": round(cost, 6),
    }


def _per_cat(fields: dict[str, int]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(_empty)
    for key, val in fields.items():
        try:
            cat, _model, f = key.split("|")
        except ValueError:
            continue
        out[cat][f] += val
    return out


def _cost(cat: str, c: dict[str, int], prices: dict[str, float]) -> float:
    """토큰 비용(USD). 캐시 적중 토큰은 캐시 단가, 사고 토큰은 출력 단가로 계산. 검색 요청비는 별도."""
    if cat == "embedding":
        return c["in"] * prices["embed_per_m"] / 1e6
    fresh_in = max(0, c["in"] - c["cached"])
    return (fresh_in * prices["input_per_m"] + c["cached"] * prices["cached_per_m"]
            + (c["out"] + c["think"]) * prices["output_per_m"]) / 1e6


def prices_from(settings) -> dict[str, float]:
    return {
        "input_per_m": settings.price_input_per_m, "output_per_m": settings.price_output_per_m,
        "cached_per_m": settings.price_cached_per_m, "embed_per_m": settings.price_embed_per_m,
        "search_per_1k": settings.price_search_per_1k, "search_free_per_month": settings.search_free_per_month,
    }


async def report(settings, period: str) -> dict:
    """기간 요약 + 이번 달 검색 누적(무료 한도 판단용)."""
    days = period_days(period)
    raw = await tracker.raw(days)
    month_raw = await tracker.raw(period_days("month"))
    month_grounded = sum(v for d in month_raw.values() for k, v in d.items() if k.endswith("|grounded"))
    data = summarize(raw, prices_from(settings), month_grounded=month_grounded)
    data["period"] = period
    data["from"], data["to"] = days[0], days[-1]
    return data


_krw_cache: tuple[float, float] | None = None


async def usd_krw(settings, http=None) -> float:
    """.env 의 USD_KRW 가 있으면 그 값, 없으면 환율 API(1시간 캐시), 실패하면 1,350원."""
    global _krw_cache
    if settings.usd_krw > 0:
        return settings.usd_krw
    if _krw_cache and time.time() - _krw_cache[0] < 3600:
        return _krw_cache[1]
    try:
        import httpx

        async with httpx.AsyncClient(timeout=8) as client:
            rate = float((await client.get("https://open.er-api.com/v6/latest/USD")).json()["rates"]["KRW"])
        _krw_cache = (time.time(), rate)
        return rate
    except Exception:  # noqa: BLE001
        return 1350.0


def fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else (f"{n / 1000:.1f}k" if n >= 1000 else str(n))


def format_report(data: dict, krw: float) -> list[str]:
    """디스코드/텍스트용 요약 줄."""
    lines = []
    for cat in ("chat", "memory", "task", "search", "embedding"):
        c = data["categories"].get(cat)
        if not c:
            continue
        if cat == "embedding":
            detail = f"요청 {c['req']:,} · 약 {fmt_tokens(c['in'])} 토큰"
        else:
            detail = (f"요청 {c['req']:,} · 입력 {fmt_tokens(c['in'])} · 출력 {fmt_tokens(c['out'] + c['think'])}"
                      + (f" (캐시 {fmt_tokens(c['cached'])})" if c["cached"] else ""))
        lines.append(f"{c['label']} — {detail} · ${c['cost_usd']:.4f} (약 {c['cost_usd'] * krw:,.0f}원)")
    s = data["search"]
    if s["grounded"]:
        lines.append(f"🔎 구글 검색 그라운딩 {s['grounded']:,}회 · 이번 달 {s['month_grounded']:,}/{s['free_per_month']:,}회 무료 한도"
                     + (f" · 초과분 ${s['cost_usd']:.3f}" if s["cost_usd"] else " (무료 한도 내)"))
    return lines
