"""Domain operations. Quantities are items; mutations use SQLite transactions."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import aiosqlite


class Conflict(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 name TEXT NOT NULL, target INTEGER NOT NULL CHECK(target>0),
 stack INTEGER NOT NULL CHECK(stack IN (1,16,64)), destination TEXT NOT NULL DEFAULT '',
 closed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS contributions (
 material_id INTEGER NOT NULL REFERENCES materials(id), user_id INTEGER NOT NULL,
 promised INTEGER NOT NULL DEFAULT 0 CHECK(promised>=0),
 delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered>=0),
 PRIMARY KEY(material_id,user_id));
CREATE TABLE IF NOT EXISTS receipts (interaction_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS profiles (
 guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,nick TEXT NOT NULL,
 skills TEXT NOT NULL DEFAULT '', visible INTEGER NOT NULL DEFAULT 1,
 invites INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS invitations (
 project_id INTEGER NOT NULL,user_id INTEGER NOT NULL,sent_at INTEGER NOT NULL,
 PRIMARY KEY(project_id,user_id));
CREATE TABLE IF NOT EXISTS polls (
 id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,author_id INTEGER NOT NULL,
 question TEXT NOT NULL,options TEXT NOT NULL,deadline INTEGER NOT NULL,
 official INTEGER NOT NULL DEFAULT 0,quorum INTEGER NOT NULL DEFAULT 1,
 hidden INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'open',
 reason TEXT NOT NULL DEFAULT '',channel_id INTEGER,message_id INTEGER,
 published INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ballots (
 poll_id INTEGER NOT NULL REFERENCES polls(id),user_id INTEGER NOT NULL,
 choice INTEGER NOT NULL,PRIMARY KEY(poll_id,user_id));
CREATE TABLE IF NOT EXISTS panels (
 guild_id INTEGER PRIMARY KEY,channel_id INTEGER NOT NULL,message_id INTEGER NOT NULL);
"""


async def migrate(db):
    await db.db.create_function(
        "CASEFOLD", 1, lambda value: str(value or "").casefold()
    )
    await db.db.executescript(SCHEMA)
    for table, fields in {
        "projects": {
            "revision": "INTEGER NOT NULL DEFAULT 0",
            "place_id": "INTEGER",
            "deleted": "INTEGER NOT NULL DEFAULT 0",
        },
        "places": {
            "revision": "INTEGER NOT NULL DEFAULT 0",
            "channel_id": "INTEGER",
            "message_id": "INTEGER",
        },
        "guild_settings": {"votes_channel_id": "INTEGER"},
    }.items():
        for column, definition in fields.items():
            await db._ensure_column(table, column, definition)
    await db.db.commit()


async def one(conn, sql, params=()):
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def edit(db, table, object_id, revision, fields):
    allowed = {
        "projects": {
            "name",
            "description",
            "skills",
            "dimension",
            "coordinates",
            "place_id",
            "organizer_id",
            "image_url",
            "status",
            "deleted",
        },
        "places": {
            "name",
            "description",
            "category",
            "dimension",
            "x",
            "y",
            "z",
            "y_is_set",
            "visibility",
            "image_url",
            "is_deleted",
        },
    }
    if table not in allowed or not fields or not set(fields) <= allowed[table]:
        raise ValueError("Недопустимые поля")
    if "status" in fields and fields["status"] not in {
        "idea",
        "preparation",
        "building",
        "paused",
        "completed",
    }:
        raise ValueError("Неизвестный статус.")
    async with db.transaction() as conn:
        cursor = await conn.execute(
            f"UPDATE {table} SET "
            + ",".join(f"{key}=?" for key in fields)
            + ",revision=revision+1 WHERE id=? AND revision=?",
            (*fields.values(), object_id, revision),
        )
        if cursor.rowcount != 1:
            raise Conflict("Запись уже изменена. Откройте карточку заново.")
        if table == "projects" and (
            fields.get("status") == "completed" or fields.get("deleted")
        ):
            await conn.execute(
                "UPDATE materials SET closed=1 WHERE project_id=?", (object_id,)
            )
            await conn.execute(
                "UPDATE contributions SET promised=0 WHERE material_id IN "
                "(SELECT id FROM materials WHERE project_id=?)",
                (object_id,),
            )


async def material_totals(db, material_id):
    row = await db.fetchone(
        "SELECT COALESCE(SUM(promised),0) p, "
        "COALESCE(SUM(delivered),0) d FROM contributions WHERE material_id=?",
        (material_id,),
    )
    return row["p"], row["d"]


