from __future__ import annotations

import logging
from pathlib import PurePath
from typing import Any

import discord
from discord import app_commands

PROJECT_STATUSES = {
    "idea": "💡 Идея",
    "preparation": "🟡 Подготовка",
    "building": "🟢 Строительство",
    "paused": "⏸️ Приостановлен",
    "completed": "✅ Завершён",
}

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

ACCESS_LEVELS = {
    "blocked": "⛔ Запрещено",
    "member": "👤 Основные команды",
    "admin": "🛡️ Администратор бота",
}


class BotAccessDenied(app_commands.CheckFailure):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def get_member_access(bot: Any, member: discord.Member) -> str:
    if (
        member.id == member.guild.owner_id
        or member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    ):
        return "admin"

    rules = await bot.db.get_role_access_rules(member.guild.id)
    levels_by_role = {row["role_id"]: row["access_level"] for row in rules}
    for role in reversed(member.roles):
        if role.id in levels_by_role:
            return levels_by_role[role.id]
    return "blocked"


async def require_access(
    bot: Any,
    interaction: discord.Interaction,
    *,
    admin: bool = False,
) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        raise BotAccessDenied("Эта команда доступна только на сервере.")
    level = await get_member_access(bot, interaction.user)
    if level == "blocked":
        raise BotAccessDenied("Ваша роль не может использовать этого бота.")
    if admin and level != "admin":
        raise BotAccessDenied("Эта команда доступна только администраторам бота.")
    return True


def bot_access_check(*, admin: bool = False) -> Any:
    async def predicate(interaction: discord.Interaction) -> bool:
        return await require_access(interaction.client, interaction, admin=admin)

    return app_commands.check(predicate)


async def component_access_check(
    bot: Any,
    interaction: discord.Interaction,
    *,
    admin: bool = False,
) -> bool:
    try:
        return await require_access(bot, interaction, admin=admin)
    except BotAccessDenied as error:
        if interaction.response.is_done():
            await interaction.followup.send(error.message, ephemeral=True)
        else:
            await interaction.response.send_message(error.message, ephemeral=True)
        return False


def is_supported_image(attachment: discord.Attachment) -> bool:
    suffix = PurePath(attachment.filename).suffix.lower()
    if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
        return False
    content_type = attachment.content_type
    return (
        content_type is None
        or content_type.startswith("image/")
        or content_type == "application/octet-stream"
    )


def image_extension(data: bytes) -> str | None:
    """Return a safe extension from the image bytes, ignoring Discord metadata."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def image_filename(prefix: str, object_id: int, attachment: discord.Attachment) -> str:
    suffix = PurePath(attachment.filename).suffix.lower()
    return f"{prefix}-{object_id}{suffix}"


def place_coordinates(place: Any) -> str:
    if place["y_is_set"]:
        return f"{place['x']} {place['y']} {place['z']}"
    return f"{place['x']} {place['z']}"


async def is_leadership(bot: Any, member: discord.Member) -> bool:
    if await get_member_access(bot, member) == "admin":
        return True
    settings = await bot.db.get_guild_settings(member.guild.id)
    if not settings or not settings["leadership_role_id"]:
        return False
    return any(role.id == settings["leadership_role_id"] for role in member.roles)


async def can_manage_project(bot: Any, member: discord.Member, project: Any) -> bool:
    return member.id == project["organizer_id"] or await is_leadership(bot, member)


async def send_audit(
    bot: Any,
    guild: discord.Guild,
    user: discord.abc.User,
    action: str,
    object_type: str,
    object_id: int | None,
    details: str,
) -> None:
    await bot.db.add_audit(guild.id, user.id, action, object_type, object_id, details)
    settings = await bot.db.get_guild_settings(guild.id)
    if not settings:
        return
    channel = guild.get_channel(settings["log_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        embed = discord.Embed(
            title="Действие бота",
            description=details,
            colour=discord.Colour.dark_grey(),
        )
        embed.add_field(name="Действие", value=action)
        embed.add_field(name="Объект", value=f"{object_type} #{object_id or '—'}")
        embed.add_field(
            name="Пользователь", value=f"<@{user.id}> ({user.id})", inline=False
        )
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logging.exception("Не удалось отправить запись в канал журнала")
