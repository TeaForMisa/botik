from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot.config import Settings
from bot.database import Database
from bot.views import ProjectView


class ClanBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.db = Database(settings.database_path)

    async def setup_hook(self) -> None:
        await self.db.initialize()

        from bot.cogs.places import PlacesCog
        from bot.cogs.projects import ProjectsCog
        from bot.cogs.setup import SetupCog

        await self.add_cog(SetupCog(self))
        await self.add_cog(ProjectsCog(self))
        await self.add_cog(PlacesCog(self))

        for project in await self.db.get_active_projects():
            if project["message_id"]:
                self.add_view(
                    ProjectView(self, project["id"]),
                    message_id=project["message_id"],
                )

        if self.settings.sync_guild_id:
            guild = discord.Object(id=self.settings.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.info("Синхронизировано команд на тестовом сервере: %s", len(synced))
        else:
            synced = await self.tree.sync()
            logging.info("Синхронизировано глобальных команд: %s", len(synced))

    async def close(self) -> None:
        await self.db.close()
        await super().close()


async def run() -> None:
    settings = Settings.from_environment()
    bot = ClanBot(settings)

    @bot.event
    async def on_ready() -> None:
        logging.info("Бот запущен: %s (%s)", bot.user, bot.user.id if bot.user else "?")

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
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

