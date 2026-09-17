from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.common import bot_access_check, send_audit


class SetupCog(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Настроить каналы и роль руководства")
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
        await self.bot.db.save_guild_settings(
            interaction.guild.id,
            projects_channel.id,
            places_channel.id,
            log_channel.id,
            leadership_role.id if leadership_role else None,
            interaction.user.id,
        )
        await interaction.response.send_message(
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
