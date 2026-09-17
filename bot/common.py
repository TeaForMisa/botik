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
    return "member"


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
    return attachment.content_type is None or attachment.content_type.startswith("image/")


def image_filename(prefix: str, object_id: int, attachment: discord.Attachment) -> str:
    suffix = PurePath(attachment.filename).suffix.lower()
    return f"{prefix}-{object_id}{suffix}"


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
    await bot.db.add_audit(
        guild.id, user.id, action, object_type, object_id, details
    )
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
        embed.add_field(name="Пользователь", value=f"<@{user.id}> ({user.id})", inline=False)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logging.exception("Не удалось отправить запись в канал журнала")


def project_embed(project: Any, members: list[Any]) -> discord.Embed:
    status = PROJECT_STATUSES.get(project["status"], project["status"])
    embed = discord.Embed(
        title=f"🏗️ {project['name']}",
        description=project["description"],
        colour=discord.Colour.gold()
        if project["status"] in {"idea", "preparation"}
        else discord.Colour.green(),
    )
    embed.add_field(name="Статус", value=status)
    embed.add_field(name="Организатор", value=f"<@{project['organizer_id']}>")
    location_parts = [part for part in (project["dimension"], project["coordinates"]) if part]
    embed.add_field(
        name="Место",
        value=" · ".join(location_parts) if location_parts else "Пока не указано",
        inline=False,
    )
    embed.add_field(
        name="Нужны",
        value=project["skills"] or "Любая помощь",
        inline=False,
    )
    if members:
        value = "\n".join(f"<@{m['user_id']}> — {m['role_text']}" for m in members)
    else:
        value = "Пока никто не присоединился"
    embed.add_field(name=f"Участники ({len(members)})", value=value[:1024], inline=False)
    if project["image_url"]:
        embed.set_image(url=project["image_url"])
    embed.set_footer(text=f"Проект #{project['id']}")
    return embed


def place_embed(place: Any, *, hide_coordinates: bool = False) -> discord.Embed:
    visibility = {
        "clan": "весь клан",
        "leadership": "только руководство",
        "author": "только автор",
    }.get(place["visibility"], place["visibility"])
    embed = discord.Embed(
        title=f"📍 {place['name']}",
        description=place["description"] or "Без описания",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Измерение", value=place["dimension"])
    coordinates = "скрыты" if hide_coordinates else f"{place['x']} {place['y']} {place['z']}"
    embed.add_field(name="Координаты", value=coordinates)
    embed.add_field(name="Категория", value=place["category"])
    embed.add_field(name="Доступ", value=visibility)
    embed.add_field(name="Добавил", value=f"<@{place['author_id']}>")
    if place["image_url"]:
        embed.set_image(url=place["image_url"])
    embed.set_footer(text=f"Место #{place['id']}")
    return embed


async def fetch_project_message(bot: Any, project: Any) -> discord.Message | None:
    if not project["channel_id"] or not project["message_id"]:
        return None
    try:
        channel = bot.get_channel(project["channel_id"])
        if channel is None:
            channel = await bot.fetch_channel(project["channel_id"])
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await channel.fetch_message(project["message_id"])
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logging.exception("Не удалось получить карточку проекта #%s", project["id"])
    return None


async def refresh_project_message(bot: Any, project_id: int) -> None:
    from bot.views import ProjectView

    project = await bot.db.get_project(project_id)
    if not project:
        return
    message = await fetch_project_message(bot, project)
    if message:
        members = await bot.db.get_project_members(project_id)
        await message.edit(
            embed=project_embed(project, members),
            view=ProjectView(bot, project_id, disabled=project["status"] == "completed"),
        )
