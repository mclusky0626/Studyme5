"""기억 관리 웹 서버: `python -m app.admin` → http://<서버>:8000"""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import settings
from ..gemini_resilience import RateLimiter
from ..memory import MemoryService
from .repo import GraphRepo

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class FactUpdate(BaseModel):
    fact: str = Field(min_length=1, max_length=2000)


class NodeUpdate(BaseModel):
    summary: str = Field(max_length=4000)


class SpaceDelete(BaseModel):
    confirm: str


def create_app(repo: GraphRepo | None = None) -> FastAPI:
    """repo 를 주입하면(테스트) FalkorDB 연결 없이 동작한다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        memory = None
        if repo is None:
            memory = MemoryService(settings, RateLimiter(settings.gemini_rpm))
            await memory.start()
            app.state.repo = GraphRepo(memory)
        else:
            app.state.repo = repo
        if not settings.admin_password:
            log.warning("⚠️  ADMIN_PASSWORD 가 비어 있어 관리 웹이 인증 없이 열려 있습니다.")
        yield
        if memory is not None:
            await memory.close()

    app = FastAPI(title="Memory Console", lifespan=lifespan, docs_url=None, redoc_url=None)

    # ---------------------------------------------------------------- Basic 인증
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if settings.admin_password and request.url.path != "/healthz":
            ok = False
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user, settings.admin_user) and \
                        secrets.compare_digest(pw, settings.admin_password)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="memory"'})
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def r(request: Request) -> GraphRepo:
        return request.app.state.repo

    # ---------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---------------------------------------------------------------- API
    @app.get("/api/meta")
    async def meta():
        return {"auth": bool(settings.admin_password), "model": settings.llm_model,
                "embedding_model": settings.embedding_model,
                "search_enabled": bool(settings.gemini_api_key)}

    @app.get("/api/status")
    async def status(request: Request):
        return await r(request).status()

    @app.get("/api/spaces")
    async def spaces(request: Request):
        return await r(request).spaces()

    @app.get("/api/spaces/{gid}/graph")
    async def graph(request: Request, gid: str, limit: int = Query(150, ge=10, le=1000),
                    episodes: bool = False):
        return await r(request).graph(gid, limit=limit, with_episodes=episodes)

    @app.get("/api/spaces/{gid}/nodes/{uuid}")
    async def node(request: Request, gid: str, uuid: str):
        data = await r(request).node(gid, uuid)
        if data is None:
            raise HTTPException(404, "노드를 찾을 수 없습니다")
        return data

    @app.patch("/api/spaces/{gid}/nodes/{uuid}")
    async def update_node(request: Request, gid: str, uuid: str, body: NodeUpdate):
        await r(request).update_node(gid, uuid, body.summary.strip())
        return {"ok": True}

    @app.delete("/api/spaces/{gid}/nodes/{uuid}")
    async def delete_node(request: Request, gid: str, uuid: str):
        await r(request).delete_node(gid, uuid)
        return {"ok": True}

    @app.get("/api/spaces/{gid}/facts")
    async def facts(request: Request, gid: str, q: str = "", skip: int = Query(0, ge=0),
                    limit: int = Query(50, ge=1, le=200), include_invalid: bool = True):
        return await r(request).facts(gid, q=q.strip(), skip=skip, limit=limit,
                                      include_invalid=include_invalid)

    @app.patch("/api/spaces/{gid}/facts/{uuid}")
    async def update_fact(request: Request, gid: str, uuid: str, body: FactUpdate):
        if not settings.gemini_api_key:
            raise HTTPException(400, "사실 수정에는 임베딩 재계산을 위해 GEMINI_API_KEY 가 필요합니다")
        await r(request).update_fact(gid, uuid, body.fact.strip())
        return {"ok": True}

    @app.post("/api/spaces/{gid}/facts/{uuid}/invalidate")
    async def invalidate_fact(request: Request, gid: str, uuid: str):
        await r(request).invalidate_fact(gid, uuid)
        return {"ok": True}

    @app.post("/api/spaces/{gid}/facts/{uuid}/restore")
    async def restore_fact(request: Request, gid: str, uuid: str):
        await r(request).restore_fact(gid, uuid)
        return {"ok": True}

    @app.delete("/api/spaces/{gid}/facts/{uuid}")
    async def delete_fact(request: Request, gid: str, uuid: str):
        await r(request).delete_fact(gid, uuid)
        return {"ok": True}

    @app.get("/api/spaces/{gid}/episodes")
    async def episodes(request: Request, gid: str, q: str = "", skip: int = Query(0, ge=0),
                       limit: int = Query(30, ge=1, le=100)):
        return await r(request).episodes(gid, q=q.strip(), skip=skip, limit=limit)

    @app.delete("/api/spaces/{gid}/episodes/{uuid}")
    async def delete_episode(request: Request, gid: str, uuid: str):
        await r(request).delete_episode(gid, uuid)
        return {"ok": True}

    @app.get("/api/spaces/{gid}/search")
    async def search(request: Request, gid: str, q: str = Query(..., min_length=1, max_length=500)):
        if not settings.gemini_api_key:
            raise HTTPException(400, "의미 검색에는 GEMINI_API_KEY 가 필요합니다")
        return await r(request).semantic_search(gid, q)

    @app.delete("/api/spaces/{gid}")
    async def delete_space(request: Request, gid: str, body: SpaceDelete):
        if body.confirm != gid:
            raise HTTPException(400, "확인 문자열이 기억 공간 ID 와 일치하지 않습니다")
        await r(request).delete_space(gid)
        return {"ok": True}

    return app


def run() -> None:
    import uvicorn

    from ..main import setup_logging

    setup_logging(settings.log_level)
    uvicorn.run(create_app(), host=settings.admin_host, port=settings.admin_port,
                log_level=settings.log_level.lower(), proxy_headers=True)


if __name__ == "__main__":
    run()
