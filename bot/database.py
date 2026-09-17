from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import aiosqlite


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    projects_channel_id INTEGER NOT NULL,
    places_channel_id INTEGER NOT NULL,
    log_channel_id INTEGER NOT NULL,
    leadership_role_id INTEGER,
    setup_by INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS guild_role_access (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    access_level TEXT NOT NULL
        CHECK (access_level IN ('blocked', 'member', 'admin')),
    updated_by INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_guild_role_access_guild
ON guild_role_access(guild_id);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    dimension TEXT,
    coordinates TEXT,
    skills TEXT,
    organizer_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'idea',
    channel_id INTEGER,
    message_id INTEGER,
    image_url TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_projects_guild_status
ON projects(guild_id, status);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    role_text TEXT NOT NULL,
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    dimension TEXT NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    z INTEGER NOT NULL,
    y_is_set INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    category TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'clan'
        CHECK (visibility IN ('clan', 'leadership', 'author')),
    author_id INTEGER NOT NULL,
    image_url TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_places_guild_active
ON places(guild_id, is_deleted);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.execute("PRAGMA busy_timeout = 5000")
        await self.connection.executescript(SCHEMA)
        await self._ensure_column("projects", "image_url", "TEXT")
        await self._ensure_column("places", "image_url", "TEXT")
        await self._ensure_column("places", "y_is_set", "INTEGER NOT NULL DEFAULT 1")
        await self.connection.commit()

    async def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = await self.fetchall(f"PRAGMA table_info({table})")
        if not any(row["name"] == column for row in rows):
            await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("База данных ещё не инициализирована")
        return self.connection

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        cursor = await self.db.execute(sql, tuple(params))
        await self.db.commit()
        return cursor.lastrowid or 0

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, tuple(params)) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, tuple(params)) as cursor:
            return await cursor.fetchall()

    async def save_guild_settings(
        self,
        guild_id: int,
        projects_channel_id: int,
        places_channel_id: int,
        log_channel_id: int,
        leadership_role_id: int | None,
        setup_by: int,
    ) -> None:
        await self.execute(
            """
            INSERT INTO guild_settings (
                guild_id, projects_channel_id, places_channel_id,
                log_channel_id, leadership_role_id, setup_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                projects_channel_id = excluded.projects_channel_id,
                places_channel_id = excluded.places_channel_id,
                log_channel_id = excluded.log_channel_id,
                leadership_role_id = excluded.leadership_role_id,
                setup_by = excluded.setup_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                guild_id,
                projects_channel_id,
                places_channel_id,
                log_channel_id,
                leadership_role_id,
                setup_by,
            ),
        )

    async def get_guild_settings(self, guild_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )

    async def set_role_access(
        self,
        guild_id: int,
        role_id: int,
        access_level: str,
        updated_by: int,
    ) -> None:
        await self.execute(
            """
            INSERT INTO guild_role_access (
                guild_id, role_id, access_level, updated_by
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET
                access_level = excluded.access_level,
                updated_by = excluded.updated_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, role_id, access_level, updated_by),
        )

    async def remove_role_access(self, guild_id: int, role_id: int) -> None:
        await self.execute(
            "DELETE FROM guild_role_access WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )

    async def get_role_access_rules(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT role_id, access_level, updated_by, updated_at
            FROM guild_role_access
            WHERE guild_id = ?
            ORDER BY role_id
            """,
            (guild_id,),
        )

    async def create_project(
        self,
        guild_id: int,
        name: str,
        description: str,
        dimension: str,
        coordinates: str,
        skills: str,
        organizer_id: int,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO projects (
                guild_id, name, description, dimension,
                coordinates, skills, organizer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, name, description, dimension, coordinates, skills, organizer_id),
        )

    async def set_project_message(
        self,
        project_id: int,
        channel_id: int,
        message_id: int,
        image_url: str = "",
    ) -> None:
        await self.execute(
            """
            UPDATE projects
            SET channel_id = ?, message_id = ?, image_url = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (channel_id, message_id, image_url or None, project_id),
        )

    async def delete_project_draft(self, project_id: int) -> None:
        await self.execute(
            "DELETE FROM projects WHERE id = ? AND message_id IS NULL", (project_id,)
        )

    async def get_project(self, project_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))

    async def get_projects(self, guild_id: int, include_completed: bool = False) -> list[aiosqlite.Row]:
        if include_completed:
            return await self.fetchall(
                "SELECT * FROM projects WHERE guild_id = ? ORDER BY id DESC", (guild_id,)
            )
        return await self.fetchall(
            """
            SELECT * FROM projects
            WHERE guild_id = ? AND status != 'completed'
            ORDER BY id DESC
            """,
            (guild_id,),
        )

    async def get_active_projects(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM projects WHERE status != 'completed' AND message_id IS NOT NULL"
        )

    async def set_project_status(self, project_id: int, status: str) -> None:
        await self.execute(
            """
            UPDATE projects SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, project_id),
        )

    async def join_project(self, project_id: int, user_id: int, role_text: str) -> None:
        await self.execute(
            """
            INSERT INTO project_members (project_id, user_id, role_text)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id, user_id) DO UPDATE SET role_text = excluded.role_text
            """,
            (project_id, user_id, role_text),
        )

    async def leave_project(self, project_id: int, user_id: int) -> None:
        await self.execute(
            "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )

    async def get_project_members(self, project_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT user_id, role_text FROM project_members
            WHERE project_id = ? ORDER BY joined_at
            """,
            (project_id,),
        )

    async def create_place(
        self,
        guild_id: int,
        name: str,
        dimension: str,
        x: int,
        y: int,
        z: int,
        description: str,
        category: str,
        visibility: str,
        author_id: int,
        image_url: str = "",
        y_is_set: bool = True,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO places (
                guild_id, name, dimension, x, y, z, y_is_set, description,
                category, visibility, author_id, image_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                name,
                dimension,
                x,
                y,
                z,
                int(y_is_set),
                description,
                category,
                visibility,
                author_id,
                image_url or None,
            ),
        )

    async def set_place_image(self, place_id: int, image_url: str) -> None:
        await self.execute(
            "UPDATE places SET image_url = ? WHERE id = ?", (image_url, place_id)
        )

    async def get_place(self, place_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM places WHERE id = ?", (place_id,))

    async def find_places(
        self,
        guild_id: int,
        query: str = "",
        category: str = "",
        dimension: str = "",
        include_deleted: bool = False,
    ) -> list[aiosqlite.Row]:
        conditions = ["guild_id = ?"]
        params: list[Any] = [guild_id]
        if not include_deleted:
            conditions.append("is_deleted = 0")
        if query:
            conditions.append("LOWER(name) LIKE ?")
            params.append(f"%{query.lower()}%")
        if category:
            conditions.append("LOWER(category) = ?")
            params.append(category.lower())
        if dimension:
            conditions.append("LOWER(dimension) = ?")
            params.append(dimension.lower())
        return await self.fetchall(
            f"SELECT * FROM places WHERE {' AND '.join(conditions)} ORDER BY name", params
        )

    async def soft_delete_place(self, place_id: int) -> None:
        await self.execute(
            """
            UPDATE places SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (place_id,),
        )

    async def restore_place(self, place_id: int) -> None:
        await self.execute(
            "UPDATE places SET is_deleted = 0, deleted_at = NULL WHERE id = ?",
            (place_id,),
        )

    async def add_audit(
        self,
        guild_id: int,
        user_id: int,
        action: str,
        object_type: str,
        object_id: int | None,
        details: str = "",
    ) -> None:
        await self.execute(
            """
            INSERT INTO audit_log (
                guild_id, user_id, action, object_type, object_id, details
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, action, object_type, object_id, details),
        )
