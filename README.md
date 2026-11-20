# 🧠 디스코드 장기 기억 AI 봇

Graphiti(시간 인식 지식 그래프) + FalkorDB + Gemini 로 만든, **대화를 오래 기억하는 디스코드 봇**입니다.
홈서버에서 `docker compose` 한 번으로 FalkorDB · 봇 · 기억 관리 웹이 함께 뜹니다.

```mermaid
flowchart LR
    U[디스코드 사용자] -- "@멘션 / DM / 답장" --> B[bot 컨테이너]
    B -- "① 단기 기억: 채널 최근 10개 (RAM)" --> B
    B -- "② 장기 기억 검색 (BM25+임베딩, RRF)" --> F[(FalkorDB<br/>group_id = 그래프)]
    B -- "③ 답변 생성" --> G[Gemini<br/>gemini-3.5-flash-lite]
    B -- "④ 답변 전송 후 버퍼에 적재" --> Q[채널별 버퍼]
    Q -- "8마디 or 90초 무응답 → 백그라운드 add_episode" --> F
    A[admin 컨테이너<br/>관리 웹 :8080] --> F
    FB[FalkorDB Browser :3000] --> F
```

## 핵심 설계

| 요구사항 | 구현 |
|---|---|
| OpenAI/Neo4j 기본값 오버라이드 | `FalkorDriver` + `GeminiClient` / `GeminiEmbedder` / `GeminiRerankerClient` 를 직접 주입 ([app/memory.py](app/memory.py)) |
| 비동기 논블로킹 저장 | 답변을 **보낸 뒤에** 버퍼에 넣고, 저장은 `asyncio` 백그라운드 태스크 ([app/bot.py](app/bot.py)) |
| 토큰 방어 배칭 | 채널별 버퍼에 모아 **8마디** 또는 **90초 무응답(디바운스)** 시 한 번에 `add_episode` ([app/batcher.py](app/batcher.py)) |
| 기억 공간 격리 | 서버 = `guild-<guild_id>`, DM = `dm-<user_id>` 를 `group_id` 로 사용 → FalkorDB 에서는 **그래프 자체가 분리**됨 |
| 429 방어 | 공용 RPM 제한기 + 호출 단위 지수 백오프 재시도 + 에피소드 동시 실행 세마포어 + 그룹별 순차 처리 ([app/gemini_resilience.py](app/gemini_resilience.py)) |
| 크래시 방지 | 저장 실패는 로그만 남기고 `data/failed_episodes.jsonl` 에 보관 → 다음 기동 때 자동 재처리 |
| 운영 | `restart: unless-stopped`, FalkorDB AOF 영속화 볼륨, `docker stop` 시 남은 버퍼 저장 후 종료 |

### 가이드라인과 다르게 판단한 부분 (의도적)

- **임베딩 모델**: `gemini-3.5-flash-lite` 는 생성형 모델이라 임베딩을 만들 수 없습니다 (`embedContent` 미지원). LLM·리랭커·채팅은 `gemini-3.5-flash-lite` 로 통일하고 임베딩만 `gemini-embedding-001` 을 씁니다.
- **429 재시도 위치**: Graphiti 의 `GeminiClient` 는 429 를 받으면 재시도 없이 즉시 실패해 그때까지 쓴 토큰이 전부 버려집니다. 그래서 클라이언트를 서브클래싱해 **개별 호출 단위**로 백오프 재시도하도록 바꿨습니다.
- **임베딩 배치**: Graphiti 는 `gemini-embedding-001` 을 1건씩 호출하도록 하드코딩돼 있지만, 실제로는 배치를 지원해서 `batch_size=100` 으로 덮어써 요청 수를 크게 줄였습니다.
- **사고 토큰**: 그래프 추출은 `thinking_level=minimal`, 채팅은 `low` 로 사고 토큰을 줄였습니다.
- **10자 규칙 보정**: 봇과 무관한 대화는 10자 미만이면 무시합니다. 다만 봇에게 **직접** 한 말은 "내 이름은 건우야"(9자)처럼 짧은 사실일 수 있어서 `ㅋㅋ`, `ㅇㅇ`, 이모지 같은 추임새만 거릅니다.
- **기억 검색**: 답변용 검색은 LLM 리랭커 없이 RRF(BM25 + 벡터)만 써서 **질문당 임베딩 1회** 외에는 토큰을 쓰지 않습니다. 아직 기억이 없는 공간은 검색 자체를 건너뜁니다.
- **group_id 접두어**: 숫자 ID만 쓰면 서버와 DM을 구분할 수 없어 `guild-`/`dm-` 접두어를 붙였습니다.
- **기억 범위**: 기본값은 봇과 주고받은 대화만 저장합니다. 채널의 모든 대화를 기억하게 하려면 `CAPTURE_ALL_MESSAGES=true` 로 켜면 됩니다(토큰 사용량이 늘어남).
- **텔레메트리**: Graphiti 의 익명 텔레메트리는 기본으로 꺼 두었습니다.

## 1. 준비

