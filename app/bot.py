"""디스코드 봇 본체."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict, deque

import discord
from discord import app_commands

from .batcher import EpisodeBatcher
from .chat import ChatResponder, ShortLine
from .config import Settings
from .gemini_resilience import RateLimiter
from .memory import MemoryService, group_id_for
from .persona import PersonaStore
from .tools.reminders import ReminderStore

log = logging.getLogger(__name__)

DISCORD_LIMIT = 1900


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
        # 이름·페르소나: 관리 웹 > persona.md > .env > 기본값 (재시작 없이 반영)
        self.persona = PersonaStore(settings, lambda: self.memory.redis if self.memory.available else None)
        self.reminders = ReminderStore(lambda: self.memory.redis if self.memory.available else None)
        self._reminder_task: asyncio.Task | None = None
        self.batcher = EpisodeBatcher(settings, self.memory.save_episode,
                                      on_event=self.memory.log_event)
        # 단기 기억: 채널별 최근 N개 (인메모리)
        self.short_term: dict[int, deque[ShortLine]] = defaultdict(
            lambda: deque(maxlen=settings.short_term_size)
        )
        self._registered_spaces: set[str] = set()
        self._status_task: asyncio.Task | None = None
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
        self._status_task = asyncio.create_task(self._status_loop())
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    async def on_ready(self) -> None:
        log.info("로그인: %s (id=%s) — 서버 %d개, 모델 %s", self.user, self.user.id,
                 len(self.guilds), self.s.chat_model)
        await self.change_presence(activity=discord.Game(name="@멘션하면 대답해요"))

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("종료 중... 남은 대화 버퍼를 장기 기억에 저장합니다.")
        for task in (self._status_task, self._reminder_task):
            if task:
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
                await self.memory.write_status(self._status())
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

    async def _deliver_reminder(self, item: dict) -> None:
        text = f"⏰ <@{item['user_id']}> 리마인더: {item['text']}"
        mentions = discord.AllowedMentions(users=True, everyone=False, roles=False)
        try:
            channel = self.get_channel(item["channel_id"]) or await self.fetch_channel(item["channel_id"])
            await channel.send(text, allowed_mentions=mentions)
        except Exception:  # noqa: BLE001 - 채널이 없어졌으면 DM 으로라도
            try:
                user = self.get_user(item["user_id"]) or await self.fetch_user(item["user_id"])
                await user.send(f"⏰ 리마인더: {item['text']}")
            except Exception as exc:  # noqa: BLE001
                log.warning("리마인더 전달 실패 %s: %s", item.get("id"), exc)
                return
        log.info("⏰ 리마인더 전달: %s", item.get("id"))

    def _status(self) -> dict:
        return {
            "bot": str(self.user) if self.user else None,
            "guilds": len(self.guilds),
            "chat_model": self.s.chat_model,
            "graph_model": self.s.llm_model,
            "embedding_model": self.s.embedding_model,
            "capture_all_messages": self.s.capture_all_messages,
            "memory_online": self.memory.available,
            **self.batcher.stats(),
        }

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
        return gid, f"DM · {user.name}", "dm", f"Discord DM ({user.name})"

    async def _ensure_space(self, gid: str, name: str, kind: str) -> None:
        if gid not in self._registered_spaces:
            self._registered_spaces.add(gid)
            await self.memory.register_space(gid, name, kind)

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

        buffer_key = f"{gid}:{message.channel.id}"
        channel_label = f"#{getattr(message.channel, 'name', 'dm')}" if message.guild else "DM"
        add_kwargs = dict(key=buffer_key, group_id=gid, channel_label=channel_label,
                          source_description=source)

        if not direct:
            if self.s.capture_all_messages:
                self.batcher.add(speaker=speaker, text=text, at=message.created_at,
                                 direct=False, **add_kwargs)
            return

        reply_text: str | None = None
        bot_name, persona = await self.persona.get()
        started = asyncio.get_running_loop().time()
        log.info("💬 [%s] %s: %s", gid, speaker, text[:80].replace("\n", " "))
        try:
            async with message.channel.typing():
                memory_ctx = await self.memory.search(gid, f"{speaker} {text}")
                reply_text = await self.chat.reply(
                    bot_name=bot_name,
                    place=space_name,
                    history=snapshot,
                    current=current,
                    memory=memory_ctx,
                    persona=persona,
                    tool_ctx=self.chat.tool_context(
                        memory=self.memory, reminders=self.reminders, group_id=gid,
                        channel_id=message.channel.id, user_id=message.author.id,
                        user_name=speaker, is_dm=message.guild is None,
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
        self.batcher.add(speaker=speaker, text=text, at=message.created_at, direct=True,
                         **add_kwargs)
        if reply_text:
            history.append(ShortLine(speaker=bot_name, text=reply_text, is_bot=True))
            self.batcher.add(speaker=bot_name, text=reply_text, is_bot=True, **add_kwargs)

    # ------------------------------------------------------------------ slash commands
    def _register_commands(self) -> None:
        group = app_commands.Group(name="memory", description="장기 기억 관리")

        @group.command(name="recall", description="이 서버(또는 DM)의 장기 기억에서 검색합니다")
        @app_commands.describe(query="찾고 싶은 내용 (예: 내가 좋아하는 음식)")
        async def recall(inter: discord.Interaction, query: str) -> None:
            await inter.response.defer(ephemeral=True, thinking=True)
            gid, *_ = self._space(inter)
            ctx = await self.memory.search(gid, query)
            if ctx.is_empty():
                await inter.followup.send("관련된 기억을 찾지 못했어요.", ephemeral=True)
                return
            embed = discord.Embed(title=f"🔎 '{query[:80]}' 관련 기억", color=0x7C5CFF)
            if ctx.facts:
                embed.add_field(name="사실", value="\n".join(f"• {f}" for f in ctx.facts)[:1024],
                                inline=False)
            if ctx.entities:
                embed.add_field(name="인물/대상",
                                value="\n".join(f"• {e}" for e in ctx.entities)[:1024], inline=False)
            await inter.followup.send(embed=embed, ephemeral=True)

        @group.command(name="save", description="이 채널에 쌓인 대화를 지금 바로 장기 기억에 저장합니다")
        async def save(inter: discord.Interaction) -> None:
            gid, *_ = self._space(inter)
            key = f"{gid}:{inter.channel_id}"
            count, _ = self.batcher.buffer_info(key)
            if count == 0:
                await inter.response.send_message("저장할 대화가 아직 없어요.", ephemeral=True)
                return
            self.batcher.flush(key, reason="command")
            await inter.response.send_message(
                f"대화 {count}마디를 장기 기억에 저장하는 중이에요. (백그라운드 처리)", ephemeral=True)

        @group.command(name="status", description="기억 저장 상태를 보여 줍니다")
        async def status(inter: discord.Interaction) -> None:
            gid, *_ = self._space(inter)
            count, chars = self.batcher.buffer_info(f"{gid}:{inter.channel_id}")
            st = self.batcher.stats()
            msg = (
                f"**기억 공간** `{gid}`\n"
                f"• 이 채널 대기 버퍼: {count}마디 / {chars}자 "
                f"({st['batch_max_messages']}마디 또는 {int(st['batch_idle_seconds'])}초 무응답 시 저장)\n"
                f"• 저장 진행 중: {st['pending_saves']}건 · 누적 저장 {st['saved']}건 · 실패 {st['failed']}건"
            )
            await inter.response.send_message(msg, ephemeral=True)

        self.tree.add_command(group)
