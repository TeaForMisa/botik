import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from bot.config import Settings
from bot.interface import (
    object_for,
    project_card,
    poll_card,
    list_objects,
    profile_screen,
    material_details,
    project_details,
    management,
    PanelView,
    Form,
    place_details,
    changed,
    create_project,
    create_place,
    create_poll,
    confirm,
    profiles_list,
)
from bot.cogs.hub import HubCog
from main import ClanBot


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = ClanBot(Settings("fake", Path(self.temp.name) / "test.db", None))
        await self.bot.db.initialize()
        self.i = SimpleNamespace(
            guild_id=10,
            user=SimpleNamespace(id=42),
            guild=SimpleNamespace(id=10),
            response=SimpleNamespace(
                is_done=lambda: False,
                defer=AsyncMock(),
                send_message=AsyncMock(),
                send_modal=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.leader = patch(
            "bot.interface.is_leadership", new=AsyncMock(return_value=False)
        )
        self.leader.start()
        self.common_leader = patch(
            "bot.common.is_leadership", new=AsyncMock(return_value=False)
        )
        self.common_leader.start()
        self.project = await self.bot.db.create_project(
            10, "Дом", "Стройка", "", "", "", 42
        )
        self.place = await self.bot.db.create_place(
            10, "Дом", "Незер", 1, 0, 2, "", "База", "author", 42
        )

    async def asyncTearDown(self):
        self.leader.stop()
        self.common_leader.stop()
        await self.bot.close()
        self.temp.cleanup()

    def check_screen(self):
        args = self.i.response.send_message.call_args.kwargs
        self.assertTrue(args["ephemeral"])
        view = args.get("view")
        if view:
            components = view.to_components()
            self.assertLessEqual(len(components), 5)
            for row in components:
                self.assertLessEqual(len(row["components"]), 5)
                for item in row["components"]:
                    if item["type"] == 3:
                        self.assertLessEqual(len(item["options"]), 25)
        embed = args.get("embed")
        if embed:
            self.assertLessEqual(len(embed), 6000)
            self.assertLessEqual(len(embed.description or ""), 4096)
        return args

    async def test_project_card_is_compact_with_30_members(self):
        for uid in range(30):
            await self.bot.db.join_project(self.project, uid, "Строительство")
        embed = await project_card(
            self.bot, await self.bot.db.get_project(self.project)
        )
        self.assertIn("30", embed.description)
        self.assertLess(len(embed), 1500)
        self.assertEqual(len(embed.fields), 0)

    async def test_private_place_owner_only(self):
        await object_for(self.bot, self.i, "place", self.place)
        self.i.user.id = 99
        with self.assertRaises(ValueError):
            await object_for(self.bot, self.i, "place", self.place)

    async def test_cross_guild_rejected(self):
        self.i.guild_id = 11
        with self.assertRaises(ValueError):
            await object_for(self.bot, self.i, "project", self.project)

    async def test_project_manage_author_only(self):
        self.i.user.id = 99
        with self.assertRaises(ValueError):
            await object_for(self.bot, self.i, "project", self.project, manage=True)

    async def test_deleted_object_rejected(self):
        await self.bot.db.soft_delete_place(self.place)
        with self.assertRaises(ValueError):
            await object_for(self.bot, self.i, "place", self.place)

    async def test_lists_filter_private_places(self):
        self.i.user.id = 99
        await list_objects(self.bot, self.i, "place")
        args = self.check_screen()
        self.assertNotIn("Дом", args["embed"].description)

    async def test_project_screens_fit_discord(self):
        await list_objects(self.bot, self.i, "project")
        self.check_screen()
        await project_details(self.bot, self.i, self.project)
        self.check_screen()
        await management(self.bot, self.i, "project", self.project)
        self.check_screen()

    async def test_profile_screen_fit(self):
        await self.bot.db.execute(
            "INSERT INTO profiles(guild_id,user_id,nick,skills) VALUES (?,?,?,?)",
            (10, 42, "Tea", "Декор"),
        )
        await profile_screen(self.bot, self.i)
        self.check_screen()

    async def test_material_screen_fit(self):
        mid = await self.bot.db.execute(
            "INSERT INTO materials(project_id,name,target,stack) VALUES (?,?,?,?)",
            (self.project, "Бетон", 100, 64),
        )
        await self.bot.db.execute(
            "INSERT INTO contributions VALUES (?,?,?,?)", (mid, 42, 20, 30)
        )
        await material_details(self.bot, self.i, mid)
        self.check_screen()

    async def test_hidden_vote_does_not_leak_counts_or_voters(self):
        pid = await self.bot.db.execute(
            "INSERT INTO polls(guild_id,author_id,question,options,deadline,hidden) VALUES (?,?,?,?,?,?)",
            (10, 42, "Что?", json.dumps(["А", "Б"]), int(time.time()) + 100, 1),
        )
        await self.bot.db.execute("INSERT INTO ballots VALUES (?,?,?)", (pid, 12345, 0))
        embed = await poll_card(
            self.bot,
            await self.bot.db.fetchone("SELECT * FROM polls WHERE id=?", (pid,)),
        )
        self.assertNotIn("12345", embed.description)
        self.assertNotIn("А —", embed.description)
        self.assertIn("Проголосовало: 1", embed.description)

    async def test_new_commands_serialize(self):
        hub = HubCog(self.bot)
        # Don't start the background loop in this offline test.
        with patch.object(HubCog, "cog_load", new=AsyncMock()):
            await self.bot.add_cog(hub)
        names = {command.name for command in self.bot.tree.get_commands()}
        self.assertTrue(
            {
                "menu",
                "panel",
                "votes",
                "profile",
                "people",
                "material",
                "diagnose",
                "backup",
            }
            <= names
        )
        for command in self.bot.tree.get_commands():
            payload = command.to_dict(self.bot.tree)
            self.assertLessEqual(len(payload.get("options", [])), 25)

    async def test_forms_can_open_without_prior_defer(self):
        await create_project(self.bot, self.i)
        self.i.response.send_modal.assert_awaited_once()
        self.i.response.defer.assert_not_awaited()

    async def test_zero_default_is_visible(self):
        form = Form(
            self.bot, 42, "Количество", [("n", "Всего", 0, True, 10)], AsyncMock()
        )
        self.assertEqual(form.inputs["n"].default, "0")

    async def test_create_place_menu_has_disclosure(self):
        await create_place(self.bot, self.i, "author")
        args = self.check_screen()
        self.assertIn("Только я", args["content"])

    async def test_confirmation_does_not_execute_twice(self):
        callback = AsyncMock()
        await confirm(self.bot, self.i, "Да?", callback)
        view = self.i.response.send_message.call_args.kwargs["view"]
        await asyncio.gather(
            view.children[0].callback(self.i), view.children[0].callback(self.i)
        )
        self.assertEqual(callback.await_count, 1)

    async def test_stale_revision_does_not_mutate(self):
        row = await self.bot.db.get_place(self.place)
        await self.bot.db.execute(
            "UPDATE places SET revision=1 WHERE id=?", (self.place,)
        )
        with self.assertRaises(ValueError):
            await changed(self.bot, self.i, "place", row, {"name": "Перезапись"})
        self.assertEqual((await self.bot.db.get_place(self.place))["name"], "Дом")

    async def test_privacy_failure_does_not_change_visibility(self):
        await self.bot.db.execute(
            "UPDATE places SET visibility='clan',channel_id=100,message_id=200 WHERE id=?",
            (self.place,),
        )
        row = await self.bot.db.get_place(self.place)
        failure = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "No access"
        )
        with patch("bot.interface.get_message", new=AsyncMock(side_effect=failure)):
            with self.assertRaises(ValueError):
                await changed(self.bot, self.i, "place", row, {"visibility": "author"})
        self.assertEqual(
            (await self.bot.db.get_place(self.place))["visibility"], "clan"
        )

    async def test_hidden_link_never_returns_coordinates(self):
        from bot.interface import project_location

        await self.bot.db.execute(
            "UPDATE projects SET place_id=? WHERE id=?", (self.place, self.project)
        )
        text = await project_location(
            self.bot, await self.bot.db.get_project(self.project)
        )
        self.assertIn("недоступно", text)
        self.assertNotIn("1 0 2", text)

    async def test_form_rechecks_access(self):
        saved = AsyncMock()
        form = Form(self.bot, 42, "Имя", [("name", "Имя", "Дом", True, 100)], saved)
        with patch("bot.interface.guard", new=AsyncMock(return_value=False)):
            await form.on_submit(self.i)
        saved.assert_not_awaited()

    async def test_form_single_submission(self):
        saved = AsyncMock()
        form = Form(self.bot, 42, "Имя", [("name", "Имя", "Дом", True, 100)], saved)
        with patch("bot.interface.guard", new=AsyncMock(return_value=True)):
            await asyncio.gather(form.on_submit(self.i), form.on_submit(self.i))
        self.assertEqual(saved.await_count, 1)

    async def test_channel_permission_overrides_bot_access(self):
        await self.bot.db.set_project_message(self.project, 100, 200)
        channel = SimpleNamespace(
            permissions_for=lambda user: SimpleNamespace(view_channel=False)
        )
        with patch.object(self.bot, "get_channel", return_value=channel):
            with self.assertRaises(ValueError):
                await object_for(self.bot, self.i, "project", self.project)
