"""외부 정보 도구: 날씨(Open-Meteo), 환율(open.er-api.com), 웹 검색(Gemini + Google Search)."""

from __future__ import annotations

import time
from typing import Any

from google.genai import types

from ..gemini_resilience import call_with_retry
from . import ToolContext, ToolError, tool

# WMO 날씨 코드 → 한국어
WMO = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "서리 안개",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비", 56: "약한 어는 이슬비", 57: "어는 이슬비",
    61: "약한 비", 63: "비", 65: "강한 비", 66: "약한 어는 비", 67: "어는 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈", 77: "싸락눈", 80: "약한 소나기", 81: "소나기",
    82: "강한 소나기", 85: "약한 눈보라", 86: "강한 눈보라", 95: "뇌우", 96: "우박 동반 뇌우",
    99: "강한 우박 동반 뇌우",
}


async def _geocode(ctx: ToolContext, name: str, country: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "count": 10, "language": "ko", "format": "json"}
    if country:
        params["countryCode"] = country.upper()[:2]
    r = await ctx.http.get("https://geocoding-api.open-meteo.com/v1/search", params=params)
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results and " " in name:
        return await _geocode(ctx, name.split()[0], country)
    if not results:
        raise ToolError(f"'{name}' 위치를 찾지 못했습니다. 영문 도시명으로 다시 시도하세요 (예: Seoul).")
    # 같은 이름이면 인구가 많은 곳(대도시)을 우선
    return max(results, key=lambda x: x.get("population") or 0)


@tool(
    "get_weather",
    "현재 날씨와 최대 7일 예보(기온, 체감, 강수확률, 바람)를 가져온다. "
    "location 은 반드시 영문 도시명(예: Seoul, Busan, Jeju, Tokyo). 구·동 단위는 상위 도시명으로. "
    "한국 도시는 country_code='KR'.",
    {
        "location": {"type": "string", "description": "영문 도시명. 예: Seoul"},
        "country_code": {"type": "string", "description": "ISO 2자리 국가 코드. 예: KR, JP, US"},
        "days": {"type": "integer", "description": "예보 일수 1~7 (기본 2: 오늘·내일)"},
    },
    ["location"],
)
async def get_weather(args, ctx: ToolContext):
    place = await _geocode(ctx, str(args.get("location", "")).strip(), args.get("country_code"))
    days = max(1, min(int(args.get("days") or 2), 7))
    r = await ctx.http.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
        "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
        "wind_speed_unit": "ms",
    })
    r.raise_for_status()
    data = r.json()
    cur = data.get("current", {})
    daily = data.get("daily", {})
    forecast = []
    for i, day in enumerate(daily.get("time", [])):
        forecast.append({
            "date": day,
            "weather": WMO.get(daily["weather_code"][i], "알 수 없음"),
            "min_c": daily["temperature_2m_min"][i], "max_c": daily["temperature_2m_max"][i],
            "rain_chance_pct": daily["precipitation_probability_max"][i],
            "uv_max": daily.get("uv_index_max", [None] * (i + 1))[i],
        })
    return {
        "location": " ".join(x for x in [place.get("name"), place.get("admin1"), place.get("country")] if x),
        "local_time": cur.get("time"),
        "current": {
            "weather": WMO.get(cur.get("weather_code"), "알 수 없음"),
            "temp_c": cur.get("temperature_2m"), "feels_like_c": cur.get("apparent_temperature"),
            "humidity_pct": cur.get("relative_humidity_2m"), "precip_mm": cur.get("precipitation"),
            "wind_ms": cur.get("wind_speed_10m"),
        },
        "forecast": forecast,
        "source": "Open-Meteo",
    }


_fx_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@tool(
    "convert_currency",
    "환율로 금액을 변환한다 (하루 1회 갱신되는 기준 환율). 통화는 ISO 코드: KRW, USD, JPY, EUR, CNY 등.",
    {
        "amount": {"type": "number"},
        "from_currency": {"type": "string", "description": "예: USD"},
        "to_currency": {"type": "string", "description": "예: KRW"},
    },
    ["amount", "from_currency", "to_currency"],
)
async def convert_currency(args, ctx: ToolContext):
    src = str(args.get("from_currency", "")).upper().strip()
    dst = str(args.get("to_currency", "")).upper().strip()
    amount = float(args.get("amount", 0))
    cached = _fx_cache.get(src)
    if not cached or time.time() - cached[0] > 3600:
        r = await ctx.http.get(f"https://open.er-api.com/v6/latest/{src}")
        data = r.json()
        if data.get("result") != "success":
            raise ToolError(f"지원하지 않는 통화: {src}")
        cached = (time.time(), data)
        _fx_cache[src] = cached
    data = cached[1]
    rate = data["rates"].get(dst)
    if rate is None:
        raise ToolError(f"지원하지 않는 통화: {dst}")
    return {"amount": amount, "from": src, "to": dst, "rate": rate,
            "converted": round(amount * rate, 2 if dst not in {"KRW", "JPY"} else 0),
            "rate_updated": data.get("time_last_update_utc"), "source": "open.er-api.com"}


@tool(
    "web_search",
    "구글 검색으로 최신·실시간 정보를 찾는다: 뉴스, 경기 결과, 주가·코인 시세, 출시일, 영업시간, 맛집, 최근 사건, "
    "잘 모르는 고유명사 등. 학습 지식이 오래됐을 수 있는 질문이면 추측하지 말고 이 도구를 쓴다.",
    {
        "query": {"type": "string", "description": "검색할 질문 (한국어 가능, 구체적으로)"},
        "detail": {"type": "string", "enum": ["brief", "detailed"],
                   "description": "brief=핵심 몇 문장(기본), detailed=정리·예문·비교·목록이 필요할 때 자세히"},
    },
    ["query"],
)
async def web_search(args, ctx: ToolContext):
    query = str(args.get("query", "")).strip()[:300]
    if not query:
        raise ToolError("검색어가 비어 있습니다")
    detailed = str(args.get("detail", "brief")).lower() == "detailed"
    guide = (
        "웹 검색 결과를 바탕으로 질문에 답하는 데 필요한 자료를 한국어로 자세히 정리하라. "
        "목록·실제 예문·인용·비교 항목이 있으면 가능한 한 많이 원문 그대로 포함하라 (최대 40줄)."
        if detailed else
        "웹 검색 결과를 바탕으로 질문에 필요한 핵심 사실만 한국어로 5문장 이내로 정리하라."
    )
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.2,
        max_output_tokens=4000 if detailed else 1200,
        system_instruction=guide + " 날짜·수치·고유명사는 정확히 쓰고, 검색으로 확인되지 않은 내용은 지어내지 마라.",
    )

    async def _call():
        return await ctx.genai_client.aio.models.generate_content(
            model=ctx.settings.chat_model, contents=query, config=config)

    resp = await call_with_retry(_call, limiter=ctx.limiter, max_retries=2, what="web_search",
                                 base_delay=2.0, max_delay=10.0)
    sources = []
    try:
        meta = resp.candidates[0].grounding_metadata
        for chunk in (meta.grounding_chunks or [])[:5]:
            if chunk.web and chunk.web.title and chunk.web.title not in sources:
                sources.append(chunk.web.title)
    except (AttributeError, IndexError, TypeError):
        pass
    return {"query": query, "summary": (resp.text or "").strip() or "검색 결과 없음", "sources": sources}
