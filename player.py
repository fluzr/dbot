import asyncio
import collections
import logging
import random
import time
from typing import Optional, List, Dict, Any
import discord
from music_source import Song, format_duration
import config

try:
    import wavelink
    WAVELINK_AVAILABLE = True
except ImportError:
    wavelink = None
    WAVELINK_AVAILABLE = False

logger = logging.getLogger("mbot.player")
ui_logger = logging.getLogger("mbot.ui")

MAX_CONSECUTIVE_ERRORS = 3


def is_lavalink_connected() -> bool:
    """Sprawdza czy węzeł Lavalink jest dostępny i połączony."""
    if not WAVELINK_AVAILABLE or not getattr(config, "LAVALINK_ENABLED", True):
        return False
    try:
        node = wavelink.Pool.get_node()
        return node is not None and node.status == wavelink.NodeStatus.CONNECTED
    except Exception:
        return False


# =====================================================================
# Wavelink Player Subclass (Lavalink v4)
# =====================================================================
if WAVELINK_AVAILABLE:
    class MusicPlayer(wavelink.Player):
        """Wavelink Player dla Lavalink v4 z obsługą kanału tekstowego i automatycznego czyszczenia."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.home_channel: Optional[discord.TextChannel] = None
            self.now_playing_message: Optional[discord.Message] = None
            self.autoplay = wavelink.AutoPlayMode.partial
            self.last_active_time = time.time()
else:
    class MusicPlayer:
        pass


def create_lavalink_now_playing_embed(player: Any, track: Any) -> discord.Embed:
    """Tworzy embed 'Aktualnie gramy' dla utworu z Lavalink."""
    embed = discord.Embed(
        title="🎶 Aktualnie gramy (Lavalink ⚡)",
        description=f"**[{track.title}]({track.uri})**",
        color=discord.Color.brand_green() if getattr(track, "source", "") == "spotify" else discord.Color.red(),
    )

    source_icons = {
        "spotify": "🟢 Spotify",
        "youtube": "🔴 YouTube",
        "soundcloud": "🟠 SoundCloud",
        "http": "🌐 Strumień bezpośredni",
    }
    source_name = getattr(track, "source", "audio")
    source_label = source_icons.get(source_name, f"🎵 {source_name.capitalize()}")

    dur_sec = (track.length // 1000) if getattr(track, "length", 0) > 0 else 0
    embed.add_field(name="Autor / Kanał", value=track.author or "Nieznany", inline=True)
    embed.add_field(name="Czas trwania", value=format_duration(dur_sec), inline=True)
    embed.add_field(name="Źródło", value=source_label, inline=True)

    requester = getattr(track.extras, "requester", None)
    if requester:
        embed.add_field(name="Zaproponowane przez", value=requester, inline=True)

    embed.add_field(name="Głośność", value=f"{player.volume}%", inline=True)

    mode_map = {
        wavelink.QueueMode.normal: "Wyłączona",
        wavelink.QueueMode.loop: "🔂 Utwór",
        wavelink.QueueMode.loop_all: "🔁 Kolejka",
    } if WAVELINK_AVAILABLE else {}
    loop_str = mode_map.get(player.queue.mode, "Wyłączona") if WAVELINK_AVAILABLE else "Wyłączona"
    embed.add_field(name="Pętla", value=loop_str, inline=True)

    if getattr(track, "artwork", None):
        embed.set_thumbnail(url=track.artwork)

    rem = len(player.queue) if hasattr(player, "queue") else 0
    embed.set_footer(text=f"W kolejce pozostało: {rem} utworów | Silnik: Lavalink v4")
    return embed


def create_lavalink_queue_embed(player: Any, page: int = 1) -> discord.Embed:
    """Tworzy embed z kolejką utworów dla Lavalink."""
    items_per_page = 10
    queue_list = list(player.queue) if hasattr(player, "queue") else []
    total_items = len(queue_list)
    total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)
    page = max(1, min(page, total_pages))

    embed = discord.Embed(
        title="📋 Kolejka utworów (Lavalink)",
        color=discord.Color.blurple(),
    )

    if player.current:
        cur_dur = format_duration(player.current.length // 1000) if player.current.length else "00:00"
        req = getattr(player.current.extras, "requester", "Nieznany")
        embed.add_field(
            name="▶️ Aktualnie odtwarzane",
            value=f"**[{player.current.title}]({player.current.uri})** `[{cur_dur}]`\nDodane przez: {req}",
            inline=False,
        )
    else:
        embed.add_field(name="▶️ Aktualnie odtwarzane", value="*Nic aktualnie nie gra*", inline=False)

    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    queue_slice = queue_list[start_idx:end_idx]

    if queue_slice:
        lines = []
        for i, track in enumerate(queue_slice, start=start_idx + 1):
            dur = format_duration(track.length // 1000) if track.length else "00:00"
            req = getattr(track.extras, "requester", "")
            req_str = f" ({req})" if req else ""
            lines.append(f"`{i:2d}.` **[{track.title}]({track.uri})** `[{dur}]`{req_str}")
        embed.add_field(
            name=f"Nadchodzące utwory (Strona {page}/{total_pages})",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="Nadchodzące utwory",
            value="*Kolejka jest pusta. Dodaj utwór za pomocą `/play`!*",
            inline=False,
        )

    total_ms = sum(getattr(t, "length", 0) or 0 for t in queue_list)
    embed.set_footer(
        text=f"Łącznie w kolejce: {total_items} utworów | Łączny czas: {format_duration(total_ms // 1000)} | Głośność: {player.volume}%"
    )
    return embed


# =====================================================================
# Unified Discord UI Buttons (Działa zarówno dla Lavalink jak i Local)
# =====================================================================
class MusicControlView(discord.ui.View):
    """Interaktywne przyciski sterowania odtwarzaniem — automatycznie wykrywa silnik (Lavalink lub lokalny)."""

    def __init__(self, local_player: Optional["GuildMusicPlayer"] = None):
        super().__init__(timeout=None)
        self.local_player = local_player

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music_toggle")
    async def toggle_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            ui_logger.warning(f"🔘 [UI: {interaction.user}] Kliknął ⏯️ Play/Pause, ale bot nie jest połączony.")
            return await interaction.response.send_message("Bot nie jest połączony z kanałem.", ephemeral=True)

        if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
            if vc.paused:
                await vc.pause(False)
                ui_logger.info(f"🔘 [UI: {interaction.user}] Lavalink ⏯️ -> Wznowiono.")
                await interaction.response.send_message("▶️ Wznowiono odtwarzanie (Lavalink).", ephemeral=True)
            elif vc.playing:
                await vc.pause(True)
                ui_logger.info(f"🔘 [UI: {interaction.user}] Lavalink ⏯️ -> Wstrzymano.")
                await interaction.response.send_message("⏸️ Wstrzymano odtwarzanie (Lavalink).", ephemeral=True)
            else:
                await interaction.response.send_message("Nic aktualnie nie gra.", ephemeral=True)
            return

        # Fallback lokalny
        if self.local_player and self.local_player.voice_client:
            if self.local_player.voice_client.is_playing():
                self.local_player.voice_client.pause()
                ui_logger.info(f"🔘 [UI: {interaction.user}] Local ⏯️ -> Wstrzymano.")
                await interaction.response.send_message("⏸️ Wstrzymano odtwarzanie.", ephemeral=True)
            elif self.local_player.voice_client.is_paused():
                self.local_player.voice_client.resume()
                ui_logger.info(f"🔘 [UI: {interaction.user}] Local ⏯️ -> Wznowiono.")
                await interaction.response.send_message("▶️ Wznowiono odtwarzanie.", ephemeral=True)
            else:
                await interaction.response.send_message("Nic aktualnie nie gra.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.primary, custom_id="music_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            return await interaction.response.send_message("Bot nie jest połączony z kanałem.", ephemeral=True)

        if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
            if not vc.current and vc.queue.is_empty:
                return await interaction.response.send_message("Kolejka jest pusta.", ephemeral=True)
            cur = vc.current.title if vc.current else "utwór"
            ui_logger.info(f"🔘 [UI: {interaction.user}] Lavalink ⏭️ Skip -> '{cur}'")
            await vc.skip(force=True)
            return await interaction.response.send_message("⏭️ Przewinięto utwór (Lavalink)!", ephemeral=True)

        if self.local_player:
            if not self.local_player.current_song:
                return await interaction.response.send_message("Kolejka jest pusta.", ephemeral=True)
            self.local_player.skip()
            await interaction.response.send_message("⏭️ Przewinięto utwór!", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music_loop")
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            return await interaction.response.send_message("Bot nie jest połączony z kanałem.", ephemeral=True)

        if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
            if vc.queue.mode is wavelink.QueueMode.normal:
                vc.queue.mode = wavelink.QueueMode.loop
                desc = "Powtarzanie bieżącego utworu 🔂"
            elif vc.queue.mode is wavelink.QueueMode.loop:
                vc.queue.mode = wavelink.QueueMode.loop_all
                desc = "Powtarzanie całej kolejki 🔁"
            else:
                vc.queue.mode = wavelink.QueueMode.normal
                desc = "Wyłączone"
            ui_logger.info(f"🔘 [UI: {interaction.user}] Lavalink 🔁 Loop -> {vc.queue.mode}")
            return await interaction.response.send_message(f"Tryb pętli: **{desc}**", ephemeral=True)

        if self.local_player:
            modes = ["off", "song", "queue"]
            next_idx = (modes.index(self.local_player.loop_mode) + 1) % len(modes)
            self.local_player.loop_mode = modes[next_idx]
            descriptions = {"off": "Wyłączone", "song": "Powtarzanie utworu 🔂", "queue": "Powtarzanie całej kolejki 🔁"}
            ui_logger.info(f"🔘 [UI: {interaction.user}] Local 🔁 Loop -> {self.local_player.loop_mode}")
            await interaction.response.send_message(f"Tryb pętli: **{descriptions[self.local_player.loop_mode]}**", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            return await interaction.response.send_message("Bot nie jest połączony z kanałem.", ephemeral=True)

        if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
            vc.queue.clear()
            await vc.stop()
            ui_logger.info(f"🔘 [UI: {interaction.user}] Lavalink ⏹️ Stop.")
            return await interaction.response.send_message("⏹️ Zatrzymano i wyczyszczono kolejkę (Lavalink).", ephemeral=True)

        if self.local_player:
            self.local_player.stop()
            ui_logger.info(f"🔘 [UI: {interaction.user}] Local ⏹️ Stop.")
            await interaction.response.send_message("⏹️ Zatrzymano i wyczyszczono kolejkę.", ephemeral=True)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="music_queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            return await interaction.response.send_message("Bot nie jest połączony z kanałem.", ephemeral=True)

        if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
            embed = create_lavalink_queue_embed(vc, page=1)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if self.local_player:
            embed = self.local_player.create_queue_embed(page=1)
            await interaction.response.send_message(embed=embed, ephemeral=True)


# =====================================================================
# Local Fallback Player (yt-dlp + FFmpeg)
# =====================================================================
class GuildMusicPlayer:
    """Lokalny odtwarzacz audio używający yt-dlp + FFmpeg jako niezawodny fallback."""

    def __init__(self, bot: discord.Client, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: collections.deque[Song] = collections.deque()
        self.current_song: Optional[Song] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.volume: float = config.DEFAULT_VOLUME / 100.0
        self.loop_mode: str = "off"  # "off", "song", "queue"
        self.text_channel: Optional[discord.TextChannel] = None

        self.play_next_song_event = asyncio.Event()
        self.player_task: Optional[asyncio.Task] = None
        self.inactivity_task: Optional[asyncio.Task] = None
        self.last_active_time = time.time()
        self._skip_flag = False
        self._consecutive_errors = 0

    def start(self):
        """Uruchamia pętle odtwarzacza i bezczynności."""
        if not self.player_task or self.player_task.done():
            logger.info(f"[G:{self.guild_id}] Uruchamianie lokalnej pętli audio_loop.")
            self.player_task = asyncio.create_task(self.audio_loop())
        if not self.inactivity_task or self.inactivity_task.done():
            logger.info(f"[G:{self.guild_id}] Uruchamianie lokalnej pętli inactivity_loop.")
            self.inactivity_task = asyncio.create_task(self.inactivity_loop())

    def reset_inactivity(self):
        self.last_active_time = time.time()

    async def inactivity_loop(self):
        """Rozłącza bota w przypadku bezczynności lub pustego kanału."""
        empty_logged = False
        while True:
            await asyncio.sleep(15)
            if not self.voice_client or not self.voice_client.is_connected():
                empty_logged = False
                continue

            channel = self.voice_client.channel
            non_bot_members = [m for m in channel.members if not m.bot]
            if len(non_bot_members) == 0:
                if not empty_logged:
                    logger.info(f"[G:{self.guild_id}] 👥 Kanał '{channel.name}' opuszczony przez wszystkich. Start odliczania 60s do rozłączenia.")
                    empty_logged = True
                if time.time() - self.last_active_time > 60:
                    logger.info(f"[G:{self.guild_id}] 🚪 Rozłączanie z powodu pustego kanału głosowego.")
                    if self.text_channel:
                        try:
                            await self.text_channel.send("👋 Wszyscy opuścili kanał głosowy, rozłączam się.")
                        except Exception:
                            pass
                    await self.disconnect()
            elif not self.voice_client.is_playing() and not self.voice_client.is_paused() and len(self.queue) == 0:
                empty_logged = False
                idle_duration = time.time() - self.last_active_time
                if idle_duration > config.AUTO_LEAVE_SECONDS:
                    logger.info(f"[G:{self.guild_id}] 💤 Rozłączanie z powodu braku aktywności przez {int(idle_duration)}s.")
                    if self.text_channel:
                        try:
                            await self.text_channel.send(
                                f"💤 Brak aktywności przez {config.AUTO_LEAVE_SECONDS // 60} minut, rozłączam się."
                            )
                        except Exception:
                            pass
                    await self.disconnect()
            else:
                empty_logged = False
                self.reset_inactivity()

    async def audio_loop(self):
        """Główna pętla lokalnego odtwarzacza."""
        await self.bot.wait_until_ready()
        logger.info(f"[G:{self.guild_id}] 🚀 Lokalna pętla odtwarzacza wystartowała.")
        while True:
            try:
                self.play_next_song_event.clear()
                self._skip_flag = False

                if not self.voice_client or not self.voice_client.is_connected():
                    await asyncio.sleep(1)
                    continue

                if self.loop_mode == "song" and self.current_song:
                    song = self.current_song
                else:
                    if len(self.queue) > 0:
                        song = self.queue.popleft()
                        if self.loop_mode == "queue" and self.current_song:
                            self.queue.append(self.current_song)
                        self.current_song = song
                    else:
                        self.current_song = None
                        await asyncio.sleep(1)
                        continue

                self.reset_inactivity()
                logger.info(
                    f"[G:{self.guild_id}] 🎵 Przygotowywanie utworu (yt-dlp): '{song.title}' [{song.formatted_duration}] "
                    f"| Pozostało w kolejce: {len(self.queue)}"
                )

                song.invalidate_stream()

                try:
                    await song.resolve_stream()
                except Exception as e:
                    self._consecutive_errors += 1
                    logger.error(
                        f"[G:{self.guild_id}] ❌ Błąd pobierania strumienia audio dla '{song.title}': {e} "
                        f"(błędy z rzędu: {self._consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})",
                        exc_info=True,
                    )
                    if self.text_channel:
                        try:
                            await self.text_channel.send(
                                f"⚠️ Nie udało się załadować utworu **{song.title}**: `{e}`. Przechodzę do kolejnego..."
                            )
                        except Exception:
                            pass
                    self.current_song = None
                    if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        logger.error(f"[G:{self.guild_id}] 🛑 Zbyt wiele błędów z rzędu — wstrzymuję odtwarzanie.")
                        if self.text_channel:
                            try:
                                await self.text_channel.send(
                                    f"🛑 Zbyt wiele błędów z rzędu ({MAX_CONSECUTIVE_ERRORS}). Odtwarzanie wstrzymane."
                                )
                            except Exception:
                                pass
                        self._consecutive_errors = 0
                        self.queue.clear()
                    continue

                try:
                    audio_source = song.create_audio_source(volume=self.volume)
                except Exception as e:
                    self._consecutive_errors += 1
                    logger.error(f"[G:{self.guild_id}] ❌ Błąd inicjalizacji FFmpeg dla '{song.title}': {e}", exc_info=True)
                    if self.text_channel:
                        try:
                            await self.text_channel.send(f"⚠️ Błąd strumienia FFmpeg: `{e}`")
                        except Exception:
                            pass
                    self.current_song = None
                    continue

                def after_playback(error):
                    try:
                        if error:
                            logger.error(f"[G:{self.guild_id}] ⚠️ Błąd FFmpeg w trakcie odtwarzania '{song.title}': {error}")
                        else:
                            logger.info(f"[G:{self.guild_id}] 🏁 Zakończono odtwarzanie utworu: '{song.title}'")
                    except Exception as ex:
                        logger.warning(f"Błąd w callbacku after_playback: {ex}")
                    finally:
                        if not self.bot.is_closed():
                            try:
                                self.bot.loop.call_soon_threadsafe(self.play_next_song_event.set)
                            except Exception:
                                pass

                try:
                    if self.voice_client.is_playing() or self.voice_client.is_paused():
                        self.voice_client.stop()
                    channel_name = self.voice_client.channel.name if self.voice_client.channel else 'nieznany'
                    logger.info(f"[G:{self.guild_id}] ▶️ Rozpoczęto odtwarzanie utworu: '{song.title}' na kanale '{channel_name}'")
                    self.voice_client.play(audio_source, after=after_playback)
                    self._consecutive_errors = 0
                except Exception as e:
                    self._consecutive_errors += 1
                    logger.error(f"[G:{self.guild_id}] ❌ Błąd podczas voice_client.play() dla '{song.title}': {e}", exc_info=True)
                    if self.text_channel:
                        try:
                            await self.text_channel.send(f"⚠️ Błąd uruchomienia odtwarzacza: `{e}`")
                        except Exception:
                            pass
                    self.current_song = None
                    continue

                if self.text_channel:
                    embed = self.create_now_playing_embed(song)
                    view = MusicControlView(local_player=self)
                    try:
                        await self.text_channel.send(embed=embed, view=view)
                    except Exception:
                        pass

                await asyncio.sleep(0.5)

                while not self.play_next_song_event.is_set():
                    if not self.voice_client or not self.voice_client.is_connected():
                        logger.warning(f"[G:{self.guild_id}] ⚠️ Rozłączono z kanałem głosowym w trakcie odtwarzania.")
                        break
                    if not self.voice_client.is_playing() and not self.voice_client.is_paused():
                        logger.info(f"[G:{self.guild_id}] ⏹️ Wykryto zakończenie utworu.")
                        break
                    try:
                        await asyncio.wait_for(self.play_next_song_event.wait(), timeout=1.5)
                    except asyncio.TimeoutError:
                        pass

            except Exception as e:
                logger.error(f"[G:{self.guild_id}] Krytyczny wyjątek w pętli audio: {e}", exc_info=True)
                await asyncio.sleep(2)

    def skip(self, count: int = 1) -> int:
        if not self.voice_client:
            return 0
        skipped_count = 1
        for _ in range(count - 1):
            if len(self.queue) > 0:
                self.queue.popleft()
                skipped_count += 1
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()
        self.play_next_song_event.set()
        return skipped_count

    def stop(self):
        self.queue.clear()
        self.current_song = None
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        self.play_next_song_event.set()

    async def disconnect(self):
        self.stop()
        if self.voice_client:
            try:
                if self.voice_client.is_connected():
                    await self.voice_client.disconnect(force=True)
            except Exception as e:
                logger.warning(f"[G:{self.guild_id}] Błąd podczas disconnect(): {e}")
            finally:
                self.voice_client = None

    def set_volume(self, volume_percent: int):
        self.volume = max(0, min(100, volume_percent)) / 100.0
        if self.voice_client and self.voice_client.source:
            if isinstance(self.voice_client.source, discord.PCMVolumeTransformer):
                self.voice_client.source.volume = self.volume

    def shuffle(self):
        items = list(self.queue)
        random.shuffle(items)
        self.queue = collections.deque(items)

    def remove(self, index: int) -> Optional[Song]:
        if 1 <= index <= len(self.queue):
            items = list(self.queue)
            removed = items.pop(index - 1)
            self.queue = collections.deque(items)
            return removed
        return None

    def create_now_playing_embed(self, song: Song) -> discord.Embed:
        embed = discord.Embed(
            title="🎶 Aktualnie gramy (Lokalny yt-dlp 🛡️)",
            description=f"**[{song.title}]({song.web_url})**",
            color=discord.Color.brand_green() if song.source_type == "spotify" else discord.Color.red(),
        )
        source_icons = {"spotify": "🟢 Spotify", "youtube": "🔴 YouTube", "soundcloud": "🟠 SoundCloud"}
        source_label = source_icons.get(song.source_type, "🎵 Dźwięk")
        embed.add_field(name="Autor / Kanał", value=song.uploader or "Nieznany", inline=True)
        embed.add_field(name="Czas trwania", value=song.formatted_duration, inline=True)
        embed.add_field(name="Źródło", value=source_label, inline=True)
        if song.requester:
            embed.add_field(name="Zaproponowane przez", value=song.requester.mention, inline=True)
        embed.add_field(name="Głośność", value=f"{int(self.volume * 100)}%", inline=True)
        loop_display = {"off": "Wyłączona", "song": "🔂 Utwór", "queue": "🔁 Kolejka"}[self.loop_mode]
        embed.add_field(name="Pętla", value=loop_display, inline=True)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.set_footer(text=f"W kolejce pozostało: {len(self.queue)} utworów | Silnik: yt-dlp fallback")
        return embed

    def create_queue_embed(self, page: int = 1) -> discord.Embed:
        items_per_page = 10
        total_items = len(self.queue)
        total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)
        page = max(1, min(page, total_pages))

        embed = discord.Embed(
            title="📋 Kolejka utworów (Lokalny yt-dlp)",
            color=discord.Color.blurple(),
        )
        if self.current_song:
            embed.add_field(
                name="▶️ Aktualnie odtwarzane",
                value=f"**[{self.current_song.title}]({self.current_song.web_url})** `[{self.current_song.formatted_duration}]`\nDodane przez: {self.current_song.requester.mention if self.current_song.requester else 'Nieznany'}",
                inline=False,
            )
        else:
            embed.add_field(name="▶️ Aktualnie odtwarzane", value="*Nic aktualnie nie gra*", inline=False)

        start_idx = (page - 1) * items_per_page
        end_idx = start_idx + items_per_page
        queue_slice = list(self.queue)[start_idx:end_idx]

        if queue_slice:
            lines = []
            for i, song in enumerate(queue_slice, start=start_idx + 1):
                req = f" ({song.requester.display_name})" if song.requester else ""
                lines.append(f"`{i:2d}.` **[{song.title}]({song.web_url})** `[{song.formatted_duration}]`{req}")
            embed.add_field(name=f"Nadchodzące utwory (Strona {page}/{total_pages})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Nadchodzące utwory", value="*Kolejka jest pusta. Dodaj utwór za pomocą `/play`!*", inline=False)

        total_seconds = sum(s.duration for s in self.queue)
        embed.set_footer(
            text=f"Łącznie w kolejce: {total_items} utworów | Łączny czas: {format_duration(total_seconds)} | Głośność: {int(self.volume * 100)}%"
        )
        return embed
