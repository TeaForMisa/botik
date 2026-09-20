import discord
from discord import app_commands
from discord.ext import commands
from bot.common import bot_access_check, get_member_access
from bot.interface import (
    create_place,
    list_objects,
    place_details,
    object_for,
    confirm,
    changed,
    parse_coordinates,
    say,
)
from bot import storage


@app_commands.guild_only()
class PlacesCog(
    commands.GroupCog, group_name="place", group_description="Места и координаты"
):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="add", description="Сохранить место без публикации в канал"
    )
    @bot_access_check()
    @app_commands.choices(
        visibility=[
            app_commands.Choice(name="Весь клан", value="clan"),
            app_commands.Choice(name="Только я", value="author"),
            app_commands.Choice(name="Руководство", value="leadership"),
        ]
    )
    async def add(
        self,
        interaction: discord.Interaction,
        visibility: app_commands.Choice[str] | None = None,
    ):
        await create_place(
            self.bot, interaction, visibility.value if visibility else "clan"
        )

    @app_commands.command(name="find", description="Найти место")
    @bot_access_check()
    async def find(
        self,
        interaction: discord.Interaction,
        query: str = "",
        category: str = "",
        dimension: str = "",
    ):
        await list_objects(
            self.bot,
            interaction,
            "place",
            query=query,
            category=category,
            dimension=dimension,
        )

    @find.autocomplete("query")
    async def suggest_places(self, interaction: discord.Interaction, current: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return []
        if await get_member_access(self.bot, interaction.user) == "blocked":
            return []
        rows = await self.bot.db.find_places(interaction.guild_id, current)
        choices = []
        for row in rows:
            try:
                await object_for(self.bot, interaction, "place", row["id"])
            except ValueError:
                continue
            choices.append(
                app_commands.Choice(name=row["name"][:100], value=row["name"][:100])
            )
            if len(choices) == 25:
                break
        return choices

    @app_commands.command(name="list", description="Открыть справочник мест")
    @bot_access_check()
    async def list_places(self, interaction: discord.Interaction):
        await list_objects(self.bot, interaction, "place")

    @app_commands.command(name="open", description="Открыть карточку места")
    @bot_access_check()
    async def open_place(self, interaction: discord.Interaction, place_id: int):
        await place_details(self.bot, interaction, place_id)

    @app_commands.command(
        name="delete", description="Удалить место с возможностью восстановления"
    )
    @bot_access_check()
    async def delete(self, interaction: discord.Interaction, place_id: int):
        row = await object_for(self.bot, interaction, "place", place_id, manage=True)

        async def save(i):
            await changed(self.bot, i, "place", row, {"is_deleted": 1})

        await confirm(self.bot, interaction, "Удалить место?", save)

    @app_commands.command(name="restore", description="Восстановить доступное место")
    @bot_access_check()
    async def restore(self, interaction: discord.Interaction, place_id: int):
        row = await object_for(
            self.bot, interaction, "place", place_id, manage=True, deleted=True
        )
        await storage.edit(
            self.bot.db, "places", place_id, row["revision"], {"is_deleted": 0}
        )
        await say(
            interaction,
            "Восстановлено. Карточку можно обновить через управление местом.",
        )