1. [Discord Developer Portal](https://discord.com/developers/applications) → 봇 생성
   - **Bot → Privileged Gateway Intents → `MESSAGE CONTENT INTENT` 켜기** (필수)
   - OAuth2 → URL Generator: scopes `bot`, `applications.commands` / 권한 `View Channels`, `Send Messages`, `Read Message History`
2. [Google AI Studio](https://aistudio.google.com/apikey) 에서 Gemini API 키 발급
3. 홈서버에 Docker + Docker Compose 설치

## 2. 설정

```bash
cp .env.example .env   # 이미 .env 가 있다면 필요한 항목만 추가
```

| 변수 | 설명 |
|---|---|
| `DISCORD_TOKEN` (또는 `TOKEN`) | 디스코드 봇 토큰 |
| `GEMINI_API_KEY` (또는 `API`) | Gemini API 키 |
| `ADMIN_PASSWORD` | 관리 웹 비밀번호 (**꼭 설정**, 비우면 인증 없이 열림) |
| `GEMINI_RPM` | 무료 티어라면 `10`~`15` 권장 (0 = 제한 없음) |

나머지 튜닝 값은 [.env.example](.env.example) 의 주석을 참고하세요. `.env` 는 git 과 Docker 이미지 양쪽에서 제외됩니다.

## 3. 실행

```bash
docker compose up -d --build
docker compose ps
```

| 서비스 | 주소 | 용도 |
|---|---|---|
| bot | – | 디스코드 봇 |
| admin | `http://<홈서버>:8080` | 기억 관리 웹 (그래프·사실·에피소드 조회/수정/삭제) |
| falkordb | `http://<홈서버>:3000` | FalkorDB Browser (원본 그래프 탐색) |

## 4. 검증 (작업 순서 2~3단계)

**② 파싱 검증** — 봇을 멘션해 대화를 몇 마디 나눈 뒤 로그를 봅니다.

```bash
docker compose logs -f bot
```

```
[guild-123] 배치 확정(idle): 4마디 312자 → 백그라운드 저장 예약
[guild-123] ✅ 장기 기억 저장 완료 (14.2s): 4마디 → 엔티티 5, 관계 4
```

`🦀 LLM generation failed parsing as JSON` 또는 `❌ 장기 기억 저장 최종 실패` 가 보이면 그 로그 블록만 복사해 수정을 요청하면 됩니다.
(개발 중 `gemini-3.5-flash-lite` 로 Graphiti 의 엔티티/관계/요약 추출 + 임베딩까지 오류 없이 파싱되는 것을 확인했습니다. 4마디 대화 1건 ≈ 입력 5.7k + 출력 1.2k 토큰.)

**③ 그래프 시각화** — 대화가 끝나고 90초 뒤:

- 관리 웹 `http://<홈서버>:8080` → 왼쪽에서 서버 선택 → **그래프** 탭
- 또는 FalkorDB Browser `http://<홈서버>:3000` → Host `localhost`, Port `6379` 로 접속 → 그래프 `guild-<서버ID>` 선택 후

```cypher
MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) RETURN a, r, b LIMIT 100
```

## 디스코드 사용법

- 서버: `@봇 안녕` / 봇 메시지에 답장 · DM: 그냥 말하기
- `/memory recall <질문>` — 이 서버(또는 DM)의 장기 기억 검색 (나에게만 보임)
- `/memory save` — 이 채널 버퍼를 지금 바로 저장
- `/memory status` — 버퍼·저장 현황

슬래시 명령은 전역 등록이라 처음엔 반영까지 최대 1시간 걸립니다. `DEV_GUILD_ID` 를 넣으면 해당 서버에 즉시 반영됩니다.

## 관리 웹 기능

- 기억 공간(서버/DM) 목록과 엔티티·사실·에피소드 수
- **그래프**: 엔티티-관계 네트워크(만료된 사실은 빨간 점선), 에피소드 노드 표시, 엔티티 검색
- **엔티티 상세**: 요약 수정, 연결된 사실, 언급된 대화, 삭제
- **사실**: 필터, 문장 수정(임베딩 자동 재계산), **만료 처리**(봇이 '과거 사실'로 취급), 복원, 삭제
- **에피소드**: 저장된 원본 대화, 삭제 시 그 대화에서만 생긴 엔티티/관계도 정리
- **의미 검색**: 봇과 같은 하이브리드 검색 결과와 점수
- **저장 로그**: 봇의 저장 성공/실패 기록, 봇 온라인 상태

## 로컬 개발

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements-dev.txt
pytest                                              # 배칭/재시도 단위 테스트
python main.py          # 봇 (FalkorDB 필요: FALKORDB_HOST=localhost)
python main.py admin    # 관리 웹 → http://localhost:8000
```

## 백업 / 복원

```bash
docker run --rm -v memory-bot_falkordb_data:/data -v "$PWD":/backup alpine tar czf /backup/falkordb-backup.tgz -C /data .
```

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 멘션해도 반응 없음 | Developer Portal 에서 **MESSAGE CONTENT INTENT** 켰는지 |
| `RateLimitError` / 429 반복 | `.env` 에 `GEMINI_RPM=10` 설정, `MAX_CONCURRENT_EPISODES=1` 유지 |
| 저장 실패분이 걱정됨 | `bot_data` 볼륨의 `failed_episodes.jsonl` → 봇 재시작 시 자동 재처리 |
| 관리 웹에 공간이 안 보임 | 아직 저장된 에피소드가 없는 경우 (90초 대기 또는 `/memory save`) |
