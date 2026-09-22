"""One private entry point for server administration."""

import discord
from discord import app_commands
from discord.ext import commands

from bot.common import bot_access_check, component_access_check, require_access
from bot.interface import Screen, Form, card, say


class AdminScreen(Screen):
    async def interaction_check(self, i):
        return await super().interaction_check(i) and await component_access_check(
            self.bot, i, admin=True
        )


async def run_action(bot, i, cog_name, method, *args, back=None):
    # Calling a command callback directly does not execute its slash checks.
    await require_access(bot, i, admin=True)
    view = AdminScreen(bot, i.user.id)
    view.button("Назад", back or (lambda j: admin_home(bot, j)))
    i.extras["return_view"] = view
    cog = bot.get_cog(cog_name)
    await getattr(cog, method).callback(cog, i, *args)


async def admin_home(bot, i):
    await require_access(bot, i, admin=True)
    view = AdminScreen(bot, i.user.id)
    view.button("Каналы и руководство", lambda j: setup_screen(bot, j))
    view.button("Доступ по ролям", lambda j: access_screen(bot, j))
    view.button("Панель в канале", lambda j: channel_screen(bot, j, "panel"))
    view.button("Канал голосований", lambda j: channel_screen(bot, j, "vote_channel"))
    view.button("Обслуживание", lambda j: maintenance_screen(bot, j))
    await say(
        i,
        embed=card("Настройки бота", "Каналы, доступ участников и обслуживание."),
        view=view,
    )


async def setup_screen(bot, i, state=None):
    await require_access(bot, i, admin=True)
    if state is None:
        settings = await bot.db.get_guild_settings(i.guild_id)
        state = {
            key: settings[key] if settings else None
            for key in (
                "projects_channel_id",
                "places_channel_id",
                "log_channel_id",
                "leadership_role_id",
            )
        }
    view = AdminScreen(bot, i.user.id)
    labels = {
        "projects_channel_id": "Проекты",
        "places_channel_id": "Места",
        "log_channel_id": "Закрытый журнал",
        "leadership_role_id": "Руководство",
    }
    for key, label in labels.items():
        role = key == "leadership_role_id"
        defaults = (
            [
                discord.SelectDefaultValue(
                    id=state[key],
                    type=(
                        discord.SelectDefaultValueType.role
                        if role
                        else discord.SelectDefaultValueType.channel
                    ),
                )
            ]
            if state[key]
            else []
        )
        if role:
            select = discord.ui.RoleSelect(
                placeholder=label + " (необязательно)",
                min_values=0,
                max_values=1,
                default_values=defaults,
            )
        else:
            types = [discord.ChannelType.text]
            if key == "projects_channel_id":
                types.append(discord.ChannelType.forum)
            select = discord.ui.ChannelSelect(
                placeholder=label, channel_types=types, default_values=defaults
            )

        async def selected(j, key=key, select=select):
            updated = dict(state)
            updated[key] = select.values[0].id if select.values else None
            await setup_screen(bot, j, updated)

        select.callback = selected
        view.add_item(select)

    async def save(j):
        await require_access(bot, j, admin=True)
        channels = []
        for key in ("projects_channel_id", "places_channel_id", "log_channel_id"):
            cid = state[key]
            if not cid:
                raise ValueError("Выберите все три канала.")
            channel = j.guild.get_channel(cid) or await j.guild.fetch_channel(cid)
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)) or (
                key != "projects_channel_id"
                and not isinstance(channel, discord.TextChannel)
            ):
                raise ValueError("Канал больше не подходит. Выберите другой.")
            channels.append(channel)
        role_id = state["leadership_role_id"]
        role = j.guild.get_role(role_id) if role_id else None
        if role_id and role is None:
            raise ValueError("Роль удалена. Выберите другую или очистите выбор.")
        await run_action(
            bot,
            j,
            "SetupCog",
            "setup",
            *channels,
            role,
            back=lambda k: setup_screen(bot, k)
        )

    view.button("Сохранить", save, primary=True)
    view.button("Назад", lambda j: admin_home(bot, j))
    await say(
        i,
        embed=card(
            "Каналы и руководство",
            "Выберите три разных канала. Журнал должен быть закрытым.\nБез отдельной роли руководство — участники с правом «Управлять сервером».",
        ),
        view=view,
    )


