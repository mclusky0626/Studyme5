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
        await self.memory.start()
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

    async def on_ready(self) -> None:
        log.info("로그인: %s (id=%s) — 서버 %d개, 모델 %s", self.user, self.user.id,
                 len(self.guilds), self.s.chat_model)
        await self.change_presence(activity=discord.Game(name="@멘션하면 대답해요"))

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("종료 중... 남은 대화 버퍼를 장기 기억에 저장합니다.")
        if self._status_task:
            self._status_task.cancel()
        try:
            await self.batcher.shutdown(timeout=100)
        finally:
            await self.memory.close()
            await super().close()

    async def _status_loop(self) -> None:
        while True:
            try:
                await self.memory.write_status(self._status())
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(30)

    def _status(self) -> dict:
        return {
            "bot": str(self.user) if self.user else None,
            "guilds": len(self.guilds),
            "chat_model": self.s.chat_model,
            "graph_model": self.s.llm_model,
            "embedding_model": self.s.embedding_model,
            "capture_all_messages": self.s.capture_all_messages,
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

    def _bot_name(self, guild: discord.Guild | None) -> str:
        if guild and guild.me:
            return guild.me.display_name
        return self.user.display_name if self.user else "bot"

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
            # @봇이름 멘션 제거
            for name in {self.user.display_name, self.user.name, self._bot_name(message.guild)}:
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
        try:
            async with message.channel.typing():
                memory_ctx = await self.memory.search(gid, f"{speaker} {text}")
                reply_text = await self.chat.reply(
                    bot_name=self._bot_name(message.guild),
                    place=space_name,
                    history=snapshot,
                    current=current,
                    memory=memory_ctx,
                )
            chunks = split_message(reply_text)
            await message.reply(chunks[0], mention_author=False)
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
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
            bot_name = self._bot_name(message.guild)
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
