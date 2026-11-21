# Studyme v5 The best and the last model

Graphiti(시간 인식 지식 그래프) + FalkorDB + Gemini 로 만든, **대화를 오래 기억하고 계산·날씨·검색까지 해 주는 디스코드 봇**입니다.
`docker compose` 한 번으로 FalkorDB · 봇 · 기억 관리 웹이 함께 뜹니다.
앞으로의 스터디미 개발은 없을 것이고 현재 기능을 추가하고 유지보수 하는 방식일 것입니다.

 **자세한 설명은 [상세 가이드 (docs/GUIDE.md)](docs/GUIDE.md)** — [설치·운영](docs/SETUP.md) · [기능](docs/FEATURES.md) · [관리자](docs/ADMIN.md) · [설계](docs/DESIGN.md)
이 봇 설명은 나의 봇인 라스페치아 기준으로 작성을 해놓았다. 니들 입맛에 따라 봇 이름 바꾸고 페르소나 바꿔라
```mermaid
flowchart LR
    U[디스코드 사용자] -- "@멘션 / DM / 답장" --> B[bot]
    B -- "단기 기억 (채널 최근 10개)" --> B
    B -- "장기 기억 검색" --> F[(FalkorDB<br/>서버별 그래프)]
    B -- "답변 + 함수 호출" --> G[Gemini<br/>3.5-flash-lite]
    G -. "계산·날씨·검색·환율·리마인더" .-> T[도구]
    B -- "8마디 or 90초 무응답 → 백그라운드 저장" --> F
    A[관리 웹 :8090] --> F
```

## 기능

- **장기 기억** — 대화를 지식 그래프로 저장하고, 답할 때 관련 기억을 떠올림. 서버(`guild-…`)별로 완전히 분리
- **토큰 절약** — 메시지마다 저장하지 않고 8마디/90초 단위로 묶어 백그라운드 저장, 429 자동 재시도
- **검색** — 구글 검색 그라운딩으로 최신 정보를 찾아 출처와 함께 답함 (월 5,000회 무료), 필요하면 원문 페이지까지 읽음 ([검색 동작](docs/FEATURES.md#️-대화-도구-gemini-function-calling))
- **대화 도구** — 계산, 날씨, 구글 검색, 환율, D-day, 세계 시각, 메뉴 고르기·주사위, **리마인더**, 기억 재검색 ([도구 목록](docs/FEATURES.md#️-대화-도구-gemini-function-calling))
- **백그라운드 작업** — "조사해서 나중에 알려줘" → 뒤에서 검색·페이지 읽기를 여러 번 하고 결과를 답장으로 올림 (단계·시간·토큰·하루 횟수 한도) ([작업](docs/FEATURES.md#-백그라운드--예약-작업))
- **예약 작업** — "매일 아침 8시에 날씨랑 뉴스 요약해줘" 같은 반복 작업
- **유저별 기억** — 서버 기억과 별개로 사람마다 개인 기억 그래프(`user-…`)가 쌓이고, 관리 웹에서 유저별 관계도를 볼 수 있음 ([유저 기억](docs/FEATURES.md#-유저별-기억))
- **관리 웹** — 기억 그래프 시각화, 사실 수정·만료·삭제, 원본 대화, 의미 검색, 저장 로그 ([관리 체계](docs/ADMIN.md#️-관리-체계-admin))
- **AI 제공자 선택** — Gemini 기본, `.env` 로 OpenAI 나 노트북의 로컬 모델(Ollama 등 OpenAI 호환)로 전환. 함수 호출·도구 그대로 동작 ([제공자 바꾸기](docs/SETUP.md#-ai-제공자-바꾸기-gemini--openai--로컬-모델))
- **외부 접속** — 관리 웹을 집 밖·다른 사람에게 `https://` 주소로 공개 (Tailscale Funnel 또는 Cloudflare Tunnel, 로그인 잠금·보안 헤더 적용) ([외부 접속](docs/ADMIN.md#-관리-웹-외부-접속))
- **페르소나** — 관리 웹 ⚙ 봇 설정 또는 `config/persona.md` 로 말투·성격 변경, 재시작 불필요 ([페르소나](docs/FEATURES.md#-페르소나-바꾸기))

## 빠른 시작

1. Discord Developer Portal 에서 봇을 만들고 **MESSAGE CONTENT INTENT** 를 켠 뒤 서버에 초대
2. Docker Desktop 설치 후 실행
3. 설정 파일 만들기 → `DISCORD_TOKEN`, `GEMINI_API_KEY`, `ADMIN_PASSWORD` 입력
   ```bash
   cp .env.example .env
   ```
4. 실행
   ```bash
   docker compose up -d --build
   ```
5. 디스코드에서 `@라스페치아 안녕` → 관리 웹 `http://localhost:8090` (아이디 `admin`)

다른 기기에서 관리 웹 접속, 토큰 교체, 백업 등 **사용자가 직접 해야 할 일**은 [체크리스트](docs/SETUP.md#-사용자가-해야-할-일-체크리스트)에 정리되어 있습니다.

## 자주 쓰는 명령

| 하고 싶은 것 | 명령 |
|---|---|
| 시작 / 업데이트 반영 | `docker compose up -d --build` |
| 봇 로그 보기 | `docker compose logs -f bot` |
| 중지 (기억 유지) | `docker compose down` |

## 디스코드에서

- `/help` 사용법 (주제별: 처음 시작 · 대화·기억 · 도구 · 알림·작업 · 프라이버시 · 관리자) — `@라스페치아 도움말` 이라고 해도 됨
- `@라스페치아 …` / 봇 메시지에 답장 / DM → 대화
- `/memory recall <질문>` 기억 검색 · `/memory me` 나에 대한 기억 · `/memory forget_me` 내 개인 기억 삭제 · `/memory save` · `/memory status`
- `/task start <할 일>` 백그라운드 작업 · `/task list` · `/task cancel <번호>` (말로 "~조사해서 알려줘", "매일 8시에 ~해줘" 해도 됨)
- `/model show|list|set|reset` 채팅 모델 보기·바꾸기 — 재시작 없이 바로 적용, 시험 호출로 확인 (서버 관리자/봇 주인)
- `/usage [기간]` Gemini API 사용량·추정 비용 (서버 관리자/봇 주인) — 관리 웹 **📊 사용량**에서도 확인

## 프로젝트 구조

```
app/
  bot.py            디스코드 봇 (메시지 처리, 슬래시 명령, 리마인더 발송)
  chat.py           답변 생성 + 함수 호출 루프
  backends.py       Gemini / OpenAI 어댑터 (함수 호출 형식 변환)
  modelswitch.py    /model 실행 중 모델 전환
  agent.py          백그라운드 조사 에이전트 (긴 루프 + 한도)
  tasks.py          백그라운드·예약 작업 관리 (대기열, 예약, 재시작 복구)
  usage.py          Gemini 사용량 기록·추정 비용
  memory.py         Graphiti(FalkorDB + Gemini) 장기 기억
  batcher.py        배칭·디바운스 저장, 재시도, 실패 보관
  persona.py        봇 이름·페르소나 (웹 > 파일 > .env > 기본값)
  help.py           /help 도움말 내용
  tools/            함수 호출 도구 (basic, web, reminders, tasks_tool, memory_tool)
  admin/            관리 웹 (FastAPI + 정적 프런트엔드)
config/persona.md   페르소나 파일
docs/GUIDE.md       상세 가이드
```
