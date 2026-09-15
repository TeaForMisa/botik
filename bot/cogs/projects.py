from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.common import PROJECT_STATUSES, project_embed, send_audit
from bot.views import ProjectView


class ProjectCreateModal(discord.ui.Modal, title="Новый проект"):
    name = discord.ui.TextInput(
        label="Название",
        placeholder="Например: Строительство ратуши",
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Описание",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )
    dimension = discord.ui.TextInput(
        label="Измерение",
        placeholder="Обычный мир / Незер / Энд",
        required=False,
        max_length=50,
    )
    coordinates = discord.ui.TextInput(
        label="Координаты",
        placeholder="X Y Z или название сохранённого места",
        required=False,
        max_length=100,
    )
    skills = discord.ui.TextInput(
        label="Кто нужен",
        placeholder="Строители, декораторы, редстоунеры",
        required=False,
        max_length=200,
    )

    def __init__(self, bot: Any, guild_id: int, author_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.author_id = author_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        settings = await self.bot.db.get_guild_settings(self.guild_id)
        if not settings:
            await interaction.response.send_message(
                "Сначала руководство должно выполнить `/setup`.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        project_id = await self.bot.db.create_project(
            self.guild_id,
            str(self.name).strip(),
            str(self.description).strip(),
            str(self.dimension).strip(),
            str(self.coordinates).strip(),
            str(self.skills).strip(),
            self.author_id,
        )
        project = await self.bot.db.get_project(project_id)
        view = ProjectView(self.bot, project_id)
        embed = project_embed(project, [])

        channel = interaction.guild.get_channel(settings["projects_channel_id"])
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(settings["projects_channel_id"])
            except discord.HTTPException:
                channel = None

        try:
            if isinstance(channel, discord.ForumChannel):
                created = await channel.create_thread(
                    name=str(self.name)[:100],
                    content="Карточка проекта",
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                message = created.message
            elif isinstance(channel, discord.TextChannel):
                thread = await channel.create_thread(
                    name=str(self.name)[:100],
                    type=discord.ChannelType.public_thread,
                )
                message = await thread.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                raise RuntimeError("Канал проектов недоступен")
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as error:
            await self.bot.db.delete_project_draft(project_id)
            await interaction.followup.send(
                "Не удалось опубликовать проект. Проверьте канал и права бота. "
                f"Техническая причина: `{type(error).__name__}`",
                ephemeral=True,
            )
            return

        await self.bot.db.set_project_message(project_id, message.channel.id, message.id)
        await send_audit(
            self.bot,
            interaction.guild,
            interaction.user,
            "project_create",
            "project",
            project_id,
            f"Создан проект «{self.name}».",
        )
        await interaction.followup.send(
            f"Проект создан: {message.jump_url}", ephemeral=True
        )


@app_commands.guild_only()
class ProjectsCog(commands.GroupCog, group_name="project", group_description="Проекты клана"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(name="create", description="Создать совместный проект")
    async def create(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        settings = await self.bot.db.get_guild_settings(interaction.guild.id)
        if not settings:
            await interaction.response.send_message(
                "Сначала руководство должно выполнить `/setup`.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ProjectCreateModal(self.bot, interaction.guild.id, interaction.user.id)
        )

    @app_commands.command(name="list", description="Показать активные проекты")
    async def list_projects(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        projects = await self.bot.db.get_projects(interaction.guild.id)
        if not projects:
            await interaction.response.send_message(
                "Активных проектов пока нет.", ephemeral=True
            )
            return
        lines = []
        for project in projects[:20]:
            status = PROJECT_STATUSES.get(project["status"], project["status"])
            if project["channel_id"] and project["message_id"]:
                link = (
                    f"https://discord.com/channels/{interaction.guild.id}/"
                    f"{project['channel_id']}/{project['message_id']}"
                )
                lines.append(f"• [#{project['id']} · {project['name']}]({link}) — {status}")
            else:
                lines.append(f"• #{project['id']} · {project['name']} — {status}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: Any) -> None:
    await bot.add_cog(ProjectsCog(bot))
