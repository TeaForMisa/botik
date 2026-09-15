from __future__ import annotations

import logging
from typing import Any

import discord


PROJECT_STATUSES = {
    "idea": "💡 Идея",
    "preparation": "🟡 Подготовка",
    "building": "🟢 Строительство",
    "paused": "⏸️ Приостановлен",
    "completed": "✅ Завершён",
}


async def is_leadership(bot: Any, member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
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
