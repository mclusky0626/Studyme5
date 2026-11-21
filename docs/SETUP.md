# 🚀 설치 · 운영

[← README](../README.md) · [🚀 설치 · 운영](SETUP.md) · [✨ 기능 사용법](FEATURES.md) · [🛡️ 관리자](ADMIN.md) · [🧩 설계 메모](DESIGN.md)

> 처음 설치할 때 할 일, 평소 운영, 백업, 문제 해결

## ✅ 사용자가 해야 할 일 (체크리스트)

봇은 대부분 자동으로 돌아가지만, **계정·보안·네트워크 관련 설정은 직접** 해야 합니다.

### A. 처음 한 번

| # | 할 일 | 어디서 | 비고 |
|---|---|---|---|
| 1 | 봇 생성 + **MESSAGE CONTENT INTENT 켜기** | [Discord Developer Portal](https://discord.com/developers/applications) → Bot | 안 켜면 멘션해도 무반응 |
| 2 | 봇을 서버에 초대 | Developer Portal → OAuth2 → URL Generator | scopes `bot`, `applications.commands` / 권한 `View Channels`, `Send Messages`, `Read Message History` |
| 3 | Gemini API 키 발급 | [Google AI Studio](https://aistudio.google.com/apikey) | 무료 티어면 `.env`에 `GEMINI_RPM=10` 권장 |
| 4 | Docker Desktop 설치 → 실행 → 약관 동의 | 이 PC | WSL2 설치 안내가 나오면 따라 하고 재부팅 |
| 5 | `.env` 작성 | 프로젝트 폴더 | `cp .env.example .env` 후 `DISCORD_TOKEN`, `GEMINI_API_KEY`, **`ADMIN_PASSWORD`** 입력 |
| 6 | 실행 | 프로젝트 폴더 | `docker compose up -d --build` |
| 7 | (다른 기기에서 관리 웹을 쓰려면) 방화벽 허용 | 관리자 PowerShell | 아래 [다른 기기에서 접속](#다른-기기에서-접속-같은-와이파이--tailscale) 참고 |
| 8 | (선택) 페르소나 작성 | 관리 웹 → ⚙ 봇 설정, 또는 `config/persona.md` | 아래 [페르소나 바꾸기](FEATURES.md#-페르소나-바꾸기) 참고 |

### B. 가끔 / 상황별

| 상황 | 할 일 |
|---|---|
| **토큰이 유출됐거나 바꾸고 싶을 때** | Developer Portal → Bot → **Reset Token** → `.env`의 `DISCORD_TOKEN` 교체 → `docker compose up -d` |
| Gemini 키 교체 | `.env`의 `GEMINI_API_KEY` 교체 → `docker compose up -d` |
| 관리 웹 비밀번호 변경 | `.env`의 `ADMIN_PASSWORD` 교체 → `docker compose up -d admin` |
| 코드 업데이트 후 | `git pull` → `docker compose up -d --build` |
| PC 재부팅 후 | Docker Desktop이 켜지면 컨테이너는 자동 재시작 (`unless-stopped`). Docker Desktop **자동 시작**을 켜 두면 편함 (Settings → General → *Start Docker Desktop when you sign in*) |
| 주 1회 정도 | 관리 웹 **저장 로그**에서 실패(⚠️) 확인, [백업](#백업--복원) |
| 이상한 기억 발견 | 관리 웹에서 해당 사실을 **만료** 또는 **수정** (아래 [관리 원칙](#관리-원칙-만료-vs-삭제)) |

> ⚠️ `.env`에는 토큰·키·비밀번호가 들어 있습니다. **절대 GitHub에 올리거나 채팅에 붙여 넣지 마세요.** (`.gitignore`, `.dockerignore`로 이미 제외되어 있음)


---

## 🧰 운영 명령어

| 하고 싶은 것 | 명령 |
|---|---|
| 전체 시작 / 업데이트 반영 | `docker compose up -d --build` |
| 상태 보기 | `docker compose ps` |
| 봇 로그 실시간 | `docker compose logs -f bot` |
| 봇만 재시작 | `docker compose restart bot` |
| 전체 중지 (기억은 유지) | `docker compose down` |
| ⚠️ 기억까지 전부 삭제 | `docker compose down -v` |

---

## 검증 (기억이 제대로 쌓이는지)

봇을 멘션해 몇 마디 나눈 뒤 90초 기다리거나 `/memory save`:

```
[guild-123] 배치 확정(idle): 4마디 312자 → 백그라운드 저장 예약
[guild-123] ✅ 장기 기억 저장 완료 (7.1s): 4마디 → 엔티티 4, 관계 2
```

`🦀 LLM generation failed parsing as JSON` 또는 `❌ 장기 기억 저장 최종 실패`가 보이면 그 로그만 복사해 수정을 요청하세요.
(`gemini-3.5-flash-lite`로 엔티티/관계/요약 추출 + 임베딩까지 파싱 오류 없이 동작 확인. 4마디 대화 1건 ≈ 7천 토큰.)

그래프 확인: 관리 웹 → 서버 선택 → **그래프** 탭. 또는 FalkorDB Browser(3000) → Host `localhost`, Port `6379` → 그래프 `guild-<서버ID>`:

```cypher
MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) RETURN a, r, b LIMIT 100
```

---

## 백업 / 복원

```bash
# 백업 (프로젝트 폴더에 falkordb-backup.tgz 생성)
docker run --rm -v memory-bot_falkordb_data:/data -v "$PWD":/backup alpine tar czf /backup/falkordb-backup.tgz -C /data .

# 복원 (먼저 docker compose down)
docker run --rm -v memory-bot_falkordb_data:/data -v "$PWD":/backup alpine sh -c "cd /data && tar xzf /backup/falkordb-backup.tgz"
```

---

## 로컬 개발

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements-dev.txt
pytest                                              # 배칭·재시도·페르소나 단위 테스트
python main.py          # 봇 (FalkorDB 없으면 단기 기억만으로 동작, 연결되면 자동 반영)
python main.py admin    # 관리 웹 → http://localhost:8000 (FalkorDB 필요)
```

---

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 멘션해도 반응 없음 | Developer Portal에서 **MESSAGE CONTENT INTENT** 켰는지, `docker compose logs bot`에 `로그인:` 줄이 있는지 |
| 같은 토큰을 다른 봇 프로그램도 쓰는 중 | 한 토큰은 한 프로그램만 쓰세요. 둘 다 응답하거나 서로 끊깁니다 |
| `RateLimitError` / 429 반복 | `.env`에 `GEMINI_RPM=10`, `MAX_CONCURRENT_EPISODES=1` 유지 |
| 다른 기기에서 관리 웹 접속 안 됨 | [방화벽 설정](#다른-기기에서-접속-같은-와이파이--tailscale), 같은 와이파이인지, PC IP가 바뀌지 않았는지 |
| `port is already allocated` | `.env`의 `ADMIN_WEB_PORT`(기본 8090) / `FALKORDB_BROWSER_PORT`(기본 3000)를 다른 번호로 |
| 관리 웹에 공간이 안 보임 | 아직 저장된 에피소드가 없음 (90초 대기 또는 `/memory save`) |
| 저장 실패분이 걱정됨 | `bot_data` 볼륨의 `failed_episodes.jsonl`에 보관 → 봇 재시작/DB 재연결 시 자동 재처리 |
| 페르소나가 안 바뀜 | 관리 웹 상단 배지가 "관리 웹에서 설정한 값"이면 파일보다 웹 값이 우선. **파일 값으로 되돌리기** 클릭 |

---

## 🔀 AI 제공자 바꾸기 (Gemini / OpenAI / 로컬 모델)

`.env` 에서 용도별로 고를 수 있습니다. 바꾼 뒤 `docker compose up -d`.

**디스코드에서 바로 바꾸기 (`/model`, 서버 관리자/봇 주인)** — 채팅·작업 모델만, 재시작 없이 즉시 적용되고 재시작 후에도 유지됩니다.

| 명령 | 하는 일 |
|---|---|
| `/model show` | 지금 쓰는 제공자·모델 (기억·검색 제공자도 표시) |
| `/model list [provider]` | 바꿀 수 있는 모델 목록 (API 에서 조회) |
| `/model set model:<이름> [provider]` | 바꾸기 — 먼저 짧은 시험 호출을 해서 이름 오타·권한 문제면 거절하고 그대로 둠 |
| `/model reset` | `.env` 기본 모델로 되돌리기 |

기억(추출·임베딩)과 검색 제공자는 기존 기억과의 호환 때문에 `.env` 에서만 바꿉니다. 모델마다 단가가 달라 `/usage` 추정 비용도 달라집니다.

| 설정 | 무엇에 쓰나 | 값 |
|---|---|---|
| `CHAT_PROVIDER` | 채팅 답변, 백그라운드·예약 작업 (**함수 호출 포함**) | `gemini` / `openai` |
| `MEMORY_PROVIDER` | 기억 추출(Graphiti) · 임베딩 | `gemini` / `openai` |
| `SEARCH_PROVIDER` | `web_search` 도구 | `gemini`(구글 검색) / `openai`(OpenAI 웹 검색) |
| `LLM_PROVIDER` | 위 셋을 한 번에 | |

- **OpenAI**: `OPENAI_API_KEY`, `OPENAI_MODEL` (기본 `gpt-6-luna`). 함수 호출·도구·리마인더·작업이 Gemini 와 똑같이 동작합니다.
- **노트북의 로컬 모델** (Ollama, LM Studio 등 OpenAI 호환 서버): `CHAT_PROVIDER=openai` + `OPENAI_BASE_URL=http://host.docker.internal:11434/v1` + `OPENAI_MODEL=<모델 이름>`. 토큰 비용 0 이지만 **함수 호출을 지원하는 모델**이어야 도구가 동작하고, 웹 검색은 `SEARCH_PROVIDER=gemini` 로 두세요.
- **섞어 쓰기 예**: 채팅은 OpenAI, 기억·검색은 Gemini → `CHAT_PROVIDER=openai` 만 설정.
- ⚠️ `MEMORY_PROVIDER` 를 바꾸면 임베딩 모델이 달라져 **기존 기억과 검색이 맞지 않습니다.** 기존 기억을 계속 쓰려면 기억은 원래 제공자로 두세요.
- 비용 추정(`/usage`)은 모델 이름으로 단가를 고릅니다 (`gemini-*` → `PRICE_*`, 그 외 → `OPENAI_PRICE_*`). OpenAI 단가는 기본 0 이니 쓰는 모델 가격으로 채우세요. 기억을 OpenAI 로 돌릴 때의 기억 저장 비용은 집계되지 않습니다.

