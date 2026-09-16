from __future__ import annotations

import math
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.common import (
    image_filename,
    is_leadership,
    is_supported_image,
    place_embed,
    send_audit,
)


VISIBILITY_LABELS = {
    "clan": "Весь клан",
    "leadership": "Только руководство",
    "author": "Только автор",
}


async def can_view_place(bot: Any, member: discord.Member, place: Any) -> bool:
    if place["visibility"] == "clan" or place["author_id"] == member.id:
        return True
    return place["visibility"] == "leadership" and await is_leadership(bot, member)


class PlaceCreateModal(discord.ui.Modal, title="Добавить место"):
    name = discord.ui.TextInput(
        label="Название",
        placeholder="Например: Ферма железа",
        max_length=100,
    )
    dimension = discord.ui.TextInput(
        label="Измерение",
        placeholder="Обычный мир / Незер / Энд",
        max_length=50,
    )
    coordinates = discord.ui.TextInput(
        label="Координаты X Y Z",
        placeholder="320 71 -840",
        max_length=100,
    )
    details = discord.ui.TextInput(
        label="Категория и описание",
        placeholder="База | Северный вход, рядом с порталом",
        style=discord.TextStyle.paragraph,
        max_length=550,
    )
    screenshot_field = discord.ui.Label(
        text="Скриншот (необязательно)",
        description="PNG, JPG, WEBP или GIF",
        component=discord.ui.FileUpload(required=False, max_values=1),
    )

    def __init__(
        self,
        bot: Any,
        guild_id: int,
        author_id: int,
        visibility: str,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.author_id = author_id
        self.visibility = visibility

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        screenshot_values = self.screenshot_field.component.values
        screenshot = screenshot_values[0] if screenshot_values else None
        if screenshot and not is_supported_image(screenshot):
            await interaction.response.send_message(
                "Скриншот должен быть в формате PNG, JPG, WEBP или GIF.",
                ephemeral=True,
            )
            return

        details = str(self.details).strip()
        if "|" in details:
            category, description = (
                part.strip() for part in details.split("|", maxsplit=1)
            )
        else:
            detail_lines = details.splitlines()
            category = detail_lines[0].strip()
            description = "\n".join(detail_lines[1:]).strip()

        normalized = str(self.coordinates).replace(",", " ").split()
        if len(normalized) != 3:
            await interaction.response.send_message(
                "Введите ровно три координаты: `X Y Z`.", ephemeral=True
            )
            return
        try:
            x, y, z = (int(value) for value in normalized)
        except ValueError:
            await interaction.response.send_message(
                "Координаты должны быть целыми числами, например `320 71 -840`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        place_id = await self.bot.db.create_place(
            self.guild_id,
            str(self.name).strip(),
            str(self.dimension).strip(),
            x,
            y,
            z,
            description,
            category,
            self.visibility,
            self.author_id,
            screenshot.url if screenshot else "",
        )
        place = await self.bot.db.get_place(place_id)
        settings = await self.bot.db.get_guild_settings(self.guild_id)

        jump_url = ""
        if self.visibility == "clan" and settings:
            channel = interaction.guild.get_channel(settings["places_channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    embed = place_embed(place)
                    send_options: dict[str, Any] = {
                        "embed": embed,
                        "allowed_mentions": discord.AllowedMentions.none(),
                    }
                    if screenshot:
                        filename = image_filename("place", place_id, screenshot)
                        send_options["file"] = await screenshot.to_file(
                            filename=filename,
                            description=f"Скриншот места {self.name}",
                        )
                        embed.set_image(url=f"attachment://{filename}")
                    message = await channel.send(
                        **send_options,
                    )
                    jump_url = message.jump_url
                    if message.attachments:
                        await self.bot.db.set_place_image(
                            place_id, message.attachments[0].url
                        )
                except (discord.Forbidden, discord.HTTPException):
                    pass

        await send_audit(
            self.bot,
            interaction.guild,
            interaction.user,
            "place_create",
            "place",
            place_id,
            f"Добавлено место «{self.name}», доступ: {VISIBILITY_LABELS[self.visibility]}.",
        )
        suffix = f" Карточка: {jump_url}" if jump_url else ""
        await interaction.followup.send(
            f"Место **{self.name}** сохранено под номером #{place_id}.{suffix}",
            ephemeral=True,
        )


class PlaceResultsView(discord.ui.View):
    def __init__(self, requester_id: int, places: list[Any]) -> None:
        super().__init__(timeout=180)
        self.requester_id = requester_id
        self.places = places
        self.page = 0
        self.per_page = 5
        self._sync_buttons()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.places) / self.per_page))

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= self.page_count - 1

    def render(self) -> discord.Embed:
        start = self.page * self.per_page
        page_items = self.places[start : start + self.per_page]
        embed = discord.Embed(title="📍 Найденные места", colour=discord.Colour.blurple())
        for place in page_items:
            embed.add_field(
                name=f"#{place['id']} · {place['name']}",
                value=(
                    f"**{place['dimension']}** · `{place['x']} {place['y']} {place['z']}`\n"
                    f"{place['category']} · {VISIBILITY_LABELS.get(place['visibility'], place['visibility'])}\n"
                    f"{place['description'] or 'Без описания'}"
                )[:1024],
                inline=False,
            )
        embed.set_footer(text=f"Страница {self.page + 1} из {self.page_count}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Эти кнопки относятся к поиску другого участника.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Дальше", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)


