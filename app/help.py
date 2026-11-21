"""/help 도움말: 처음 쓰는 사람도 기능을 한눈에 알 수 있게 주제별로 정리."""

from __future__ import annotations

import discord

COLOR = 0x7C5CFF

TOPICS = {
    "start": "처음 시작",
    "chat": "대화 · 기억",
    "tools": "할 수 있는 일 (도구)",
    "tasks": "알림 · 백그라운드 · 예약",
    "privacy": "내 정보 · 프라이버시",
    "admin": "관리자",
}

HELP_WORDS = {"도움말", "도움", "사용법", "명령어", "help", "헬프", "뭐할수있어", "뭐 할 수 있어", "뭐해줄수있어", "기능"}


def is_help_request(text: str) -> bool:
    t = text.strip().rstrip("?!. ").lower()
    return t in HELP_WORDS or t.replace(" ", "") in {w.replace(" ", "") for w in HELP_WORDS}


def build_help(topic: str, bot_name: str) -> discord.Embed:
    n = bot_name
    if topic == "chat":
        e = discord.Embed(title=f"💬 대화 · 🧠 기억", color=COLOR, description=(
            f"**말 거는 법**\n"
            f"• 서버: `@{n} 안녕` 처럼 멘션하거나, {n}의 메시지에 **답장**\n"
            f"• DM: 그냥 말하면 됩니다\n\n"
            f"**기억하는 방식**\n"
            f"• 채널의 최근 대화 10개를 보고 흐름을 이어 갑니다 (단기 기억)\n"
            f"• 대화가 8마디 쌓이거나 90초 조용해지면 **장기 기억**으로 저장돼요\n"
            f"• **서버 기억**: 이 서버에서 나눈 이야기 (다른 서버와 섞이지 않음)\n"
            f"• **개인 기억**: 어느 서버·DM에서든 나에 대해 말한 것 — 다른 서버에서도 기억해요\n"
            f"• 틀리게 기억했으면 그냥 말로 정정해 주세요 (\"아니야, 나 이제 서울 살아\")"))
        e.add_field(name="기억 명령", value=(
            "`/memory recall 질문` 기억에서 찾아보기\n"
            "`/memory me` 나에 대해 기억하는 것 보기\n"
            "`/memory save` 지금까지 대화 바로 저장\n"
            "`/memory status` 저장 대기 상황"), inline=False)
        return e
    if topic == "tools":
        e = discord.Embed(title="🛠️ 할 수 있는 일", color=COLOR,
                          description="명령어를 외울 필요 없이 **그냥 말하면** 알아서 처리해요.")
        for name, value in [
            ("🧮 계산", "\"18,500원짜리 7명에 봉사료 10%면?\""),
            ("🌤️ 날씨", "\"내일 부산 날씨 어때? 우산 챙겨?\""),
            ("🔎 검색", "\"요즘 엔비디아 주가 어때?\" · \"RTX 5080 두 모델 쿨링 비교해줘\""),
            ("🔗 웹페이지", "\"이 링크 내용 요약해줘 https://…\" (일부 사이트는 막혀 있음)"),
            ("💱 환율", "\"100달러면 원화로 얼마야?\""),
            ("📅 날짜", "\"크리스마스까지 며칠?\" · \"오늘부터 100일 뒤는?\" · \"뉴욕 지금 몇 시?\""),
            ("🎲 랜덤", "\"점심 골라줘: 김치찌개, 돈까스, 쌀국수\" · \"주사위 굴려줘\""),
        ]:
            e.add_field(name=name, value=value, inline=False)
        return e
    if topic == "tasks":
        e = discord.Embed(title="⏰ 알림 · 📋 백그라운드 · 🗓️ 예약", color=COLOR)
        e.add_field(name="⏰ 리마인더 (단순 알림)", value=(
            "\"30분 뒤에 빨래 꺼내라고 알려줘\" · \"내일 9시에 회의 알려줘\"\n"
            "\"내 알림 뭐 있어?\" · \"그 알림 취소해\"\n"
            "→ 시간이 되면 이 채널에서 멘션해요. **무엇을** 알려줄지 같이 말해 주세요."), inline=False)
        e.add_field(name="📋 백그라운드 작업 (오래 걸리는 조사)", value=(
            "\"OO **자세히 조사해서 나중에 알려줘**\" 처럼 말하면 뒤에서 여러 번 검색·정리한 뒤 결과를 답장으로 올려요.\n"
            "진행 상황은 메시지가 계속 바뀌며 보여요 (🔄 3/12단계).\n"
            "`/task start 할 일` · `/task list` · `/task cancel 번호`"), inline=False)
        e.add_field(name="🗓️ 예약 작업 (정해진 시각에 조사·요약)", value=(
            "\"매일 아침 8시에 서울 날씨랑 뉴스 3개 요약해줘\"\n"
            "\"매주 금요일 18시에 주말 영화 추천해줘\"\n"
            "→ 그 시각마다 작업을 실행해 결과를 올려요. 확인·취소는 `/task list` · `/task cancel`"), inline=False)
        e.set_footer(text="작업은 1인당 동시 2개 · 하루 10회 · 예약 5개까지 (서버 설정에 따라 다를 수 있음)")
        return e
    if topic == "privacy":
        return discord.Embed(title="🔒 내 정보 · 프라이버시", color=COLOR, description=(
            f"• {n}과(와) 나눈 대화는 **서버 기억**과 **개인 기억**에 저장돼요. 다른 사람과 하는 대화는 기본적으로 저장하지 않아요.\n"
            f"• 개인 기억은 다른 서버에서도 쓰일 수 있어요. 대신 여러 사람이 보는 곳에서 민감한 이야기(건강·연애·주소·돈)는 먼저 꺼내지 않도록 되어 있어요.\n"
            f"• `/memory me` — 나에 대해 무엇을 기억하는지 보기 (나에게만 보임)\n"
            f"• `/memory forget_me confirm:삭제` — **내 개인 기억 전체 삭제** (서버 기억은 서버 관리자가 관리)\n"
            f"• 서버 기억에서 특정 내용을 지우고 싶으면 서버 관리자에게 요청하세요."))
    if topic == "admin":
        return discord.Embed(title="🛡️ 관리자", color=COLOR, description=(
            "• `/usage` — Gemini API 사용량·추정 비용 (서버 관리 권한 필요)\n"
            "• `/model show · list · set · reset` — 채팅 모델 보기·바꾸기 (재시작 없이 적용)\n"
            "• **관리 웹** (봇을 운영하는 PC에서 실행) — 기억 그래프 보기, 사실 수정·만료·삭제, 작업 관리, 페르소나 변경, 사용량\n"
            "• 자세한 운영 방법은 GitHub 의 `docs/GUIDE.md` 를 참고하세요."))
    # start (기본)
    e = discord.Embed(title=f"👋 {n} 사용법", color=COLOR, description=(
        f"저는 대화를 **오래 기억하는** 디스코드 친구예요. `@{n}` 으로 불러 주세요!\n"
        f"자세한 건 `/help 주제` 로 골라 볼 수 있어요."))
    e.add_field(name="💬 대화", value=f"`@{n} 안녕` · 답장 · DM\n지난 이야기를 기억해서 이어가요", inline=True)
    e.add_field(name="🛠️ 도구", value="계산 · 날씨 · 검색 · 환율\nD-day · 고르기 · 웹페이지 요약", inline=True)
    e.add_field(name="⏰ 알림", value="\"30분 뒤에 ○○ 알려줘\"", inline=True)
    e.add_field(name="📋 조사 맡기기", value="\"○○ 자세히 조사해서\n나중에 알려줘\"", inline=True)
    e.add_field(name="🗓️ 예약", value="\"매일 8시에 날씨랑\n뉴스 요약해줘\"", inline=True)
    e.add_field(name="🧠 내 기억", value="`/memory me`\n`/memory forget_me`", inline=True)
    e.add_field(name="주제별 도움말", value=" · ".join(f"`/help {v}`" for v in TOPICS.values()), inline=False)
    return e
