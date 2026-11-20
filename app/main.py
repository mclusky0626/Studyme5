"""봇 실행 진입점: `python -m app.main`"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .config import settings


def setup_logging(level: str) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 깨짐 방지
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # 너무 시끄러운 로거 정리 (graphiti 의 JSON 파싱 오류 등은 ERROR 로 그대로 보인다)
    for noisy in ("httpx", "httpcore", "google_genai.models", "discord.gateway", "discord.http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("graphiti_core.driver.falkordb_driver").setLevel(logging.WARNING)


async def _amain() -> None:
    from .bot import MemoryBot

    bot = MemoryBot(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:  # docker stop(SIGTERM) 시 남은 버퍼를 저장하고 종료
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.close()))
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    async with bot:
        await bot.start(settings.discord_token)  # type: ignore[arg-type]


def run() -> None:
    setup_logging(settings.log_level)
    settings.require("discord_token", "gemini_api_key")
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
