from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot.cogs.places import PlacesCog
from bot.cogs.projects import ProjectsCog
from bot.cogs.setup import SetupCog
from bot.config import Settings
from bot.views import ProjectView
from main import ClanBot


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = Settings("test-token", Path(self.temp_dir.name) / "test.db", None)
        self.bot = ClanBot(settings)
        await self.bot.add_cog(SetupCog(self.bot))
        await self.bot.add_cog(ProjectsCog(self.bot))
        await self.bot.add_cog(PlacesCog(self.bot))

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        self.temp_dir.cleanup()

    async def test_expected_slash_commands_are_registered(self) -> None:
        commands = {command.name: command for command in self.bot.tree.get_commands()}
        self.assertIn("setup", commands)
        self.assertIn("bot-status", commands)
        self.assertIn("project", commands)
        self.assertIn("place", commands)

        project_commands = {command.name for command in commands["project"].commands}
        place_commands = {command.name for command in commands["place"].commands}
        self.assertEqual(project_commands, {"create", "list"})
        self.assertEqual(place_commands, {"add", "find", "list", "delete", "restore"})

    async def test_project_view_is_persistent(self) -> None:
        view = ProjectView(self.bot, 42)
        self.assertTrue(view.is_persistent())
        custom_ids = {item.custom_id for item in view.children}
        self.assertEqual(
            custom_ids,
            {
                "project:42:join",
                "project:42:leave",
                "project:42:coordinates",
                "project:42:status",
            },
        )


if __name__ == "__main__":
    unittest.main()

