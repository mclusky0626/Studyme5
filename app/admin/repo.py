"""관리 웹용 FalkorDB 조회/수정 계층.

Graphiti 가 FalkorDB 에 만드는 스키마
  (:Entity {uuid, name, summary, group_id, created_at, name_embedding, ...})
  (:Entity)-[:RELATES_TO {uuid, name, fact, valid_at, invalid_at, expired_at, created_at, episodes}]->(:Entity)
  (:Episodic {uuid, name, content, source_description, valid_at, created_at})-[:MENTIONS]->(:Entity)
group_id 하나 = FalkorDB 그래프 하나.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from graphiti_core.search.search_config import (
    EdgeReranker,
    EdgeSearchConfig,
    EdgeSearchMethod,
    NodeReranker,
    NodeSearchConfig,
    NodeSearchMethod,
    SearchConfig,
)

from ..memory import EVENTS_KEY, SPACES_KEY, STATUS_KEY, MemoryService
from ..tasks import TASKS_KEY, describe_schedule

log = logging.getLogger(__name__)
_GID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_EDGE_FIELDS = """
    r.uuid AS uuid, a.uuid AS source, b.uuid AS target, a.name AS source_name, b.name AS target_name,
    r.name AS name, r.fact AS fact, r.valid_at AS valid_at, r.invalid_at AS invalid_at,
    r.expired_at AS expired_at, r.created_at AS created_at
