"""Lavalink/Wavelink music commands for ShieldCore Security."""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections import defaultdict
from contextlib import suppress
from pathlib import Path
from typing import Any

import discord
import wavelink
from discord.ext import commands, tasks
from PIL import Image, ImageOps

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
MUSIC_CONFIG_FILE = os.path.join(BOT_DIR, "music_config.json")
MUSIC_ICON_DIR = os.path.join(BOT_DIR, "icons")
BUTTON_ICON_FILES = {
    "music": ("sc_music_note", "music-note-red.png", "🎵"),
    "previous": ("sc_music_previous", "previous-button-icon.png", "⏮️"),
    "play": ("sc_music_play", "play-button-icon.png", "▶️"),
    "pause": ("sc_music_pause", "pause-button-icon.png", "⏸️"),
    "next": ("sc_music_next", "next-button-icon.png", "⏭️"),
    "autoplay": ("sc_music_autoplay", "autoplay-button-icon.png", "🔁"),
    "volume": ("sc_music_volume", "volume-button-icon.png", "🔊"),
    "loop": ("sc_music_loop", "loop-button-icon.png", "🔂"),
}


def _load_node_config() -> dict[str, str]:
    """Load a Lavalink node from the local JSON config, with .env overrides."""
    config: dict[str, Any] = {}
    try:
        with open(MUSIC_CONFIG_FILE, "r", encoding="utf-8") as config_file:
            raw_config = json.load(config_file)
        if isinstance(raw_config, dict):
            candidate = raw_config.get("lavalink", raw_config)
            if isinstance(candidate, dict):
                config = candidate
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not load music_config.json: {error}")

    uri = os.getenv("LAVALINK_URI", str(config.get("uri", ""))).strip()
    password = os.getenv("LAVALINK_PASSWORD", str(config.get("password", ""))).strip()
    identifier = os.getenv(
        "LAVALINK_IDENTIFIER", str(config.get("identifier", "ShieldCore-Node"))
    ).strip()
    return {"uri": uri, "password": password, "identifier": identifier}


