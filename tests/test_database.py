from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

