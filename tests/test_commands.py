from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import discord

from bot.cogs.places import PlaceCreateModal, PlacesCog
from bot.cogs.projects import ProjectCreateModal, ProjectsCog
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

        project_create = next(
            command for command in commands["project"].commands if command.name == "create"
        )
        place_add = next(
            command for command in commands["place"].commands if command.name == "add"
        )
        self.assertEqual(project_create.parameters, [])
        self.assertEqual([parameter.name for parameter in place_add.parameters], ["visibility"])

    async def test_create_forms_contain_optional_file_upload(self) -> None:
        project_modal = ProjectCreateModal(self.bot, 10, 20)
        place_modal = PlaceCreateModal(self.bot, 10, 20, "clan")

        self.assertEqual(len(project_modal.children), 5)
        self.assertEqual(len(place_modal.children), 5)
        self.assertIsInstance(project_modal.screenshot_field.component, discord.ui.FileUpload)
        self.assertIsInstance(place_modal.screenshot_field.component, discord.ui.FileUpload)
        self.assertFalse(project_modal.screenshot_field.component.required)
        self.assertFalse(place_modal.screenshot_field.component.required)

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
