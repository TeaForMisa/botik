"""Compact public cards and permission-checked personal screens."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import discord

from bot.common import (
    PROJECT_STATUSES,
    component_access_check,
    can_manage_project,
    is_leadership,
    place_coordinates,
    send_audit,
    image_extension,
)
from bot import storage

DIMENSIONS = ["Обычный мир", "Незер", "Энд"]
CATEGORIES = ["База", "Город", "Ферма", "Склад", "Портал", "Деревня", "Другое"]
SKILLS = [
    "Строительство",
    "Декор",
    "Редстоун",
    "Фермы",
    "Добыча",
    "Исследование",
    "Любая помощь",
]
ACCESS = {"clan": "Весь клан", "leadership": "Руководство", "author": "Только я"}
COLOUR = 0x7986CB
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def clean(value):
    return discord.utils.escape_markdown(str(value)).replace("@", "@\u200b")


def card(title, description="", footer=None):
    result = discord.Embed(
        title=str(title)[:256], description=description or None, colour=COLOUR
    )
    if footer:
        result.set_footer(text=footer)
    return result


def page_footer(page, total, size):
    pages = max(1, (total + size - 1) // size)
    return f"Страница {page + 1}/{pages} · Всего: {total}"


def private_component(i):
    """Return whether an interaction belongs to our ephemeral navigation message."""
    message = getattr(i, "message", None)
    flags = getattr(message, "flags", None)
    return bool(message and flags and flags.ephemeral)


async def say(i, text="", *, embed=None, view=None, files=None):
    kwargs = dict(
        content=text or None,
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    if private_component(i):
        kwargs["attachments"] = files or []
        if i.response.is_done():
            return await i.edit_original_response(**kwargs)
        return await i.response.edit_message(**kwargs)
    kwargs["ephemeral"] = True
    # Webhook.send rejects an explicit None view. Editing above must keep
    # view=None, however, to remove the previous screen's controls.
    if view is None:
        kwargs.pop("view")
    if files:
        kwargs["files"] = files
    if i.response.is_done():
        return await i.followup.send(**kwargs)
    return await i.response.send_message(**kwargs)


async def start(i):
    if not i.response.is_done():
        if private_component(i):
            await i.response.defer()
        else:
            await i.response.defer(ephemeral=True, thinking=True)


async def guard(bot, i):
    return await component_access_check(bot, i)


async def object_for(bot, i, kind, oid, manage=False, deleted=False):
    table = {"project": "projects", "place": "places", "poll": "polls"}[kind]
    row = await bot.db.fetchone(
        f"SELECT * FROM {table} WHERE id=? AND guild_id=?", (oid, i.guild_id)
    )
    if not row:
        raise ValueError("Запись не найдена.")
    if kind in {"project", "poll"} and row["channel_id"]:
        channel = bot.get_channel(row["channel_id"])
        if channel is None:
            try:
                channel = await bot.fetch_channel(row["channel_id"])
            except discord.NotFound:
                channel = None
            except discord.Forbidden:
                raise ValueError("Канал записи недоступен.") from None
        if channel is not None and not channel.permissions_for(i.user).view_channel:
            raise ValueError("У вас нет доступа к каналу этой записи.")
    if kind == "project":
        if row["deleted"] and not deleted:
            raise ValueError("Проект удалён.")
        if manage and not await can_manage_project(bot, i.user, row):
            raise ValueError("Это действие доступно организатору и руководству.")
    if kind == "place":
        if row["is_deleted"] and not deleted:
            raise ValueError("Место удалено.")
        author = row["author_id"] == i.user.id
        leader = await is_leadership(bot, i.user)
        if (row["visibility"] == "author" and not author) or (
            row["visibility"] == "leadership" and not (author or leader)
        ):
            raise ValueError("Место недоступно.")
        if manage and not (author or leader):
            raise ValueError("Изменять место может автор или руководство.")
    if kind == "poll" and manage:
        if row["author_id"] != i.user.id and not await is_leadership(bot, i.user):
            raise ValueError("Отменить голосование может автор или руководство.")
    return row


async def error(i, exc):
    if isinstance(exc, ValueError):
        await say(i, str(exc))
    else:
        logging.exception("Ошибка интерфейса", exc_info=exc)
        await say(
            i,
            "Не удалось выполнить действие. Откройте меню заново. Подробности — в журнале бота.",
        )


class Screen(discord.ui.View):
    def __init__(self, bot, owner=None, timeout=300):
        super().__init__(timeout=timeout)
        self.bot, self.owner = bot, owner

    async def interaction_check(self, i):
        if not await guard(self.bot, i):
            return False
        if self.owner and self.owner != i.user.id:
            await say(i, "Откройте своё меню командой /menu.")
            return False
        return True

    async def on_timeout(self):
        # Personal menus expire; persistent public cards remain the entry point.
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_error(self, i, exc, item):
        await error(i, exc)

    def button(self, label, callback, *, key=None, primary=False, danger=False):
        button = discord.ui.Button(
            label=label,
            custom_id=key,
            style=(
                discord.ButtonStyle.danger
                if danger
                else (
                    discord.ButtonStyle.primary
                    if primary
                    else discord.ButtonStyle.secondary
                )
            ),
        )
        button.callback = callback
        self.add_item(button)
        return button

    def select(self, placeholder, options, callback, multiple=False):
        select = discord.ui.Select(
            placeholder=placeholder,
            options=options[:25],
            max_values=min(len(options), 25) if multiple else 1,
        )

        async def selected(i):
            await callback(i, select.values)

        select.callback = selected
        self.add_item(select)
        return select

    def back(self, callback):
        return self.button("Назад", callback)


class Form(discord.ui.Modal):
    def __init__(self, bot, owner, title, fields, callback):
        super().__init__(title=title[:45], timeout=600)
        self.bot, self.owner, self.saved = bot, owner, callback
        self.submitted = False
        self.submit_lock = asyncio.Lock()
        self.inputs = {}
        for name, label, default, required, limit in fields:
            field = discord.ui.TextInput(
                label=label,
                default="" if default is None else str(default),
                required=required,
                max_length=limit,
                style=(
                    discord.TextStyle.paragraph
                    if limit > 150
                    else discord.TextStyle.short
                ),
            )
            self.inputs[name] = field
            self.add_item(field)

    async def on_submit(self, i):
        if not await guard(self.bot, i) or i.user.id != self.owner:
            return
        await start(i)
        try:
            values = {k: str(v).strip() for k, v in self.inputs.items()}
            for key, field in self.inputs.items():
                if field.required and not values[key]:
                    raise ValueError("Обязательное поле не может состоять из пробелов.")
            async with self.submit_lock:
                if self.submitted:
                    raise ValueError("Эта форма уже отправлена. Откройте новое меню.")
                self.submitted = True
                await self.saved(i, values)
        except Exception as exc:
            if isinstance(exc, ValueError) and str(exc).startswith(
                "invalid literal for int"
            ):
                exc = ValueError("Введите целое число в числовом поле.")
            await error(i, exc)

    async def on_error(self, i, exc):
        await error(i, exc)


async def confirm(bot, i, text, callback, *, danger=False):
    view = Screen(bot, i.user.id)
    used = False
    lock = asyncio.Lock()

    async def yes(j):
        nonlocal used
        await start(j)
        async with lock:
            if used:
                return await say(j, "Это подтверждение уже использовано.")
            used = True
            await callback(j)
        view.stop()

    async def no(j):
        nonlocal used
        used = True
        await panel_home(bot, j)
        view.stop()

    view.button("Подтвердить", yes, danger=danger, primary=not danger)
    view.button("Отмена", no)
    await say(i, text, view=view)


def parse_coordinates(value):
    parts = value.replace(",", " ").split()
    if len(parts) not in (2, 3):
        raise ValueError("Введите X Z или X Y Z — две или три целые координаты.")
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        raise ValueError("Координаты должны быть целыми числами.") from None
    if any(abs(n) > 30_000_000 for n in nums):
        raise ValueError("Слишком большие координаты.")
    return (nums[0], 0, nums[1], False) if len(nums) == 2 else (*nums, True)


async def picture(bot, row, embed, existing=None):
    url = row["image_url"]
    if not url:
        return []
    if url.startswith("local:"):
        name = url[6:]
        if Path(name).name != name:
            return []
        path = bot.db.path.parent / "media" / name
        if path.is_file():
            embed.set_image(url="attachment://" + name)
            return [discord.File(path, filename=name)]
    else:
        if existing is not None and existing.attachments:
            attachment = existing.attachments[0]
            embed.set_image(url="attachment://" + attachment.filename)
            return list(existing.attachments)
        embed.set_image(url=url)
    return []


async def project_card(bot, row):
    members = await bot.db.get_project_members(row["id"])
    desc = clean(row["description"])
    desc += f"\n\n{PROJECT_STATUSES.get(row['status'],row['status'])} · Организатор <@{row['organizer_id']}>"
    desc += f"\nУчастников: **{len(members)}**"
    if row["skills"]:
        desc += " · Нужны: " + clean(row["skills"])
    return card("🏗️ " + row["name"], desc, f"Проект #{row['id']}")


async def project_location(bot, row):
    if row["place_id"]:
        place = await bot.db.get_place(row["place_id"])
        if (
            not place
            or place["is_deleted"]
            or place["visibility"] != "clan"
            or place["guild_id"] != row["guild_id"]
        ):
            return "Связанное место недоступно. Организатор может выбрать другое."
        return f"{clean(place['name'])} · {clean(place['dimension'])}\n`{place_coordinates(place)}`"
    return " · ".join(clean(x) for x in (row["dimension"], row["coordinates"]) if x)


async def get_message(bot, channel_id, message_id):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    return await channel.fetch_message(message_id)


async def sync_project(bot, oid, *, reopen=False):
    row = await bot.db.get_project(oid)
    if not row or not row["message_id"]:
        return False
    try:
        message = await get_message(bot, row["channel_id"], row["message_id"])
        was_archived = (
            isinstance(message.channel, discord.Thread) and message.channel.archived
        )
        if was_archived:
            await message.channel.edit(archived=False)
        embed = await project_card(bot, row)
        files = await picture(bot, row, embed, existing=message)
        if row["deleted"]:
            embed = card("Проект удалён", footer=f"Проект #{oid}")
            files = []
        await message.edit(
            embed=embed,
            view=None if row["deleted"] else ProjectView(bot, oid),
            attachments=files,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if isinstance(message.channel, discord.Thread):
            thread = message.channel
            options = {"name": row["name"][:100]}
            if isinstance(thread.parent, discord.ForumChannel):
                label = PROJECT_STATUSES[row["status"]]
                tag = next(
                    (t for t in thread.parent.available_tags if t.name == label), None
                )
                if tag:
                    statuses = set(PROJECT_STATUSES.values())
                    options["applied_tags"] = [
                        t for t in thread.applied_tags if t.name not in statuses
                    ][:4] + [tag]
            await thread.edit(**options)
            # Inactivity archive never changes the domain status.
            if (
                row["status"] == "completed"
                or row["deleted"]
                or (was_archived and not reopen)
            ):
                await thread.edit(archived=True)
    except discord.HTTPException:
        logging.exception("Не обновлена карточка проекта %s", oid)
        return False
    return True


async def sync_place(bot, oid):
    row = await bot.db.get_place(oid)
    if not row["message_id"]:
        return True
    try:
        message = await get_message(bot, row["channel_id"], row["message_id"])
        if row["is_deleted"] or row["visibility"] != "clan":
            await message.edit(
                content=None, embed=card("Место недоступно"), attachments=[], view=None
            )
        else:
            embed = place_card(row)
            files = await picture(bot, row, embed, existing=message)
            await message.edit(
                embed=embed,
                attachments=files,
                view=PlaceView(bot, oid),
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except discord.NotFound:
        await bot.db.execute(
            "UPDATE places SET channel_id=NULL,message_id=NULL WHERE id=?", (oid,)
        )
    except discord.HTTPException:
        logging.exception("Не обновлена карточка места %s", oid)
        return False
    return True


def place_card(row):
    desc = f"{clean(row['dimension'])} · {clean(row['category'])}\n`{place_coordinates(row)}`"
    if row["description"]:
        desc += "\n\n" + clean(row["description"])
    if row["visibility"] != "clan":
        desc += "\n\nДоступ: " + ACCESS[row["visibility"]]
    return card("📍 " + row["name"], desc, f"Место #{row['id']}")


async def changed(bot, i, kind, row, fields):
    async with bot.ui_lock:
        fresh = await object_for(bot, i, kind, row["id"], manage=True)
        if fresh["revision"] != row["revision"]:
            raise storage.Conflict("Запись уже изменена. Откройте карточку заново.")
        if kind == "place" and (
            fields.get("visibility", fresh["visibility"]) != "clan"
            or fields.get("is_deleted")
        ):
            if fresh["message_id"]:
                try:
                    message = await get_message(
                        bot, fresh["channel_id"], fresh["message_id"]
                    )
                    await message.edit(
                        content=None,
                        embed=card("Место недоступно"),
                        attachments=[],
                        view=None,
                    )
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    raise ValueError(
                        "Публичную карточку не удалось скрыть. Данные не изменены; проверьте права бота."
                    ) from None
        await storage.edit(
            bot.db,
            "projects" if kind == "project" else "places",
            row["id"],
            row["revision"],
            fields,
        )
        ok = await (
            sync_project(
                bot,
                row["id"],
                reopen="status" in fields and fields["status"] != "completed",
            )
            if kind == "project"
            else sync_place(bot, row["id"])
        )
    await send_audit(
        bot,
        i.guild,
        i.user,
        "edit",
        kind,
        row["id"],
        "Изменены поля: " + ", ".join(fields),
    )
    if fields.get("deleted") or fields.get("is_deleted"):
        return await list_objects(bot, i, kind)
    notice = ""
    if not ok:
        notice = "Сохранено, но общая карточка не обновилась. Проверьте права бота или восстановите карточку через управление."
    await (
        project_details(bot, i, row["id"], notice=notice)
        if kind == "project"
        else place_details(bot, i, row["id"], notice=notice)
    )


async def text_editor(bot, i, kind, oid):
    row = await object_for(bot, i, kind, oid, manage=True)
    fields = [
        ("name", "Название", row["name"], True, 100),
        (
            "description",
            "Описание",
            row["description"],
            False,
            1000 if kind == "project" else 500,
        ),
    ]
    if kind == "project":
        fields.append(("skills", "Кого ищем", row["skills"], False, 200))

    async def save(j, values):
        await changed(bot, j, kind, row, values)

    await i.response.send_modal(Form(bot, i.user.id, "Изменить описание", fields, save))


class ImageForm(discord.ui.Modal):
    def __init__(self, bot, owner, kind, row):
        super().__init__(title="Заменить изображение", timeout=300)
        self.bot, self.owner, self.kind, self.row = bot, owner, kind, row
        self.upload = discord.ui.FileUpload(required=True, max_values=1)
        self.add_item(
            discord.ui.Label(
                text="PNG, JPG, WEBP или GIF, до 10 МБ", component=self.upload
            )
        )

    async def on_submit(self, i):
        if not await guard(self.bot, i) or i.user.id != self.owner:
            return
        await start(i)
        saved_path = None
        try:
            current = await object_for(
                self.bot, i, self.kind, self.row["id"], manage=True
            )
            attachment = self.upload.values[0]
            if attachment.size and attachment.size > MAX_IMAGE_BYTES:
                raise ValueError(
                    "Нужна картинка PNG, JPG, WEBP или GIF размером до 10 МБ."
                )
            if current["message_id"]:
                channel = self.bot.get_channel(
                    current["channel_id"]
                ) or await self.bot.fetch_channel(current["channel_id"])
                bot_member = i.guild.me
                if bot_member is None or not channel.permissions_for(
                    bot_member
                ).attach_files:
                    raise ValueError(
                        "Бот не может прикреплять файлы в канал этой карточки. "
                        "Выдайте ему право «Прикреплять файлы» и попробуйте ещё раз."
                    )
            try:
                data = await attachment.read()
            except discord.NotFound:
                data = await attachment.read(use_cached=True)
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ValueError(
                    "Картинка не загрузилась или превышает 10 МБ. Попробуйте другой файл."
                )
            extension = image_extension(data)
            if extension is None:
                raise ValueError(
                    "Discord передал файл, который не удалось распознать как PNG, "
                    "JPG, WEBP или GIF."
                )
            folder = self.bot.db.path.parent / "media"
            folder.mkdir(parents=True, exist_ok=True)
            name = uuid.uuid4().hex + extension
            saved_path = folder / name
            await asyncio.to_thread(saved_path.write_bytes, data)
            await changed(
                self.bot, i, self.kind, self.row, {"image_url": "local:" + name}
            )
        except Exception as exc:
            if saved_path is not None and saved_path.is_file():
                table = "projects" if self.kind == "project" else "places"
                saved = await self.bot.db.fetchone(
                    f"SELECT image_url FROM {table} WHERE id=?", (self.row["id"],)
                )
                if not saved or saved["image_url"] != "local:" + saved_path.name:
                    saved_path.unlink()
            await error(i, exc)


class ProjectView(Screen):
    def __init__(self, bot, project_id, **kwargs):
        super().__init__(bot, timeout=None)
        self.project_id = project_id

        async def participation(i):
            await project_participation(bot, i, project_id)

        async def materials(i):
            await material_list(bot, i, project_id)

        async def details(i):
            await project_details(bot, i, project_id)

        self.button(
            "Участие", participation, key=f"project:{project_id}:join", primary=True
        )
        self.button("Материалы", materials, key=f"project:{project_id}:materials")
        self.button("Подробнее", details, key=f"project:{project_id}:details")


class PlaceView(Screen):
    def __init__(self, bot, oid):
        super().__init__(bot, timeout=None)

        async def open_place(i):
            await place_details(bot, i, oid)

        self.button("Открыть", open_place, key=f"place:{oid}:open")


async def project_participation(bot, i, oid):
    row = await object_for(bot, i, "project", oid)
    if row["status"] == "completed":
        view = Screen(bot, i.user.id)

        async def back(j):
            await project_details(bot, j, oid)

        view.back(back)
        return await say(
            i,
            embed=card("Участие · " + row["name"], "Проект завершён, набор закрыт."),
            view=view,
        )
    member = await bot.db.fetchone(
        "SELECT * FROM project_members WHERE project_id=? AND user_id=?",
        (oid, i.user.id),
    )
    view = Screen(bot, i.user.id)

    async def back(j):
        await project_details(bot, j, oid)

    view.back(back)

    async def choose(j, values):
        await start(j)
        async with bot.ui_lock:
            current = await object_for(bot, j, "project", oid)
            if current["status"] == "completed":
                raise ValueError("Проект уже завершён.")
            await bot.db.join_project(oid, j.user.id, values[0])
            await sync_project(bot, oid)
        await project_participation(bot, j, oid)

    view.select(
        "Чем хотите помочь?", [discord.SelectOption(label=s) for s in SKILLS], choose
    )
    if member:

        async def leave(j):
            await start(j)
            await object_for(bot, j, "project", oid)
            async with bot.ui_lock:
                await bot.db.leave_project(oid, j.user.id)
                await sync_project(bot, oid)
            await project_participation(bot, j, oid)

        view.button("Выйти из проекта", leave)
    description = (
        "Сейчас вы участвуете как: **" + clean(member["role_text"]) + "**."
        if member
        else "Выберите, чем хотите помочь проекту."
    )
    await say(i, embed=card("Участие · " + row["name"], description), view=view)


async def project_details(bot, i, oid, *, notice="", back_to=None):
    row = await object_for(bot, i, "project", oid)
    await start(i)
    embed = await project_card(bot, row)
    location = await project_location(bot, row)
    if location:
        embed.add_field(name="Место", value=location[:1024], inline=False)
    view = Screen(bot, i.user.id)

    async def default_back(j):
        await list_objects(bot, j, "project")

    view.back(back_to or default_back)

    async def people(j):
        await object_for(bot, j, "project", oid)
        people_rows = await bot.db.get_project_members(oid)
        await text_pages(
            bot,
            j,
            "Участники",
            [f"<@{m['user_id']}> — {clean(m['role_text'])}" for m in people_rows],
            check=lambda k: object_for(bot, k, "project", oid),
            back=lambda k: project_details(bot, k, oid),
        )

    view.button("Участники", people)
    if await can_manage_project(bot, i.user, row):

        async def manage(j):
            await management(bot, j, "project", oid)

        view.button("Управление", manage)
    await say(i, notice, embed=embed, view=view)


async def place_details(bot, i, oid, *, notice="", back_to=None):
    row = await object_for(bot, i, "place", oid)
    await start(i)
    view = Screen(bot, i.user.id)

    async def default_back(j):
        await list_objects(bot, j, "place")

    view.back(back_to or default_back)
    if row["author_id"] == i.user.id or await is_leadership(bot, i.user):

        async def manage(j):
            await management(bot, j, "place", oid)

        view.button("Управление", manage)
    embed = place_card(row)
    files = await picture(bot, row, embed)
    await say(i, notice, embed=embed, view=view, files=files)


async def text_pages(bot, i, title, lines, page=0, check=None, back=None):
    if check:
        await check(i)
    page = max(0, min(page, max(0, (len(lines) - 1) // 10)))
    view = Screen(bot, i.user.id)
    for label, new in [("Пред.", page - 1), ("Дальше", page + 1)]:
        if 0 <= new * 10 < len(lines):

            async def go(j, n=new):
                await text_pages(bot, j, title, lines, n, check, back)

            view.button(label, go)
    if back:
        view.back(back)
    await say(
        i,
        embed=card(
            title,
            "\n".join(lines[page * 10 : page * 10 + 10]) or "Здесь пока пусто.",
            page_footer(page, len(lines), 10),
        ),
        view=view,
    )


async def management(bot, i, kind, oid):
    row = await object_for(bot, i, kind, oid, manage=True)
    view = Screen(bot, i.user.id)

    async def back(j):
        await (
            project_details(bot, j, oid)
            if kind == "project"
            else place_details(bot, j, oid)
        )

    view.back(back)
    options = [
        ("Описание", "text"),
        (
            ("Место и координаты", "location")
            if kind == "project"
            else ("Координаты", "location")
        ),
        ("Заменить изображение", "image"),
        ("Убрать изображение", "noimage"),
        ("Опубликовать / обновить карточку", "publish"),
    ]
    if kind == "project":
        options += [
            ("Статус", "status"),
            ("Передать управление", "transfer"),
            ("Удалить проект", "delete"),
        ]
    else:
        options += [
            ("Категория", "category"),
            ("Доступ", "visibility"),
            ("Удалить место", "delete"),
        ]

    async def choose(j, values):
        current = await object_for(bot, j, kind, oid, manage=True)
        action = values[0]
        if action == "text":
            return await text_editor(bot, j, kind, oid)
        if action == "image":
            return await j.response.send_modal(ImageForm(bot, j.user.id, kind, current))
        if action == "location":
            return await location_menu(bot, j, kind, current)
        if action == "status":
            return await status_menu(bot, j, current)
        if action == "transfer":
            return await transfer_menu(bot, j, current)
        if action in {"category", "visibility"}:
            return await place_option(bot, j, current, action)
        if action == "delete":

            async def remove(k):
                await changed(
                    bot,
                    k,
                    kind,
                    current,
                    {"deleted": 1} if kind == "project" else {"is_deleted": 1},
                )

            return await confirm(
                bot,
                j,
                "Удалить запись? История сохранится; восстановление доступно в разделе «Удалённые».",
                remove,
                danger=True,
            )
        await start(j)
        if action == "noimage":
            return await changed(bot, j, kind, current, {"image_url": None})
        if kind == "place" and current["visibility"] != "clan":
            raise ValueError(
                "Личное место нельзя публиковать. Сначала измените доступ."
            )
        await publish(bot, j, kind, oid)

    view.select(
        "Что изменить?",
        [discord.SelectOption(label=a, value=b) for a, b in options],
        choose,
    )
    await say(
        i,
        embed=card(
            "Управление · " + clean(row["name"]),
            "Выберите, что хотите изменить.",
        ),
        view=view,
    )


async def status_menu(bot, i, row):
    view = Screen(bot, i.user.id)

    async def back(j):
        await management(bot, j, "project", row["id"])

    view.back(back)

    async def selected(j, values):
        status = values[0]

        async def save(k):
            await changed(bot, k, "project", row, {"status": status})

        if status == "completed":
            await confirm(
                bot,
                j,
                "Завершить проект? Все запросы материалов закроются, невыполненные обещания будут сняты. Доставки сохранятся.",
                save,
            )
        else:
            await start(j)
            await save(j)

    view.select(
        "Новый статус",
        [discord.SelectOption(label=v, value=k) for k, v in PROJECT_STATUSES.items()],
        selected,
    )
    await say(
        i,
        embed=card(
            "Статус проекта",
            "Выберите новый статус. После возобновления запросы материалов открываются отдельно.",
        ),
        view=view,
    )


async def transfer_menu(bot, i, row):
    view = Screen(bot, i.user.id)

    async def back(j):
        await management(bot, j, "project", row["id"])

    view.back(back)
    select = discord.ui.UserSelect(placeholder="Новый организатор", max_values=1)

    async def selected(j):
        target = select.values[0]

        async def save(k):
            from bot.common import get_member_access

            member = await k.guild.fetch_member(target.id)
            if member.bot or await get_member_access(bot, member) == "blocked":
                raise ValueError("Выберите участника с доступом к боту.")
            await changed(bot, k, "project", row, {"organizer_id": target.id})

        await confirm(bot, j, f"Передать управление <@{target.id}>?", save)

    select.callback = selected
    view.add_item(select)
    await say(
        i,
        embed=card(
            "Передача проекта",
            "Выберите нового организатора. После передачи управлять проектом сможет он и руководство.",
        ),
        view=view,
    )


async def location_menu(bot, i, kind, row):
    view = Screen(bot, i.user.id)

    async def back(j):
        await management(bot, j, kind, row["id"])

    view.back(back)

    async def manual(j, values):
        dimension = values[0]
        current = await object_for(bot, j, kind, row["id"], manage=True)
        default = (
            current["coordinates"] if kind == "project" else place_coordinates(current)
        )

        async def save(k, data):
            x, y, z, has_y = parse_coordinates(data["coordinates"])
            fields = {"dimension": dimension}
            if kind == "project":
                fields.update(
                    coordinates=f"{x} {y} {z}" if has_y else f"{x} {z}", place_id=None
                )
            else:
                fields.update(x=x, y=y, z=z, y_is_set=int(has_y))
            await changed(bot, k, kind, current, fields)

        await j.response.send_modal(
            Form(
                bot,
                j.user.id,
                "Координаты",
                [("coordinates", "X Z или X Y Z", default, True, 100)],
                save,
            )
        )

    view.select(
        "Ввести координаты: выберите измерение",
        [discord.SelectOption(label=x) for x in DIMENSIONS],
        manual,
    )
    if kind == "project":

        async def link(j):
            await list_objects(bot, j, "place", link_project=row["id"])

        view.button("Выбрать общее место", link)
    await say(
        i,
        embed=card(
            "Место проекта" if kind == "project" else "Координаты места",
            (
                "Введите координаты вручную или выберите общее место из справочника."
                if kind == "project"
                else "Выберите измерение, затем введите координаты."
            ),
        ),
        view=view,
    )


async def place_option(bot, i, row, action):
    view = Screen(bot, i.user.id)

    async def back(j):
        await management(bot, j, "place", row["id"])

    view.back(back)
    options = CATEGORIES if action == "category" else list(ACCESS)

    async def choose(j, values):
        value = values[0]

        async def save(k):
            current = await object_for(bot, k, "place", row["id"], manage=True)
            if current["revision"] != row["revision"]:
                raise storage.Conflict("Место изменено. Откройте меню заново.")
            if (
                action == "visibility"
                and value == "leadership"
                and not await is_leadership(bot, k.user)
            ):
                raise ValueError("Этот доступ назначает руководство.")
            await changed(bot, k, "place", row, {action: value})

        if action == "visibility":
            await confirm(
                bot,
                j,
                "Изменить доступ? Уже увиденные координаты скрыть обратно нельзя. Если в канале осталась старая карточка этого места, удалите её вручную.",
                save,
            )
        else:
            await start(j)
            await save(j)

    view.select(
        "Выберите значение",
        [discord.SelectOption(label=ACCESS.get(x, x), value=x) for x in options],
        choose,
    )
    if action == "category":

        async def custom(j):
            async def save(k, data):
                await changed(bot, k, "place", row, {"category": data["category"]})

            await j.response.send_modal(
                Form(
                    bot,
                    j.user.id,
                    "Своя категория",
                    [("category", "Название категории", "", True, 50)],
                    save,
                )
            )

        view.button("Своя категория", custom)
    await say(
        i,
        embed=card(
            "Категория места" if action == "category" else "Доступ к месту",
            (
                "Выберите подходящую категорию или добавьте свою."
                if action == "category"
                else "Выберите, кто сможет найти это место и увидеть его координаты."
            ),
        ),
        view=view,
    )


async def publish(bot, i, kind, oid):
    async with bot.ui_lock:
        row = await object_for(bot, i, kind, oid, manage=True)
        if row["message_id"]:
            try:
                await get_message(bot, row["channel_id"], row["message_id"])
            except discord.NotFound:
                pass
            else:
                ok = await (
                    sync_project(bot, oid)
                    if kind == "project"
                    else sync_place(bot, oid)
                )
                notice = (
                    "Карточка обновлена."
                    if ok
                    else "Карточка не обновилась. Проверьте права бота."
                )
                return await (
                    project_details(bot, i, oid, notice=notice)
                    if kind == "project"
                    else place_details(bot, i, oid, notice=notice)
                )
        settings = await bot.db.get_guild_settings(i.guild_id)
        if not settings:
            raise ValueError("Сначала выполните /setup.")
        channel_id = settings[
            "projects_channel_id" if kind == "project" else "places_channel_id"
        ]
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        embed = await project_card(bot, row) if kind == "project" else place_card(row)
        files = await picture(bot, row, embed)
        options = dict(
            embed=embed,
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),
            view=ProjectView(bot, oid) if kind == "project" else PlaceView(bot, oid),
        )
        if kind == "project":
            if isinstance(channel, discord.ForumChannel):
                options["content"] = ""
                tag = next(
                    (
                        t
                        for t in channel.available_tags
                        if t.name == PROJECT_STATUSES[row["status"]]
                    ),
                    None,
                )
                if tag:
                    options["applied_tags"] = [tag]
                elif channel.flags.require_tag:
                    raise ValueError(
                        "Форум требует тег. Создайте теги статусов из инструкции и повторите публикацию."
                    )
                result = await channel.create_thread(name=row["name"], **options)
                message = result.message
            else:
                thread = await channel.create_thread(
                    name=row["name"], type=discord.ChannelType.public_thread
                )
                message = await thread.send(**options)
            await bot.db.set_project_message(
                oid, message.channel.id, message.id, row["image_url"] or ""
            )
        else:
            if row["visibility"] != "clan":
                raise ValueError("Публиковать можно только общие места.")
            message = await channel.send(**options)
            await bot.db.execute(
                "UPDATE places SET channel_id=?,message_id=? WHERE id=?",
                (channel.id, message.id, oid),
            )
    notice = "Карточка опубликована: " + message.jump_url
    await (
        project_details(bot, i, oid, notice=notice)
        if kind == "project"
        else place_details(bot, i, oid, notice=notice)
    )


async def create_project(bot, i):
    async def save(j, data):
        if not await bot.db.get_guild_settings(j.guild_id):
            raise ValueError("Сначала выполните /setup.")
        oid = await bot.db.create_project(
            j.guild_id, data["name"], data["description"], "", "", "", j.user.id
        )
        await send_audit(
            bot, j.guild, j.user, "create", "project", oid, "Создан проект."
        )
        try:
            await publish(bot, j, "project", oid)
        except discord.HTTPException:
            await say(
                j,
                "Проект сохранён, но карточка не опубликовалась. Откройте проект через «Моё» и выберите «Управление» → «Опубликовать / обновить карточку».",
            )

    await i.response.send_modal(
        Form(
            bot,
            i.user.id,
            "Новый проект",
            [
                ("name", "Название", "", True, 100),
                ("description", "Что строим?", "", True, 1000),
            ],
            save,
        )
    )


async def create_place(bot, i, visibility="clan"):
    if visibility == "leadership" and not await is_leadership(bot, i.user):
        raise ValueError("Места для руководства добавляет руководство.")
    view = Screen(bot, i.user.id)

    async def choose(j, values):
        await create_place_category(bot, j, visibility, values[0])

    view.select(
        "Измерение", [discord.SelectOption(label=x) for x in DIMENSIONS], choose
    )
    await say(
        i,
        embed=card(
            "Новое место",
            "Сначала выберите измерение.\n\n"
            "Доступ: **"
            + ACCESS[visibility]
            + "** · публикация в канал выполняется отдельно.",
        ),
        view=view,
    )


async def create_place_category(bot, i, visibility, dimension):
    view = Screen(bot, i.user.id)

    async def choose(j, values):
        category = values[0]

        async def save(k, data):
            if visibility == "leadership" and not await is_leadership(bot, k.user):
                raise ValueError("Доступ руководства отозван.")
            x, y, z, has_y = parse_coordinates(data["coordinates"])
            oid = await bot.db.create_place(
                k.guild_id,
                data["name"],
                dimension,
                x,
                y,
                z,
                data["description"],
                category,
                visibility,
                k.user.id,
                y_is_set=has_y,
            )
            await send_audit(
                bot,
                k.guild,
                k.user,
                "create",
                "place",
                oid,
                "Создано место; доступ: " + ACCESS[visibility],
            )
            await place_details(bot, k, oid)

        await j.response.send_modal(
            Form(
                bot,
                j.user.id,
                "Добавить место",
                [
                    ("name", "Название", "", True, 100),
                    ("coordinates", "X Z или X Y Z", "", True, 100),
                    ("description", "Описание (необязательно)", "", False, 500),
                ],
                save,
            )
        )

    async def back(j):
        await create_place(bot, j, visibility)

    view.select(
        "Категория", [discord.SelectOption(label=x) for x in CATEGORIES], choose
    )
    view.back(back)
    await say(
        i,
        embed=card(
            "Новое место",
            "Измерение: **" + clean(dimension) + "**\nТеперь выберите категорию.",
        ),
        view=view,
    )


async def list_objects(
    bot,
    i,
    kind,
    *,
    page=0,
    query="",
    mode="active",
    link_project=None,
    category="",
    dimension="",
    origin="panel",
):
    await start(i)
    if kind == "project":
        rows = await bot.db.get_projects(i.guild_id, include_completed=True)
        rows = [r for r in rows if bool(r["deleted"]) == (mode == "deleted")]
        if mode == "active":
            rows = [r for r in rows if r["status"] != "completed"]
        elif mode == "archive":
            rows = [r for r in rows if r["status"] == "completed"]
        elif mode == "mine":
            joined = await bot.db.fetchall(
                "SELECT project_id FROM project_members WHERE user_id=?", (i.user.id,)
            )
            ids = {r["project_id"] for r in joined}
            rows = [r for r in rows if r["organizer_id"] == i.user.id or r["id"] in ids]
        if mode == "deleted":
            rows = [r for r in rows if await can_manage_project(bot, i.user, r)]
        permitted = []
        for r in rows:
            try:
                await object_for(bot, i, kind, r["id"], deleted=mode == "deleted")
            except ValueError:
                continue
            permitted.append(r)
        rows = permitted
    else:
        rows = await bot.db.find_places(
            i.guild_id, query, category, dimension, include_deleted=mode == "deleted"
        )
        visible = []
        for r in rows:
            if bool(r["is_deleted"]) != (mode == "deleted"):
                continue
            try:
                await object_for(bot, i, "place", r["id"], deleted=mode == "deleted")
            except ValueError:
                continue
            if mode == "mine" and r["author_id"] != i.user.id:
                continue
            if link_project and r["visibility"] != "clan":
                continue
            visible.append(r)
        rows = visible
    if query:
        rows = [r for r in rows if query.casefold() in r["name"].casefold()]
    page = max(0, min(page, max(0, (len(rows) - 1) // 5)))
    items = rows[page * 5 : page * 5 + 5]
    view = Screen(bot, i.user.id)
    desc = "\n\n".join(
        "**"
        + clean(r["name"])
        + "**\n"
        + (
            PROJECT_STATUSES[r["status"]]
            if kind == "project"
            else clean(r["dimension"]) + " · " + clean(r["category"])
        )
        for r in items
    )
    if items:

        async def selected(j, values):
            oid = int(values[0])
            if mode == "deleted":
                row = await object_for(bot, j, kind, oid, manage=True, deleted=True)

                async def restore(k):
                    await object_for(bot, k, kind, oid, manage=True, deleted=True)
                    await storage.edit(
                        bot.db,
                        "projects" if kind == "project" else "places",
                        oid,
                        row["revision"],
                        {"deleted": 0} if kind == "project" else {"is_deleted": 0},
                    )
                    await (
                        project_details(bot, k, oid)
                        if kind == "project"
                        else place_details(bot, k, oid)
                    )

                return await confirm(bot, j, "Восстановить запись?", restore)
            if link_project:
                project = await object_for(bot, j, "project", link_project, manage=True)
                place = await object_for(bot, j, "place", oid)
                if place["visibility"] != "clan":
                    raise ValueError("Выберите общее место.")
                await start(j)
                return await changed(
                    bot,
                    j,
                    "project",
                    project,
                    {"place_id": oid, "coordinates": "", "dimension": ""},
                )

            async def return_to_list(k):
                await list_objects(
                    bot,
                    k,
                    kind,
                    page=page,
                    query=query,
                    mode=mode,
                    category=category,
                    dimension=dimension,
                    origin=origin,
                )

            await (
                project_details(bot, j, oid, back_to=return_to_list)
                if kind == "project"
                else place_details(bot, j, oid, back_to=return_to_list)
            )

        view.select(
            "Выберите запись",
            [
                discord.SelectOption(label=r["name"][:100], value=str(r["id"]))
                for r in items
            ],
            selected,
        )
    for label, new in [("Пред.", page - 1), ("Дальше", page + 1)]:
        if 0 <= new * 5 < len(rows):

            async def go(j, n=new):
                await list_objects(
                    bot,
                    j,
                    kind,
                    page=n,
                    query=query,
                    mode=mode,
                    link_project=link_project,
                    category=category,
                    dimension=dimension,
                    origin=origin,
                )

            view.button(label, go)

    async def search(j):
        async def save(k, values):
            await list_objects(
                bot,
                k,
                kind,
                query=values["query"],
                mode=mode,
                link_project=link_project,
                category=values.get("category", ""),
                dimension=values.get("dimension", ""),
                origin=origin,
            )

        fields = [("query", "Часть названия", query, False, 100)]
        if kind == "place":
            fields += [
                ("category", "Категория (необязательно)", category, False, 50),
                ("dimension", "Измерение (необязательно)", dimension, False, 50),
            ]
        await j.response.send_modal(Form(bot, j.user.id, "Поиск", fields, save))

    view.button("Поиск", search)
    if not link_project:

        async def add(j):
            await (
                create_project(bot, j) if kind == "project" else create_place(bot, j)
            )

        view.button("Новый проект" if kind == "project" else "Новое место", add)

        async def modes(j, values):
            await list_objects(bot, j, kind, mode=values[0], origin=origin)

        choices = [
            ("Активные" if kind == "project" else "Все доступные", "active"),
            ("Мои", "mine"),
            ("Удалённые", "deleted"),
        ]
        if kind == "project":
            choices.insert(1, ("Завершённые", "archive"))
        view.select(
            "Раздел",
            [discord.SelectOption(label=a, value=b) for a, b in choices],
            modes,
        )

    async def back(j):
        if link_project:
            project = await object_for(bot, j, "project", link_project, manage=True)
            await location_menu(bot, j, "project", project)
        elif origin == "mine":
            await my_menu(bot, j)
        else:
            await panel_home(bot, j)

    view.back(back)
    if kind == "project":
        titles = {
            "active": "🏗️ Проекты",
            "archive": "🏗️ Завершённые проекты",
            "mine": "🏗️ Мои проекты",
            "deleted": "🏗️ Удалённые проекты",
        }
        empty = {
            "active": "Активных проектов пока нет. Можно создать первый.",
            "archive": "Завершённых проектов пока нет.",
            "mine": "У вас пока нет своих проектов или участия в чужих.",
            "deleted": "Удалённых проектов нет.",
        }
    else:
        titles = {
            "active": "📍 Места",
            "mine": "📍 Мои места",
            "deleted": "📍 Удалённые места",
        }
        empty = {
            "active": "Мест пока нет. Сохраните базу, ферму, склад или другую точку.",
            "mine": "Вы пока не добавили ни одного места.",
            "deleted": "Удалённых мест нет.",
        }
    if query and not rows:
        empty_text = "По вашему запросу ничего не найдено."
    else:
        empty_text = empty.get(mode, "Здесь пока пусто.")
    if link_project:
        titles[mode] = "📍 Место проекта"
        empty_text = "Подходящих общих мест пока нет."
    await say(
        i,
        embed=card(
            titles.get(mode, "🏗️ Проекты" if kind == "project" else "📍 Места"),
            desc or empty_text,
            page_footer(page, len(rows), 5),
        ),
        view=view,
    )


async def material_list(bot, i, project_id, page=0):
    project = await object_for(bot, i, "project", project_id)
    await start(i)
    rows = await bot.db.fetchall(
        "SELECT * FROM materials WHERE project_id=? ORDER BY closed,id", (project_id,)
    )
    page = max(0, min(page, max(0, (len(rows) - 1) // 5)))
    items = rows[page * 5 : page * 5 + 5]
    lines = []
    for row in items:
        promised, delivered = await storage.material_totals(bot.db, row["id"])
        q = lambda value: storage.quantity(value, row["stack"])
        lines.append(
            f"**{clean(row['name'])}**"
            + (" · закрыт" if row["closed"] else "")
            + f"\nДоставлено {q(delivered)} из {q(row['target'])}"
            f"\nЕщё обещано {q(promised)} · Нужна помощь: {q(max(0,row['target']-delivered-promised))}"
        )
    view = Screen(bot, i.user.id)
    if items:

        async def selected(j, values):
            await material_details(bot, j, int(values[0]))

        view.select(
            "Выберите материал",
            [
                discord.SelectOption(label=r["name"][:100], value=str(r["id"]))
                for r in items
            ],
            selected,
        )
    for label, new in [("Пред.", page - 1), ("Дальше", page + 1)]:
        if 0 <= new * 5 < len(rows):

            async def go(j, n=new):
                await material_list(bot, j, project_id, n)

            view.button(label, go)
    if (
        await can_manage_project(bot, i.user, project)
        and project["status"] != "completed"
    ):

        async def add(j):
            async def save(k, data):
                async with bot.ui_lock:
                    current = await object_for(
                        bot, k, "project", project_id, manage=True
                    )
                    if current["status"] == "completed":
                        raise ValueError("Проект завершён.")
                    target, stack = int(data["target"]), int(data["stack"])
                    if not 0 < target <= 100_000_000 or stack not in (1, 16, 64):
                        raise ValueError(
                            "Нужно положительное количество до 100 000 000; стак: 1, 16 или 64."
                        )
                    await bot.db.execute(
                        "INSERT INTO materials(project_id,name,target,stack,destination) VALUES (?,?,?,?,?)",
                        (project_id, data["name"], target, stack, data["destination"]),
                    )
                await material_list(bot, k, project_id)

            await j.response.send_modal(
                Form(
                    bot,
                    j.user.id,
                    "Новый материал",
                    [
                        ("name", "Название материала", "", True, 80),
                        ("target", "Нужно всего, в штуках", "", True, 10),
                        ("stack", "Размер стака: 1, 16 или 64", "64", True, 2),
                        ("destination", "Куда доставить", "", False, 200),
                    ],
                    save,
                )
            )

        view.button("Добавить материал", add)

    async def back(j):
        await project_details(bot, j, project_id)

    view.back(back)
    await say(
        i,
        embed=card(
            "📦 Материалы · " + project["name"],
            "\n\n".join(lines)
            or "Для этого проекта пока не запрашивали материалы.",
            page_footer(page, len(rows), 5),
        ),
        view=view,
    )


async def material_for(bot, i, mid, manage=False):
    row = await bot.db.fetchone("SELECT * FROM materials WHERE id=?", (mid,))
    if not row:
        raise ValueError("Запрос не найден.")
    project = await object_for(bot, i, "project", row["project_id"], manage=manage)
    return row, project


async def material_details(bot, i, mid):
    row, project = await material_for(bot, i, mid)
    await start(i)
    own = await bot.db.fetchone(
        "SELECT * FROM contributions WHERE material_id=? AND user_id=?",
        (mid, i.user.id),
    )
    promised, delivered = await storage.material_totals(bot.db, mid)
    q = lambda v: storage.quantity(v, row["stack"])
    desc = f"Нужно: {q(row['target'])}\nДоставлено: {q(delivered)}\nЕщё обещано: {q(promised)}"
    desc += "\nОсталось доставить: " + q(max(0, row["target"] - delivered))
    if row["destination"]:
        desc += "\n\nКуда: " + clean(row["destination"])
    if own:
        desc += f"\n\nВы: доставлено {q(own['delivered'])}, ещё обещано {q(own['promised'])}"
    view = Screen(bot, i.user.id)

    async def back(j):
        await material_list(bot, j, project["id"])

    view.back(back)
    if not row["closed"] and project["status"] != "completed":
        for label, action in [
            ("Принесу", "promise"),
            ("Доставил", "deliver"),
            ("Исправить доставку", "correct"),
        ]:

            async def open_form(j, action=action):
                current, _ = await material_for(bot, j, mid)
                previous = await bot.db.fetchone(
                    "SELECT * FROM contributions WHERE material_id=? AND user_id=?",
                    (mid, j.user.id),
                )
                default = (
                    (
                        previous["promised"]
                        if action == "promise"
                        else previous["delivered"]
                    )
                    if previous and action != "deliver"
                    else ""
                )
                title = {
                    "promise": "Сколько ещё принесёте, всего?",
                    "deliver": "Сколько доставили сейчас?",
                    "correct": "Сколько вы доставили за всё время?",
                }[action]
                used = False
                form_lock = asyncio.Lock()

                async def save(k, data):
                    nonlocal used
                    async with form_lock:
                        if used:
                            raise ValueError(
                                "Эта форма уже сохранена. Откройте материал заново."
                            )
                        async with bot.ui_lock:
                            await material_for(bot, k, mid)
                            unit = data["unit"].casefold()
                            amount = int(data["amount"])
                            if unit not in ("шт", "стак"):
                                raise ValueError("Единица: шт или стак.")
                            if unit == "стак":
                                amount *= current["stack"]
                            # Absolute correction must not overwrite a concurrent delivery.
                            if action in {"correct", "promise"}:
                                latest = await bot.db.fetchone(
                                    "SELECT * FROM contributions WHERE material_id=? AND user_id=?",
                                    (mid, k.user.id),
                                )
                                column = (
                                    "delivered" if action == "correct" else "promised"
                                )
                                if (latest[column] if latest else 0) != (
                                    previous[column] if previous else 0
                                ):
                                    raise storage.Conflict(
                                        "Ваши данные изменились. Откройте материал заново."
                                    )
                            await storage.contribute(
                                bot.db, mid, k.user.id, action, amount, k.id
                            )
                        used = True
                    await send_audit(
                        bot,
                        k.guild,
                        k.user,
                        "material_" + action,
                        "material",
                        mid,
                        f"Количество: {amount} шт.",
                    )
                    await material_details(bot, k, mid)

                await j.response.send_modal(
                    Form(
                        bot,
                        j.user.id,
                        title,
                        [
                            ("amount", "Количество", default, True, 10),
                            ("unit", "Единица: шт или стак", "шт", True, 4),
                        ],
                        save,
                    )
                )

            view.button(label, open_form, primary=action == "promise")

        async def cancel(j):
            async def save(k):
                await material_for(bot, k, mid)
                await storage.contribute(bot.db, mid, k.user.id, "cancel", 0, k.id)
                await material_details(bot, k, mid)

            await confirm(
                bot, j, "Снять оставшееся обещание? Уже доставленное сохранится.", save
            )

        if own and own["promised"]:
            view.button("Снять обещание", cancel)

    async def people(j):
        await material_for(bot, j, mid)
        members = await bot.db.fetchall(
            "SELECT * FROM contributions WHERE material_id=?", (mid,)
        )
        await text_pages(
            bot,
            j,
            "Вклад участников",
            [
                f"<@{m['user_id']}> · доставлено {q(m['delivered'])}, обещано {q(m['promised'])}"
                for m in members
            ],
            check=lambda k: material_for(bot, k, mid),
            back=lambda k: material_details(bot, k, mid),
        )

    view.button("Кто помогает", people)
    if await can_manage_project(bot, i.user, project):

        async def manage(j, values):
            current, p = await material_for(bot, j, mid, manage=True)
            action = values[0]
            if action == "edit":

                async def save(k, data):
                    async with bot.ui_lock:
                        fresh, _ = await material_for(bot, k, mid, manage=True)
                        if dict(fresh) != dict(current):
                            raise storage.Conflict("Запрос изменён. Откройте заново.")
                        target = int(data["target"])
                        if not 0 < target <= 100_000_000:
                            raise ValueError(
                                "Количество должно быть положительным и не больше 100 000 000."
                            )
                        await bot.db.execute(
                            "UPDATE materials SET name=?,target=?,destination=? WHERE id=?",
                            (data["name"], target, data["destination"], mid),
                        )
                    await material_details(bot, k, mid)

                await j.response.send_modal(
                    Form(
                        bot,
                        j.user.id,
                        "Изменить запрос",
                        [
                            ("name", "Название", current["name"], True, 80),
                            (
                                "target",
                                "Нужно всего, в штуках",
                                current["target"],
                                True,
                                10,
                            ),
                            (
                                "destination",
                                "Куда доставить",
                                current["destination"],
                                False,
                                200,
                            ),
                        ],
                        save,
                    )
                )
                return

            async def toggle(k):
                async with bot.ui_lock:
                    fresh, p = await material_for(bot, k, mid, manage=True)
                    closed = 1 if action == "close" else 0
                    if not closed and p["status"] == "completed":
                        raise ValueError("Сначала возобновите проект.")
                    async with bot.db.transaction() as conn:
                        await conn.execute(
                            "UPDATE materials SET closed=? WHERE id=?", (closed, mid)
                        )
                        if closed:
                            await conn.execute(
                                "UPDATE contributions SET promised=0 WHERE material_id=?",
                                (mid,),
                            )
                await material_details(bot, k, mid)

            await confirm(
                bot,
                j,
                (
                    "Закрытие снимет невыполненные обещания. Доставки сохранятся."
                    if action == "close"
                    else "Открыть запрос заново?"
                ),
                toggle,
            )

        view.select(
            "Управление запросом",
            [
                discord.SelectOption(label="Изменить", value="edit"),
                discord.SelectOption(
                    label="Открыть" if row["closed"] else "Закрыть",
                    value="open" if row["closed"] else "close",
                ),
            ],
            manage,
        )
    await say(
        i,
        embed=card(
            row["name"] + (" · закрыт" if row["closed"] else ""),
            desc,
            f"Запрос #{mid} · В стаке: {row['stack']}",
        ),
        view=view,
    )


async def profile_screen(bot, i):
    await start(i)
    row = await bot.db.fetchone(
        "SELECT * FROM profiles WHERE guild_id=? AND user_id=?", (i.guild_id, i.user.id)
    )
    view = Screen(bot, i.user.id)

    async def back(j):
        await my_menu(bot, j)

    view.back(back)

    async def edit(j):
        async def save(k, data):
            await bot.db.execute(
                "INSERT INTO profiles(guild_id,user_id,nick) VALUES (?,?,?) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET nick=excluded.nick",
                (k.guild_id, k.user.id, data["nick"]),
            )
            await profile_screen(bot, k)

        await j.response.send_modal(
            Form(
                bot,
                j.user.id,
                "Моя анкета",
                [("nick", "Minecraft-ник", row["nick"] if row else "", True, 32)],
                save,
            )
        )

    view.button("Изменить ник" if row else "Заполнить анкету", edit, primary=True)
    if row:

        async def skills(j, values):
            await start(j)
            await bot.db.execute(
                "UPDATE profiles SET skills=? WHERE guild_id=? AND user_id=?",
                (", ".join(values), j.guild_id, j.user.id),
            )
            await profile_screen(bot, j)

        view.select(
            "Чем хотите заниматься?",
            [
                discord.SelectOption(label=s, default=s in row["skills"].split(", "))
                for s in SKILLS
            ],
            skills,
            multiple=True,
        )

        async def visibility(j):
            await bot.db.execute(
                "UPDATE profiles SET visible=1-visible WHERE guild_id=? AND user_id=?",
                (j.guild_id, j.user.id),
            )
            await profile_screen(bot, j)

        async def invites(j):
            await bot.db.execute(
                "UPDATE profiles SET invites=1-invites WHERE guild_id=? AND user_id=?",
                (j.guild_id, j.user.id),
            )
            await profile_screen(bot, j)

        async def delete(j):
            async def save(k):
                await bot.db.execute(
                    "DELETE FROM profiles WHERE guild_id=? AND user_id=?",
                    (k.guild_id, k.user.id),
                )
                await profile_screen(bot, k)

            await confirm(bot, j, "Удалить анкету?", save)

        view.button(
            "Скрыть анкету" if row["visible"] else "Показать анкету", visibility
        )
        view.button(
            "Выключить приглашения" if row["invites"] else "Разрешить приглашения",
            invites,
        )
        view.button("Удалить", delete, danger=True)
    desc = (
        (
            f"Minecraft: **{clean(row['nick'])}**\n{clean(row['skills'])}\n\n"
            f"В списке участников: {'да' if row['visible'] else 'нет'} · Приглашения: {'включены' if row['invites'] else 'выключены'}"
        )
        if row
        else "Укажите Minecraft-ник и интересы. Так другим будет проще позвать вас в подходящий проект."
    )
    await say(i, embed=card("👤 Моя анкета", desc), view=view)


async def profiles_list(bot, i, skill="", page=0):
    await start(i)
    from bot.common import get_member_access

    rows = await bot.db.fetchall(
        "SELECT * FROM profiles WHERE guild_id=? AND visible=1 ORDER BY nick",
        (i.guild_id,),
    )
    visible = []
    for row in rows:
        if skill and skill not in row["skills"].split(", "):
            continue
        try:
            member = i.guild.get_member(row["user_id"]) or await i.guild.fetch_member(
                row["user_id"]
            )
        except discord.NotFound:
            continue
        if await get_member_access(bot, member) != "blocked":
            visible.append(row)
    page = max(0, min(page, max(0, (len(visible) - 1) // 5)))
    items = visible[page * 5 : page * 5 + 5]
    view = Screen(bot, i.user.id)

    async def back(j):
        await panel_home(bot, j)

    view.back(back)

    async def filter_skill(j, values):
        await profiles_list(bot, j, "" if values[0] == "all" else values[0])

    view.select(
        "Фильтр по интересам",
        [discord.SelectOption(label="Все", value="all")]
        + [discord.SelectOption(label=s, value=s) for s in SKILLS],
        filter_skill,
    )
    eligible = [r for r in items if r["invites"] and r["user_id"] != i.user.id]
    if eligible:

        async def invite(j, values):
            target = int(values[0])
            await invitation_menu(bot, j, target)

        view.select(
            "Пригласить в проект",
            [
                discord.SelectOption(label=r["nick"][:100], value=str(r["user_id"]))
                for r in eligible
            ],
            invite,
        )
    for label, new in [("Пред.", page - 1), ("Дальше", page + 1)]:
        if 0 <= new * 5 < len(visible):

            async def go(j, n=new):
                await profiles_list(bot, j, skill, n)

            view.button(label, go)
    desc = "\n\n".join(
        f"**{clean(r['nick'])}** · <@{r['user_id']}>\n{clean(r['skills'])}"
        for r in items
    )
    empty = (
        "По этому направлению никого не найдено."
        if skill
        else "Открытых анкет пока нет."
    )
    await say(
        i,
        embed=card(
            "👥 Участники",
            desc or empty,
            page_footer(page, len(visible), 5),
        ),
        view=view,
    )


async def invitation_menu(bot, i, target, page=0):
    rows = await bot.db.get_projects(i.guild_id)
    rows = [
        r for r in rows if not r["deleted"] and await can_manage_project(bot, i.user, r)
    ]
    view = Screen(bot, i.user.id)

    async def back(j):
        await profiles_list(bot, j)

    view.back(back)
    items = rows[page * 20 : page * 20 + 20]
    if not items:
        return await say(
            i,
            embed=card(
                "Приглашение в проект",
                "У вас нет активных проектов, в которые можно пригласить участника.",
            ),
            view=view,
        )

    async def selected(j, values):
        oid = int(values[0])

        async def send(k):
            from bot.common import get_member_access

            async with bot.ui_lock:
                project = await object_for(bot, k, "project", oid, manage=True)
                if project["status"] == "completed":
                    raise ValueError("Проект завершён.")
                profile = await bot.db.fetchone(
                    "SELECT * FROM profiles WHERE guild_id=? AND user_id=?",
                    (k.guild_id, target),
                )
                if not profile or not profile["invites"] or not profile["visible"]:
                    raise ValueError("Участник больше не принимает приглашения.")
                member = await k.guild.fetch_member(target)
                if await get_member_access(bot, member) == "blocked":
                    raise ValueError("Участник больше не имеет доступа к боту.")
                channel = bot.get_channel(
                    project["channel_id"]
                ) or await bot.fetch_channel(project["channel_id"])
                if not channel.permissions_for(member).view_channel:
                    raise ValueError("Участник не видит канал проекта.")
                last = await bot.db.fetchone(
                    "SELECT * FROM invitations WHERE project_id=? AND user_id=?",
                    (oid, target),
                )
                if last and time.time() - last["sent_at"] < 86400:
                    raise ValueError(
                        "Приглашение в этот проект уже отправлялось за последние сутки."
                    )
                link = f"https://discord.com/channels/{k.guild_id}/{project['channel_id']}/{project['message_id']}"
                try:
                    await member.send(
                        f"Вас приглашают в проект **{clean(project['name'])}** на сервере **{clean(k.guild.name)}**.\n{link}\n"
                        "Участие добровольное. Приглашения отключаются в /menu → Моё → Моя анкета.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.Forbidden:
                    raise ValueError(
                        "Личные сообщения закрыты. Приглашение не доставлено."
                    ) from None
                await bot.db.execute(
                    "INSERT INTO invitations VALUES (?,?,?) ON CONFLICT(project_id,user_id) "
                    "DO UPDATE SET sent_at=excluded.sent_at",
                    (oid, target, int(time.time())),
                )
            await invitation_menu(bot, k, target)

        await confirm(bot, j, f"Отправить одно личное приглашение <@{target}>?", send)

    view.select(
        "В какой проект?",
        [
            discord.SelectOption(label=r["name"][:100], value=str(r["id"]))
            for r in items
        ],
        selected,
    )
    for label, new in [("Пред.", page - 1), ("Дальше", page + 1)]:
        if 0 <= new * 20 < len(rows):

            async def go(j, n=new):
                await invitation_menu(bot, j, target, n)

            view.button(label, go)
    await say(
        i,
        embed=card(
            "Приглашение в проект",
            f"Выберите проект, в который хотите пригласить <@{target}>.",
        ),
        view=view,
    )


async def poll_card(bot, row):
    options = json.loads(row["options"])
    results = await bot.db.fetchall(
        "SELECT choice,COUNT(*) n FROM ballots WHERE poll_id=? GROUP BY choice",
        (row["id"],),
    )
    counts = [0] * len(options)
    for r in results:
        counts[r["choice"]] = r["n"]
    desc = ""
    if row["status"] == "open":
        desc = f"До <t:{row['deadline']}:f> · <t:{row['deadline']}:R>\n"
    else:
        desc = storage.poll_result(row, counts) + "\n"
    show_counts = not row["hidden"] or row["status"] != "open"
    desc += "\n" + "\n".join(
        f"{n+1}. {clean(option)}" + (f" — **{counts[n]}**" if show_counts else "")
        for n, option in enumerate(options)
    )
    desc += f"\n\nПроголосовало: {sum(counts)}"
    if row["official"]:
        desc += f"\nПорог участия: {row['quorum']}. «За» должно быть больше «Против»; воздержавшиеся считаются в участии."
    if row["hidden"]:
        desc += "\nВыбор скрыт от участников; администратор базы технически может его увидеть."
    else:
        desc += "\nВыбор участников доступен в подробностях."
    return card(
        ("Решение · " if row["official"] else "Опрос · ") + row["question"],
        desc,
        f"Голосование #{row['id']}",
    )


class PollView(Screen):
    def __init__(self, bot, oid, back=None):
        super().__init__(bot, timeout=None)

        async def show_poll(i, notice=""):
            row = await object_for(bot, i, "poll", oid)
            await say(
                i,
                notice,
                embed=await poll_card(bot, row),
                view=PollView(bot, oid, back=back),
            )

        async def vote_menu(i):
            row = await object_for(bot, i, "poll", oid)
            if row["status"] != "open" or time.time() >= row["deadline"]:
                return await show_poll(i, "Голосование уже завершено.")
            view = Screen(bot, i.user.id)

            async def return_to_poll(j):
                await show_poll(j)

            view.back(return_to_poll)

            async def choose(j, values):
                await start(j)
                await object_for(bot, j, "poll", oid)
                await storage.vote(bot.db, oid, j.user.id, int(values[0]))
                await sync_poll(bot, oid)
                await show_poll(j, "Голос сохранён. До окончания его можно изменить.")

            options = json.loads(row["options"])
            view.select(
                "Ваш вариант",
                [
                    discord.SelectOption(label=o[:100], value=str(n))
                    for n, o in enumerate(options)
                ],
                choose,
            )
            await say(
                i,
                embed=card(
                    "Ваш голос",
                    clean(row["question"])
                    + "\n\nВыберите один вариант. Новый выбор заменит предыдущий.",
                ),
                view=view,
            )

        async def details(i):
            row = await object_for(bot, i, "poll", oid)
            own = await bot.db.fetchone(
                "SELECT choice FROM ballots WHERE poll_id=? AND user_id=?",
                (oid, i.user.id),
            )
            view = Screen(bot, i.user.id)

            async def return_to_poll(j):
                await show_poll(j)

            view.back(return_to_poll)
            if not row["hidden"]:

                async def people(j):
                    current = await object_for(bot, j, "poll", oid)
                    if current["hidden"]:
                        raise ValueError("Выбор скрыт.")
                    opts = json.loads(current["options"])
                    rows = await bot.db.fetchall(
                        "SELECT * FROM ballots WHERE poll_id=? ORDER BY user_id", (oid,)
                    )
                    await text_pages(
                        bot,
                        j,
                        "Голоса",
                        [
                            f"<@{r['user_id']}> — {clean(opts[r['choice']])}"
                            for r in rows
                        ],
                        check=lambda k: object_for(bot, k, "poll", oid),
                        back=lambda k: details(k),
                    )

                view.button("Кто как голосовал", people)
            if row["status"] == "open" and (
                row["author_id"] == i.user.id or await is_leadership(bot, i.user)
            ):

                async def cancel(j):
                    async def save(k, data):
                        await object_for(bot, k, "poll", oid, manage=True)
                        async with bot.db.transaction() as conn:
                            cursor = await conn.execute(
                                "UPDATE polls SET status='cancelled',reason=?,published=0 WHERE id=? AND status='open' AND deadline>?",
                                (data["reason"], oid, int(time.time())),
                            )
                            if cursor.rowcount != 1:
                                raise ValueError("Голосование уже завершено.")
                        await sync_poll(bot, oid)
                        await show_poll(k, "Голосование отменено.")

                    await j.response.send_modal(
                        Form(
                            bot,
                            j.user.id,
                            "Отменить голосование",
                            [("reason", "Причина отмены", "", True, 300)],
                            save,
                        )
                    )

                view.button("Отменить голосование", cancel, danger=True)
            text = (
                "Ваш голос: " + clean(json.loads(row["options"])[own["choice"]])
                if own
                else "Вы ещё не голосовали."
            )
            await say(i, text, embed=await poll_card(bot, row), view=view)

        self.button("Голосовать", vote_menu, key=f"poll:{oid}:vote", primary=True)
        self.button("Подробнее", details, key=f"poll:{oid}:details")
        if back:
            self.back(back)


async def sync_poll(bot, oid):
    async with bot.poll_lock:
        row = await bot.db.fetchone("SELECT * FROM polls WHERE id=?", (oid,))
        if not row or not row["message_id"]:
            return False
        try:
            message = await get_message(bot, row["channel_id"], row["message_id"])
            view = PollView(bot, oid)
            if row["status"] != "open":
                view.children[0].disabled = True
            await message.edit(
                embed=await poll_card(bot, row),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if row["status"] != "open":
                await bot.db.execute("UPDATE polls SET published=1 WHERE id=?", (oid,))
        except discord.HTTPException:
            logging.exception("Не обновлено голосование %s", oid)
            return False
    return True


async def create_poll(bot, i, official=False, hidden=False):
    if official and not await is_leadership(bot, i.user):
        raise ValueError("Официальные решения создаёт руководство.")
    fields = [
        ("question", "Вопрос", "", True, 180),
        ("hours", "Длительность, часов (1–720)", "24", True, 3),
    ]
    if official:
        fields.append(
            ("quorum", "Минимум участников (включая воздержавшихся)", "10", True, 4)
        )
    else:
        fields.append(("options", "Варианты: каждый с новой строки", "", True, 700))

    async def save(j, data):
        if official and not await is_leadership(bot, j.user):
            raise ValueError("Право создавать решения отозвано.")
        hours = int(data["hours"])
        if not 1 <= hours <= 720:
            raise ValueError("Длительность — от 1 до 720 часов.")
        options = (
            ["За", "Против", "Воздержаться"]
            if official
            else [s.strip() for s in data["options"].splitlines() if s.strip()]
        )
        if (
            not 2 <= len(options) <= 10
            or any(len(s) > 80 for s in options)
            or len({s.casefold() for s in options}) != len(options)
        ):
            raise ValueError("Нужно 2–10 разных вариантов, каждый до 80 символов.")
        quorum = int(data.get("quorum", 1))
        if not 1 <= quorum <= 1000:
            raise ValueError("Порог участия — от 1 до 1000.")
        settings = await bot.db.get_guild_settings(j.guild_id)
        if not settings or not settings["votes_channel_id"]:
            raise ValueError("Руководство должно выбрать канал командой /vote-channel.")
        oid = await bot.db.execute(
            "INSERT INTO polls(guild_id,author_id,question,options,deadline,official,quorum,hidden,channel_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                j.guild_id,
                j.user.id,
                data["question"],
                json.dumps(options, ensure_ascii=False),
                int(time.time()) + hours * 3600,
                int(official),
                quorum,
                int(hidden),
                settings["votes_channel_id"],
            ),
        )
        row = await bot.db.fetchone("SELECT * FROM polls WHERE id=?", (oid,))
        channel = bot.get_channel(row["channel_id"]) or await bot.fetch_channel(
            row["channel_id"]
        )
        try:
            message = await channel.send(
                embed=await poll_card(bot, row),
                view=PollView(bot, oid),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await bot.db.execute(
                "UPDATE polls SET status='cancelled',reason='Не удалось опубликовать' WHERE id=?",
                (oid,),
            )
            raise ValueError(
                "Не удалось опубликовать голосование. Проверьте канал и права бота."
            ) from None
        await bot.db.execute(
            "UPDATE polls SET message_id=? WHERE id=?", (message.id, oid)
        )
        await say(
            j,
            "Голосование опубликовано: " + message.jump_url,
            embed=await poll_card(bot, row),
            view=PollView(bot, oid),
        )

    await i.response.send_modal(
        Form(
            bot, i.user.id, "Новое решение" if official else "Новый опрос", fields, save
        )
    )


async def polls_list(bot, i, page=0, archive=False):
    await start(i)
    rows = await bot.db.fetchall(
        "SELECT * FROM polls WHERE guild_id=? AND message_id IS NOT NULL AND "
        + ("status!='open'" if archive else "status='open'")
        + " ORDER BY id DESC",
        (i.guild_id,),
    )
    permitted = []
    for row in rows:
        try:
            await object_for(bot, i, "poll", row["id"])
        except ValueError:
            continue
        permitted.append(row)
    rows = permitted
    page = max(0, min(page, max(0, (len(rows) - 1) // 5)))
    view = Screen(bot, i.user.id)

    async def back(j):
        await panel_home(bot, j)

    view.back(back)
    items = rows[page * 5 : page * 5 + 5]
    if items:

        async def selected(j, values):
            row = await object_for(bot, j, "poll", int(values[0]))
            async def back(k):
                await polls_list(bot, k, page, archive)

            detail = PollView(bot, row["id"], back=back)
            await say(j, embed=await poll_card(bot, row), view=detail)

        view.select(
            "Открыть голосование",
            [
                discord.SelectOption(label=r["question"][:100], value=str(r["id"]))
                for r in items
            ],
            selected,
        )
    for label, new in [("Пред.", page - 1), ("Дальше", page + 1)]:
        if 0 <= new * 5 < len(rows):

            async def go(j, n=new):
                await polls_list(bot, j, n, archive)

            view.button(label, go)

    async def switch(j):
        await polls_list(bot, j, archive=not archive)

    view.button("Активные" if archive else "Архив", switch)

    async def create(j, values):
        official, hidden = map(int, values[0].split(":"))
        await create_poll(bot, j, bool(official), bool(hidden))

    options = [
        discord.SelectOption(label="Обычный опрос", value="0:0"),
        discord.SelectOption(label="Опрос со скрытым выбором", value="0:1"),
    ]
    if await is_leadership(bot, i.user):
        options += [
            discord.SelectOption(label="Официальное решение", value="1:0"),
            discord.SelectOption(label="Решение со скрытым выбором", value="1:1"),
        ]
    view.select("Создать голосование", options, create)
    empty = "В архиве пока пусто." if archive else "Активных голосований пока нет."
    await say(
        i,
        embed=card(
            "🗳️ Архив голосований" if archive else "🗳️ Голосования",
            "\n".join(clean(r["question"]) for r in items) or empty,
            page_footer(page, len(rows), 5),
        ),
        view=view,
    )


class PanelView(Screen):
    def __init__(self, bot):
        super().__init__(bot, timeout=None)

        async def projects(i):
            await list_objects(bot, i, "project")

        async def places(i):
            await list_objects(bot, i, "place")

        async def mine(i):
            await my_menu(bot, i)

        async def people(i):
            await profiles_list(bot, i)

        async def votes(i):
            await polls_list(bot, i)

        self.button("🏗️ Проекты", projects, key="panel:projects", primary=True)
        self.button("📍 Места", places, key="panel:places")
        self.button("👥 Участники", people, key="panel:people")
        self.button("🗳️ Голосования", votes, key="panel:votes")
        self.button("👤 Моё", mine, key="panel:mine")


async def panel_home(bot, i):
    await say(
        i,
        embed=card(
            "Меню клана",
            "Проекты и материалы, места и координаты, участники, голосования и ваши записи.",
        ),
        view=PanelView(bot),
    )


async def my_menu(bot, i):
    view = Screen(bot, i.user.id)

    async def back(j):
        await panel_home(bot, j)

    view.back(back)

    async def projects(j):
        await list_objects(bot, j, "project", mode="mine", origin="mine")

    async def places(j):
        await list_objects(bot, j, "place", mode="mine", origin="mine")

    async def profile(j):
        await profile_screen(bot, j)

    async def promises(j):
        rows = await bot.db.fetchall(
            "SELECT m.id,m.name,c.promised,m.stack,p.name project FROM contributions c "
            "JOIN materials m ON m.id=c.material_id JOIN projects p ON p.id=m.project_id "
            "WHERE p.guild_id=? AND c.user_id=? AND c.promised>0 AND m.closed=0 AND p.deleted=0",
            (j.guild_id, j.user.id),
        )
        await text_pages(
            bot,
            j,
            "Мои обещания",
            [
                f"**{clean(r['project'])}** · {clean(r['name'])}: {storage.quantity(r['promised'],r['stack'])}\n"
                f"Открыть: /material id:{r['id']}"
                for r in rows
            ],
            back=lambda k: my_menu(bot, k),
        )

    view.button("Мои проекты", projects)
    view.button("Мои места", places)
    view.button("Моя анкета", profile)
    view.button("Обещанные ресурсы", promises)
    await say(
        i,
        embed=card(
            "👤 Моё",
            "Ваши проекты, сохранённые места, анкета и обещанные ресурсы.",
        ),
        view=view,
    )
