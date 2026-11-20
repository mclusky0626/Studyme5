"""장기 기억 서비스: Graphiti(FalkorDB + Gemini) 래퍼.

설계 포인트
- FalkorDB 에서 Graphiti 의 group_id 는 "그래프 이름" 이 된다. 즉 서버(guild)/DM 마다
  물리적으로 분리된 그래프가 생겨 기억이 절대 섞이지 않는다.
- group_id 마다 Graphiti 인스턴스를 하나씩 캐시한다. (driver._database == group_id 이면
  Graphiti 가 호출마다 드라이버를 복제하고 인덱스 생성 쿼리를 날리는 낭비를 피할 수 있다.)
- LLM / 임베더 / 리랭커는 모두 Gemini 로 교체 (OpenAI 기본값 사용 안 함).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from google import genai
from google.genai import types
from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.gemini import GeminiEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.nodes import EpisodeType
from graphiti_core.search.search_config import (
    EdgeReranker,
    EdgeSearchConfig,
    EdgeSearchMethod,
    NodeReranker,
    NodeSearchConfig,
    NodeSearchMethod,
    SearchConfig,
)

from .config import Settings
from .gemini_resilience import (
    RateLimiter,
    ResilientGeminiClient,
    ResilientGeminiEmbedder,
    ResilientGeminiReranker,
)

log = logging.getLogger(__name__)

# Redis(FalkorDB) 에 함께 저장하는 봇 메타데이터 키 — 관리 웹이 읽는다.
SPACES_KEY = "memorybot:spaces"
EVENTS_KEY = "memorybot:events"
STATUS_KEY = "memorybot:status"
MAX_EVENTS = 300

_GROUP_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

EXTRACTION_HINT = (
    "이 에피소드는 디스코드 대화 기록이다. "
    "사람(사용자)에 대한 지속적인 사실, 선호, 계획, 관계, 경험, 소유물 위주로 추출하라. "
    "AI 봇의 인사말·맞장구·일반 상식 설명은 사실로 추출하지 마라. "
    "엔티티 이름과 사실은 대화에 쓰인 언어(주로 한국어) 그대로 유지하라."
)


class MemoryUnavailable(RuntimeError):
    """FalkorDB 미연결 — 재시도하지 않고 바로 파일에 보관할 대상."""


def group_id_for(guild_id: int | None, user_id: int) -> str:
    """서버는 guild-<id>, DM 은 dm-<user_id> 로 기억 공간을 분리한다."""
    return f"guild-{guild_id}" if guild_id is not None else f"dm-{user_id}"


@dataclass
class Episode:
    group_id: str
    name: str
    body: str
    source_description: str
    reference_time: datetime
    message_count: int
    attempts: int = 0

    def to_json(self) -> str:
        data = asdict(self)
        data["reference_time"] = self.reference_time.isoformat()
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Episode":
        data = json.loads(raw)
        data["reference_time"] = datetime.fromisoformat(data["reference_time"])
        return cls(**data)


@dataclass
class MemoryContext:
    facts: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.facts and not self.entities


def _fmt_date(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value[:10]
    return value.strftime("%Y-%m-%d")


class MemoryService:
    def __init__(self, settings: Settings, limiter: RateLimiter) -> None:
        self.s = settings
        self.limiter = limiter
        self._base_driver: FalkorDriver | None = None
        self._graphs: dict[str, Graphiti] = {}
        self._graph_lock = asyncio.Lock()
        self._existing_graphs: set[str] = set()

        # 관리 웹은 키 없이도 조회는 가능하도록 placeholder 로 생성 (검색/수정 호출 시에만 실패)
        genai_client = genai.Client(api_key=settings.gemini_api_key or "missing-api-key")
        retries = settings.gemini_max_retries
        thinking = (
            types.ThinkingConfig(thinking_level=settings.graph_thinking_level)
            if settings.graph_thinking_level
            else None
        )
        # ---- 기본값 오버라이드: LLM / 임베딩 / 리랭커 전부 Gemini ----
        self.llm_client = ResilientGeminiClient(
            config=LLMConfig(
                api_key=settings.gemini_api_key,
                model=settings.llm_model,
                small_model=settings.llm_small_model,
                temperature=0.0,
            ),
            client=genai_client,
            thinking_config=thinking,
            limiter=limiter,
            max_retries=retries,
        )
        self.embedder = ResilientGeminiEmbedder(
            config=GeminiEmbedderConfig(
                api_key=settings.gemini_api_key,
                embedding_model=settings.embedding_model,
            ),
            client=genai_client,
            # graphiti 는 gemini-embedding-001 을 1건씩 호출하도록 하드코딩돼 있으나
            # 실제로는 배치를 지원하므로 요청 수(=RPM 소모)를 크게 줄일 수 있다.
            batch_size=settings.embedding_batch_size,
            limiter=limiter,
            max_retries=retries,
        )
        self.reranker = ResilientGeminiReranker(
            config=LLMConfig(api_key=settings.gemini_api_key, model=settings.reranker_model),
            client=genai_client,
            limiter=limiter,
            max_retries=retries,
        )

    # ------------------------------------------------------------------ lifecycle
    @property
    def available(self) -> bool:
        return self._base_driver is not None

    async def _connect_once(self) -> bool:
        # FalkorDB 클라이언트는 생성자에서 "동기식"으로 접속(클러스터 여부 확인)한다.
        try:
            driver = await asyncio.to_thread(
                FalkorDriver,
                host=self.s.falkordb_host,
                port=self.s.falkordb_port,
                username=self.s.falkordb_username,
                password=self.s.falkordb_password,
            )
            await driver.client.connection.ping()
        except Exception as exc:  # noqa: BLE001
            log.debug("FalkorDB 연결 실패: %s", exc)
            return False
        self._base_driver = driver
        await self.refresh_graph_list()
        log.info("FalkorDB 연결 완료 %s:%s — 기존 기억 공간 %d개",
                 self.s.falkordb_host, self.s.falkordb_port, len(self._existing_graphs))
        return True

    async def start(self, *, required: bool = False) -> bool:
        """FalkorDB 가 늦게 뜨는 경우를 대비해 FALKORDB_WAIT_SECONDS 동안 재시도.
        실패하면 required=False 일 때 장기 기억 없이(단기 기억만) 계속 동작한다."""
        attempts = max(1, int(self.s.falkordb_wait_seconds // 2))
        for attempt in range(1, attempts + 1):
            if await self._connect_once():
                return True
            if attempt < attempts:
                log.info("FalkorDB(%s:%s) 연결 대기 중... %d/%d", self.s.falkordb_host,
                         self.s.falkordb_port, attempt, attempts)
                await asyncio.sleep(2)
        if required:
            raise ConnectionError(f"FalkorDB {self.s.falkordb_host}:{self.s.falkordb_port} 에 연결할 수 없습니다")
        log.warning("⚠️ FalkorDB 에 연결할 수 없어 장기 기억 없이(단기 기억만) 동작합니다. "
                    "저장할 대화는 파일에 보관했다가 DB 가 연결되면 자동으로 반영합니다.")
        return False

    async def try_reconnect(self) -> bool:
        if self.available:
            return True
        return await self._connect_once()

    async def close(self) -> None:
        if self._base_driver is not None:
            await self._base_driver.close()

    @property
    def redis(self):
        assert self._base_driver is not None, "MemoryService.start() 를 먼저 호출해야 합니다"
        return self._base_driver.client.connection

    async def refresh_graph_list(self) -> None:
        try:
            names = await self._base_driver.client.list_graphs()  # type: ignore[union-attr]
            self._existing_graphs = {n.decode() if isinstance(n, bytes) else str(n) for n in names}
        except Exception as exc:  # noqa: BLE001
            log.warning("그래프 목록 조회 실패: %s", exc)

    async def prune_cache(self) -> None:
        """관리 웹에서 삭제된 그래프의 캐시를 버린다 (다시 쓰일 때 인덱스부터 재생성)."""
        if not self.available:
            return
        await self.refresh_graph_list()
        for gid in [g for g in self._graphs if g not in self._existing_graphs]:
            self._graphs.pop(gid, None)
            log.info("[%s] 그래프가 삭제되어 캐시를 비웠습니다.", gid)

    def forget_group(self, group_id: str) -> None:
        self._graphs.pop(group_id, None)
        self._existing_graphs.discard(group_id)

    async def for_group(self, group_id: str) -> Graphiti:
        """group_id 전용 Graphiti 인스턴스 (드라이버의 기본 DB = group_id)."""
        if not _GROUP_RE.match(group_id):
            raise ValueError(f"잘못된 group_id: {group_id!r}")
        g = self._graphs.get(group_id)
        if g is not None:
            return g
        async with self._graph_lock:
            g = self._graphs.get(group_id)
            if g is None:
                assert self._base_driver is not None
                driver = self._base_driver.clone(database=group_id)
                g = Graphiti(
                    graph_driver=driver,
                    llm_client=self.llm_client,
                    embedder=self.embedder,
                    cross_encoder=self.reranker,
                    max_coroutines=self.s.graphiti_max_coroutines,
                )
                # FalkorDriver 생성자가 이미 인덱스 생성 태스크를 예약해 두므로 그것을 기다린다.
                try:
                    init_task = getattr(driver, "_init_task", None)
                    if init_task is not None:
                        await init_task
                    else:
                        await g.build_indices_and_constraints()
                except Exception as exc:  # noqa: BLE001
                    log.warning("[%s] 인덱스 생성 경고: %s", group_id, exc)
                self._graphs[group_id] = g
        return g

    # ------------------------------------------------------------------ read
    async def search(self, group_id: str, query: str) -> MemoryContext:
        """질문과 관련된 장기 기억(사실 + 엔티티 요약)을 가져온다. 실패해도 빈 결과로 진행."""
        ctx = MemoryContext()
        query = query.strip()
        # 아직 저장된 기억이 없는 공간이면 임베딩 호출 자체를 생략 (토큰/RPM 절약)
        if not query or group_id not in self._existing_graphs:
            return ctx
        config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity],
                reranker=EdgeReranker.rrf,  # LLM 리랭킹 없이 RRF → 추가 토큰 0
            ),
            node_config=NodeSearchConfig(
                search_methods=[NodeSearchMethod.bm25, NodeSearchMethod.cosine_similarity],
                reranker=NodeReranker.rrf,
            ),
            limit=max(self.s.memory_search_limit, self.s.memory_node_limit),
        )
        started = time.perf_counter()
        try:
            g = await self.for_group(group_id)
            results = await g.search_(query[:500], config=config, group_ids=[group_id])
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 장기 기억 검색 실패(무시하고 진행): %s", group_id, exc)
            return ctx

        for edge in results.edges[: self.s.memory_search_limit]:
            fact = (edge.fact or "").strip()
            if not fact:
                continue
            since = _fmt_date(edge.valid_at)
            until = _fmt_date(edge.invalid_at) or _fmt_date(edge.expired_at)
            if until:
                fact += f" (과거 사실, {since or '?'} ~ {until})"
            elif since:
                fact += f" (since {since})"
            ctx.facts.append(fact)
        for node in results.nodes[: self.s.memory_node_limit]:
            summary = (node.summary or "").strip()
            if summary:
                ctx.entities.append(f"{node.name}: {summary[:300]}")
        log.debug("[%s] 기억 검색 %.0fms — 사실 %d, 엔티티 %d", group_id,
                  (time.perf_counter() - started) * 1000, len(ctx.facts), len(ctx.entities))
        return ctx

    # ------------------------------------------------------------------ write
    async def save_episode(self, ep: Episode):
        if not self.available:
            raise MemoryUnavailable("FalkorDB 미연결")
        g = await self.for_group(ep.group_id)
        result = await g.add_episode(
            name=ep.name,
            episode_body=ep.body,
            source_description=ep.source_description,
            reference_time=ep.reference_time,
            source=EpisodeType.message,
            group_id=ep.group_id,
            custom_extraction_instructions=EXTRACTION_HINT,
        )
        self._existing_graphs.add(ep.group_id)
        return result

    # ------------------------------------------------------------------ metadata (관리 웹용)
    async def register_space(self, group_id: str, name: str, kind: str) -> None:
        try:
            payload = json.dumps(
                {"name": name, "kind": kind, "updated_at": datetime.now(timezone.utc).isoformat()},
                ensure_ascii=False,
            )
            await self.redis.hset(SPACES_KEY, group_id, payload)
        except Exception as exc:  # noqa: BLE001
            log.debug("공간 메타데이터 기록 실패: %s", exc)

    async def log_event(self, event: dict[str, Any]) -> None:
        try:
            event.setdefault("time", datetime.now(timezone.utc).isoformat())
            await self.redis.lpush(EVENTS_KEY, json.dumps(event, ensure_ascii=False))
            await self.redis.ltrim(EVENTS_KEY, 0, MAX_EVENTS - 1)
        except Exception as exc:  # noqa: BLE001
            log.debug("이벤트 기록 실패: %s", exc)

    async def write_status(self, status: dict[str, Any]) -> None:
        try:
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self.redis.set(STATUS_KEY, json.dumps(status, ensure_ascii=False), ex=90)
        except Exception as exc:  # noqa: BLE001
            log.debug("상태 기록 실패: %s", exc)
