"""디스코드 봇 본체."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .agent import run_agent
from .batcher import EpisodeBatcher
from .chat import ChatResponder, ShortLine
from .config import Settings
from .gemini_resilience import RateLimiter
from .memory import (
    MemoryContext,
    MemoryService,
    group_id_for,
    legacy_dm_group_id,
    user_extraction_hint,
    user_group_id,
)
from .persona import PersonaStore
from .tasks import Messenger, TaskManager, TaskStore, describe_schedule
from .tools import ToolError
from .tools.reminders import ReminderStore
from .usage import format_report, report, tracker, usd_krw

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

DISCORD_LIMIT = 1900
MENTION_USERS = discord.AllowedMentions(users=True, everyone=False, roles=False, replied_user=False)


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """디스코드 2000자 제한에 맞춰 줄 단위로 자른다."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def user_label(user: discord.abc.User) -> str:
    """서버 닉네임과 무관하게 일정한 이름 (유저 기억의 엔티티 이름이 흔들리지 않게)."""
    return getattr(user, "global_name", None) or user.name


class MemoryBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # Developer Portal 에서도 켜야 함
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True,
                                                     replied_user=False),
        )
        self.s = settings
        self.tree = app_commands.CommandTree(self)
        self.limiter = RateLimiter(settings.gemini_rpm)
        self.memory = MemoryService(settings, self.limiter)
        self.chat = ChatResponder(settings, self.limiter)
        redis = lambda: self.memory.redis if self.memory.available else None  # noqa: E731
        tracker.bind(redis)  # 모든 Gemini 호출의 토큰을 일별로 기록
        # 이름·페르소나: 관리 웹 > persona.md > .env > 기본값 (재시작 없이 반영)
        self.persona = PersonaStore(settings, redis)
        self.reminders = ReminderStore(redis)
        self.tasks = TaskManager(settings, TaskStore(redis),
                                 Messenger(send=self._task_send, edit=self._task_edit),
                                 agent_runner=self._run_task_agent)
        self.batcher = EpisodeBatcher(settings, self.memory.save_episode,
                                      on_event=self.memory.log_event)
        # 단기 기억: 채널별 최근 N개 (인메모리)
        self.short_term: dict[int, deque[ShortLine]] = defaultdict(
            lambda: deque(maxlen=settings.short_term_size)
        )
        self._registered_spaces: set[str] = set()
        self._bg: list[asyncio.Task] = []
        self._closing = False
        self._register_commands()

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        if await self.memory.start():
            await self.batcher.replay_failed()
        try:
            if self.s.dev_guild_id:
                guild = discord.Object(id=self.s.dev_guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("슬래시 명령을 개발 서버(%s)에 즉시 동기화했습니다.", self.s.dev_guild_id)
            else:
                await self.tree.sync()
                log.info("슬래시 명령을 전역 동기화했습니다. (반영까지 최대 1시간)")
        except Exception as exc:  # noqa: BLE001
            log.warning("슬래시 명령 동기화 실패(봇은 계속 동작): %s", exc)
        self._bg = [asyncio.create_task(self._status_loop()),
                    asyncio.create_task(self._reminder_loop()),
                    asyncio.create_task(self._tasks_loop())]

    async def on_ready(self) -> None:
        log.info("로그인: %s (id=%s) — 서버 %d개, 모델 %s", self.user, self.user.id,
                 len(self.guilds), self.s.chat_model)
        await self.change_presence(activity=discord.Game(name="@멘션하면 대답해요"))

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("종료 중... 남은 대화 버퍼를 장기 기억에 저장합니다.")
        for task in self._bg:
            task.cancel()
        try:
            await self.batcher.shutdown(timeout=100)
        finally:
            await self.chat.aclose()
            await self.memory.close()
            await super().close()

    async def _status_loop(self) -> None:
        while True:
            try:
                if not self.memory.available and await self.memory.try_reconnect():
                    log.info("FalkorDB 재연결 성공 — 보관해 둔 대화를 장기 기억에 반영합니다.")
                    await self.batcher.replay_failed()
                await self.memory.write_status(await self._status())
                await self.memory.prune_cache()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(30)

    async def _reminder_loop(self) -> None:
        await self.wait_until_ready()
        while True:
            try:
                for item in await self.reminders.pop_due():
                    await self._deliver_reminder(item)
            except Exception as exc:  # noqa: BLE001
                log.warning("리마인더 처리 오류: %s", exc)
            await asyncio.sleep(15)

    async def _tasks_loop(self) -> None:
        await self.wait_until_ready()
        try:
            await self.tasks.recover()
        except Exception as exc:  # noqa: BLE001
            log.warning("작업 복구 실패: %s", exc)
        await self.tasks.loop()

    async def _deliver_reminder(self, item: dict) -> None:
        text = f"⏰ <@{item['user_id']}> 리마인더: {item['text']}"
        try:
            channel = self.get_channel(item["channel_id"]) or await self.fetch_channel(item["channel_id"])
            await channel.send(text, allowed_mentions=MENTION_USERS)
        except Exception:  # noqa: BLE001 - 채널이 없어졌으면 DM 으로라도
            try:
                user = self.get_user(item["user_id"]) or await self.fetch_user(item["user_id"])
                await user.send(f"⏰ 리마인더: {item['text']}")
            except Exception as exc:  # noqa: BLE001
                log.warning("리마인더 전달 실패 %s: %s", item.get("id"), exc)
                return
        log.info("⏰ 리마인더 전달: %s", item.get("id"))

    async def _status(self) -> dict:
        tasks = await self.tasks.store.all()
        return {
            "bot": str(self.user) if self.user else None,
            "guilds": len(self.guilds),
            "chat_model": self.s.chat_model,
            "graph_model": self.s.llm_model,
            "embedding_model": self.s.embedding_model,
            "capture_all_messages": self.s.capture_all_messages,
            "user_memory": self.s.user_memory,
            "user_memory_mode": self.s.user_memory_mode,
            "memory_online": self.memory.available,
            "tasks_running": sum(t["status"] == "running" for t in tasks),
            "tasks_queued": sum(t["status"] == "queued" for t in tasks),
            "tasks_scheduled": sum(t["status"] == "scheduled" for t in tasks),
            **self.batcher.stats(),
        }

    # ------------------------------------------------------------------ 백그라운드 작업 연결
    async def _channel(self, channel_id: int):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def _task_send(self, channel_id: int, text: str, reply_to: int | None = None,
                         user_id: int | None = None) -> int | None:
        channel = await self._channel(channel_id)
        first_id = None
        for i, chunk in enumerate(split_message(text)):
            if i == 0 and reply_to:
                try:
                    msg = await channel.get_partial_message(reply_to).reply(
                        chunk, mention_author=False, allowed_mentions=MENTION_USERS)
                except discord.HTTPException:  # 원본 메시지가 지워졌으면 그냥 보낸다
                    msg = await channel.send(chunk, allowed_mentions=MENTION_USERS)
            else:
                msg = await channel.send(chunk, allowed_mentions=MENTION_USERS)
            first_id = first_id or msg.id
        return first_id

    async def _task_edit(self, channel_id: int, message_id: int, text: str) -> None:
        channel = await self._channel(channel_id)
        await channel.get_partial_message(message_id).edit(content=text[:DISCORD_LIMIT])

    async def _run_task_agent(self, task: dict, progress, is_cancelled):
        bot_name, persona = await self.persona.get()
        ctx = self.chat.tool_context(memory=self.memory, group_id=task.get("group_id") or None,
                                     channel_id=task["channel_id"], user_id=task["user_id"],
                                     user_name=task.get("user_name"), is_dm=task.get("is_dm", False))
        return await run_agent(self.chat, task["goal"], ctx=ctx, limits=self.tasks.agent_limits(),
                               bot_name=bot_name, persona=persona, model=self.s.task_model,
                               on_progress=progress, is_cancelled=is_cancelled)

    # ------------------------------------------------------------------ helpers
    def _space(self, message_or_inter) -> tuple[str, str, str, str]:
        """(group_id, 공간 이름, 종류, 출처 설명)"""
        guild = message_or_inter.guild
        channel = message_or_inter.channel
        user = getattr(message_or_inter, "author", None) or message_or_inter.user
        gid = group_id_for(guild.id if guild else None, user.id)
        if guild:
            ch_name = getattr(channel, "name", "channel")
            return gid, guild.name, "guild", f"Discord 서버 '{guild.name}' #{ch_name}"
        return gid, f"👤 {user_label(user)}", "user", f"Discord DM ({user_label(user)})"

    async def _ensure_space(self, gid: str, name: str, kind: str) -> None:
        if gid not in self._registered_spaces:
            self._registered_spaces.add(gid)
            await self.memory.register_space(gid, name, kind)

    async def _recall(self, gid: str, user: discord.abc.User, query: str) -> MemoryContext:
        """서버 기억 + 말하는 사람의 개인 기억(+ 예전 DM 기억)을 함께 찾는다."""
        ugid = user_group_id(user.id)
        if gid == ugid:  # DM: 개인 기억이 곧 이 공간
            main, legacy = await asyncio.gather(self.memory.search(ugid, query),
                                                self.memory.search(legacy_dm_group_id(user.id), query))
            main.facts += [f for f in legacy.facts if f not in main.facts]
            main.entities += [e for e in legacy.entities if e not in main.entities]
            return main
        if not self.s.user_memory:
            return await self.memory.search(gid, query)
        server, personal = await asyncio.gather(self.memory.search(gid, query),
                                                self.memory.search(ugid, query))
        return server.merge_personal(personal)

    def _is_direct(self, message: discord.Message) -> bool:
        if message.guild is None:
            return True
        if self.user in message.mentions:
            return True
        ref = message.reference
        if ref is not None and isinstance(ref.resolved, discord.Message):
            return ref.resolved.author.id == self.user.id
        return False

    def _clean(self, message: discord.Message) -> str:
        text = message.clean_content
        if self.user:
            # @봇이름 멘션 제거 (계정명, 서버 닉네임)
            nick = message.guild.me.display_name if message.guild and message.guild.me else None
            for name in {self.user.display_name, self.user.name, nick} - {None}:
                text = text.replace(f"@{name}", "")
        text = re.sub(r"\s+", " ", text).strip()
        if message.attachments and not text:
            text = "[첨부파일]"
        return text

    # ------------------------------------------------------------------ events
    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.author.id == self.user.id or message.author.bot:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        text = self._clean(message)
        if not text:
            return

        direct = self._is_direct(message)
        gid, space_name, kind, source = self._space(message)
        await self._ensure_space(gid, space_name, kind)
        speaker = message.author.display_name
        history = self.short_term[message.channel.id]
        snapshot = list(history)
        current = ShortLine(speaker=speaker, text=text)
        history.append(current)

        channel_label = f"#{getattr(message.channel, 'name', 'dm')}" if message.guild else "DM"
        server_buf = dict(key=f"{gid}:{message.channel.id}", group_id=gid, channel_label=channel_label,
                          source_description=source)
        # 유저별 기억: 서버·DM 어디서든 이 사람과 나눈 대화를 개인 공간에도 모은다 (서버 기억은 그대로)
        uname = user_label(message.author)
        ugid = user_group_id(message.author.id)
        user_buf = None
        if message.guild is None or self.s.user_memory:
            where = f"{message.guild.name} {channel_label}" if message.guild else "DM"
            user_buf = dict(key=f"{ugid}:all", group_id=ugid, channel_label=f"{uname} · {where}",
                            source_description=f"{uname}와(과)의 대화 ({where})",
                            extraction_hint=user_extraction_hint(uname))
            # lite: 서버 대화를 개인 공간에 넣을 때는 더 크게 묶고, 봇 답변은 짧은 맥락만 남겨 토큰 절약
            # (DM 은 개인 공간에만 저장되므로 중복이 없어 그대로 저장)
            if message.guild is not None and self.s.user_memory_mode == "lite":
                user_buf.update(max_messages=self.s.user_lite_batch_messages,
                                idle_seconds=self.s.user_lite_idle_seconds,
                                bot_line_max=self.s.user_lite_bot_chars)
            await self._ensure_space(ugid, f"👤 {uname}", "user")

        if not direct:
            if self.s.capture_all_messages:
                self.batcher.add(speaker=speaker, text=text, at=message.created_at,
                                 direct=False, **server_buf)
            return

        reply_text: str | None = None
        bot_name, persona = await self.persona.get()
        started = asyncio.get_running_loop().time()
        log.info("💬 [%s] %s: %s", gid, speaker, text[:80].replace("\n", " "))
        try:
            async with message.channel.typing():
                memory_ctx = await self._recall(gid, message.author, f"{speaker} {text}")
                reply_text = await self.chat.reply(
                    bot_name=bot_name,
                    place=space_name,
                    history=snapshot,
                    current=current,
                    memory=memory_ctx,
                    persona=persona,
                    tool_ctx=self.chat.tool_context(
                        memory=self.memory, reminders=self.reminders, tasks=self.tasks, group_id=gid,
                        channel_id=message.channel.id, user_id=message.author.id, message_id=message.id,
                        user_name=uname, is_dm=message.guild is None,
                    ),
                )
            chunks = split_message(reply_text)
            await message.reply(chunks[0], mention_author=False)
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
            log.info("↩️ [%s] 답변 전송 (%.1fs, %d자, 메시지 %d개)", gid,
                     asyncio.get_running_loop().time() - started, len(reply_text), len(chunks))
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 답변 생성/전송 실패: %s", gid, exc)
            reply_text = None
            try:
                await message.reply("앗, 지금 생각이 잘 안 나요 😵 잠시 후에 다시 불러 주세요!",
                                    mention_author=False)
            except Exception:  # noqa: BLE001
                pass

        # ---- 답변 전송 "이후" 에만 기억 버퍼에 적재 (저장은 백그라운드) ----
        buffers = [user_buf] if message.guild is None else [server_buf] + ([user_buf] if user_buf else [])
        for buf in buffers:
            who = uname if buf is user_buf else speaker
            self.batcher.add(speaker=who, text=text, at=message.created_at, direct=True, **buf)
            if reply_text:
                self.batcher.add(speaker=bot_name, text=reply_text, is_bot=True, **buf)
        if reply_text:
            history.append(ShortLine(speaker=bot_name, text=reply_text, is_bot=True))

    # ------------------------------------------------------------------ slash commands
    def _register_commands(self) -> None:
        group = app_commands.Group(name="memory", description="장기 기억 관리")

        def facts_embed(title: str, ctx: MemoryContext) -> discord.Embed:
            embed = discord.Embed(title=title, color=0x7C5CFF)
            for name, items in (("서버 기억", ctx.facts), ("인물/대상", ctx.entities),
                                ("내 개인 기억", ctx.personal_facts), ("개인 기억 요약", ctx.personal_entities)):
                if items:
                    embed.add_field(name=name, value="\n".join(f"• {x}" for x in items)[:1024], inline=False)
            return embed

        @group.command(name="recall", description="이 서버(또는 DM)와 내 개인 기억에서 검색합니다")
        @app_commands.describe(query="찾고 싶은 내용 (예: 내가 좋아하는 음식)")
        async def recall(inter: discord.Interaction, query: str) -> None:
            await inter.response.defer(ephemeral=True, thinking=True)
            gid, *_ = self._space(inter)
            ctx = await self._recall(gid, inter.user, query)
            if ctx.is_empty():
                await inter.followup.send("관련된 기억을 찾지 못했어요.", ephemeral=True)
                return
            await inter.followup.send(embed=facts_embed(f"🔎 '{query[:80]}' 관련 기억", ctx), ephemeral=True)

        @group.command(name="me", description="봇이 나에 대해 기억하는 것 (나에게만 보임)")
        async def me(inter: discord.Interaction) -> None:
            await inter.response.defer(ephemeral=True, thinking=True)
            name = user_label(inter.user)
            ctx = await self.memory.search(user_group_id(inter.user.id), f"{name} 취향 계획 경험 관계")
            if ctx.is_empty():
                await inter.followup.send("아직 개인 기억이 없어요. 대화를 나누면 쌓여요!", ephemeral=True)
                return
            await inter.followup.send(embed=facts_embed(f"👤 {name}에 대한 기억", MemoryContext(
                personal_facts=ctx.facts, personal_entities=ctx.entities)), ephemeral=True)

        @group.command(name="forget_me", description="내 개인 기억을 전부 삭제합니다 (서버 기억은 유지)")
        @app_commands.describe(confirm="정말 지우려면 '삭제' 라고 입력")
        async def forget_me(inter: discord.Interaction, confirm: str) -> None:
            if confirm.strip() != "삭제":
                await inter.response.send_message("취소했어요. 지우려면 confirm 에 `삭제` 를 입력하세요.",
                                                  ephemeral=True)
                return
            ok = await self.memory.delete_group(user_group_id(inter.user.id))
            await inter.response.send_message(
                "개인 기억을 삭제했어요. (서버 기억은 그대로예요)" if ok else "삭제할 개인 기억이 없어요.",
                ephemeral=True)

        @group.command(name="save", description="이 채널에 쌓인 대화를 지금 바로 장기 기억에 저장합니다")
        async def save(inter: discord.Interaction) -> None:
            gid, *_ = self._space(inter)
            keys = [f"{gid}:{inter.channel_id}", f"{user_group_id(inter.user.id)}:all"]
            count = sum(self.batcher.buffer_info(k)[0] for k in keys)
            if count == 0:
                await inter.response.send_message("저장할 대화가 아직 없어요.", ephemeral=True)
                return
            for k in keys:
                self.batcher.flush(k, reason="command")
            await inter.response.send_message(
                f"대화 {count}마디를 장기 기억에 저장하는 중이에요. (백그라운드 처리)", ephemeral=True)

        @group.command(name="status", description="기억 저장 상태를 보여 줍니다")
        async def status(inter: discord.Interaction) -> None:
            gid, *_ = self._space(inter)
            count, chars = self.batcher.buffer_info(f"{gid}:{inter.channel_id}")
            st = self.batcher.stats()
            msg = (
                f"**기억 공간** `{gid}` · 개인 `{user_group_id(inter.user.id)}`\n"
                f"• 이 채널 대기 버퍼: {count}마디 / {chars}자 "
                f"({st['batch_max_messages']}마디 또는 {int(st['batch_idle_seconds'])}초 무응답 시 저장)\n"
                f"• 저장 진행 중: {st['pending_saves']}건 · 누적 저장 {st['saved']}건 · 실패 {st['failed']}건"
            )
            await inter.response.send_message(msg, ephemeral=True)

        self.tree.add_command(group)

        # -------------------------------------------------------------- /task
        task_group = app_commands.Group(name="task", description="백그라운드·예약 작업")

        @task_group.command(name="start", description="조사·정리 작업을 백그라운드로 맡깁니다")
        @app_commands.describe(goal="할 일 (예: 블루아카이브 카스미 대사 한섭/일섭 비교 10개)")
        async def task_start(inter: discord.Interaction, goal: str) -> None:
            gid, *_ = self._space(inter)
            await inter.response.send_message(f"📋 작업을 맡았어요: {goal[:200]}")
            origin = await inter.original_response()
            try:
                task = await self.tasks.start(goal=goal, user_id=inter.user.id, user_name=user_label(inter.user),
                                              channel_id=inter.channel_id, group_id=gid,
                                              is_dm=inter.guild is None, origin_message_id=origin.id)
                await inter.edit_original_response(content=f"📋 **작업 #{task['id']}** 접수: {goal[:200]}")
            except ToolError as exc:
                await inter.edit_original_response(content=f"⚠️ {exc}")

        @task_group.command(name="list", description="내 작업 목록 (진행 중·예약·최근 완료)")
        async def task_list(inter: discord.Interaction) -> None:
            tasks = await self.tasks.list_for(inter.user.id)
            if not tasks:
                await inter.response.send_message("맡긴 작업이 없어요.", ephemeral=True)
                return
            icon = {"queued": "⏳", "running": "🔄", "scheduled": "🗓️", "done": "✅", "failed": "⚠️",
                    "cancelled": "⛔", "cancel_requested": "⛔"}
            lines = []
            for t in tasks[:12]:
                extra = ""
                if t["status"] == "scheduled":
                    nr = datetime.fromtimestamp(t["next_run"], KST).strftime("%m/%d %H:%M") if t.get("next_run") else "-"
                    extra = f" · {describe_schedule(t['schedule'])} (다음 {nr})"
                elif t["status"] == "running":
                    extra = f" · {t.get('steps', 0)}단계"
                lines.append(f"{icon.get(t['status'], '•')} `#{t['id']}` {t['goal'][:60]}{extra}")
            await inter.response.send_message("\n".join(lines), ephemeral=True)

        @task_group.command(name="cancel", description="작업 또는 예약을 취소합니다")
        @app_commands.describe(task_id="취소할 작업 번호 (/task list 로 확인)")
        async def task_cancel(inter: discord.Interaction, task_id: str) -> None:
            ok = await self.tasks.cancel(task_id.strip().lstrip("#"), user_id=inter.user.id)
            await inter.response.send_message("취소했어요." if ok else "취소할 수 있는 작업이 아니에요.",
                                              ephemeral=True)

        self.tree.add_command(task_group)

        # -------------------------------------------------------------- /usage
        @app_commands.command(name="usage", description="Gemini API 사용량과 추정 비용 (서버 관리자/봇 주인)")
        @app_commands.describe(period="기간")
        @app_commands.choices(period=[
            app_commands.Choice(name="오늘", value="today"),
            app_commands.Choice(name="최근 7일", value="week"),
            app_commands.Choice(name="이번 달", value="month"),
            app_commands.Choice(name="최근 30일", value="last30"),
        ])
        @app_commands.default_permissions(manage_guild=True)
        async def usage_cmd(inter: discord.Interaction, period: app_commands.Choice[str] | None = None) -> None:
            if inter.guild is None and not await self.is_owner(inter.user):
                await inter.response.send_message("DM 에서는 봇 주인만 볼 수 있어요.", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True, thinking=True)
            key = period.value if period else "month"
            data = await report(self.s, key)
            krw = await usd_krw(self.s)
            t = data["total"]
            title = {"today": "오늘", "week": "최근 7일", "month": "이번 달", "last30": "최근 30일"}[key]
            embed = discord.Embed(
                title=f"📊 API 사용량 — {title} ({data['from'][4:6]}/{data['from'][6:]}~{data['to'][4:6]}/{data['to'][6:]})",
                description=(f"**추정 비용 ${data['cost_usd']:.4f} (약 {data['cost_usd'] * krw:,.0f}원)**\n"
                             f"요청 {t['req']:,}회 · 입력 {t['in']:,} · 출력 {t['out'] + t['think']:,} 토큰"),
                color=0x7C5CFF,
            )
            lines = format_report(data, krw)
            embed.add_field(name="항목별", value="\n".join(lines)[:1024] if lines else "기록 없음", inline=False)
            embed.set_footer(text=f"봇이 직접 센 추정치 (유료 단가 기준, 1$={krw:,.0f}원). 무료 티어면 실제 청구 0원. "
                                  "정확한 청구액은 Google AI Studio 결제 화면에서 확인.")
            await inter.followup.send(embed=embed, ephemeral=True)

        self.tree.add_command(usage_cmd)