async def contribute(db, material_id, user_id, action, amount, interaction_id):
    if action not in {"promise", "deliver", "correct", "cancel"}:
        raise ValueError("Неизвестное действие")
    if not isinstance(amount, int) or amount < 0 or amount > 100_000_000:
        raise ValueError("Количество: от 0 до 100 000 000 предметов.")
    if action in {"promise", "deliver"} and amount == 0:
        raise ValueError("Количество должно быть больше нуля.")
    async with db.transaction() as conn:
        if await one(
            conn, "SELECT 1 FROM receipts WHERE interaction_id=?", (interaction_id,)
        ):
            return False
        material = await one(
            conn,
            "SELECT m.*,p.status,p.deleted FROM materials m JOIN projects p "
            "ON p.id=m.project_id WHERE m.id=?",
            (material_id,),
        )
        if (
            not material
            or material["closed"]
            or material["deleted"]
            or material["status"] == "completed"
        ):
            raise ValueError("Запрос закрыт. Организатор может открыть его заново.")
        await conn.execute(
            "INSERT OR IGNORE INTO contributions(material_id,user_id) VALUES (?,?)",
            (material_id, user_id),
        )
        row = await one(
            conn,
            "SELECT * FROM contributions WHERE material_id=? AND user_id=?",
            (material_id, user_id),
        )
        totals = await one(
            conn,
            "SELECT SUM(promised) p,SUM(delivered) d FROM contributions "
            "WHERE material_id=?",
            (material_id,),
        )
        promised, delivered = row["promised"], row["delivered"]
        if action == "promise":
            available = max(
                0, material["target"] - totals["d"] - totals["p"] + promised
            )
            if amount > available:
                raise ValueError(
                    f"Свободно для обещания: {available} предметов. Обновите список."
                )
            promised = amount  # Absolute outstanding promise, not an increment.
        elif action == "deliver":
            delivered += amount
            promised = max(0, promised - amount)
        elif action == "correct":
            delivered = amount  # Absolute total; a repeated submit does not add items.
        else:
            promised = 0
        await conn.execute(
            "UPDATE contributions SET promised=?,delivered=? "
            "WHERE material_id=? AND user_id=?",
            (promised, delivered, material_id, user_id),
        )
        await conn.execute("INSERT INTO receipts VALUES (?)", (interaction_id,))
    return True


async def vote(db, poll_id, user_id, choice, now=None):
    now = int(time.time()) if now is None else now
    async with db.transaction() as conn:
        poll = await one(conn, "SELECT * FROM polls WHERE id=?", (poll_id,))
        if not poll or poll["status"] != "open" or now >= poll["deadline"]:
            raise ValueError("Голосование уже закрыто.")
        if choice < 0 or choice >= len(json.loads(poll["options"])):
            raise ValueError("Такого варианта нет.")
        await conn.execute(
            "INSERT INTO ballots VALUES (?,?,?) ON CONFLICT(poll_id,user_id) "
            "DO UPDATE SET choice=excluded.choice",
            (poll_id, user_id, choice),
        )


def poll_result(poll, counts):
    if poll["status"] == "cancelled":
        return "Отменено: " + poll["reason"]
    total = sum(counts)
    if poll["official"]:
        if total < poll["quorum"]:
            return "Решение не принято: недостаточно участников."
        return "Решение принято." if counts[0] > counts[1] else "Решение не принято."
    best = max(counts, default=0)
    if not best:
        return "Никто не проголосовал."
    options = json.loads(poll["options"])
    winners = [option for option, count in zip(options, counts) if count == best]
    return (
        "Равенство голосов: " if len(winners) > 1 else "Больше голосов: "
    ) + ", ".join(winners)


def quantity(value, stack):
    if stack == 1 or value < stack:
        return f"{value} шт."
    whole, rest = divmod(value, stack)
    return f"{whole} ст." + (f" + {rest} шт." if rest else "")


async def backup(db):
    folder = db.path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3"
    )
    async with db.lock:
        async with aiosqlite.connect(path) as target:
            await db.db.backup(target)
            async with target.execute("PRAGMA integrity_check") as cursor:
                row = await cursor.fetchone()
                if row[0] != "ok":
                    raise RuntimeError("Проверка резервной копии не пройдена")
    # Keep all backups; retention/deletion is deliberately not automatic.
    return path
