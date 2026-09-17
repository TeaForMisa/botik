from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.common import ACCESS_LEVELS, require_access, send_audit


@app_commands.guild_only()
class AccessCog(
    commands.GroupCog,
    group_name="access",
    group_description="Доступ к командам бота по ролям",
):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_access(self.bot, interaction, admin=True)

    @app_commands.command(name="set", description="Назначить уровень доступа роли")
    @app_commands.describe(
        role="Роль Discord",
        level="Что участники с этой ролью могут делать",
    )
    @app_commands.choices(
        level=[
            app_commands.Choice(name="Запрещено", value="blocked"),
            app_commands.Choice(name="Основные команды", value="member"),
            app_commands.Choice(name="Администратор бота", value="admin"),
        ]
    )
    async def set_access(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        level: app_commands.Choice[str],
    ) -> None:
        if not interaction.guild:
            return
        await self.bot.db.set_role_access(
            interaction.guild.id,
            role.id,
            level.value,
            interaction.user.id,
        )
        await interaction.response.send_message(
            f"Для {role.mention} установлен уровень: **{ACCESS_LEVELS[level.value]}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await send_audit(
            self.bot,
            interaction.guild,
            interaction.user,
            "access_set",
            "role",
            role.id,
            f"Для роли {role.name} установлен доступ: {ACCESS_LEVELS[level.value]}.",
        )

    @app_commands.command(name="remove", description="Убрать особое правило для роли")
    @app_commands.describe(role="Роль, для которой нужно удалить правило")
    async def remove_access(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if not interaction.guild:
            return
        await self.bot.db.remove_role_access(interaction.guild.id, role.id)
        await interaction.response.send_message(
            f"Для {role.mention} удалено особое правило. Будет действовать обычный доступ.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await send_audit(
            self.bot,
            interaction.guild,
            interaction.user,
            "access_remove",
            "role",
            role.id,
            f"Удалено правило доступа для роли {role.name}.",
        )

    @app_commands.command(name="list", description="Показать правила доступа по ролям")
    async def list_access(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        rules = await self.bot.db.get_role_access_rules(interaction.guild.id)
        if not rules:
            await interaction.response.send_message(
                "Особых правил пока нет. Все роли могут пользоваться основными командами.",
                ephemeral=True,
            )
            return

        lines = []
        for rule in rules:
            role = interaction.guild.get_role(rule["role_id"])
            role_text = role.mention if role else f"Удалённая роль (`{rule['role_id']}`)"
            lines.append(f"{role_text} — **{ACCESS_LEVELS[rule['access_level']]}**")
        await interaction.response.send_message(
            "**Доступ к боту**\n" + "\n".join(lines) +
            "\n\nЕсли у участника несколько настроенных ролей, действует самая высокая роль в списке Discord.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(AccessCog(bot))
