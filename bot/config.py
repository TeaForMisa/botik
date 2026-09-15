from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    database_path: Path
    sync_guild_id: int | None

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv()

        token = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "Не задан DISCORD_BOT_TOKEN. Добавьте его в .env или в переменные Bothost."
            )

        configured_path = os.getenv("DATABASE_PATH", "").strip()
        if configured_path:
            database_path = Path(configured_path)
        elif Path("/app").is_dir():
            database_path = Path("/app/data/clan_bot.db")
        else:
            database_path = Path("data/clan_bot.db")

        database_path.parent.mkdir(parents=True, exist_ok=True)

        raw_guild_id = os.getenv("SYNC_GUILD_ID", "").strip()
        sync_guild_id = int(raw_guild_id) if raw_guild_id else None
        return cls(token, database_path, sync_guild_id)

