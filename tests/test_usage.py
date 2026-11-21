from types import SimpleNamespace

from app.config import settings
from app.usage import UsageTracker, day_key, format_report, period_days, prices_from, summarize

PRICES = {"input_per_m": 0.30, "output_per_m": 2.50, "cached_per_m": 0.03, "embed_per_m": 0.15,
          "search_per_1k": 14.0, "search_free_per_month": 5000}


async def test_record_and_cost():
    t = UsageTracker()
    resp = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=1_000_000, candidates_token_count=100_000,
                                                          thoughts_token_count=100_000, cached_content_token_count=0))
    await t.record_response("chat", "m", resp)
    await t.record("embedding", "e", input=1_000_000)
    raw = await t.raw([day_key()])
    data = summarize(raw, PRICES)
    # 입력 1M*0.30 + 출력(사고 포함) 0.2M*2.50 + 임베딩 1M*0.15 = 0.30 + 0.50 + 0.15
    assert round(data["cost_usd"], 4) == 0.95
    assert data["categories"]["chat"]["req"] == 1
    assert any("채팅" in line for line in format_report(data, 1350))


async def test_cached_tokens_are_cheaper():
    t = UsageTracker()
    await t.record("chat", "m", input=1_000_000, cached=1_000_000)
    data = summarize(await t.raw([day_key()]), PRICES)
    assert round(data["cost_usd"], 4) == 0.03


def test_search_free_quota():
    raw = {"20261121": {"search|m|grounded": 10, "search|m|req": 10}}
    assert summarize(raw, PRICES, month_grounded=10)["search"]["cost_usd"] == 0
    over = summarize(raw, PRICES, month_grounded=5010)["search"]
    assert over["billable"] == 10 and round(over["cost_usd"], 3) == 0.14


def test_periods_and_prices():
    assert len(period_days("today")) == 1 and len(period_days("week")) == 7
    assert prices_from(settings)["search_per_1k"] > 0
