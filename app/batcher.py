"""토큰 방어용 배칭 저장기.

- 메시지 1개마다 그래프에 저장하지 않는다.
- 채널별 버퍼에 모았다가 (1) N마디가 쌓이거나 (2) 일정 시간 대화가 끊기면(디바운스)
  하나의 에피소드로 묶어 "백그라운드"에서 add_episode 를 호출한다.
- 동시 실행 세마포어 + 그룹별 순차 처리 + 재시도. 최종 실패분은 파일로 보관했다가
  다음 기동 시 재처리한다. 어떤 실패도 봇 프로세스를 죽이지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .gemini_resilience import is_retryable
from .memory import Episode, MemoryUnavailable

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+")
_DISCORD_TOKEN_RE = re.compile(r"<(?:@[!&]?|#|a?:\w+:)\d+>")
_JAMO_RE = re.compile(r"[㄰-㆏]")  # ㅋㅋ, ㅎㅎ, ㅇㅇ, ㅠㅠ 같은 자모 단독 입력

USER_LINE_MAX = 2000
BOT_LINE_MAX = 600  # 봇의 긴 설명은 추출 가치가 낮아 잘라서 토큰을 아낀다


def clean_text(text: str) -> str:
    text = _URL_RE.sub("[링크]", text)
    text = _DISCORD_TOKEN_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def core_length(text: str) -> int:
    """문자/숫자만 센 길이 (자모 단독, 이모지, 구두점 제외)."""
    text = _JAMO_RE.sub("", clean_text(text).replace("[링크]", ""))
    return sum(1 for ch in text if unicodedata.category(ch)[0] in "LN")


def should_store(text: str, *, direct: bool, is_bot: bool, min_chars: int) -> bool:
    """장기 기억 버퍼에 넣을 가치가 있는 메시지인지 판단."""
    if is_bot:
        return core_length(text) > 0
    core = core_length(text)
    if core == 0:  # ㅋㅋㅋ, ㅇㅇ, 이모지만 등
        return False
    if direct:
        # 봇에게 직접 한 말은 짧아도 사실일 수 있다 ("내 이름은 건우야" = 9자)
        return core >= 2
    return len(clean_text(text)) >= min_chars


@dataclass
class _Line:
    speaker: str
    text: str
    at: datetime


@dataclass
class _Buffer:
    group_id: str
    channel_label: str
    source_description: str
    lines: list[_Line] = field(default_factory=list)
    chars: int = 0
    timer: asyncio.TimerHandle | None = None


EventSink = Callable[[dict[str, Any]], Awaitable[None]]


class EpisodeBatcher:
    def __init__(
        self,
        settings: Settings,
        saver: Callable[[Episode], Awaitable[Any]],
        *,
        on_event: EventSink | None = None,
        failed_path: Path | None = None,
    ) -> None:
        self.s = settings
        self._saver = saver
        self._on_event = on_event
        self._failed_path = failed_path or (settings.data_dir / "failed_episodes.jsonl")
        self._buffers: dict[str, _Buffer] = {}
        self._tasks: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrent_episodes))
        self._group_locks: dict[str, asyncio.Lock] = {}
        self.saved_count = 0
        self.failed_count = 0
        self.last_saved_at: str | None = None
        self._closing = False

    # ------------------------------------------------------------------ 입력
    def add(
        self,
        *,
        key: str,
        group_id: str,
        speaker: str,
        text: str,
        at: datetime | None = None,
        channel_label: str,
        source_description: str,
        is_bot: bool = False,
        direct: bool = False,
    ) -> bool:
        """메시지를 버퍼에 추가한다. 저장 대상이 아니면 False. (동기 함수 — 절대 블로킹하지 않음)"""
        if self._closing:
            return False
        if not should_store(text, direct=direct, is_bot=is_bot, min_chars=self.s.min_message_chars):
            return False
        limit = BOT_LINE_MAX if is_bot else USER_LINE_MAX
        body = clean_text(text)
        if len(body) > limit:
            body = body[:limit] + "…"

        buf = self._buffers.get(key)
        if buf is None:
            buf = _Buffer(group_id=group_id, channel_label=channel_label,
                          source_description=source_description)
            self._buffers[key] = buf
        buf.lines.append(_Line(speaker=speaker, text=body, at=at or datetime.now(timezone.utc)))
        buf.chars += len(body)

        if len(buf.lines) >= self.s.batch_max_messages or buf.chars >= self.s.batch_max_chars:
            self.flush(key, reason="size")
        else:
            self._arm_timer(key, buf)
        return True

    def _arm_timer(self, key: str, buf: _Buffer) -> None:
        """디바운스: 새 메시지가 올 때마다 타이머를 다시 건다."""
        if buf.timer is not None:
            buf.timer.cancel()
        loop = asyncio.get_running_loop()
        buf.timer = loop.call_later(self.s.batch_idle_seconds, self.flush, key, "idle")

    # ------------------------------------------------------------------ flush
    def flush(self, key: str, reason: str = "manual") -> Episode | None:
        buf = self._buffers.pop(key, None)
        if buf is None or not buf.lines:
            return None
        if buf.timer is not None:
            buf.timer.cancel()
        first = buf.lines[0].at
        episode = Episode(
            group_id=buf.group_id,
            name=f"{buf.channel_label} · {first.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC",
            body="\n".join(f"{ln.speaker}: {ln.text}" for ln in buf.lines),
            source_description=buf.source_description,
            reference_time=first,
            message_count=len(buf.lines),
        )
        log.info("[%s] 배치 확정(%s): %d마디 %d자 → 백그라운드 저장 예약",
                 buf.group_id, reason, len(buf.lines), buf.chars)
        self._spawn(episode)
        return episode

    def _spawn(self, episode: Episode) -> None:
        task = asyncio.get_running_loop().create_task(self._save_with_retry(episode))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:  # 방어적: 원래 안 일어남
            log.error("백그라운드 저장 태스크 예외: %r", task.exception())

    # ------------------------------------------------------------------ 저장 (백그라운드)
    async def _save_with_retry(self, ep: Episode) -> None:
        try:
            await self._save_loop(ep)
        except asyncio.CancelledError:
            # 종료 시간 초과 등으로 취소되면 잃어버리지 않도록 파일에 보관
            self._dead_letter(ep, "cancelled")
            raise

    async def _save_loop(self, ep: Episode) -> None:
        lock = self._group_locks.setdefault(ep.group_id, asyncio.Lock())
        max_retries = self.s.episode_max_retries
        while True:
            started = time.perf_counter()
            error: Exception | None = None
            result = None
            try:
                # 같은 기억 공간은 순서대로 (엔티티 중복 방지), 전체 동시 실행은 세마포어로 제한
                async with lock, self._sem:
                    result = await self._saver(ep)
            except Exception as exc:  # noqa: BLE001 - 어떤 오류도 봇을 죽이지 않게
                error = exc
            elapsed = time.perf_counter() - started

            if error is None:
                self.saved_count += 1
                self.last_saved_at = datetime.now(timezone.utc).isoformat()
                nodes = len(getattr(result, "nodes", []) or [])
                edges = len(getattr(result, "edges", []) or [])
                log.info("[%s] ✅ 장기 기억 저장 완료 (%.1fs): %d마디 → 엔티티 %d, 관계 %d",
                         ep.group_id, elapsed, ep.message_count, nodes, edges)
                await self._emit({"type": "saved", "group_id": ep.group_id, "episode": ep.name,
                                  "messages": ep.message_count, "nodes": nodes, "edges": edges,
                                  "seconds": round(elapsed, 1)})
                return

            if isinstance(error, MemoryUnavailable):
                # DB 미연결: 토큰을 쓰지 않았으므로 재시도 없이 보관 → 재연결 시 자동 반영
                self._dead_letter(ep, "FalkorDB 미연결")
                return

            ep.attempts += 1
            retryable = is_retryable(error)
            # 재시도 불가 오류(파싱 실패 등)는 토큰 낭비를 막기 위해 1회만 더 시도
            allowed = max_retries if retryable else min(1, max_retries)
            if ep.attempts <= allowed and not self._closing:
                delay = min(600.0, 30.0 * (2 ** (ep.attempts - 1))) + random.uniform(0, 5)
                log.warning("[%s] 저장 실패(%s: %s) → %.0fs 후 재시도 %d/%d",
                            ep.group_id, type(error).__name__, str(error)[:200], delay,
                            ep.attempts, allowed)
                await asyncio.sleep(delay)
                continue

            self.failed_count += 1
            log.error("[%s] ❌ 장기 기억 저장 최종 실패: %s: %s", ep.group_id,
                      type(error).__name__, str(error)[:500])
            self._dead_letter(ep, f"{type(error).__name__}: {error}")
            await self._emit({"type": "failed", "group_id": ep.group_id, "episode": ep.name,
                              "messages": ep.message_count,
                              "error": f"{type(error).__name__}: {str(error)[:300]}"})
            return

    async def _emit(self, event: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(event)
        except Exception:  # noqa: BLE001
            pass

    def _dead_letter(self, ep: Episode, reason: str) -> None:
        try:
            self._failed_path.parent.mkdir(parents=True, exist_ok=True)
            with self._failed_path.open("a", encoding="utf-8") as fh:
                fh.write(ep.to_json() + "\n")
            log.info("실패한 에피소드를 %s 에 보관했습니다 (%s). 다음 기동 때 재처리합니다.",
                     self._failed_path, reason[:120])
        except Exception as exc:  # noqa: BLE001
            log.error("실패 에피소드 보관 불가: %s", exc)

    async def replay_failed(self) -> int:
        """이전 실행에서 실패했던 에피소드를 다시 큐에 넣는다."""
        path = self._failed_path
        if not path.exists():
            return 0
        try:
            raw = path.read_text(encoding="utf-8").splitlines()
            path.unlink()
        except Exception as exc:  # noqa: BLE001
            log.error("실패 에피소드 파일 읽기 실패: %s", exc)
            return 0
        count = 0
        for line in raw:
            if not line.strip():
                continue
            try:
                ep = Episode.from_json(line)
                ep.attempts = 0
                self._spawn(ep)
                count += 1
            except Exception as exc:  # noqa: BLE001
                log.error("손상된 실패 에피소드 건너뜀: %s", exc)
        if count:
            log.info("이전 실행에서 실패한 에피소드 %d건을 재처리합니다.", count)
        return count

    # ------------------------------------------------------------------ 종료 / 상태
    async def shutdown(self, timeout: float = 120.0) -> None:
        """남은 버퍼를 전부 저장 예약하고, 진행 중인 저장을 최대 timeout 초 기다린다."""
        for key in list(self._buffers):
            self.flush(key, reason="shutdown")
        self._closing = True
        if not self._tasks:
            return
        log.info("종료 전 남은 저장 작업 %d건 대기 (최대 %.0fs)", len(self._tasks), timeout)
        done, pending = await asyncio.wait(set(self._tasks), timeout=timeout)
        for task in pending:
            task.cancel()  # 취소된 에피소드는 dead-letter 로 보관됨
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def stats(self) -> dict[str, Any]:
        return {
            "buffers": len(self._buffers),
            "buffered_messages": sum(len(b.lines) for b in self._buffers.values()),
            "pending_saves": len(self._tasks),
            "saved": self.saved_count,
            "failed": self.failed_count,
            "last_saved_at": self.last_saved_at,
            "batch_max_messages": self.s.batch_max_messages,
            "batch_idle_seconds": self.s.batch_idle_seconds,
        }

    def buffer_info(self, key: str) -> tuple[int, int]:
        buf = self._buffers.get(key)
        return (len(buf.lines), buf.chars) if buf else (0, 0)
