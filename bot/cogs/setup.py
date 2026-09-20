from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

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
        await interaction.response.defer(ephemeral=True, thinking=True)
        if len({projects_channel.id, places_channel.id, log_channel.id}) != 3:
            await interaction.followup.send(
                "Выберите три разных канала: проекты, места и закрытый журнал.",
                ephemeral=True,
            )
            return
        for channel in (projects_channel, places_channel, log_channel):
            permissions = channel.permissions_for(interaction.guild.me)
            if not (
                permissions.view_channel
                and permissions.send_messages
                and permissions.embed_links
            ):
                await interaction.followup.send(
                    f"Бот не может читать и отправлять карточки в {channel.mention}. Исправьте права.",
                    ephemeral=True,
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
        await interaction.followup.send(
            "Настройка сохранена.\n"
            f"Проекты: {projects_channel.mention}\n"
            f"Места: {places_channel.mention}\n"
            f"Журнал: {log_channel.mention}\n"
            f"Руководство: {leadership_role.mention if leadership_role else 'пользователи с правом «Управлять сервером»'}",
            ephemeral=True,
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
            await interaction.response.send_message(
                "Бот запущен, но сервер ещё не настроен. Используйте `/setup`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Бот работает. Задержка: {round(self.bot.latency * 1000)} мс.",
            ephemeral=True,
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(SetupCog(bot))