class DeletePlaceView(discord.ui.View):
    def __init__(self, bot: Any, place: Any, requester_id: int) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.place = place
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester_id

    @discord.ui.button(label="Удалить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.bot.db.soft_delete_place(self.place["id"])
        if interaction.guild:
            await send_audit(
                self.bot,
                interaction.guild,
                interaction.user,
                "place_delete",
                "place",
                self.place["id"],
                f"Место «{self.place['name']}» помечено удалённым.",
            )
        await interaction.response.edit_message(
            content="Место удалено. Руководство может восстановить его командой `/place restore`.",
            embed=None,
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Удаление отменено.", embed=None, view=None)
        self.stop()


@app_commands.guild_only()
class PlacesCog(commands.GroupCog, group_name="place", group_description="Координаты и места"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(name="add", description="Сохранить новое место")
    @app_commands.describe(
        visibility="Кто сможет найти и увидеть координаты",
    )
    @app_commands.choices(
        visibility=[
            app_commands.Choice(name="Весь клан", value="clan"),
            app_commands.Choice(name="Только руководство", value="leadership"),
            app_commands.Choice(name="Только я", value="author"),
        ]
    )
    async def add(
        self,
        interaction: discord.Interaction,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        settings = await self.bot.db.get_guild_settings(interaction.guild.id)
        if not settings:
            await interaction.response.send_message(
                "Сначала руководство должно выполнить `/setup`.", ephemeral=True
            )
            return
        visibility_value = visibility.value if visibility else "clan"
        if visibility_value == "leadership" and not await is_leadership(self.bot, interaction.user):
            await interaction.response.send_message(
                "Места для руководства может добавлять только руководство.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            PlaceCreateModal(
                self.bot,
                interaction.guild.id,
                interaction.user.id,
                visibility_value,
            )
        )

    async def _show_results(
        self,
        interaction: discord.Interaction,
        query: str = "",
        category: str = "",
        dimension: str = "",
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        places = await self.bot.db.find_places(
            interaction.guild.id, query.strip(), category.strip(), dimension.strip()
        )
        visible = [
            place for place in places if await can_view_place(self.bot, interaction.user, place)
        ]
        if not visible:
            await interaction.response.send_message("Подходящих мест не найдено.", ephemeral=True)
            return
        view = PlaceResultsView(interaction.user.id, visible)
        await interaction.response.send_message(embed=view.render(), view=view, ephemeral=True)

    @app_commands.command(name="find", description="Найти место по названию или фильтрам")
    @app_commands.describe(
        query="Часть названия",
        category="Категория, например ферма",
        dimension="Измерение, например Незер",
    )
    async def find(
        self,
        interaction: discord.Interaction,
        query: str = "",
        category: str = "",
        dimension: str = "",
    ) -> None:
        await self._show_results(interaction, query, category, dimension)

    @app_commands.command(name="list", description="Показать все доступные места")
    async def list_places(self, interaction: discord.Interaction) -> None:
        await self._show_results(interaction)

    @app_commands.command(name="delete", description="Удалить своё место")
    async def delete(self, interaction: discord.Interaction, place_id: int) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        place = await self.bot.db.get_place(place_id)
        if not place or place["guild_id"] != interaction.guild.id or place["is_deleted"]:
            await interaction.response.send_message("Место не найдено.", ephemeral=True)
            return
        if place["author_id"] != interaction.user.id and not await is_leadership(
            self.bot, interaction.user
        ):
            await interaction.response.send_message(
                "Удалять место может его автор или руководство.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Удалить это место? Запись можно будет восстановить.",
            embed=place_embed(place),
            view=DeletePlaceView(self.bot, place, interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(name="restore", description="Восстановить удалённое место")
    async def restore(self, interaction: discord.Interaction, place_id: int) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not await is_leadership(self.bot, interaction.user):
            await interaction.response.send_message(
                "Восстанавливать места может только руководство.", ephemeral=True
            )
            return
        place = await self.bot.db.get_place(place_id)
        if not place or place["guild_id"] != interaction.guild.id or not place["is_deleted"]:
            await interaction.response.send_message(
                "Удалённое место с таким номером не найдено.", ephemeral=True
            )
            return
        await self.bot.db.restore_place(place_id)
        await send_audit(
            self.bot,
            interaction.guild,
            interaction.user,
            "place_restore",
            "place",
            place_id,
            f"Место «{place['name']}» восстановлено.",
        )
        await interaction.response.send_message("Место восстановлено.", ephemeral=True)


async def setup(bot: Any) -> None:
    await bot.add_cog(PlacesCog(bot))