async def access_screen(bot, i, role_id=None, level=None):
    await require_access(bot, i, admin=True)
    view = AdminScreen(bot, i.user.id)
    roles = discord.ui.RoleSelect(
        placeholder="Роль Discord",
        default_values=(
            [
                discord.SelectDefaultValue(
                    id=role_id, type=discord.SelectDefaultValueType.role
                )
            ]
            if role_id
            else []
        ),
    )

    async def role_changed(j):
        await access_screen(bot, j, roles.values[0].id, level)

    roles.callback = role_changed
    view.add_item(roles)
    choices = [
        ("Запрещено", "blocked"),
        ("Участник", "member"),
        ("Администратор бота", "admin"),
        ("Убрать правило", "remove"),
    ]
    view.select(
        "Уровень доступа",
        [
            discord.SelectOption(label=a, value=b, default=b == level)
            for a, b in choices
        ],
        lambda j, values: access_screen(bot, j, role_id, values[0]),
    )

    async def save(j):
        role = j.guild.get_role(role_id)
        if role is None or level is None:
            raise ValueError("Выберите роль и уровень доступа.")
        args = (
            (role,)
            if level == "remove"
            else (role, app_commands.Choice(name=level, value=level))
        )
        await run_action(
            bot,
            j,
            "AccessCog",
            "remove_access" if level == "remove" else "set_access",
            *args,
            back=lambda k: access_screen(bot, k)
        )

    view.button("Сохранить", save, primary=True).disabled = (
        role_id is None or level is None
    )
    view.button(
        "Текущие правила",
        lambda j: run_action(
            bot, j, "AccessCog", "list_access", back=lambda k: access_screen(bot, k)
        ),
    )
    view.button("Назад", lambda j: admin_home(bot, j))
    await say(
        i,
        embed=card(
            "Доступ по ролям",
            "При нескольких настроенных ролях действует самая высокая в списке Discord. Роли без правила доступа не дают.",
        ),
        view=view,
    )


async def channel_screen(bot, i, action):
    await require_access(bot, i, admin=True)
    view = AdminScreen(bot, i.user.id)
    select = discord.ui.ChannelSelect(
        placeholder="Выберите текстовый канал", channel_types=[discord.ChannelType.text]
    )

    async def selected(j):
        channel = j.guild.get_channel(
            select.values[0].id
        ) or await j.guild.fetch_channel(select.values[0].id)
        await run_action(bot, j, "HubCog", action, channel)

    select.callback = selected
    view.add_item(select)
    view.button("Назад", lambda j: admin_home(bot, j))
    await say(
        i,
        embed=card(
            "Панель в канале" if action == "panel" else "Канал голосований",
            (
                "После выбора панель будет опубликована или перенесена в этот канал."
                if action == "panel"
                else "В выбранном канале будут публиковаться новые голосования. Проверьте, кому он виден."
            ),
        ),
        view=view,
    )


async def maintenance_screen(bot, i):
    await require_access(bot, i, admin=True)
    view = AdminScreen(bot, i.user.id)
    for label, method in [
        ("Проверить настройки", "diagnose"),
        ("Обновить карточки мест", "places_refresh"),
        ("Резервная копия", "backup"),
    ]:
        view.button(
            label,
            lambda j, method=method: run_action(
                bot, j, "HubCog", method, back=lambda k: maintenance_screen(bot, k)
            ),
        )

    async def repair(j):
        async def save(k, data):
            await run_action(
                bot,
                k,
                "HubCog",
                "poll_repair",
                int(data["id"]),
                back=lambda m: maintenance_screen(bot, m),
            )

        await j.response.send_modal(
            Form(
                bot,
                j.user.id,
                "Восстановить голосование",
                [("id", "Номер голосования", "", True, 20)],
                save,
            )
        )

    view.button("Восстановить голосование", repair)
    view.button("Назад", lambda j: admin_home(bot, j))
    await say(
        i,
        embed=card(
            "Обслуживание",
            "Проверка каналов, обновление опубликованных карточек и резервная копия.",
        ),
        view=view,
    )


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="admin", description="Настроить каналы, доступ и обслуживание бота"
    )
    @app_commands.guild_only()
    @bot_access_check(admin=True)
    async def admin(self, i: discord.Interaction):
        await admin_home(self.bot, i)


def simplify_commands(bot):
    # Keep legacy callbacks for the checked admin UI; publish only two entry points.
    for command in list(bot.tree.get_commands()):
        if command.name not in {"menu", "admin"}:
            bot.tree.remove_command(command.name)