"""


def _s(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _rows(result) -> list[dict[str, Any]]:
    header = [h[1] if isinstance(h, (list, tuple)) else h for h in result.header]
    header = [_s(h) for h in header]
    return [{k: _s(v) for k, v in zip(header, row)} for row in result.result_set]


def _clean_props(props: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in props.items() if not k.endswith("_embedding")}


class GraphRepo:
    def __init__(self, memory: MemoryService) -> None:
        self.memory = memory

    @property
    def db(self):
        return self.memory._base_driver.client  # type: ignore[union-attr]

    @property
    def redis(self):
        return self.memory.redis

    def _graph(self, gid: str):
        if not _GID_RE.match(gid):
            raise ValueError("invalid group id")
        return self.db.select_graph(gid)

    async def _q(self, gid: str, query: str, params: dict | None = None, *, write: bool = False):
        graph = self._graph(gid)
        if write:
            return _rows(await graph.query(query, params or {}))
        return _rows(await graph.ro_query(query, params or {}))

    # ------------------------------------------------------------------ 공간 / 상태
    async def spaces(self) -> list[dict[str, Any]]:
        names = [_s(n) for n in await self.db.list_graphs()]
        raw_meta = await self.redis.hgetall(SPACES_KEY)
        meta = {}
        for k, v in (raw_meta or {}).items():
            try:
                meta[_s(k)] = json.loads(_s(v))
            except Exception:  # noqa: BLE001
                pass
        out = []
        for gid in names:
            stats: dict[str, Any] = {}
            try:
                rows = await self._q(gid, """
                    OPTIONAL MATCH (n:Entity) WITH count(n) AS entities
                    OPTIONAL MATCH (e:Episodic) WITH entities, count(e) AS episodes
                    OPTIONAL MATCH (:Entity)-[r:RELATES_TO]->(:Entity)
                    RETURN entities, episodes, count(r) AS facts
                """)
                stats = rows[0] if rows else {}
                last = await self._q(gid, """
                    MATCH (e:Episodic) RETURN e.created_at AS last ORDER BY e.created_at DESC LIMIT 1
                """)
                stats["last"] = last[0]["last"] if last else None
            except Exception as exc:  # noqa: BLE001
                log.debug("stats 실패 %s: %s", gid, exc)
            m = meta.get(gid, {})
            if not m and not stats.get("entities") and not stats.get("episodes"):
                continue  # 비어 있는 기본 그래프(default_db 등)는 숨김
            kind = m.get("kind") or ("guild" if gid.startswith("guild-") else
                                     "user" if gid.startswith("user-") else
                                     "dm" if gid.startswith("dm-") else "other")
            out.append({
                "group_id": gid,
                "name": m.get("name") or gid,
                "kind": kind,
                "entities": stats.get("entities", 0),
                "facts": stats.get("facts", 0),
                "episodes": stats.get("episodes", 0),
                "last_episode_at": stats.get("last"),
                "updated_at": m.get("updated_at"),
            })
        out.sort(key=lambda s: (s["last_episode_at"] or s["updated_at"] or ""), reverse=True)
        return out

    async def status(self) -> dict[str, Any]:
        raw = await self.redis.get(STATUS_KEY)
        status = json.loads(_s(raw)) if raw else None
        events = []
        for item in await self.redis.lrange(EVENTS_KEY, 0, 99):
            try:
                events.append(json.loads(_s(item)))
            except Exception:  # noqa: BLE001
                pass
        return {"online": status is not None, "bot": status, "events": events}

    # ------------------------------------------------------------------ 작업
    async def tasks(self) -> list[dict[str, Any]]:
        out = []
        for raw in (await self.redis.hgetall(TASKS_KEY) or {}).values():
            try:
                task = json.loads(_s(raw))
            except Exception:  # noqa: BLE001
                continue
            if task.get("schedule"):
                task["schedule_text"] = describe_schedule(task["schedule"])
            out.append(task)
        return sorted(out, key=lambda x: x.get("created", 0), reverse=True)

    async def cancel_task(self, task_id: str) -> bool:
        raw = await self.redis.hget(TASKS_KEY, task_id)
        if not raw:
            return False
        task = json.loads(_s(raw))
        if task["status"] in {"done", "failed", "cancelled"}:
            return False
        task["status"] = "cancelled" if task["status"] in {"queued", "scheduled"} else "cancel_requested"
        await self.redis.hset(TASKS_KEY, task_id, json.dumps(task, ensure_ascii=False))
        return True

    # ------------------------------------------------------------------ 그래프
    async def graph(self, gid: str, limit: int = 150, with_episodes: bool = False) -> dict[str, Any]:
        nodes = await self._q(gid, """
            MATCH (n:Entity)
            OPTIONAL MATCH (n)-[r:RELATES_TO]-(:Entity)
            WITH n, count(r) AS degree
            RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary, labels(n) AS labels,
                   n.created_at AS created_at, degree
            ORDER BY degree DESC, n.created_at DESC
            LIMIT $limit
        """, {"limit": limit})
        uuids = [n["uuid"] for n in nodes]
        edges = await self._q(gid, f"""
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
            WHERE a.uuid IN $uuids AND b.uuid IN $uuids
            RETURN {_EDGE_FIELDS}
        """, {"uuids": uuids}) if uuids else []
        episodes: list[dict[str, Any]] = []
        mentions: list[dict[str, Any]] = []
        if with_episodes and uuids:
            episodes = await self._q(gid, """
                MATCH (e:Episodic)
                RETURN e.uuid AS uuid, e.name AS name, e.valid_at AS valid_at,
                       substring(e.content, 0, 160) AS preview
                ORDER BY e.created_at DESC LIMIT 40
            """)
            ep_ids = [e["uuid"] for e in episodes]
            if ep_ids:
                mentions = await self._q(gid, """
                    MATCH (e:Episodic)-[:MENTIONS]->(n:Entity)
                    WHERE e.uuid IN $eps AND n.uuid IN $uuids
                    RETURN e.uuid AS source, n.uuid AS target
                """, {"eps": ep_ids, "uuids": uuids})
        total = await self._q(gid, "MATCH (n:Entity) RETURN count(n) AS c")
        return {"nodes": nodes, "edges": edges, "episodes": episodes, "mentions": mentions,
                "total_entities": total[0]["c"] if total else 0}

    async def node(self, gid: str, uuid: str) -> dict[str, Any] | None:
        rows = await self._q(gid, """
            MATCH (n:Entity {uuid: $uuid}) RETURN properties(n) AS props, labels(n) AS labels
        """, {"uuid": uuid})
        if not rows:
            return None
        props = _clean_props(rows[0]["props"] or {})
        facts = await self._q(gid, f"""
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
            WHERE a.uuid = $uuid OR b.uuid = $uuid
            RETURN {_EDGE_FIELDS}
            ORDER BY r.created_at DESC LIMIT 100
        """, {"uuid": uuid})
        episodes = await self._q(gid, """
            MATCH (e:Episodic)-[:MENTIONS]->(n:Entity {uuid: $uuid})
            RETURN e.uuid AS uuid, e.name AS name, e.content AS content, e.valid_at AS valid_at
            ORDER BY e.created_at DESC LIMIT 20
        """, {"uuid": uuid})
        return {"props": props, "labels": rows[0]["labels"], "facts": facts, "episodes": episodes}

    async def facts(self, gid: str, q: str = "", skip: int = 0, limit: int = 50,
                    include_invalid: bool = True) -> dict[str, Any]:
        params = {"q": q.lower(), "skip": skip, "limit": limit}
        where = "($q = '' OR toLower(r.fact) CONTAINS $q OR toLower(a.name) CONTAINS $q OR toLower(b.name) CONTAINS $q)"
        if not include_invalid:
            where += " AND r.invalid_at IS NULL AND r.expired_at IS NULL"
        rows = await self._q(gid, f"""
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE {where}
            RETURN {_EDGE_FIELDS}
            ORDER BY r.created_at DESC SKIP $skip LIMIT $limit
        """, params)
        total = await self._q(gid, f"""
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE {where} RETURN count(r) AS c
        """, params)
        return {"items": rows, "total": total[0]["c"] if total else 0}

    async def episodes(self, gid: str, q: str = "", skip: int = 0, limit: int = 30) -> dict[str, Any]:
        params = {"q": q.lower(), "skip": skip, "limit": limit}
        where = "($q = '' OR toLower(e.content) CONTAINS $q OR toLower(e.name) CONTAINS $q)"
        rows = await self._q(gid, f"""
            MATCH (e:Episodic) WHERE {where}
            OPTIONAL MATCH (e)-[:MENTIONS]->(n:Entity)
            WITH e, count(n) AS mentions
            RETURN e.uuid AS uuid, e.name AS name, e.content AS content,
                   e.source_description AS source_description, e.valid_at AS valid_at,
                   e.created_at AS created_at, mentions
            ORDER BY e.created_at DESC SKIP $skip LIMIT $limit
        """, params)
        total = await self._q(gid, f"MATCH (e:Episodic) WHERE {where} RETURN count(e) AS c", params)
        return {"items": rows, "total": total[0]["c"] if total else 0}

    async def semantic_search(self, gid: str, query: str, limit: int = 15) -> dict[str, Any]:
        g = await self.memory.for_group(gid)
        config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity],
                reranker=EdgeReranker.rrf),
            node_config=NodeSearchConfig(
                search_methods=[NodeSearchMethod.bm25, NodeSearchMethod.cosine_similarity],
                reranker=NodeReranker.rrf),
            limit=limit,
        )
        res = await g.search_(query, config=config, group_ids=[gid])
        names = {}
        uuids = list({e.source_node_uuid for e in res.edges} | {e.target_node_uuid for e in res.edges})
        if uuids:
            for row in await self._q(gid, "MATCH (n:Entity) WHERE n.uuid IN $u RETURN n.uuid AS uuid, n.name AS name",
                                     {"u": uuids}):
                names[row["uuid"]] = row["name"]

        def iso(v):
            return v.isoformat() if isinstance(v, datetime) else v

        edge_scores = list(res.edge_reranker_scores or [])
        node_scores = list(res.node_reranker_scores or [])
        return {
            "facts": [{
                "uuid": e.uuid, "name": e.name, "fact": e.fact,
                "source": e.source_node_uuid, "target": e.target_node_uuid,
                "source_name": names.get(e.source_node_uuid), "target_name": names.get(e.target_node_uuid),
                "valid_at": iso(e.valid_at), "invalid_at": iso(e.invalid_at),
                "expired_at": iso(e.expired_at), "created_at": iso(e.created_at),
                "score": edge_scores[i] if i < len(edge_scores) else None,
            } for i, e in enumerate(res.edges)],
            "entities": [{
                "uuid": n.uuid, "name": n.name, "summary": n.summary, "labels": n.labels,
                "score": node_scores[i] if i < len(node_scores) else None,
            } for i, n in enumerate(res.nodes)],
        }

    # ------------------------------------------------------------------ 수정 / 삭제
    async def update_fact(self, gid: str, uuid: str, fact: str) -> None:
        embedding = await self.memory.embedder.create(fact.replace("\n", " "))
        await self._q(gid, """
            MATCH ()-[r:RELATES_TO {uuid: $uuid}]->()
            SET r.fact = $fact, r.fact_embedding = vecf32($emb)
        """, {"uuid": uuid, "fact": fact, "emb": list(embedding)}, write=True)

    async def invalidate_fact(self, gid: str, uuid: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._q(gid, """
            MATCH ()-[r:RELATES_TO {uuid: $uuid}]->()
            SET r.invalid_at = $now, r.expired_at = $now
        """, {"uuid": uuid, "now": now}, write=True)

    async def restore_fact(self, gid: str, uuid: str) -> None:
        await self._q(gid, """
            MATCH ()-[r:RELATES_TO {uuid: $uuid}]->()
            SET r.invalid_at = NULL, r.expired_at = NULL
        """, {"uuid": uuid}, write=True)

    async def delete_fact(self, gid: str, uuid: str) -> None:
        await self._q(gid, "MATCH ()-[r:RELATES_TO {uuid: $uuid}]->() DELETE r", {"uuid": uuid}, write=True)

    async def update_node(self, gid: str, uuid: str, summary: str) -> None:
        await self._q(gid, "MATCH (n:Entity {uuid: $uuid}) SET n.summary = $summary",
                      {"uuid": uuid, "summary": summary}, write=True)

    async def delete_node(self, gid: str, uuid: str) -> None:
        await self._q(gid, "MATCH (n:Entity {uuid: $uuid}) DETACH DELETE n", {"uuid": uuid}, write=True)

    async def delete_episode(self, gid: str, uuid: str) -> None:
        # Graphiti 의 정리 로직 사용: 이 에피소드에서만 생긴 엔티티/관계도 함께 제거
        g = await self.memory.for_group(gid)
        await g.remove_episode(uuid)

    async def delete_space(self, gid: str) -> None:
        await self._graph(gid).delete()
        await self.redis.hdel(SPACES_KEY, gid)
        self.memory.forget_group(gid)
