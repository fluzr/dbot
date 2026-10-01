import asyncio
import collections
import logging
import random
import time
from typing import Optional, List
import discord
from music_source import Song, format_duration
import config

logger = logging.getLogger("mbot.player")
ui_logger = logging.getLogger("mbot.ui")


class MusicControlView(discord.ui.View):
    """Interactive Discord buttons attached to the 'Now Playing' message."""

    def __init__(self, player: "GuildMusicPlayer"):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music_toggle")
    async def toggle_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.voice_client or not self.player.voice_client.is_connected():
            ui_logger.warning(f"🔘 [UI: {interaction.user}] Kliknął ⏯️ Play/Pause, ale bot nie jest połączony.")
            return await interaction.response.send_message("Bot nie jest połączony.", ephemeral=True)

        if self.player.voice_client.is_playing():
            self.player.voice_client.pause()
            ui_logger.info(f"🔘 [UI: {interaction.user}] Kliknął ⏯️ -> Wstrzymano odtwarzanie.")
            await interaction.response.send_message("⏸️ Wstrzymano odtwarzanie.", ephemeral=True)
        elif self.player.voice_client.is_paused():
            self.player.voice_client.resume()
            ui_logger.info(f"🔘 [UI: {interaction.user}] Kliknął ⏯️ -> Wznowiono odtwarzanie.")
            await interaction.response.send_message("▶️ Wznowiono odtwarzanie.", ephemeral=True)
        else:
            ui_logger.info(f"🔘 [UI: {interaction.user}] Kliknął ⏯️, ale nic aktualnie nie gra.")
            await interaction.response.send_message("Nic aktualnie nie gra.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.primary, custom_id="music_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.current_song:
            ui_logger.info(f"🔘 [UI: {interaction.user}] Kliknął ⏭️ Skip, ale kolejka jest pusta.")
            return await interaction.response.send_message("Kolejka jest pusta.", ephemeral=True)
        cur_title = self.player.current_song.title
        ui_logger.info(f"🔘 [UI: {interaction.user}] Kliknął ⏭️ Skip -> Pomijanie utworu: '{cur_title}'")
        self.player.skip()
        await interaction.response.send_message("⏭️ Przewinięto utwór!", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music_loop")
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        modes = ["off", "song", "queue"]
        next_idx = (modes.index(self.player.loop_mode) + 1) % len(modes)
        self.player.loop_mode = modes[next_idx]
        descriptions = {
            "off": "Wyłączone",
            "song": "Powtarzanie utworu 🔂",
            "queue": "Powtarzanie całej kolejki 🔁",
        }
        ui_logger.info(f"🔘 [UI: {interaction.user}] Kliknął 🔁 Loop -> Nowy tryb pętli: {self.player.loop_mode}")
        await interaction.response.send_message(
            f"Tryb pętli: **{descriptions[self.player.loop_mode]}**", ephemeral=True
        )

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        ui_logger.info(f"🔘 [UI: {interaction.user}] Kliknął ⏹️ Stop -> Zatrzymanie muzyki i czyszczenie kolejki.")
        self.player.stop()
        await interaction.response.send_message("⏹️ Zatrzymano i wyczyszczono kolejkę.", ephemeral=True)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="music_queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        ui_logger.info(f"🔘 [UI: {interaction.user}] Kliknął 📜 Pokaż kolejkę (strona 1)")
        embed = self.player.create_queue_embed(page=1)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GuildMusicPlayer:
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

    def start(self):
        """Starts background loops if not running."""
        if not self.player_task or self.player_task.done():
            logger.info(f"[G:{self.guild_id}] Uruchamianie pętli audio_loop.")
            self.player_task = asyncio.create_task(self.audio_loop())
        if not self.inactivity_task or self.inactivity_task.done():
            logger.info(f"[G:{self.guild_id}] Uruchamianie pętli inactivity_loop.")
            self.inactivity_task = asyncio.create_task(self.inactivity_loop())

    def reset_inactivity(self):
        self.last_active_time = time.time()

    async def inactivity_loop(self):
        """Disconnects if idle for too long or if the voice channel is empty."""
        logger.debug(f"[G:{self.guild_id}] Inactivity loop wystartował.")
        empty_logged = False
        while True:
            await asyncio.sleep(15)
            if not self.voice_client or not self.voice_client.is_connected():
                empty_logged = False
                continue

            channel = self.voice_client.channel
            # Check if all users left (excluding bot)
            non_bot_members = [m for m in channel.members if not m.bot]
            if len(non_bot_members) == 0:
                if not empty_logged:
                    logger.info(f"[G:{self.guild_id}] 👥 Kanał '{channel.name}' opuszczony przez wszystkich. Start odliczania 60s do rozłączenia.")
                    empty_logged = True
                if time.time() - self.last_active_time > 60:
                    logger.info(f"[G:{self.guild_id}] 🚪 Rozłączanie z powodu pustego kanału głosowego '{channel.name}' przez 60s.")
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
                    logger.info(f"[G:{self.guild_id}] 💤 Rozłączanie z powodu braku aktywności przez {int(idle_duration)}s (limit: {config.AUTO_LEAVE_SECONDS}s).")
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
        """Main loop that plays queued audio tracks."""
        await self.bot.wait_until_ready()
        logger.info(f"[G:{self.guild_id}] 🚀 Główna pętla odtwarzacza wystartowała dla serwera ID: {self.guild_id}")
        while True:
            try:
                self.play_next_song_event.clear()
                self._skip_flag = False

                if not self.voice_client or not self.voice_client.is_connected():
                    await asyncio.sleep(1)
                    continue

                # Determine next song to play
                if self.loop_mode == "song" and self.current_song:
                    song = self.current_song
                    logger.info(f"[G:{self.guild_id}] 🔂 Tryb pętli utworu aktywny: ponowne odtwarzanie '{song.title}'")
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
                    f"[G:{self.guild_id}] 🎵 Przygotowywanie utworu: '{song.title}' [{song.formatted_duration}] "
                    f"| Dodał: {song.requester} | Tryb pętli: {self.loop_mode} | Pozostało w kolejce: {len(self.queue)}"
                )

                # Resolve streaming URL lazily
                try:
                    await song.resolve_stream()
                except Exception as e:
                    logger.error(f"[G:{self.guild_id}] ❌ Błąd pobierania strumienia audio dla '{song.title}': {e}", exc_info=True)
                    if self.text_channel:
                        try:
                            await self.text_channel.send(
                                f"⚠️ Nie udało się załadować utworu **{song.title}**: `{e}`. Przechodzę do kolejnego..."
                            )
                        except Exception:
                            pass
                    self.current_song = None
                    continue

                # Create audio source
                try:
                    audio_source = song.create_audio_source(volume=self.volume)
                except Exception as e:
                    logger.error(f"[G:{self.guild_id}] ❌ Błąd inicjalizacji FFmpeg dla '{song.title}': {e}", exc_info=True)
                    if self.text_channel:
                        try:
                            await self.text_channel.send(f"⚠️ Błąd strumienia FFmpeg: `{e}`")
                        except Exception:
                            pass
                    self.current_song = None
                    continue

                def after_playback(error):
                    if error:
                        logger.error(f"[G:{self.guild_id}] ⚠️ Błąd FFmpeg w trakcie odtwarzania '{song.title}': {error}", exc_info=True)
                        if self.text_channel:
                            asyncio.run_coroutine_threadsafe(
                                self.text_channel.send(f"⚠️ Błąd odtwarzania: {error}"),
                                self.bot.loop,
                            )
                    else:
                        logger.info(f"[G:{self.guild_id}] 🏁 Zakończono odtwarzanie utworu: '{song.title}'")
                    self.bot.loop.call_soon_threadsafe(self.play_next_song_event.set)

                # Ensure previous playback is stopped
                try:
                    if self.voice_client.is_playing() or self.voice_client.is_paused():
                        self.voice_client.stop()
                    channel_name = self.voice_client.channel.name if self.voice_client.channel else 'nieznany'
                    logger.info(f"[G:{self.guild_id}] ▶️ Rozpoczęto odtwarzanie utworu: '{song.title}' na kanale '{channel_name}'")
                    self.voice_client.play(audio_source, after=after_playback)
                except Exception as e:
                    logger.error(f"[G:{self.guild_id}] ❌ Błąd podczas wywołania voice_client.play() dla '{song.title}': {e}", exc_info=True)
                    if self.text_channel:
                        try:
                            await self.text_channel.send(f"⚠️ Błąd uruchomienia odtwarzacza: `{e}`")
                        except Exception:
                            pass
                    self.current_song = None
                    continue

                # Send Now Playing embed
                if self.text_channel:
                    embed = self.create_now_playing_embed(song)
                    view = MusicControlView(self)
                    try:
                        await self.text_channel.send(embed=embed, view=view)
                    except Exception:
                        pass

                # Robust wait for completion (poll every 1.5s so we NEVER hang indefinitely)
                while not self.play_next_song_event.is_set():
                    if not self.voice_client or not self.voice_client.is_connected():
                        logger.warning(f"[G:{self.guild_id}] ⚠️ Rozłączono z kanałem głosowym w trakcie odtwarzania.")
                        break
                    if not self.voice_client.is_playing() and not self.voice_client.is_paused():
                        logger.info(f"[G:{self.guild_id}] ⏹️ Wykryto brak aktywnego odtwarzania (utwór zakończony).")
                        break
                    try:
                        await asyncio.wait_for(self.play_next_song_event.wait(), timeout=1.5)
                    except asyncio.TimeoutError:
                        pass

            except asyncio.CancelledError:
                logger.info(f"[G:{self.guild_id}] Pętla audio została zatrzymana (CancelledError).")
                break
            except Exception as e:
                logger.error(f"[G:{self.guild_id}] 💥 KRYTYCZNY BŁĄD PĘTLI AUDIO: {e}", exc_info=True)
                await asyncio.sleep(2)

    def skip(self, count: int = 1) -> int:
        """Skips the current song and optionally drops subsequent tracks."""
        if not self.voice_client:
            logger.warning(f"[G:{self.guild_id}] Próba skip(), ale voice_client nie istnieje.")
            return 0

        # If skipping multiple songs
        skipped_count = 1
        if count > 1:
            for _ in range(count - 1):
                if len(self.queue) > 0:
                    self.queue.popleft()
                    skipped_count += 1

        self._skip_flag = True
        cur_title = self.current_song.title if self.current_song else 'brak'
        logger.info(f"[G:{self.guild_id}] ⏭️ Wywołano skip() dla {count} utworów. Pominięto: {skipped_count} (bieżący: '{cur_title}')")
        if self.loop_mode == "song":
            # Force advance past this song even if song loop was active
            self.current_song = None

        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()
        self.play_next_song_event.set()

        return skipped_count

    def stop(self):
        """Stops playback and clears queue."""
        logger.info(f"[G:{self.guild_id}] ⏹️ Wywołano stop() - wyczyszczono kolejkę ({len(self.queue)} utworów) i zatrzymano playback.")
        self.queue.clear()
        self.current_song = None
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        self.play_next_song_event.set()

    async def disconnect(self):
        """Disconnects voice client and stops playback."""
        logger.info(f"[G:{self.guild_id}] 🔌 Wywołano disconnect() - rozłączanie z kanałem głosowym.")
        self.stop()
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect(force=True)
        self.voice_client = None

    def set_volume(self, volume_percent: int):
        """Sets the volume from 0 to 100."""
        self.volume = max(0, min(100, volume_percent)) / 100.0
        logger.info(f"[G:{self.guild_id}] 🔊 Zmieniono poziom głośności: {volume_percent}% (mnożnik: {self.volume})")
        if self.voice_client and self.voice_client.source:
            if isinstance(self.voice_client.source, discord.PCMVolumeTransformer):
                self.voice_client.source.volume = self.volume

    def shuffle(self):
        """Shuffles remaining items in queue."""
        logger.info(f"[G:{self.guild_id}] 🔀 Wymieszano kolejność utworów w kolejce (utworów: {len(self.queue)}).")
        items = list(self.queue)
        random.shuffle(items)
        self.queue = collections.deque(items)

    def remove(self, index: int) -> Optional[Song]:
        """Removes a song at index (1-based)."""
        if 1 <= index <= len(self.queue):
            items = list(self.queue)
            removed = items.pop(index - 1)
            self.queue = collections.deque(items)
            logger.info(f"[G:{self.guild_id}] 🗑️ Usunięto utwór z pozycji {index}: '{removed.title}'")
            return removed
        logger.warning(f"[G:{self.guild_id}] ⚠️ Próba usunięcia nieistniejącej pozycji {index} (rozmiar kolejki: {len(self.queue)})")
        return None

    def create_now_playing_embed(self, song: Song) -> discord.Embed:
        embed = discord.Embed(
            title="🎶 Aktualnie gramy",
            description=f"**[{song.title}]({song.web_url})**",
            color=discord.Color.brand_green() if song.source_type == "spotify" else discord.Color.red(),
        )

        source_icons = {
            "spotify": "🟢 Spotify",
            "youtube": "🔴 YouTube",
            "soundcloud": "🟠 SoundCloud",
        }
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

        embed.set_footer(text=f"W kolejce pozostało: {len(self.queue)} utworów")
        return embed

    def create_queue_embed(self, page: int = 1) -> discord.Embed:
        items_per_page = 10
        total_items = len(self.queue)
        total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)
        page = max(1, min(page, total_pages))

        embed = discord.Embed(
            title="📋 Kolejka utworów",
            color=discord.Color.blurple(),
        )

        # Current song field
        if self.current_song:
            embed.add_field(
                name="▶️ Aktualnie odtwarzane",
                value=f"**[{self.current_song.title}]({self.current_song.web_url})** `[{self.current_song.formatted_duration}]`\nDodane przez: {self.current_song.requester.mention if self.current_song.requester else 'Nieznany'}",
                inline=False,
            )
        else:
            embed.add_field(
                name="▶️ Aktualnie odtwarzane",
                value="*Nic aktualnie nie gra*",
                inline=False,
            )

        # Calculate upcoming queue slice
        start_idx = (page - 1) * items_per_page
        end_idx = start_idx + items_per_page
        queue_slice = list(self.queue)[start_idx:end_idx]

        if queue_slice:
            lines = []
            for i, song in enumerate(queue_slice, start=start_idx + 1):
                req = f" ({song.requester.display_name})" if song.requester else ""
                lines.append(f"`{i:2d}.` **[{song.title}]({song.web_url})** `[{song.formatted_duration}]`{req}")
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

        # Total duration of queue
        total_seconds = sum(s.duration for s in self.queue)
        embed.set_footer(
            text=f"Łącznie w kolejce: {total_items} utworów | Łączny czas: {format_duration(total_seconds)} | Głośność: {int(self.volume * 100)}%"
        )
        return embed
