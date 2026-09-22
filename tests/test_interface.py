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
    place_card,
    project_card,
    poll_card,
    list_objects,
    profile_screen,
    material_details,
    project_details,
    management,
    PanelView,
    Form,
    ImageForm,
    place_details,
    changed,
    create_project,
    create_place,
    create_place_category,
    create_poll,
    confirm,
    profiles_list,
    say,
    object_search,
    material_list,
    my_promises,
    text_editor,
    location_menu,
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

    async def test_private_navigation_edits_one_ephemeral_message(self):
        interaction = SimpleNamespace(
            message=SimpleNamespace(flags=SimpleNamespace(ephemeral=True)),
            response=SimpleNamespace(
                is_done=lambda: False,
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await say(interaction, "Следующий экран")
        interaction.response.edit_message.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        self.assertIsNone(interaction.response.edit_message.call_args.kwargs["view"])
        self.assertEqual(
            interaction.response.edit_message.call_args.kwargs["attachments"], []
        )

    async def test_deferred_report_omits_none_view(self):
        self.i.response.is_done = lambda: True

        async def webhook_send(**kwargs):
            if "view" in kwargs and not isinstance(kwargs["view"], discord.ui.View):
                raise TypeError("expected view parameter to be of type View")

        self.i.followup.send.side_effect = webhook_send
        await say(self.i, embed=discord.Embed(title="Проверка настроек"))
        self.assertNotIn("view", self.i.followup.send.call_args.kwargs)
        view = PanelView(self.bot)
        await say(self.i, view=view)
        self.assertIs(self.i.followup.send.call_args.kwargs["view"], view)

    async def test_public_panel_opens_a_new_private_message(self):
        interaction = SimpleNamespace(
            message=SimpleNamespace(flags=SimpleNamespace(ephemeral=False)),
            response=SimpleNamespace(
                is_done=lambda: False,
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await say(interaction, "Личное меню")
        interaction.response.send_message.assert_awaited_once()
        interaction.response.edit_message.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])

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
        from bot.cogs.admin import AdminCog, simplify_commands
        from bot.cogs.setup import SetupCog
        from bot.cogs.access import AccessCog
        from bot.cogs.places import PlacesCog
        from bot.cogs.projects import ProjectsCog

        for cls in (AdminCog, SetupCog, AccessCog, PlacesCog, ProjectsCog):
            await self.bot.add_cog(cls(self.bot))
        simplify_commands(self.bot)
        names = {command.name for command in self.bot.tree.get_commands()}
        self.assertEqual(names, {"menu", "admin"})
        for command in self.bot.tree.get_commands():
            payload = command.to_dict(self.bot.tree)
            self.assertLessEqual(len(payload.get("options", [])), 25)

    async def test_diagnose_reports_one_broken_channel_without_crashing(self):
        await self.bot.db.save_guild_settings(10, 101, 102, 103, None, 42)
        await self.bot.db.execute(
            "UPDATE guild_settings SET votes_channel_id=? WHERE guild_id=?", (104, 10)
        )
        bot_member = object()
        default_role = object()

        def permissions_for(member):
            if member is bot_member:
                raise AttributeError("unexpected channel implementation")
            return SimpleNamespace(view_channel=False)

        channel = SimpleNamespace(permissions_for=permissions_for)
        interaction = SimpleNamespace(
            guild_id=10,
            user=SimpleNamespace(id=42),
            guild=SimpleNamespace(me=bot_member, default_role=default_role),
            response=SimpleNamespace(
                is_done=lambda: False,
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with self.assertLogs(level="ERROR"):
            with patch.object(self.bot, "get_channel", return_value=channel):
                await HubCog.diagnose.callback(HubCog(self.bot), interaction)
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        self.assertIn("не удалось проверить права", embed.description)
        self.assertIn("База данных: доступна", embed.description)

    async def test_places_refresh_updates_each_published_place(self):
        await self.bot.db.execute(
            "UPDATE places SET visibility='clan',channel_id=100,message_id=200 "
            "WHERE id=?",
            (self.place,),
        )
        unpublished = await self.bot.db.create_place(
            10, "Склад", "Обычный мир", 3, 0, 4, "", "Склад", "clan", 42
        )
        refresh = AsyncMock(return_value=True)

        with patch("bot.cogs.hub.sync_place", new=refresh):
            await HubCog.places_refresh.callback(HubCog(self.bot), self.i)

        refresh.assert_awaited_once_with(self.bot, self.place)
        self.assertNotEqual(self.place, unpublished)
        message = self.i.response.send_message.call_args.kwargs["content"]
        self.assertIn("Обновлено карточек мест: **1**", message)

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
        self.assertIn("Только я", args["embed"].description)

    async def test_place_card_has_clear_coordinates_and_visual_style(self):
        two_coordinates = await self.bot.db.create_place(
            10,
            "Портал",
            "Незер",
            7351,
            0,
            4734,
            "",
            "Портал",
            "clan",
            42,
            y_is_set=False,
        )
        embed = place_card(await self.bot.db.get_place(two_coordinates))
        self.assertIn(
            "🔥 Незер · Портал\n\n🧭 **X** `7351` · **Z** `4734`",
            embed.description,
        )
        self.assertTrue(embed.title.startswith("🌀 "))
        self.assertEqual(embed.colour.value, 0xB64242)
        self.assertEqual(embed.fields, [])

        embed = place_card(await self.bot.db.get_place(self.place))
        self.assertIn("🧭 **X** `1` · **Y** `0` · **Z** `2`", embed.description)

    async def test_create_place_asks_for_category(self):
        await create_place_category(self.bot, self.i, "clan", "Незер")
        args = self.check_screen()
        self.assertIn("Незер", args["embed"].description)
        select = next(
            item
            for item in args["view"].children
            if isinstance(item, discord.ui.Select)
            and item.placeholder == "Выберите категорию"
        )
        self.assertEqual(
            [option.label for option in select.options],
            ["База", "Ферма", "Склад", "Трейдхолл", "Другое"],
        )
        self.assertTrue(
            any(
                getattr(item, "label", None) == "К списку"
                for item in args["view"].children
            )
        )

    async def test_image_can_be_added_when_place_did_not_have_one(self):
        row = await self.bot.db.get_place(self.place)
        self.assertIsNone(row["image_url"])
        form = ImageForm(self.bot, 42, "place", row)
        image_data = b"\x89PNG\r\n\x1a\nimage-data"
        attachment = SimpleNamespace(
            filename="discord-upload",
            content_type="application/octet-stream",
            size=len(image_data),
            read=AsyncMock(return_value=image_data),
        )
        form.upload._values = [attachment]

        with patch("bot.interface.guard", new=AsyncMock(return_value=True)):
            await form.on_submit(self.i)

        saved = await self.bot.db.get_place(self.place)
        self.assertTrue(saved["image_url"].startswith("local:"))
        image_path = (
            self.bot.db.path.parent
            / "media"
            / saved["image_url"].removeprefix("local:")
        )
        self.assertEqual(image_path.suffix, ".png")
        self.assertEqual(image_path.read_bytes(), image_data)
        sent = self.i.response.send_message.call_args.kwargs
        self.assertEqual(len(sent["files"]), 1)
        self.assertEqual(sent["embed"].image.url, "attachment://" + image_path.name)
        sent["files"][0].close()

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

    def button(self, label):
        view = self.check_screen()["view"]
        return next(x for x in view.children if getattr(x, "label", None) == label)

    def select(self, placeholder):
        return next(
            x
            for x in self.check_screen()["view"].children
            if isinstance(x, discord.ui.Select) and x.placeholder == placeholder
        )

    async def test_creation_preserves_choices_and_requires_category(self):
        await create_place(self.bot, self.i)
        self.assertTrue(self.button("Далее").disabled)
        select = self.select("Выберите категорию")
        select._values = ["Ферма"]
        await select.callback(self.i)
        select = self.select("Измерение")
        select._values = ["Энд"]
        await select.callback(self.i)
        select = self.select("Кто видит место")
        select._values = ["author"]
        await select.callback(self.i)
        await self.button("Далее").callback(self.i)
        form = self.i.response.send_modal.call_args.args[0]
        await form.saved(
            self.i, {"name": "Новая ферма", "coordinates": "40 -20", "description": ""}
        )
        row = await self.bot.db.fetchone(
            "SELECT * FROM places WHERE name='Новая ферма'"
        )
        self.assertEqual(
            (row["dimension"], row["category"], row["visibility"], row["y_is_set"]),
            ("Энд", "Ферма", "author", 0),
        )
        self.assertIsNone(row["message_id"])
        self.assertNotIn(
            "Опубликовать",
            [getattr(x, "label", None) for x in self.check_screen()["view"].children],
        )

    async def test_search_edit_return_preserves_filters_and_page(self):
        for n in range(27):
            await self.bot.db.create_place(
                10, f"Точка {n}", "Незер", n, 0, n, "", "Ферма", "author", 42
            )
        await list_objects(
            self.bot,
            self.i,
            "place",
            page=1,
            query="Точка",
            category="Ферма",
            dimension="Незер",
        )
        select = self.select("Открыть место")
        self.assertEqual(len(select.options), 2)
        oid = select.options[0].value
        self.assertNotIn(
            select.options[0].label, self.check_screen()["embed"].description
        )
        select._values = [oid]
        await select.callback(self.i)
        await self.button("Изменить").callback(self.i)
        form = self.i.response.send_modal.call_args.args[0]
        await form.saved(
            self.i,
            {"name": "Точка изменена", "coordinates": "8 9", "description": "Описание"},
        )
        row = await self.bot.db.get_place(int(oid))
        self.assertEqual((row["x"], row["z"], row["y_is_set"]), (8, 9, 0))
        await self.button("К списку").callback(self.i)
        args = self.check_screen()
        self.assertIn("2/2", args["embed"].footer.text)
        self.assertIn("Точка · Ферма · Незер", args["embed"].description)
        self.assertEqual(len(self.select("Открыть место").options), 2)

    async def test_search_cancel_keeps_original_page(self):
        back = AsyncMock()
        await object_search(self.bot, self.i, "place", query="Дом", cancel_to=back)
        selected = self.select("Измерение")
        selected._values = ["Незер"]
        await selected.callback(self.i)
        await self.button("Назад").callback(self.i)
        back.assert_awaited_once_with(self.i)

    async def test_delete_cancel_returns_to_management_without_mutation(self):
        back = AsyncMock()
        await management(self.bot, self.i, "place", self.place, back_to=back)
        selected = self.select("Другие действия…")
        selected._values = ["delete"]
        await selected.callback(self.i)
        await self.button("Отмена").callback(self.i)
        self.assertTrue(self.check_screen()["embed"].title.startswith("Настройки"))
        row = await self.bot.db.get_place(self.place)
        self.assertFalse(row["is_deleted"])
        await self.button("Назад").callback(self.i)
        await self.button("К списку").callback(self.i)
        back.assert_awaited_once()

    async def test_dimension_change_does_not_require_coordinates_again(self):
        await location_menu(
            self.bot, self.i, "place", await self.bot.db.get_place(self.place)
        )
        select = self.select("Выберите измерение")
        select._values = ["Энд"]
        await select.callback(self.i)
        self.i.response.send_modal.assert_not_awaited()
        row = await self.bot.db.get_place(self.place)
        self.assertEqual((row["dimension"], row["x"], row["z"]), ("Энд", 1, 2))

    async def test_deleted_places_show_only_manageable_records(self):
        other = await self.bot.db.create_place(
            10, "Чужое удалённое", "Незер", 1, 0, 2, "", "База", "clan", 99
        )
        await self.bot.db.soft_delete_place(other)
        await self.bot.db.soft_delete_place(self.place)
        await list_objects(self.bot, self.i, "place", mode="deleted", origin="mine")
        self.assertEqual(
            [x.value for x in self.select("Открыть место").options], [str(self.place)]
        )

    async def test_material_contribution_actions_and_return(self):
        mid = await self.bot.db.execute(
            "INSERT INTO materials(project_id,name,target,stack) VALUES (?,?,?,?)",
            (self.project, "Бетон", 100, 64),
        )
        back = AsyncMock()
        await material_details(self.bot, self.i, mid, back_to=back)
        labels = [
            getattr(x, "label", None) for x in self.check_screen()["view"].children
        ]
        self.assertIn("Принесу", labels)
        self.assertIn("Доставил", labels)
        self.assertNotIn("Мой вклад", labels)
        self.assertNotIn("Исправить доставку", labels)
        await self.bot.db.execute(
            "INSERT INTO contributions VALUES (?,?,?,?)", (mid, 42, 20, 30)
        )
        await material_details(self.bot, self.i, mid, back_to=back)
        await self.button("Мой вклад").callback(self.i)
        self.button("Исправить доставку")
        await self.button("Снять обещание").callback(self.i)
        await self.button("Отмена").callback(self.i)
        own = await self.bot.db.fetchone(
            "SELECT * FROM contributions WHERE material_id=? AND user_id=42", (mid,)
        )
        self.assertEqual((own["promised"], own["delivered"]), (20, 30))
        await self.button("К материалу").callback(self.i)
        await self.button("К списку").callback(self.i)
        back.assert_awaited_once()

    async def test_project_material_navigation_preserves_project_origin(self):
        back = AsyncMock()
        await project_details(self.bot, self.i, self.project, back_to=back)
        await self.button("Материалы").callback(self.i)
        await self.button("Назад").callback(self.i)
        await self.button("К списку").callback(self.i)
        back.assert_awaited_once()

    async def test_promises_open_material_without_id_command(self):
        mid = await self.bot.db.execute(
            "INSERT INTO materials(project_id,name,target,stack) VALUES (?,?,?,?)",
            (self.project, "Бетон", 100, 64),
        )
        await self.bot.db.execute(
            "INSERT INTO contributions VALUES (?,?,?,?)", (mid, 42, 20, 0)
        )
        await my_promises(self.bot, self.i)
        select = self.select("Открыть материал")
        select._values = [str(mid)]
        await select.callback(self.i)
        self.button("Доставил")
        await self.button("К списку").callback(self.i)
        self.assertEqual(self.check_screen()["embed"].title, "Обещанные ресурсы")

    async def test_admin_screens_fit_and_actions_recheck_permissions(self):
        from bot.cogs.admin import (
            admin_home,
            setup_screen,
            access_screen,
            maintenance_screen,
            run_action,
        )
        from bot.common import BotAccessDenied

        with patch("bot.cogs.admin.require_access", new=AsyncMock(return_value=True)):
            for screen in (admin_home, setup_screen, access_screen, maintenance_screen):
                await screen(self.bot, self.i)
                self.check_screen()
        with patch(
            "bot.cogs.admin.require_access",
            new=AsyncMock(side_effect=BotAccessDenied("Отозван доступ")),
        ):
            with self.assertRaises(BotAccessDenied):
                await run_action(self.bot, self.i, "HubCog", "backup")

    async def test_category_search_respects_privacy_and_discord_limit(self):
        for n in range(30):
            await self.bot.db.create_place(
                10, f"Место {n}", "Незер", 1, 0, 2, "", f"Категория {n}", "clan", 42
            )
        await self.bot.db.create_place(
            10, "Секрет", "Незер", 1, 0, 2, "", "Секретная категория", "author", 99
        )
        await object_search(self.bot, self.i, "place", category="Категория 29")
        options = self.select("Категория").options
        self.assertLessEqual(len(options), 25)
        self.assertNotIn("Секретная категория", [x.label for x in options])
        self.assertTrue(any(x.label == "Категория 29" and x.default for x in options))
        self.button("Другая категория")

    async def test_admin_reports_keep_a_return_button(self):
        from bot.cogs.admin import run_action

        with patch.object(HubCog, "cog_load", new=AsyncMock()):
            await self.bot.add_cog(HubCog(self.bot))
        self.i.extras = {}
        with patch("bot.cogs.admin.require_access", new=AsyncMock(return_value=True)):
            await run_action(self.bot, self.i, "HubCog", "diagnose")
        self.button("Назад")
