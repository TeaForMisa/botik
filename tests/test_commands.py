from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import discord

from bot.cogs.access import AccessCog
from bot.cogs.places import PlacesCog, parse_coordinates
from bot.cogs.projects import ProjectsCog
from bot.interface import Form, ImageForm, PanelView, PlaceView, PollView
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
        await self.bot.add_cog(AccessCog(self.bot))
        await self.bot.add_cog(ProjectsCog(self.bot))
        await self.bot.add_cog(PlacesCog(self.bot))

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        self.temp_dir.cleanup()

    async def test_expected_slash_commands_are_registered(self) -> None:
        commands = {command.name: command for command in self.bot.tree.get_commands()}
        self.assertIn("setup", commands)
        self.assertIn("bot-status", commands)
        self.assertIn("access", commands)
        self.assertIn("project", commands)
        self.assertIn("place", commands)

        project_commands = {command.name for command in commands["project"].commands}
        place_commands = {command.name for command in commands["place"].commands}
        self.assertEqual(project_commands, {"create", "list", "open"})
        self.assertEqual(
            place_commands, {"add", "find", "list", "delete", "restore", "open"}
        )
        access_commands = {command.name for command in commands["access"].commands}
        self.assertEqual(access_commands, {"set", "remove", "list"})

        project_create = next(
            command
            for command in commands["project"].commands
            if command.name == "create"
        )
        place_add = next(
            command for command in commands["place"].commands if command.name == "add"
        )
        self.assertEqual(project_create.parameters, [])
        self.assertEqual(
            [parameter.name for parameter in place_add.parameters], ["visibility"]
        )

    async def test_small_forms_and_separate_image_upload(self) -> None:
        async def save(i, data):
            pass

        form = Form(
            self.bot, 20, "Описание", [("name", "Название", "Дом", True, 100)], save
        )
        self.assertEqual(len(form.children), 1)
        self.assertEqual(form.inputs["name"].default, "Дом")
        image = ImageForm(self.bot, 20, "project", {"id": 1})
        self.assertEqual(len(image.children), 1)
        self.assertIsInstance(image.upload, discord.ui.FileUpload)

    def test_place_coordinates_accept_two_or_three_numbers(self) -> None:
        self.assertEqual(parse_coordinates("320 -840"), (320, 0, -840, False))
        self.assertEqual(parse_coordinates("320 71 -840"), (320, 71, -840, True))
        with self.assertRaises(ValueError):
            parse_coordinates("320")

    async def test_project_view_is_persistent(self) -> None:
        view = ProjectView(self.bot, 42)
        self.assertTrue(view.is_persistent())
        custom_ids = {item.custom_id for item in view.children}
        self.assertEqual(
            custom_ids,
            {
                "project:42:join",
                "project:42:materials",
                "project:42:details",
            },
        )

    async def test_all_public_views_are_persistent(self):
        for view in [
            PanelView(self.bot),
            PlaceView(self.bot, 7),
            PollView(self.bot, 8),
        ]:
            self.assertTrue(view.is_persistent())
            self.assertLessEqual(len(view.to_components()), 5)
        self.assertEqual(PlaceView(self.bot, 7).children[0].label, "Подробнее")

    async def test_panel_has_five_equal_sections(self):
        view = PanelView(self.bot)
        self.assertTrue(all(isinstance(item, discord.ui.Button) for item in view.children))
        self.assertEqual(len(view.to_components()), 1)
        self.assertEqual(
            {item.custom_id for item in view.children},
            {
                "panel:projects",
                "panel:places",
                "panel:people",
                "panel:votes",
                "panel:mine",
            },
        )


if __name__ == "__main__":
    unittest.main()
