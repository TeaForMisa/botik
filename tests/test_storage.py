import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiosqlite

from bot.database import Database
from bot import storage


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")
        await self.db.initialize()
        self.project = await self.db.create_project(
            10, "Дом", "Стройка", "", "", "", 42
        )
        self.mid = await self.db.execute(
            "INSERT INTO materials(project_id,name,target,stack) VALUES (?,?,?,?)",
            (self.project, "Бетон", 100, 64),
        )

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_unicode_search(self):
        await self.db.create_place(
            10, "Ферма Железа", "Незер", 1, 0, 2, "", "Ферма", "clan", 42
        )
        rows = await self.db.find_places(10, "ферма", "ферма", "НЕЗЕР")
        self.assertEqual(len(rows), 1)

    async def test_revision_conflict(self):
        await storage.edit(self.db, "projects", self.project, 0, {"name": "Мост"})
        with self.assertRaises(storage.Conflict):
            await storage.edit(
                self.db, "projects", self.project, 0, {"name": "Старое имя"}
            )
        self.assertEqual((await self.db.get_project(self.project))["name"], "Мост")

    async def test_reject_arbitrary_columns(self):
        with self.assertRaises(ValueError):
            await storage.edit(self.db, "projects", self.project, 0, {"guild_id": 99})

    async def test_atomic_reservations(self):
        result = await asyncio.gather(
            storage.contribute(self.db, self.mid, 1, "promise", 70, 100),
            storage.contribute(self.db, self.mid, 2, "promise", 70, 101),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, ValueError) for r in result), 1)
        self.assertEqual(await storage.material_totals(self.db, self.mid), (70, 0))

    async def test_partial_delivery_and_cancel(self):
        await storage.contribute(self.db, self.mid, 1, "promise", 70, 1)
        await storage.contribute(self.db, self.mid, 1, "deliver", 20, 2)
        self.assertEqual(await storage.material_totals(self.db, self.mid), (50, 20))
        await storage.contribute(self.db, self.mid, 1, "cancel", 0, 3)
        self.assertEqual(await storage.material_totals(self.db, self.mid), (0, 20))

    async def test_delivery_without_promise_and_idempotency(self):
        await storage.contribute(self.db, self.mid, 1, "deliver", 20, 1)
        changed = await storage.contribute(self.db, self.mid, 1, "deliver", 20, 1)
        self.assertFalse(changed)
        self.assertEqual(await storage.material_totals(self.db, self.mid), (0, 20))

    async def test_correction_is_absolute(self):
        await storage.contribute(self.db, self.mid, 1, "deliver", 20, 1)
        await storage.contribute(self.db, self.mid, 1, "correct", 10, 2)
        await storage.contribute(self.db, self.mid, 1, "correct", 10, 3)
        self.assertEqual(await storage.material_totals(self.db, self.mid), (0, 10))

    async def test_promise_is_absolute(self):
        await storage.contribute(self.db, self.mid, 1, "promise", 20, 1)
        await storage.contribute(self.db, self.mid, 1, "promise", 30, 2)
        self.assertEqual(await storage.material_totals(self.db, self.mid), (30, 0))

    async def test_complete_closes_requests_and_clears_promises(self):
        await storage.contribute(self.db, self.mid, 1, "promise", 50, 1)
        await storage.contribute(self.db, self.mid, 1, "deliver", 20, 2)
        await storage.edit(
            self.db, "projects", self.project, 0, {"status": "completed"}
        )
        self.assertEqual(await storage.material_totals(self.db, self.mid), (0, 20))
        with self.assertRaises(ValueError):
            await storage.contribute(self.db, self.mid, 1, "deliver", 10, 3)
        await storage.edit(self.db, "projects", self.project, 1, {"status": "building"})
        with self.assertRaises(ValueError):
            await storage.contribute(self.db, self.mid, 1, "promise", 10, 4)

    async def test_invalid_quantities(self):
        for amount in (-1, 0, 100_000_001):
            with self.assertRaises(ValueError):
                await storage.contribute(self.db, self.mid, 1, "deliver", amount, 1)
        with self.assertRaises(ValueError):
            await storage.contribute(self.db, self.mid, 1, "bogus", 1, 1)

    async def test_vote_replace_and_deadline(self):
        pid = await self.db.execute(
            "INSERT INTO polls(guild_id,author_id,question,options,deadline) VALUES (?,?,?,?,?)",
            (10, 42, "Что?", json.dumps(["А", "Б"]), 100),
        )
        await storage.vote(self.db, pid, 1, 0, now=90)
        await storage.vote(self.db, pid, 1, 1, now=91)
        rows = await self.db.fetchall("SELECT * FROM ballots WHERE poll_id=?", (pid,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["choice"], 1)
        with self.assertRaises(ValueError):
            await storage.vote(self.db, pid, 1, 0, now=100)

    async def test_cancelled_vote_rejects_ballots(self):
        pid = await self.db.execute(
            "INSERT INTO polls(guild_id,author_id,question,options,deadline,status) VALUES (?,?,?,?,?,?)",
            (10, 42, "Что?", json.dumps(["А", "Б"]), 100, "cancelled"),
        )
        with self.assertRaises(ValueError):
            await storage.vote(self.db, pid, 1, 0, now=90)

    def test_quorum_abstention_and_tie(self):
        poll = {
            "status": "closed",
            "official": 1,
            "quorum": 5,
            "options": json.dumps(["За", "Против", "Воздержаться"]),
        }
        self.assertEqual(storage.poll_result(poll, [2, 1, 2]), "Решение принято.")
        self.assertEqual(storage.poll_result(poll, [2, 2, 1]), "Решение не принято.")
        self.assertIn("недостаточно", storage.poll_result(poll, [3, 0, 0]))
        self.assertEqual(storage.poll_result(poll, [0, 0, 5]), "Решение не принято.")

    def test_poll_tie_and_no_votes(self):
        poll = {"status": "closed", "official": 0, "options": json.dumps(["А", "Б"])}
        self.assertIn("Равенство", storage.poll_result(poll, [2, 2]))
        self.assertIn("Никто", storage.poll_result(poll, [0, 0]))

    async def test_backup_restores_data(self):
        await storage.contribute(self.db, self.mid, 1, "deliver", 20, 1)
        path = await storage.backup(self.db)
        async with aiosqlite.connect(path) as restored:
            row = await storage.one(
                restored, "SELECT delivered FROM contributions WHERE user_id=1"
            )
            self.assertEqual(row[0], 20)

    async def test_restart_preserves_data(self):
        await storage.contribute(self.db, self.mid, 1, "deliver", 20, 1)
        await self.db.close()
        await self.db.initialize()
        self.assertEqual(await storage.material_totals(self.db, self.mid), (0, 20))
        self.assertTrue(list((self.db.path.parent / "backups").glob("*.sqlite3")))

    def test_quantity_display(self):
        self.assertEqual(storage.quantity(144, 64), "2 ст. + 16 шт.")
        self.assertEqual(storage.quantity(12, 1), "12 шт.")
