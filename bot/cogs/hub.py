import logging
import asyncio
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.common import bot_access_check
from bot import storage
from bot.interface import (
    PanelView,
    PollView,
    card,
    say,
    start,
    profile_screen,
    profiles_list,
    polls_list,
    material_details,
    sync_poll,
    object_for,
    panel_home,
)


class HubCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_backup_day = None

    async def cog_load(self):
        self.maintenance.start()

    async def cog_unload(self):
        self.maintenance.cancel()
        task = self.maintenance.get_task()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    @tasks.loop(seconds=30)
    async def maintenance(self):
        try:
            await self.bot.db.execute(
                "UPDATE polls SET status='closed',published=0 WHERE status='open' AND deadline<=?",
                (int(time.time()),),
            )
            rows = await self.bot.db.fetchall(
                "SELECT id FROM polls WHERE status!='open' AND published=0 AND message_id IS NOT NULL"
            )
            for row in rows:
                await sync_poll(self.bot, row["id"])
            today = datetime.now(timezone.utc).date()
            if today != self.last_backup_day:
                await storage.backup(self.bot.db)
                self.last_backup_day = today
        except Exception:
            logging.exception("Ошибка фонового обслуживания")

    @maintenance.before_loop
    async def wait_ready(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="menu", description="Открыть клановую панель")
    @app_commands.guild_only()
    @bot_access_check()
    async def menu(self, i: discord.Interaction):
        await panel_home(self.bot, i)

    @app_commands.command(
        name="panel", description="Опубликовать постоянную панель в канале"
    )
    @app_commands.guild_only()
    @bot_access_check(admin=True)
    async def panel(self, i: discord.Interaction, channel: discord.TextChannel):
        await start(i)
        async with self.bot.ui_lock:
            row = await self.bot.db.fetchone(
                "SELECT * FROM panels WHERE guild_id=?", (i.guild_id,)
            )
            options = dict(
                embed=card(
                    "Клан",
                    "Стройки, места и общие решения.\nМеню будет видно только вам.",
                ),
                view=PanelView(self.bot),
            )
            if row:
                from bot.interface import get_message

                try:
                    message = await get_message(
                        self.bot, row["channel_id"], row["message_id"]
                    )
                except discord.NotFound:
                    message = None
                if message:
                    if row["channel_id"] == channel.id:
                        await message.edit(**options)
                        return await say(i, "Панель обновлена.")
                    await message.edit(
                        embed=card("Панель перенесена", channel.mention), view=None
                    )
            message = await channel.send(**options)
            await self.bot.db.execute(
                "INSERT INTO panels VALUES (?,?,?) ON CONFLICT(guild_id) "
                "DO UPDATE SET channel_id=excluded.channel_id,message_id=excluded.message_id",
                (i.guild_id, channel.id, message.id),
            )
        await say(i, "Панель: " + message.jump_url)

    @app_commands.command(name="profile", description="Ваша игровая анкета")
    @app_commands.guild_only()
    @bot_access_check()
    async def profile(self, i: discord.Interaction):
        await profile_screen(self.bot, i)

    @app_commands.command(name="people", description="Найти участников по интересам")
    @app_commands.guild_only()
    @bot_access_check()
    async def people(self, i: discord.Interaction):
        await profiles_list(self.bot, i)

    @app_commands.command(name="votes", description="Опросы, решения и архив")
    @app_commands.guild_only()
    @bot_access_check()
    async def votes(self, i: discord.Interaction):
        await polls_list(self.bot, i)

    @app_commands.command(
        name="vote-channel", description="Выбрать канал для голосований и результатов"
    )
    @app_commands.guild_only()
    @bot_access_check(admin=True)
    async def vote_channel(self, i: discord.Interaction, channel: discord.TextChannel):
        settings = await self.bot.db.get_guild_settings(i.guild_id)
        if not settings:
            return await say(i, "Сначала выполните /setup.")
        await self.bot.db.execute(
            "UPDATE guild_settings SET votes_channel_id=? WHERE guild_id=?",
            (channel.id, i.guild_id),
        )
        await say(
            i,
            "Голосования будут в "
            + channel.mention
            + ". Ограничьте чтение участниками клана.",
        )

    @app_commands.command(
        name="material", description="Открыть запрос материала по номеру"
    )
    @app_commands.guild_only()
    @bot_access_check()
    async def material(self, i: discord.Interaction, id: int):
        await material_details(self.bot, i, id)

    @app_commands.command(
        name="backup", description="Создать локальную резервную копию базы"
    )
    @app_commands.guild_only()
    @bot_access_check(admin=True)
    async def backup(self, i: discord.Interaction):
        await start(i)
        await storage.backup(self.bot.db)
        await say(
            i,
            "Копия создана в backups рядом с базой. Заберите её через хостинг; изображения находятся в media. Копия не отправляется в Discord, поскольку содержит приватные данные.",
        )

    @app_commands.command(name="diagnose", description="Проверить каналы и права")
    @app_commands.guild_only()
    @bot_access_check(admin=True)
    async def diagnose(self, i: discord.Interaction):
        await start(i)
        settings = await self.bot.db.get_guild_settings(i.guild_id)
        if not settings:
            return await say(i, "Сервер не настроен: /setup.")
        lines = []
        for key, label in [
            ("projects_channel_id", "Проекты"),
            ("places_channel_id", "Места"),
            ("log_channel_id", "Журнал"),
            ("votes_channel_id", "Голосования"),
        ]:
            cid = settings[key]
            if not cid:
                lines.append(label + ": не настроен")
                continue
            try:
                channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                perms = channel.permissions_for(i.guild.me)
                needed = [
                    "view_channel",
                    "send_messages",
                    "embed_links",
                    "attach_files",
                    "read_message_history",
                ]
                if key == "projects_channel_id":
                    needed += ["send_messages_in_threads", "manage_threads"]
                    if isinstance(channel, discord.TextChannel):
                        needed.append("create_public_threads")
                missing = [name for name in needed if not getattr(perms, name)]
                lines.append(
                    label
                    + ": "
                    + (
                        "не хватает " + ", ".join(missing)
                        if missing
                        else "права в порядке"
                    )
                )
                if channel.permissions_for(i.guild.default_role).view_channel:
                    lines.append(
                        "⚠ " + label + ": канал виден @everyone; проверьте доступ."
                    )
            except discord.HTTPException:
                lines.append(label + ": канал недоступен")
        lines.append(
            "База: " + ("OK" if await self.bot.db.fetchone("SELECT 1") else "ошибка")
        )
        await say(i, embed=card("Проверка настроек", "\n".join(lines)))

    @app_commands.command(
        name="poll-repair", description="Восстановить удалённую карточку голосования"
    )
    @app_commands.guild_only()
    @bot_access_check(admin=True)
    async def poll_repair(self, i: discord.Interaction, poll_id: int):
        await start(i)
        row = await object_for(self.bot, i, "poll", poll_id)
        from bot.interface import get_message, poll_card

        async with self.bot.poll_lock:
            if row["message_id"]:
                try:
                    await get_message(self.bot, row["channel_id"], row["message_id"])
                except discord.NotFound:
                    pass
                else:
                    return await say(i, "Карточка уже существует.")
            channel = self.bot.get_channel(
                row["channel_id"]
            ) or await self.bot.fetch_channel(row["channel_id"])
            view = PollView(self.bot, poll_id)
            view.children[0].disabled = row["status"] != "open"
            message = await channel.send(
                embed=await poll_card(self.bot, row), view=view
            )
            await self.bot.db.execute(
                "UPDATE polls SET message_id=?,published=0 WHERE id=?",
                (message.id, poll_id),
            )
        await say(i, "Карточка восстановлена.")
