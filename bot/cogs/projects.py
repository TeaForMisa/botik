import discord
from discord import app_commands
from discord.ext import commands
from bot.common import bot_access_check
from bot.interface import create_project, list_objects, project_details


@app_commands.guild_only()
class ProjectsCog(
    commands.GroupCog, group_name="project", group_description="Проекты клана"
):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="create", description="Создать проект")
    @bot_access_check()
    async def create(self, interaction: discord.Interaction):
        await create_project(self.bot, interaction)

    @app_commands.command(name="list", description="Проекты, архив и мои стройки")
    @bot_access_check()
    async def list_projects(self, interaction: discord.Interaction):
        await list_objects(self.bot, interaction, "project")

    @app_commands.command(name="open", description="Открыть проект по номеру")
    @bot_access_check()
    async def open_project(self, interaction: discord.Interaction, project_id: int):
        await project_details(self.bot, interaction, project_id)
