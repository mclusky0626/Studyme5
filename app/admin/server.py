"""기억 관리 웹 서버: `python -m app.admin` → http://<서버>:8000"""

from __future__ import annotations

import base64
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import settings
from ..gemini_resilience import RateLimiter
from ..chat import ChatResponder, ShortLine
from ..memory import MemoryContext, MemoryService
from ..persona import MAX_NAME_CHARS, MAX_PERSONA_CHARS, PersonaStore
from ..usage import format_report, report, tracker, usd_krw
from .repo import GraphRepo

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class FactUpdate(BaseModel):
    fact: str = Field(min_length=1, max_length=2000)


class NodeUpdate(BaseModel):
    summary: str = Field(max_length=4000)


class SpaceDelete(BaseModel):
    confirm: str


class PersonaUpdate(BaseModel):
    name: str = Field(default="", max_length=MAX_NAME_CHARS)
    persona: str = Field(default="", max_length=MAX_PERSONA_CHARS)


class PersonaTest(PersonaUpdate):
    message: str = Field(min_length=1, max_length=500)



# ---------------------------------------------------------------- 보안 도우미
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "font-src 'self' https://cdn.jsdelivr.net data:; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'"
    ),
}


def is_proxied(request: Request) -> bool:
    """Tailscale Funnel / Cloudflare Tunnel 같은 프록시를 거쳐 들어온 요청인지."""
    h = request.headers
    return any(k in h for k in ("x-forwarded-for", "cf-connecting-ip", "tailscale-funnel-request"))


def client_ip(request: Request) -> str:
    h = request.headers
    ip = h.get("cf-connecting-ip") or h.get("x-forwarded-for", "").split(",")[0].strip()
    return ip or (request.client.host if request.client else "?")


class LoginGuard:
    """IP별: 10분에 10번 틀리면 15분 잠금. 전체: 10분에 50번 틀리면 10분 잠금(프록시가 IP를 숨기는 경우 대비)."""

    def __init__(self, per_ip: int = 10, total: int = 50, window: float = 600, ip_lock: float = 900,
                 total_lock: float = 600) -> None:
        self.per_ip, self.total, self.window = per_ip, total, window
        self.ip_lock, self.total_lock = ip_lock, total_lock
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _now(self) -> float:
        return time.monotonic()

    def locked(self, ip: str) -> int:
        now = self._now()
        until = max(self._locked_until.get(ip, 0), self._locked_until.get("*", 0))
        return int(until - now) + 1 if until > now else 0

    def fail(self, ip: str) -> None:
        now = self._now()
        for key, limit, lock in ((ip, self.per_ip, self.ip_lock), ("*", self.total, self.total_lock)):
            hits = [x for x in self._fails.get(key, []) if now - x < self.window] + [now]
            self._fails[key] = hits
            if len(hits) >= limit:
                self._locked_until[key] = now + lock
                self._fails[key] = []


def create_app(repo: GraphRepo | None = None, persona: PersonaStore | None = None) -> FastAPI:
    """repo 를 주입하면(테스트) FalkorDB 연결 없이 동작한다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        memory = None
        if repo is None:
            memory = MemoryService(settings, RateLimiter(settings.gemini_rpm))
            await memory.start(required=True)
            app.state.repo = GraphRepo(memory)
            app.state.persona = PersonaStore(settings, lambda: memory.redis)
            tracker.bind(lambda: memory.redis)
        else:
            app.state.repo = repo
            app.state.persona = persona or PersonaStore(settings, lambda: None)
        app.state.chat = ChatResponder(settings, RateLimiter(0)) if settings.gemini_api_key else None
        if not settings.admin_password:
            log.warning("⚠️  ADMIN_PASSWORD 가 비어 있어 관리 웹이 인증 없이 열려 있습니다.")
        yield
        if memory is not None:
            await memory.close()

    app = FastAPI(title="Memory Console", lifespan=lifespan, docs_url=None, redoc_url=None)

    # ---------------------------------------------------------------- Basic 인증 + 무차별 대입 방어
    guard = LoginGuard()

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        path = request.url.path
        if path != "/healthz":
            ip = client_ip(request)
            proxied = is_proxied(request)
            if not settings.admin_password and proxied:
                # 터널(Funnel/Cloudflare)로 들어온 요청인데 비밀번호가 없으면 절대 열지 않는다
                return Response("외부 접속에는 ADMIN_PASSWORD 설정이 필요합니다.", status_code=403,
                                media_type="text/plain; charset=utf-8")
            if settings.admin_password:
                wait = guard.locked(ip)
                if wait:
                    return Response(f"로그인 실패가 많아 {wait}초 동안 잠겼습니다.", status_code=429,
                                    headers={"Retry-After": str(wait)}, media_type="text/plain; charset=utf-8")
                header = request.headers.get("authorization", "")
                ok = False
                if header.lower().startswith("basic "):
                    try:
                        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                        ok = secrets.compare_digest(user, settings.admin_user) and \
                            secrets.compare_digest(pw, settings.admin_password)
                    except Exception:  # noqa: BLE001
                        ok = False
                    if not ok:  # 자격 증명을 보냈는데 틀린 경우만 실패로 센다
                        guard.fail(ip)
                        log.warning("관리 웹 로그인 실패: %s%s", ip, " (터널)" if proxied else "")
                if not ok:
                    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="memory"'})
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

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

    # ---------------------------------------------------------------- 봇 설정 (이름·페르소나)
    @app.get("/api/persona")
    async def get_persona(request: Request):
        return await request.app.state.persona.info()

    @app.put("/api/persona")
    async def put_persona(request: Request, body: PersonaUpdate):
        try:
            await request.app.state.persona.save(body.name, body.persona)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return await request.app.state.persona.info()

    @app.post("/api/persona/reset")
    async def reset_persona(request: Request):
        await request.app.state.persona.reset()
        return await request.app.state.persona.info()

    @app.post("/api/persona/test")
    async def test_persona(request: Request, body: PersonaTest):
        chat: ChatResponder | None = request.app.state.chat
        if chat is None:
            raise HTTPException(400, "미리보기에는 GEMINI_API_KEY 가 필요합니다")
        info = await request.app.state.persona.info()
        name = body.name.strip() or info["name"]
        reply = await chat.reply(
            bot_name=name, place="미리보기", history=[],
            current=ShortLine(speaker="관리자", text=body.message.strip()),
            memory=MemoryContext(), persona=body.persona.strip() or info["persona"],
            tool_ctx=chat.tool_context(),
        )
        return {"reply": reply}

    @app.get("/api/usage")
    async def usage(period: str = Query("month", pattern="^(today|week|month|last30)$")):
        data = await report(settings, period)
        krw = await usd_krw(settings)
        data["usd_krw"] = krw
        data["lines"] = format_report(data, krw)
        return data

    @app.get("/api/tasks")
    async def tasks(request: Request):
        return await r(request).tasks()

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: str):
        if not await r(request).cancel_task(task_id):
            raise HTTPException(400, "취소할 수 있는 작업이 아닙니다")
        return {"ok": True}

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
