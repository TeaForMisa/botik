from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot.common import BotAccessDenied
from bot.config import Settings
from bot.database import Database
from bot.views import ProjectView


class ClanBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.db = Database(settings.database_path)
        self.ui_lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        await self.db.initialize()

        from bot.cogs.access import AccessCog
        from bot.cogs.places import PlacesCog
        from bot.cogs.projects import ProjectsCog
        from bot.cogs.setup import SetupCog
        from bot.cogs.hub import HubCog
        from bot.interface import PanelView, PlaceView, PollView

        await self.add_cog(SetupCog(self))
        await self.add_cog(AccessCog(self))
        await self.add_cog(ProjectsCog(self))
        await self.add_cog(PlacesCog(self))
        await self.add_cog(HubCog(self))
        self.add_view(PanelView(self))

        for project in await self.db.fetchall(
            "SELECT * FROM projects WHERE deleted=0 AND message_id IS NOT NULL"
        ):
            if project["message_id"]:
                self.add_view(
                    ProjectView(self, project["id"]),
                    message_id=project["message_id"],
                )

        for place in await self.db.fetchall(
            "SELECT * FROM places WHERE is_deleted=0 AND visibility='clan' AND message_id IS NOT NULL"
        ):
            self.add_view(PlaceView(self, place["id"]), message_id=place["message_id"])
        for poll in await self.db.fetchall(
            "SELECT * FROM polls WHERE message_id IS NOT NULL"
        ):
            self.add_view(PollView(self, poll["id"]), message_id=poll["message_id"])
        self.refresh_on_ready = True

        if self.settings.sync_guild_id:
            guild = discord.Object(id=self.settings.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.info("Синхронизировано команд на тестовом сервере: %s", len(synced))
        else:
            synced = await self.tree.sync()
            logging.info("Синхронизировано глобальных команд: %s", len(synced))

    async def close(self) -> None:
        if self.get_cog("HubCog"):
            await self.remove_cog("HubCog")
        await self.db.close()
        await super().close()


async def run() -> None:
    settings = Settings.from_environment()
    bot = ClanBot(settings)

    @bot.event
    async def on_ready() -> None:
        logging.info("Бот запущен: %s (%s)", bot.user, bot.user.id if bot.user else "?")
        if getattr(bot, "refresh_on_ready", False):
            bot.refresh_on_ready = False
            from bot.interface import sync_project

            for row in await bot.db.fetchall(
                "SELECT id FROM projects WHERE deleted=0 AND message_id IS NOT NULL"
            ):
                await sync_project(bot, row["id"])

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, BotAccessDenied):
            text = error.message
        elif isinstance(original, ValueError):
            text = str(original)
        else:
            logging.exception("Ошибка slash-команды", exc_info=error)
            text = "Произошла ошибка. Подробности сохранены в журнале бота."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    asyncio.run(run())