class ShieldCoreMusic(commands.Cog):
    """A per-server music player backed by a Lavalink v4 node."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.player_messages: dict[int, discord.Message] = {}
        self.player_contexts: dict[int, commands.Context] = {}
        self.track_history: dict[int, list[wavelink.Playable]] = defaultdict(list)
        self.history_cursor: dict[int, int] = {}
        self.history_navigation: set[int] = set()
        self.card_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.guild_button_emojis: dict[int, dict[str, discord.PartialEmoji]] = {}
        self.emoji_setup_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        self.refresh_active_cards.start()

    async def cog_unload(self) -> None:
        self.refresh_active_cards.cancel()

    @tasks.loop(seconds=15)
    async def refresh_active_cards(self) -> None:
        for voice_client in list(self.bot.voice_clients):
            if isinstance(voice_client, wavelink.Player) and voice_client.current:
                await self.refresh_player_card(voice_client.guild.id)

    @refresh_active_cards.before_loop
    async def before_refresh_active_cards(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def _embed(title: str, description: str, color: discord.Color) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=color)

    async def _player_for(self, ctx: commands.Context) -> wavelink.Player | None:
        player = ctx.voice_client
        return player if isinstance(player, wavelink.Player) else None

    async def _check_same_voice(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            await ctx.send("Music commands can only be used in a server.")
            return False
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("Join a voice channel first.")
            return False
        player = await self._player_for(ctx)
        if player and player.channel and player.channel.id != ctx.author.voice.channel.id:
            await ctx.send("You need to be in the same voice channel as ShieldCore.")
            return False
        return True

    @staticmethod
    def _track_title(track: wavelink.Playable) -> str:
        title = discord.utils.escape_markdown(track.title or "Unknown track")
        if len(title) > 56:
            title = title[:53].rstrip() + "…"
        uri = getattr(track, "uri", None)
        return f"[{title}]({uri})" if uri else title

    @staticmethod
    def _prepare_button_emoji(image_path: str) -> bytes:
        """Crop a red neon glyph from a supplied button mockup for guild emoji upload."""
        with Image.open(image_path) as source:
            image = source.convert("RGB")
        width, height = image.size
        red_pixels = []
        for y in range(24, height - 24):
            for x in range(24, int(width * 0.43)):
                red, green, blue = image.getpixel((x, y))
                if red > 70 and red > green * 1.6 and red > blue * 1.35 and green < 110:
                    red_pixels.append((x, y))
        if not red_pixels:
            raise ValueError(f"No neon-red icon could be located in {os.path.basename(image_path)}")

        left = min(x for x, _ in red_pixels)
        top = min(y for _, y in red_pixels)
        right = max(x for x, _ in red_pixels) + 1
        bottom = max(y for _, y in red_pixels) + 1
        padding = 12
        side = max(right - left, bottom - top) + padding * 2
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2
        crop_left = round(center_x - side / 2)
        crop_top = round(center_y - side / 2)
        square = image.crop((crop_left, crop_top, crop_left + side, crop_top + side))

        rgba = Image.new("RGBA", square.size)
        colored_pixels = []
        for red, green, blue in square.getdata():
            alpha = max(0, red - max(green, blue))
            colored_pixels.append((255, 0, 25, alpha))
        rgba.putdata(colored_pixels)
        rgba = ImageOps.fit(rgba, (128, 128), method=Image.Resampling.LANCZOS)
        output = io.BytesIO()
        rgba.save(output, format="PNG", optimize=True)
        return output.getvalue()

    async def ensure_guild_button_emojis(
        self,
        guild: discord.Guild,
    ) -> dict[str, discord.PartialEmoji]:
        """Create/reuse custom neon glyph emojis from the supplied artwork."""
        if guild.id in self.guild_button_emojis:
            return self.guild_button_emojis[guild.id]

        async with self.emoji_setup_locks[guild.id]:
            if guild.id in self.guild_button_emojis:
                return self.guild_button_emojis[guild.id]

            member = guild.me
            permissions = member.guild_permissions if member else None
            can_create = bool(
                permissions
                and (
                    permissions.administrator
                    or getattr(permissions, "create_expressions", False)
                    or getattr(permissions, "manage_emojis_and_stickers", False)
                )
            )
            resolved: dict[str, discord.PartialEmoji] = {}
            for key, (emoji_name, filename, _) in BUTTON_ICON_FILES.items():
                existing = discord.utils.get(guild.emojis, name=emoji_name)
                if existing:
                    resolved[key] = discord.PartialEmoji(name=existing.name, id=existing.id)
                    continue
                if not can_create:
                    continue
                image_path = os.path.join(MUSIC_ICON_DIR, filename)
                try:
                    if key == "music":
                        image_data = await asyncio.to_thread(Path(image_path).read_bytes)
                    else:
                        image_data = await asyncio.to_thread(self._prepare_button_emoji, image_path)
                    emoji = await guild.create_custom_emoji(
                        name=emoji_name,
                        image=image_data,
                        reason="ShieldCore music player neon button icons",
                    )
                    resolved[key] = discord.PartialEmoji(name=emoji.name, id=emoji.id)
                except (discord.HTTPException, OSError, ValueError) as error:
                    print(f"Could not create music button emoji {emoji_name} in {guild.id}: {error}")

            self.guild_button_emojis[guild.id] = resolved
            if len(resolved) == len(BUTTON_ICON_FILES):
                print(f"Loaded all {len(resolved)} red neon music button emojis for {guild.name}.")
            elif not can_create:
                print(f"Music icons unavailable in {guild.name}: grant Create Expressions permission to the bot.")
            else:
                print(f"Loaded {len(resolved)}/{len(BUTTON_ICON_FILES)} music button emojis for {guild.name}.")
            return resolved

    @staticmethod
    def _same_track(first: wavelink.Playable, second: wavelink.Playable) -> bool:
        return (
            getattr(first, "identifier", None) == getattr(second, "identifier", None)
            and first.title == second.title
        )

    def _player_for_guild(self, guild_id: int | None) -> wavelink.Player | None:
        guild = self.bot.get_guild(guild_id) if guild_id else None
        player = guild.voice_client if guild else None
        return player if isinstance(player, wavelink.Player) else None

    def _record_track_start(self, guild_id: int, track: wavelink.Playable) -> None:
        history = self.track_history[guild_id]
        if guild_id in self.history_navigation:
            self.history_navigation.discard(guild_id)
            return
        cursor = self.history_cursor.get(guild_id, -1)
        if cursor >= 0 and self._same_track(history[cursor], track):
            return
        del history[cursor + 1:]
        history.append(track)
        if len(history) > 50:
            history.pop(0)
        self.history_cursor[guild_id] = len(history) - 1

    def _card_title(self, guild_id: int) -> str:
        music_emoji = self.guild_button_emojis.get(guild_id, {}).get("music")
        return f"{music_emoji or '🎵'}  SHIELDCORE MUSIC"

    def _player_embed(self, player: wavelink.Player) -> discord.Embed:
        track = player.current
        autoplay_on = player.autoplay == wavelink.AutoPlayMode.enabled
        if track:
            duration = "LIVE" if track.is_stream else f"{track.length // 60000}:{(track.length // 1000) % 60:02d}"
            position = max(0, player.position)
            if not track.is_stream and track.length:
                progress = min(1.0, position / track.length)
                filled = round(progress * 18)
                bar = f"{'▬' * filled}🔘{'▬' * (18 - filled)}"
                progress_text = f"{position // 60000}:{(position // 1000) % 60:02d} {bar} {duration}"
            else:
                progress_text = f"🔴 LIVE · {duration}"
            description = (
                f"**{ShieldCoreMusic._track_title(track)}**\n"
                f"{discord.utils.escape_markdown((track.author or 'Unknown artist')[:40])}\n"
                f"`{progress_text}`"
            )
        else:
            description = "Nothing is playing · use `!sc.play <song>` to start the queue."

        embed = discord.Embed(
            title=self._card_title(player.guild.id),
            description=description,
            color=discord.Color.from_rgb(22, 26, 40),
        )
        if track and track.artwork:
            embed.set_thumbnail(url=track.artwork)
        queue_count = len(player.queue)
        loop_on = player.queue.mode == wavelink.QueueMode.loop
        embed.set_footer(
            text=(
                f"Queue: {queue_count}  ·  AutoPlay: {'ON' if autoplay_on else 'OFF'}  ·  "
                f"Loop: {'ON' if loop_on else 'OFF'}  ·  "
                "Developed by - Spyder X Scripts [ Aasim </> ]"
            )
        )
        return embed

    async def refresh_player_card(
        self,
        guild_id: int,
        ctx: commands.Context | None = None,
        *,
        fresh: bool = False,
    ) -> None:
        async with self.card_locks[guild_id]:
            await self._refresh_player_card(guild_id, ctx, fresh=fresh)

    async def _refresh_player_card(
        self,
        guild_id: int,
        ctx: commands.Context | None = None,
        *,
        fresh: bool = False,
    ) -> None:
        player = self._player_for_guild(guild_id)
        if not player:
            return
        if ctx:
            self.player_contexts[guild_id] = ctx
        if fresh and self.player_messages.get(guild_id):
            old_message = self.player_messages.pop(guild_id, None)
            if old_message:
                with suppress(discord.HTTPException):
                    await old_message.delete()
        view = MusicPlayerView(self, guild_id)
        embed = self._player_embed(player)
        message = self.player_messages.get(guild_id)
        context = self.player_contexts.get(guild_id)
        if context and (message is None or fresh):
            try:
                older_cards = []
                legacy_statuses = []
                async for candidate in context.channel.history(limit=50):
                    if candidate.author.id != self.bot.user.id:
                        continue
                    if any(
                        (existing.title or "").endswith("SHIELDCORE MUSIC")
                        for existing in candidate.embeds
                    ):
                        older_cards.append(candidate)
                    elif candidate.content.startswith(
                        ("Now playing [", "Added [", "Started playlist **", "Queued **")
                    ):
                        legacy_statuses.append(candidate)
                for legacy_message in legacy_statuses:
                    with suppress(discord.HTTPException):
                        await legacy_message.delete()
                if fresh:
                    for old_card in older_cards:
                        with suppress(discord.HTTPException):
                            await old_card.delete()
                elif older_cards:
                    message = older_cards[0]
                    self.player_messages[guild_id] = message
                    for duplicate in older_cards[1:]:
                        with suppress(discord.HTTPException):
                            await duplicate.delete()
            except discord.HTTPException:
                pass
        if message:
            try:
                await message.edit(embed=embed, view=view)
                return
            except discord.HTTPException:
                self.player_messages.pop(guild_id, None)
        if context:
            try:
                self.player_messages[guild_id] = await context.send(embed=embed, view=view)
            except discord.HTTPException as error:
                print(f"Could not send the music player card in guild {guild_id}: {error}")

    async def clear_player_card(self, guild_id: int) -> None:
        message = self.player_messages.get(guild_id)
        if not message:
            return
        try:
            await message.edit(
                embed=discord.Embed(
                    title=self._card_title(guild_id),
                    description="Playback stopped · add a song with `!sc.play <song>`. ",
                    color=discord.Color.from_rgb(22, 26, 40),
                ),
                view=None,
            )
        except discord.HTTPException:
            self.player_messages.pop(guild_id, None)


class VolumeModal(discord.ui.Modal, title="Set Music Volume"):
    volume_value = discord.ui.TextInput(
        label="Volume (0–150)",
        placeholder="Enter a number from 0 to 150",
        min_length=1,
        max_length=3,
        required=True,
    )

    def __init__(self, cog: ShieldCoreMusic, guild_id: int, current_volume: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.volume_value.default = str(current_volume)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        player = self.cog._player_for_guild(self.guild_id)
        if not player:
            await interaction.response.send_message("The music player is no longer connected.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice:
            await interaction.response.send_message("Join the bot's voice channel to change volume.", ephemeral=True)
            return
        if not player.channel or interaction.user.voice.channel.id != player.channel.id:
            await interaction.response.send_message("Join the same voice channel as the bot first.", ephemeral=True)
            return
        try:
            volume = int(self.volume_value.value.strip())
        except ValueError:
            await interaction.response.send_message("Enter a whole number from 0 to 150.", ephemeral=True)
            return
        if not 0 <= volume <= 150:
            await interaction.response.send_message("Volume must be between 0 and 150.", ephemeral=True)
            return
        await player.set_volume(volume)
        await self.cog.refresh_player_card(self.guild_id)
        await interaction.response.send_message(f"Volume set to **{volume}%**.", ephemeral=True)


class MusicPlayerView(discord.ui.View):
    """Compact one-row controls for the guild's active music player."""

    def __init__(self, cog: ShieldCoreMusic, guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        player = cog._player_for_guild(guild_id)
        autoplay_on = bool(player and player.autoplay == wavelink.AutoPlayMode.enabled)
        self.autoplay_button.label = f"AutoPlay: {'ON' if autoplay_on else 'OFF'}"
        self.autoplay_button.style = discord.ButtonStyle.secondary
        loop_on = bool(player and player.queue.mode == wavelink.QueueMode.loop)
        self.loop_button.label = f"Loop: {'ON' if loop_on else 'OFF'}"
        self.loop_button.style = discord.ButtonStyle.secondary
        emojis = cog.guild_button_emojis.get(guild_id, {})
        self.previous_button.emoji = emojis.get("previous", BUTTON_ICON_FILES["previous"][2])
        self.play_button.emoji = emojis.get("play", BUTTON_ICON_FILES["play"][2])
        self.pause_button.emoji = emojis.get("pause", BUTTON_ICON_FILES["pause"][2])
        self.next_button.emoji = emojis.get("next", BUTTON_ICON_FILES["next"][2])
        self.autoplay_button.emoji = emojis.get("autoplay", BUTTON_ICON_FILES["autoplay"][2])
        self.volume_button.emoji = emojis.get("volume", BUTTON_ICON_FILES["volume"][2])
        self.loop_button.emoji = emojis.get("loop", BUTTON_ICON_FILES["loop"][2])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        player = self.cog._player_for_guild(interaction.guild_id)
        if not player or not player.channel:
            await interaction.response.send_message("The music player is not connected.", ephemeral=True)
            return False
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice:
            await interaction.response.send_message("Join the bot's voice channel to use these controls.", ephemeral=True)
            return False
        if interaction.user.voice.channel.id != player.channel.id:
            await interaction.response.send_message("Join the same voice channel as the bot first.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="shieldcore:music:previous", row=0)
    async def previous_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        player = self.cog._player_for_guild(self.guild_id)
        history = self.cog.track_history[self.guild_id]
        cursor = self.cog.history_cursor.get(self.guild_id, -1)
        if not player or cursor <= 0:
            await interaction.response.send_message("There is no previous track in this session yet.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cursor -= 1
        self.cog.history_cursor[self.guild_id] = cursor
        self.cog.history_navigation.add(self.guild_id)
        await player.play(history[cursor])
        await self.cog.refresh_player_card(self.guild_id)
        await interaction.followup.send("Playing the previous track.", ephemeral=True)

    @discord.ui.button(label="Play", style=discord.ButtonStyle.secondary, custom_id="shieldcore:music:play", row=0)
    async def play_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        player = self.cog._player_for_guild(self.guild_id)
        if not player:
            await interaction.response.send_message("The music player is not connected.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if player.paused:
            await player.pause(False)
            message = "Playback resumed."
        elif player.playing:
            message = "A track is already playing."
        elif player.current:
            await player.play(player.current)
            message = "Replaying the current track."
        elif not player.queue.is_empty:
            await player.play(await player.queue.get_wait())
            message = "Started the next queued track."
        else:
            message = "The queue is empty. Add a track with `!sc.play <song>`."
        await self.cog.refresh_player_card(self.guild_id)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, custom_id="shieldcore:music:pause", row=0)
    async def pause_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        player = self.cog._player_for_guild(self.guild_id)
        if not player or not player.playing:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await player.pause(True)
        await self.cog.refresh_player_card(self.guild_id)
        await interaction.followup.send("Playback paused.", ephemeral=True)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="shieldcore:music:next", row=0)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        player = self.cog._player_for_guild(self.guild_id)
        if not player or not player.current:
            await interaction.response.send_message("There is no track to skip.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await player.skip(force=True)
        await self.cog.refresh_player_card(self.guild_id)
        await interaction.followup.send("Skipped the current track.", ephemeral=True)

    @discord.ui.button(label="AutoPlay: ON", style=discord.ButtonStyle.secondary, custom_id="shieldcore:music:autoplay", row=0)
    async def autoplay_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        player = self.cog._player_for_guild(self.guild_id)
        if not player:
            await interaction.response.send_message("The music player is not connected.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        enabled = player.autoplay != wavelink.AutoPlayMode.enabled
        player.autoplay = (
            wavelink.AutoPlayMode.enabled if enabled else wavelink.AutoPlayMode.disabled
        )
        await self.cog.refresh_player_card(self.guild_id)
        await interaction.followup.send(f"AutoPlay {'enabled' if enabled else 'disabled'}.", ephemeral=True)

    @discord.ui.button(label="Volume", style=discord.ButtonStyle.secondary, custom_id="shieldcore:music:volume", row=1)
    async def volume_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        player = self.cog._player_for_guild(self.guild_id)
        if not player:
            await interaction.response.send_message("The music player is not connected.", ephemeral=True)
            return
        await interaction.response.send_modal(
            VolumeModal(self.cog, self.guild_id, int(player.volume or 100))
        )

    @discord.ui.button(label="Loop: OFF", style=discord.ButtonStyle.secondary, custom_id="shieldcore:music:loop", row=1)
    async def loop_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        player = self.cog._player_for_guild(self.guild_id)
        if not player:
            await interaction.response.send_message("The music player is not connected.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        enabled = player.queue.mode == wavelink.QueueMode.normal
        player.queue.mode = (
            wavelink.QueueMode.loop if enabled else wavelink.QueueMode.normal
        )
        await self.cog.refresh_player_card(self.guild_id)
        await interaction.followup.send(f"Track loop {'enabled' if enabled else 'disabled'}.", ephemeral=True)

class ShieldCoreMusicCommands(ShieldCoreMusic):
    """Register message commands separately from the player-card view."""

    @commands.command(name="play", aliases=["p", "sc.play", "sc.p"])
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str):
        if not await self._check_same_voice(ctx):
            return
        if not wavelink.Pool.nodes:
            await ctx.send("Music is unavailable: no Lavalink node is connected. Configure music_config.json or the LAVALINK_* environment variables.")
            return

        voice_channel = ctx.author.voice.channel
        permissions = voice_channel.permissions_for(ctx.guild.me)
        if not permissions.connect or not permissions.speak:
            await ctx.send("I need Connect and Speak permissions in that voice channel.")
            return

        try:
            player = await self._player_for(ctx)
            if player is None:
                player = await voice_channel.connect(cls=wavelink.Player)
                await player.set_volume(100)
                player.queue.mode = wavelink.QueueMode.normal
                player.autoplay = wavelink.AutoPlayMode.enabled
            self.player_contexts[ctx.guild.id] = ctx
            results = await wavelink.Playable.search(query.strip())
            if not results:
                await ctx.send("No playable tracks were found for that search.")
                return
            await self.ensure_guild_button_emojis(ctx.guild)

            if isinstance(results, wavelink.Playlist):
                tracks = list(results.tracks)
                if not tracks:
                    await ctx.send("That playlist did not contain any playable tracks.")
                    return
                if player.playing or player.paused:
                    await player.queue.put_wait(tracks)
                else:
                    await player.play(tracks[0])
                    if len(tracks) > 1:
                        await player.queue.put_wait(tracks[1:])
                await self.refresh_player_card(ctx.guild.id, ctx)
                return

            track = results[0]
            if player.playing or player.paused:
                await player.queue.put_wait(track)
            else:
                await player.play(track)
            await self.refresh_player_card(ctx.guild.id, ctx)
        except (wavelink.LavalinkException, discord.HTTPException) as error:
            await ctx.send(f"Could not start playback: {error}")
        except Exception as error:
            print(f"Music play failed in guild {ctx.guild.id}: {error}")
            await ctx.send("Playback failed. Check the Lavalink connection and try again.")

    @commands.command(name="pause", aliases=["sc.pause"])
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        if not await self._check_same_voice(ctx):
            return
        player = await self._player_for(ctx)
        if not player or not player.playing:
            await ctx.send("Nothing is playing right now.")
            return
        await player.pause(True)
        await ctx.send("Playback paused.")
        await self.refresh_player_card(ctx.guild.id)

    @commands.command(name="resume", aliases=["sc.resume"])
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        if not await self._check_same_voice(ctx):
            return
        player = await self._player_for(ctx)
        if not player or not player.paused:
            await ctx.send("Playback is not paused.")
            return
        await player.pause(False)
        await ctx.send("Playback resumed.")
        await self.refresh_player_card(ctx.guild.id)

    @commands.command(name="skip", aliases=["s", "sc.skip", "sc.s"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        if not await self._check_same_voice(ctx):
            return
        player = await self._player_for(ctx)
        if not player or not player.current:
            await ctx.send("There is no track to skip.")
            return
        await player.skip(force=True)
        await ctx.send("Skipped the current track.")
        await self.refresh_player_card(ctx.guild.id)

    @commands.command(name="stop", aliases=["sc.stop"])
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        if not await self._check_same_voice(ctx):
            return
        player = await self._player_for(ctx)
        if not player:
            await ctx.send("I am not connected to a voice channel.")
            return
        player.queue.clear()
        await player.stop()
        await player.disconnect()
        await self.clear_player_card(ctx.guild.id)
        await ctx.send("Playback stopped, queue cleared, and voice channel left.")

    @commands.command(name="leave", aliases=["disconnect", "dc", "sc.leave", "sc.disconnect", "sc.dc"])
    @commands.guild_only()
    async def leave(self, ctx: commands.Context):
        if not await self._check_same_voice(ctx):
            return
        player = await self._player_for(ctx)
        if not player:
            await ctx.send("I am not connected to a voice channel.")
            return
        await player.disconnect()
        await self.clear_player_card(ctx.guild.id)
        await ctx.send("Left the voice channel.")

    @commands.command(name="queue", aliases=["q", "sc.queue", "sc.q"])
    @commands.guild_only()
    async def queue(self, ctx: commands.Context):
        player = await self._player_for(ctx)
        if not player or (not player.current and player.queue.is_empty):
            await ctx.send(embed=self._embed("Music Queue", "The queue is empty.", discord.Color.blurple()))
            return

        lines = []
        if player.current:
            lines.append(f"**Now playing:** {self._track_title(player.current)}")
        for index, track in enumerate(list(player.queue)[:10], start=1):
            lines.append(f"`{index}.` {self._track_title(track)}")
        remaining = max(0, len(player.queue) - 10)
        if remaining:
            lines.append(f"…and {remaining} more track(s).")
        await ctx.send(embed=self._embed("Music Queue", "\n".join(lines), discord.Color.blurple()))

    @commands.command(name="nowplaying", aliases=["np", "sc.nowplaying", "sc.np"])
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context):
        player = await self._player_for(ctx)
        if not player or not player.current:
            await ctx.send("Nothing is playing right now.")
            return
        track = player.current
        duration = "Live" if track.is_stream else f"{max(0, player.position) // 60000}:{(max(0, player.position) // 1000) % 60:02d} / {track.length // 60000}:{(track.length // 1000) % 60:02d}"
        embed = self._embed("Now Playing", f"{self._track_title(track)}\n**Artist:** {discord.utils.escape_markdown(track.author or 'Unknown')}\n**Progress:** `{duration}`", discord.Color.green())
        await ctx.send(embed=embed)

    @commands.command(name="volume", aliases=["vol", "sc.volume", "sc.vol"])
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, level: int):
        if not await self._check_same_voice(ctx):
            return
        player = await self._player_for(ctx)
        if not player:
            await ctx.send("I am not connected to a voice channel.")
            return
        if not 0 <= level <= 150:
            await ctx.send("Volume must be between 0 and 150.")
            return
        await player.set_volume(level)
        await ctx.send(f"Volume set to **{level}%**.")

    @commands.command(name="shuffle", aliases=["sc.shuffle"])
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context):
        if not await self._check_same_voice(ctx):
            return
        player = await self._player_for(ctx)
        if not player or player.queue.is_empty:
            await ctx.send("There are no queued tracks to shuffle.")
            return
        player.queue.shuffle()
        await ctx.send("The queue has been shuffled.")

    @commands.command(name="loop", aliases=["sc.loop"])
    @commands.guild_only()
    async def loop(self, ctx: commands.Context, mode: str = "track"):
        if not await self._check_same_voice(ctx):
            return
        player = await self._player_for(ctx)
        if not player:
            await ctx.send("I am not connected to a voice channel.")
            return
        modes = {
            "off": wavelink.QueueMode.normal,
            "track": wavelink.QueueMode.loop,
            "queue": wavelink.QueueMode.loop_all,
        }
        selected_mode = modes.get(mode.lower())
        if selected_mode is None:
            await ctx.send("Choose a loop mode: `track`, `queue`, or `off`.")
            return
        player.queue.mode = selected_mode
        await ctx.send(f"Loop mode set to **{mode.lower()}**.")
        await self.refresh_player_card(ctx.guild.id)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player = payload.player
        if not player:
            return
        await self.ensure_guild_button_emojis(player.guild)
        self._record_track_start(player.guild.id, payload.track)
        await self.refresh_player_card(player.guild.id, fresh=True)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player = payload.player
        if not player or str(payload.reason).lower() == "replaced":
            return
        if not player.queue.is_empty:
            try:
                next_track = await player.queue.get_wait()
                await player.play(next_track)
            except Exception as error:
                print(f"Could not advance the music queue: {error}")
        else:
            await self.refresh_player_card(player.guild.id)


async def setup_music(bot: commands.Bot) -> None:
    """Load music commands and connect the configured Lavalink node once."""
    if bot.get_cog("ShieldCoreMusicCommands") is None:
        await bot.add_cog(ShieldCoreMusicCommands(bot))
        print("Music commands loaded: !play / !sc.play and player controls are ready.")

    if getattr(bot, "_shieldcore_lavalink_started", False):
        return
    bot._shieldcore_lavalink_started = True

    config = _load_node_config()
    if not config["uri"] or not config["password"] or config["password"] == "CHANGE_ME":
        print("Music commands loaded, but Lavalink is not configured. Set LAVALINK_URI and LAVALINK_PASSWORD.")
        return

    try:
        node = wavelink.Node(
            identifier=config["identifier"],
            uri=config["uri"],
            password=config["password"],
        )
        await wavelink.Pool.connect(nodes=[node], client=bot)
        print(f"Connected to Lavalink node: {config['identifier']}")
    except Exception as error:
        print(f"Lavalink connection failed; music commands are loaded but unavailable: {error}")
