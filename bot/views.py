from __future__ import annotations

from typing import Any

import discord

from bot.common import (
    PROJECT_STATUSES,
    can_manage_project,
    component_access_check,
    refresh_project_message,
    send_audit,
)


class ProjectRoleSelect(discord.ui.Select):
    def __init__(self, bot: Any, project_id: int) -> None:
        self.bot = bot
        self.project_id = project_id
        options = [
            discord.SelectOption(label="Строитель", emoji="🏗️"),
            discord.SelectOption(label="Декоратор", emoji="🎨"),
            discord.SelectOption(label="Редстоунер", emoji="🔴"),
            discord.SelectOption(label="Добытчик ресурсов", emoji="⛏️"),
            discord.SelectOption(label="Организатор", emoji="📋"),
            discord.SelectOption(label="Любая помощь", emoji="🙌"),
        ]
        super().__init__(placeholder="Чем хотите помочь?", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        project = await self.bot.db.get_project(self.project_id)
        if not project or project["status"] == "completed":
            await interaction.response.edit_message(content="Проект уже завершён.", view=None)
            return
        role_text = self.values[0]
        await self.bot.db.join_project(self.project_id, interaction.user.id, role_text)
        await refresh_project_message(self.bot, self.project_id)
        if interaction.guild:
            await send_audit(
                self.bot,
                interaction.guild,
                interaction.user,
                "project_join",
                "project",
                self.project_id,
                f"Участник присоединился к проекту как «{role_text}».",
            )
        await interaction.response.edit_message(
            content=f"Готово. Ваша роль в проекте: **{role_text}**.", view=None
        )


class ProjectRoleView(discord.ui.View):
    def __init__(self, bot: Any, project_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.add_item(ProjectRoleSelect(bot, project_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await component_access_check(self.bot, interaction)


class ProjectStatusSelect(discord.ui.Select):
    def __init__(self, bot: Any, project_id: int) -> None:
        self.bot = bot
        self.project_id = project_id
        super().__init__(
            placeholder="Выберите новый статус",
            options=[
                discord.SelectOption(label=label, value=value)
                for value, label in PROJECT_STATUSES.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        project = await self.bot.db.get_project(self.project_id)
        if not project or not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.edit_message(content="Проект не найден.", view=None)
            return
        if not await can_manage_project(self.bot, interaction.user, project):
            await interaction.response.edit_message(
                content="Менять статус может организатор проекта или руководство.", view=None
            )
            return
        new_status = self.values[0]
        await self.bot.db.set_project_status(self.project_id, new_status)
        await refresh_project_message(self.bot, self.project_id)
        await send_audit(
            self.bot,
            interaction.guild,
            interaction.user,
            "project_status",
            "project",
            self.project_id,
            f"Статус изменён: {PROJECT_STATUSES[new_status]}.",
        )
        await interaction.response.edit_message(
            content=f"Статус изменён: **{PROJECT_STATUSES[new_status]}**.", view=None
        )


class ProjectStatusView(discord.ui.View):
    def __init__(self, bot: Any, project_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.add_item(ProjectStatusSelect(bot, project_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await component_access_check(self.bot, interaction)


class ProjectView(discord.ui.View):
    def __init__(self, bot: Any, project_id: int, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.project_id = project_id

        join = discord.ui.Button(
            label="Присоединиться",
            style=discord.ButtonStyle.success,
            custom_id=f"project:{project_id}:join",
            disabled=disabled,
        )
        leave = discord.ui.Button(
            label="Покинуть проект",
            style=discord.ButtonStyle.secondary,
            custom_id=f"project:{project_id}:leave",
            disabled=disabled,
        )
        coordinates = discord.ui.Button(
            label="Координаты",
            style=discord.ButtonStyle.secondary,
            custom_id=f"project:{project_id}:coordinates",
        )
        status = discord.ui.Button(
            label="Изменить статус",
            style=discord.ButtonStyle.primary,
            custom_id=f"project:{project_id}:status",
            disabled=disabled,
        )
        join.callback = self.join_callback
        leave.callback = self.leave_callback
        coordinates.callback = self.coordinates_callback
        status.callback = self.status_callback
        self.add_item(join)
        self.add_item(leave)
        self.add_item(coordinates)
        self.add_item(status)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await component_access_check(self.bot, interaction):
            return False
        project = await self.bot.db.get_project(self.project_id)
        if not project or not interaction.guild or project["guild_id"] != interaction.guild.id:
            await interaction.response.send_message("Этот проект больше недоступен.", ephemeral=True)
            return False
        return True

    async def join_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Выберите роль внутри этого проекта:",
            view=ProjectRoleView(self.bot, self.project_id),
            ephemeral=True,
        )

    async def leave_callback(self, interaction: discord.Interaction) -> None:
        await self.bot.db.leave_project(self.project_id, interaction.user.id)
        await refresh_project_message(self.bot, self.project_id)
        if interaction.guild:
            await send_audit(
                self.bot,
                interaction.guild,
                interaction.user,
                "project_leave",
                "project",
                self.project_id,
                "Участник покинул проект.",
            )
        await interaction.response.send_message("Вы вышли из проекта.", ephemeral=True)

    async def coordinates_callback(self, interaction: discord.Interaction) -> None:
        project = await self.bot.db.get_project(self.project_id)
        if not project:
            await interaction.response.send_message("Проект не найден.", ephemeral=True)
            return
        text = " · ".join(
            part for part in (project["dimension"], project["coordinates"]) if part
        )
        await interaction.response.send_message(
            text or "Координаты пока не указаны.", ephemeral=True
        )

    async def status_callback(self, interaction: discord.Interaction) -> None:
        project = await self.bot.db.get_project(self.project_id)
        if (
            not project
            or not isinstance(interaction.user, discord.Member)
            or not await can_manage_project(self.bot, interaction.user, project)
        ):
            await interaction.response.send_message(
                "Менять статус может организатор проекта или руководство.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Выберите новый статус:",
            view=ProjectStatusView(self.bot, self.project_id),
            ephemeral=True,
        )
