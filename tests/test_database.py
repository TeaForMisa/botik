from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.common import get_member_access, place_coordinates
from bot.database import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_project_membership_and_status(self) -> None:
        project_id = await self.db.create_project(
            10, "Ратуша", "Главное здание", "Обычный мир", "1 64 2", "Строители", 42
        )
        await self.db.join_project(project_id, 99, "Строитель")
        await self.db.join_project(project_id, 99, "Декоратор")

        members = await self.db.get_project_members(project_id)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["role_text"], "Декоратор")

        await self.db.set_project_status(project_id, "completed")
        active = await self.db.get_projects(10)
        self.assertEqual(active, [])

    async def test_place_soft_delete_and_restore(self) -> None:
        place_id = await self.db.create_place(
            10, "Ферма", "Обычный мир", 1, 64, -2, "Тест", "Ферма", "clan", 42
        )
        self.assertEqual(len(await self.db.find_places(10)), 1)

        await self.db.soft_delete_place(place_id)
        self.assertEqual(await self.db.find_places(10), [])
        self.assertEqual(len(await self.db.find_places(10, include_deleted=True)), 1)

        await self.db.restore_place(place_id)
        self.assertEqual(len(await self.db.find_places(10)), 1)

    async def test_image_urls_are_saved(self) -> None:
        project_id = await self.db.create_project(
            10, "Порт", "Морской порт", "Обычный мир", "10 65 20", "Строители", 42
        )
        await self.db.set_project_message(
            project_id, 100, 200, "https://cdn.example/project.png"
        )
        project = await self.db.get_project(project_id)
        self.assertEqual(project["image_url"], "https://cdn.example/project.png")

        place_id = await self.db.create_place(
            10,
            "Порт",
            "Обычный мир",
            10,
            65,
            20,
            "Причал",
            "Город",
            "clan",
            42,
            "https://cdn.example/place.png",
        )
        place = await self.db.get_place(place_id)
        self.assertEqual(place["image_url"], "https://cdn.example/place.png")

    async def test_place_can_store_coordinates_without_height(self) -> None:
        place_id = await self.db.create_place(
            10,
            "Портал",
            "Незер",
            120,
            0,
            -340,
            "",
            "Портал",
            "clan",
            42,
            y_is_set=False,
        )
        place = await self.db.get_place(place_id)
        self.assertEqual(place_coordinates(place), "120 -340")

    async def test_existing_database_gets_image_columns(self) -> None:
        await self.db.execute("ALTER TABLE projects DROP COLUMN image_url")
        await self.db.execute("ALTER TABLE places DROP COLUMN image_url")
        await self.db.execute("ALTER TABLE places DROP COLUMN y_is_set")
        await self.db.close()

        self.db = Database(Path(self.temp_dir.name) / "test.db")
        await self.db.initialize()

        project_columns = {
            row["name"] for row in await self.db.fetchall("PRAGMA table_info(projects)")
        }
        place_columns = {
            row["name"] for row in await self.db.fetchall("PRAGMA table_info(places)")
        }
        self.assertIn("image_url", project_columns)
        self.assertIn("image_url", place_columns)
        self.assertIn("y_is_set", place_columns)

    async def test_role_access_rules_and_highest_role_priority(self) -> None:
        await self.db.set_role_access(10, 1, "blocked", 42)
        await self.db.set_role_access(10, 2, "member", 42)
        await self.db.set_role_access(10, 3, "admin", 42)

        rules = await self.db.get_role_access_rules(10)
        self.assertEqual(
            {row["role_id"]: row["access_level"] for row in rules},
            {1: "blocked", 2: "member", 3: "admin"},
        )

        guild = SimpleNamespace(id=10, owner_id=999)
        permissions = SimpleNamespace(administrator=False, manage_guild=False)
        bot = SimpleNamespace(db=self.db)
        member = SimpleNamespace(
            id=50,
            guild=guild,
            guild_permissions=permissions,
            roles=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
        )
        self.assertEqual(await get_member_access(bot, member), "member")

        member.roles = [SimpleNamespace(id=1)]
        self.assertEqual(await get_member_access(bot, member), "blocked")

        member.roles = [SimpleNamespace(id=99)]
        self.assertEqual(await get_member_access(bot, member), "blocked")

        member.roles = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        member.roles.append(SimpleNamespace(id=3))
        self.assertEqual(await get_member_access(bot, member), "admin")

        await self.db.remove_role_access(10, 3)
        self.assertEqual(await get_member_access(bot, member), "member")

        member.guild_permissions.manage_guild = True
        member.roles = [SimpleNamespace(id=1)]
        self.assertEqual(await get_member_access(bot, member), "admin")


if __name__ == "__main__":
    unittest.main()
