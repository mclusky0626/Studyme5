"""환경변수 기반 설정.

- 로컬 실행 시 프로젝트 루트의 `.env` 를 자동으로 읽는다.
- `TOKEN =값` 처럼 키 뒤에 공백이 있는 형식도 허용한다.
- 기존 `.env` 의 `TOKEN` / `API` 키 이름을 별칭으로 지원한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Graphiti 는 기본적으로 익명 텔레메트리를 전송한다. import 전에 꺼 둔다.
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

try:  # 로컬 실행 편의용 (Docker 에서는 env_file 로 주입됨)
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _env(*names: str, default: str | None = None) -> str | None:
    """여러 후보 이름 중 처음으로 값이 있는 환경변수를 반환한다."""
    stripped = None
    for name in names:
        value = os.environ.get(name)
        if value is None:
            # "TOKEN =값" 형식 대응: 키 이름에 공백이 붙어 들어온 경우
            if stripped is None:
                stripped = {k.strip(): v for k, v in os.environ.items()}
            value = stripped.get(name)
        if value is not None and value.strip() != "":
            return value.strip().strip('"').strip("'")
    return default


def _int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # --- 비밀값 ---
    discord_token: str | None
    gemini_api_key: str | None

    # --- 모델 (기본값 오버라이드: OpenAI 대신 전부 Gemini) ---
    llm_model: str
    llm_small_model: str
    reranker_model: str
    chat_model: str
    embedding_model: str
    embedding_batch_size: int
    graph_thinking_level: str | None
    chat_thinking_level: str | None
    chat_temperature: float

    # --- FalkorDB ---
    falkordb_host: str
    falkordb_port: int
    falkordb_username: str | None
    falkordb_password: str | None

    # --- 메모리 동작 ---
    short_term_size: int
    batch_max_messages: int
    batch_max_chars: int
    batch_idle_seconds: float
    min_message_chars: int
    capture_all_messages: bool
    memory_search_limit: int
    memory_node_limit: int

    # --- 안정성 / 429 방어 ---
    max_concurrent_episodes: int
    max_concurrent_chats: int
    graphiti_max_coroutines: int
    gemini_rpm: int
    gemini_max_retries: int
    episode_max_retries: int

    # --- 봇 ---
    bot_persona: str | None
    dev_guild_id: int | None
    data_dir: Path
    log_level: str

    # --- 관리 웹 ---
    admin_host: str
    admin_port: int
    admin_user: str
    admin_password: str | None

    @classmethod
    def load(cls) -> "Settings":
        base_model = _env("GEMINI_MODEL", default="gemini-3.5-flash-lite")
        dev_guild = _env("DEV_GUILD_ID")
        return cls(
            discord_token=_env("DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "TOKEN"),
            gemini_api_key=_env("GEMINI_API_KEY", "GOOGLE_API_KEY", "API"),
            llm_model=base_model,
            llm_small_model=_env("GEMINI_SMALL_MODEL", default=base_model),
            reranker_model=_env("GEMINI_RERANKER_MODEL", default=base_model),
            chat_model=_env("CHAT_MODEL", default=base_model),
            # 임베딩은 생성형 모델(flash-lite)로 만들 수 없으므로 전용 임베딩 모델을 쓴다.
            embedding_model=_env("GEMINI_EMBEDDING_MODEL", default="gemini-embedding-001"),
            embedding_batch_size=_int("EMBEDDING_BATCH_SIZE", 100),
            graph_thinking_level=_env("GRAPH_THINKING_LEVEL", default="minimal"),
            chat_thinking_level=_env("CHAT_THINKING_LEVEL", default="low"),
            chat_temperature=_float("CHAT_TEMPERATURE", 0.8),
            falkordb_host=_env("FALKORDB_HOST", default="localhost"),
            falkordb_port=_int("FALKORDB_PORT", 6379),
            falkordb_username=_env("FALKORDB_USERNAME"),
            falkordb_password=_env("FALKORDB_PASSWORD"),
            short_term_size=_int("SHORT_TERM_SIZE", 10),
            batch_max_messages=_int("BATCH_MAX_MESSAGES", 8),
            batch_max_chars=_int("BATCH_MAX_CHARS", 6000),
            batch_idle_seconds=_float("BATCH_IDLE_SECONDS", 90.0),
            min_message_chars=_int("MIN_MESSAGE_CHARS", 10),
            capture_all_messages=_bool("CAPTURE_ALL_MESSAGES", False),
            memory_search_limit=_int("MEMORY_SEARCH_LIMIT", 8),
            memory_node_limit=_int("MEMORY_NODE_LIMIT", 4),
            max_concurrent_episodes=_int("MAX_CONCURRENT_EPISODES", 1),
            max_concurrent_chats=_int("MAX_CONCURRENT_CHATS", 4),
            graphiti_max_coroutines=_int("GRAPHITI_MAX_COROUTINES", 4),
            gemini_rpm=_int("GEMINI_RPM", 0),
            gemini_max_retries=_int("GEMINI_MAX_RETRIES", 5),
            episode_max_retries=_int("EPISODE_MAX_RETRIES", 4),
            bot_persona=_env("BOT_PERSONA"),
            dev_guild_id=int(dev_guild) if dev_guild else None,
            data_dir=Path(_env("DATA_DIR", default=str(Path(__file__).resolve().parent.parent / "data"))),
            log_level=_env("LOG_LEVEL", default="INFO").upper(),
            admin_host=_env("ADMIN_HOST", default="0.0.0.0"),
            admin_port=_int("ADMIN_PORT", 8000),
            admin_user=_env("ADMIN_USER", default="admin"),
            admin_password=_env("ADMIN_PASSWORD"),
        )

    def require(self, *fields: str) -> None:
        missing = [f for f in fields if not getattr(self, f)]
        if missing:
            hints = {
                "discord_token": "DISCORD_TOKEN (또는 TOKEN)",
                "gemini_api_key": "GEMINI_API_KEY (또는 API)",
            }
            names = ", ".join(hints.get(f, f) for f in missing)
            raise SystemExit(f"[설정 오류] 환경변수가 비어 있습니다: {names}")


settings = Settings.load()
