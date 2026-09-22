from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.interface import say, start
from bot.common import bot_access_check, send_audit, PROJECT_STATUSES


class SetupCog(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(
        name="setup", description="Настроить каналы и роль руководства"
    )
    @bot_access_check(admin=True)
    @app_commands.describe(
        projects_channel="Форум или текстовый канал для проектов",
        places_channel="Текстовый канал для карточек мест",
        log_channel="Закрытый текстовый канал журнала",
        leadership_role="Роль руководства (необязательно)",
    )
    @app_commands.guild_only()
    async def setup(
        self,
        interaction: discord.Interaction,
        projects_channel: discord.TextChannel | discord.ForumChannel,
        places_channel: discord.TextChannel,
        log_channel: discord.TextChannel,
        leadership_role: discord.Role | None = None,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        await start(interaction)
        if len({projects_channel.id, places_channel.id, log_channel.id}) != 3:
            await say(
                interaction,
                "Выберите три разных канала: проекты, места и закрытый журнал.",
            )
            return
        for channel in (projects_channel, places_channel, log_channel):
            permissions = channel.permissions_for(interaction.guild.me)
            if not (
                permissions.view_channel
                and permissions.send_messages
                and permissions.embed_links
                and permissions.attach_files
                and permissions.read_message_history
            ):
                await say(
                    interaction,
                    f"Боту не хватает прав в {channel.mention}. Нужны: просмотр "
                    "канала, отправка сообщений, встраивание ссылок, прикрепление "
                    "файлов и чтение истории.",
                )
                return
        await self.bot.db.save_guild_settings(
            interaction.guild.id,
            projects_channel.id,
            places_channel.id,
            log_channel.id,
            leadership_role.id if leadership_role else None,
            interaction.user.id,
        )
        if isinstance(projects_channel, discord.ForumChannel):
            tags = list(projects_channel.available_tags)
            names = {tag.name for tag in tags}
            for status in PROJECT_STATUSES.values():
                if status not in names and len(tags) < 20:
                    tags.append(discord.ForumTag(name=status))
            try:
                await projects_channel.edit(available_tags=tags)
            except discord.HTTPException:
                # Creating forum tags needs Manage Channels; bot otherwise works without it.
                pass
        await say(
            interaction,
            "Настройка сохранена.\n"
            f"Проекты: {projects_channel.mention}\n"
            f"Места: {places_channel.mention}\n"
            f"Журнал: {log_channel.mention}\n"
            f"Руководство: {leadership_role.mention if leadership_role else 'пользователи с правом «Управлять сервером»'}",
        )
        await send_audit(
            self.bot,
            interaction.guild,
            interaction.user,
            "setup",
            "guild",
            interaction.guild.id,
            "Обновлена конфигурация сервера.",
        )

    @app_commands.command(name="bot-status", description="Проверить состояние бота")
    @bot_access_check()
    @app_commands.guild_only()
    async def status_command(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        settings = await self.bot.db.get_guild_settings(interaction.guild.id)
        if not settings:
            await say(
                interaction,
                "Бот запущен, но сервер ещё не настроен. Откройте /admin → «Каналы и руководство».",
            )
            return
        await say(
            interaction,
            f"Бот работает. Задержка: {round(self.bot.latency * 1000)} мс.",
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(SetupCog(bot))
