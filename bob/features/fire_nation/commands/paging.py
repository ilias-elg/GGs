"""Paged embeds with Previous / Next buttons, for long lists."""

import discord

class PagedView(discord.ui.View):
    """Previous/Next buttons for a paged embed. Only the person who ran the command can page."""

    def __init__(self, interaction: discord.Interaction, total_pages: int, build_embed) -> None:
        super().__init__(timeout=5 * 60)
        self.interaction = interaction
        self.total_pages = total_pages
        self.build_embed = build_embed
        self.page = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.interaction.user.id:
            return True
        await interaction.response.send_message(
            "Only the user who ran this command can control these pages. Run it yourself to browse.",
            ephemeral=True,
        )
        return False

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(self.page, self.total_pages), view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._show(interaction)

    async def on_timeout(self) -> None:
        try:
            await self.interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass


async def send_paged(interaction: discord.Interaction, row_count: int, page_size: int, build_embed) -> None:
    total_pages = max(1, -(-row_count // page_size))
    if total_pages <= 1:
        await interaction.edit_original_response(embed=build_embed(0, 1))
        return
    view = PagedView(interaction, total_pages, build_embed)
    await interaction.edit_original_response(embed=build_embed(0, total_pages), view=view)
