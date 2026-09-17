from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.common import (
    PROJECT_STATUSES,
    bot_access_check,
    image_filename,
    is_supported_image,
    project_embed,
    send_audit,
)
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
    location = discord.ui.TextInput(
        label="Измерение и координаты",
        placeholder="Обычный мир | 322 32 3",
        required=False,
        max_length=150,
    )
    skills = discord.ui.TextInput(
        label="Кто нужен",
        placeholder="Строители, декораторы, редстоунеры",
        required=False,
        max_length=200,
    )
    screenshot_field = discord.ui.Label(
        text="Скриншот (необязательно)",
        description="PNG, JPG, WEBP или GIF",
        component=discord.ui.FileUpload(required=False, max_values=1),
    )

    def __init__(
        self,
        bot: Any,
        guild_id: int,
        author_id: int,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.author_id = author_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        screenshot_values = self.screenshot_field.component.values
        screenshot = screenshot_values[0] if screenshot_values else None
        if screenshot and not is_supported_image(screenshot):
            await interaction.response.send_message(
                "Скриншот должен быть в формате PNG, JPG, WEBP или GIF.",
                ephemeral=True,
            )
            return

        location = str(self.location).strip()
        if "|" in location:
            dimension, coordinates = (
                part.strip() for part in location.split("|", maxsplit=1)
            )
        else:
            dimension, coordinates = "", location

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
            dimension,
            coordinates,
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
            send_options: dict[str, Any] = {
                "embed": embed,
                "view": view,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if screenshot:
                filename = image_filename("project", project_id, screenshot)
                send_options["file"] = await screenshot.to_file(
                    filename=filename,
                    description=f"Скриншот проекта {self.name}",
                )
                embed.set_image(url=f"attachment://{filename}")

            if isinstance(channel, discord.ForumChannel):
                created = await channel.create_thread(
                    name=str(self.name)[:100],
                    content="Карточка проекта",
                    **send_options,
                )
                message = created.message
            elif isinstance(channel, discord.TextChannel):
                thread = await channel.create_thread(
                    name=str(self.name)[:100],
                    type=discord.ChannelType.public_thread,
                )
                message = await thread.send(**send_options)
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

        uploaded_image_url = message.attachments[0].url if message.attachments else ""
        await self.bot.db.set_project_message(
            project_id,
            message.channel.id,
            message.id,
            uploaded_image_url,
        )
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
    @bot_access_check()
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
            ProjectCreateModal(
                self.bot,
                interaction.guild.id,
                interaction.user.id,
            )
        )

    @app_commands.command(name="list", description="Показать активные проекты")
    @bot_access_check()
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
