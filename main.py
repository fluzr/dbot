import asyncio
import logging
import os
import sys
from typing import Optional, Literal
import discord
from discord import app_commands
from discord.ext import commands

import config
from music_source import MusicSourceManager, Song, format_duration
from player import (
    GuildMusicPlayer,
    MusicPlayer,
    MusicControlView,
    is_lavalink_connected,
    create_lavalink_now_playing_embed,
    create_lavalink_queue_embed,
    WAVELINK_AVAILABLE,
)

if WAVELINK_AVAILABLE:
    import wavelink

# Wymuszenie kodowania UTF-8 dla strumieni wyjścia konsoli (Windows/Linux)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Konfiguracja systemu logowania
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger("mbot.main")
cmd_logger = logging.getLogger("mbot.cmd")
voice_logger = logging.getLogger("mbot.voice")

# Validate config before running
validate_func = getattr(config, "validate_config", None)
config_errors = validate_func() if callable(validate_func) else []
if config_errors:
    logger.error("=" * 60)
    logger.error("BŁĘDY KONFIGURACJI (.env):")
    for err in config_errors:
        logger.error(f" - {err}")
    logger.error("=" * 60)

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
source_manager = MusicSourceManager()
local_music_player: Optional[GuildMusicPlayer] = None


def get_or_create_local_player(guild: discord.Guild) -> GuildMusicPlayer:
    global local_music_player
    if local_music_player is None:
        logger.info(f"[INIT] Tworzenie lokalnego GuildMusicPlayer dla serwera: '{guild.name}' (ID: {guild.id})")
        local_music_player = GuildMusicPlayer(bot, guild.id)
    local_music_player.start()
    return local_music_player


def guild_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        cmd_logger.warning(f"Odrzucono interakcję od {interaction.user}: brak kontekstu serwera (DM).")
        return False
    if config.DISCORD_GUILD_ID and interaction.guild.id != config.DISCORD_GUILD_ID:
        cmd_logger.warning(
            f"Odrzucono interakcję od {interaction.user}: nieautoryzowany serwer ID {interaction.guild.id} "
            f"(dozwolony serwer: {config.DISCORD_GUILD_ID})."
        )
        return False
    return True


# =====================================================================
# Wavelink Event Listeners
# =====================================================================
if WAVELINK_AVAILABLE:
    @bot.event
    async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
        logger.info(f"🟢 [LAVALINK] Węzeł '{payload.node.identifier}' jest gotowy i połączony! ({payload.node.uri})")

    @bot.event
    async def on_wavelink_node_disconnected(payload: wavelink.NodeDisconnectedEventPayload):
        logger.warning(f"⚠️ [LAVALINK] Węzeł '{payload.node.identifier}' został rozłączony. Tryb fallback (yt-dlp) aktywny.")

    @bot.event
    async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
        player = payload.player
        track = payload.track
        if not player or not getattr(player, "home_channel", None):
            return
        logger.info(f"▶️ [LAVALINK] Rozpoczęto odtwarzanie utworu: '{track.title}' [{format_duration(track.length // 1000 if track.length else 0)}]")
        embed = create_lavalink_now_playing_embed(player, track)
        view = MusicControlView()
        try:
            msg = await player.home_channel.send(embed=embed, view=view)
            player.now_playing_message = msg
        except Exception as e:
            logger.warning(f"Nie udało się wysłać embedu Now Playing (Lavalink): {e}")

    @bot.event
    async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
        track = payload.track
        logger.info(f"🏁 [LAVALINK] Zakończono odtwarzanie utworu: '{track.title}' (powód: {payload.reason})")

    @bot.event
    async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
        player = payload.player
        track = payload.track
        logger.error(f"❌ [LAVALINK] Błąd odtwarzania '{track.title}': {payload.exception}. Uruchamiam fallback yt-dlp...")
        if player and getattr(player, "home_channel", None):
            try:
                await player.home_channel.send(
                    f"⚠️ [Lavalink] Błąd odtwarzania **{track.title}**: `{payload.exception}`. Pobieram przez fallback yt-dlp..."
                )
            except Exception:
                pass

        # Próba odzyskania i odtworzenia przez yt-dlp
        try:
            songs, _ = await source_manager.get_songs(track.title or track.uri)
            if songs:
                song = songs[0]
                await song.resolve_stream()
                direct = await wavelink.Playable.search(song.stream_url)
                if direct:
                    await player.play(direct[0])
        except Exception as e:
            logger.error(f"Błąd awaryjnego odzyskiwania utworu: {e}")


# =====================================================================
# Bot Lifecycle Events
# =====================================================================
@bot.event
async def on_ready():
    logger.info(f"✅ Zalogowano pomyślnie jako: {bot.user} (ID: {bot.user.id}) | Serwerów: {len(bot.guilds)}")

    # Inicjalizacja połączenia z Lavalink (jeśli włączone)
    if WAVELINK_AVAILABLE and getattr(config, "LAVALINK_ENABLED", True):
        try:
            uri = getattr(config, "LAVALINK_URI", "http://127.0.0.1:2333")
            password = getattr(config, "LAVALINK_PASSWORD", "youshallnotpass")
            node = wavelink.Node(uri=uri, password=password)
            await wavelink.Pool.connect(nodes=[node], client=bot)
            logger.info(f"🚀 [INIT] Wysłano zapytanie połączenia do Lavalink: {uri}")
        except Exception as e:
            logger.warning(f"⚠️ [INIT] Nie udało się zainicjalizować Lavalink: {e}. Bot działa w trybie awaryjnym (yt-dlp).")
    else:
        logger.info("ℹ️ [INIT] Lavalink wyłączony lub brak modułu wavelink. Bot działa w trybie lokalnym (yt-dlp + FFmpeg).")

    # Synchronizacja komend slash
    if config.DISCORD_GUILD_ID:
        guild_obj = discord.Object(id=config.DISCORD_GUILD_ID)
        try:
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            logger.info(f"🚀 Zsynchronizowano {len(synced)} komend slash dla serwera ID: {config.DISCORD_GUILD_ID}")
        except Exception as e:
            logger.error(f"❌ Błąd podczas synchronizacji komend slash: {e}", exc_info=True)
    else:
        logger.warning("⚠️ Brak DISCORD_GUILD_ID w konfiguracji. Komendy slash nie zostały zsynchronizowane!")

    bot_status = getattr(config, "BOT_STATUS", os.getenv("BOT_STATUS", "/play | Muzyka"))
    activity = discord.Activity(type=discord.ActivityType.listening, name=bot_status)
    await bot.change_presence(activity=activity)
    logger.info(f"🎮 Ustawiono status bota: 'Słucha {bot_status}'")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    global local_music_player
    if member == bot.user:
        if before.channel is not None and after.channel is None:
            voice_logger.warning(f"🤖 Bot został odłączony od kanału głosowego '{before.channel.name}' (ID: {before.channel.id})!")
            if local_music_player:
                local_music_player.stop()
                local_music_player.voice_client = None
        elif before.channel != after.channel and after.channel is not None:
            voice_logger.info(f"🤖 Bot przeniósł się na kanał głosowy: '{after.channel.name}'")
        return


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    cmd_name = interaction.command.name if interaction.command else "nieznana"
    cmd_logger.error(
        f"💥 [BŁĄD KOMENDY] /{cmd_name} wywołana przez {interaction.user} w #{interaction.channel}: {error}",
        exc_info=error,
    )
    try:
        msg = f"❌ Wystąpił błąd podczas wykonywania komendy: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# =====================================================================
# KOMENDY SLASH
# =====================================================================

@bot.tree.command(name="join", description="Dołącza bota do Twojego kanału głosowego.")
async def join_cmd(interaction: discord.Interaction, kanal: Optional[discord.VoiceChannel] = None):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    await interaction.response.defer()
    target_channel = kanal or (interaction.user.voice.channel if interaction.user.voice else None)
    if not target_channel:
        return await interaction.followup.send("❌ Musisz być na kanale głosowym lub wskazać kanał w parametrze!", ephemeral=True)

    try:
        if is_lavalink_connected():
            if isinstance(interaction.guild.voice_client, MusicPlayer):
                await interaction.guild.voice_client.move_to(target_channel)
            else:
                if interaction.guild.voice_client:
                    await interaction.guild.voice_client.disconnect(force=True)
                player: MusicPlayer = await target_channel.connect(cls=MusicPlayer)
                player.home_channel = interaction.channel
            return await interaction.followup.send(f"🔊 Połączono z {target_channel.mention} (Lavalink)!")
        else:
            vc = interaction.guild.voice_client
            if vc:
                await vc.move_to(target_channel)
            else:
                vc = await target_channel.connect(reconnect=True, timeout=20.0)
            lp = get_or_create_local_player(interaction.guild)
            lp.voice_client = vc
            lp.text_channel = interaction.channel
            return await interaction.followup.send(f"🔊 Połączono z {target_channel.mention}!")
    except Exception as e:
        cmd_logger.error(f"Błąd dołączania do kanału: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Nie udało się połączyć z kanałem: `{e}`", ephemeral=True)


@bot.tree.command(name="leave", description="Rozłącza bota i czyści kolejkę.")
async def leave_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    await interaction.response.defer()
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.followup.send("❌ Bot nie znajduje się na żadnym kanale głosowym.")

    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        vc.queue.clear()
        await vc.disconnect()
    else:
        lp = get_or_create_local_player(interaction.guild)
        await lp.disconnect()

    await interaction.followup.send("👋 Rozłączono z kanału i wyczyszczono kolejkę.")


@bot.tree.command(name="play", description="Odtwarza utwór lub playlistę z YouTube, Spotify lub SoundCloud.")
@app_commands.describe(szukaj="Tytuł utworu lub link (YouTube, Spotify, SoundCloud)")
async def play_cmd(interaction: discord.Interaction, szukaj: str):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] /play '{szukaj}'")
    await interaction.response.defer()

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ Musisz być na kanale głosowym, aby użyć tej komendy!", ephemeral=True)

    user_channel = interaction.user.voice.channel

    # =====================================================================
    # SCIEZKA 1: LAVALINK (GŁÓWNY SILNIK)
    # =====================================================================
    if is_lavalink_connected():
        cmd_logger.info(f"⚡ [PLAY] Używam głównego silnika Lavalink dla: '{szukaj}'")
        try:
            # Upewnij się, że bot jest połączony jako MusicPlayer
            vc = interaction.guild.voice_client
            if not isinstance(vc, MusicPlayer):
                if vc:
                    await vc.disconnect(force=True)
                player: MusicPlayer = await user_channel.connect(cls=MusicPlayer)
            else:
                player: MusicPlayer = vc
                if player.channel != user_channel:
                    await player.move_to(user_channel)

            player.home_channel = interaction.channel

            tracks: wavelink.Search = await wavelink.Playable.search(szukaj)
            if tracks:
                if isinstance(tracks, wavelink.Playlist):
                    for t in tracks.tracks:
                        t.extras = {"requester": interaction.user.display_name}
                    await player.queue.put_wait(tracks)
                    if not player.playing:
                        await player.play(player.queue.get(), volume=config.DEFAULT_VOLUME)

                    total_ms = sum(getattr(t, "length", 0) or 0 for t in tracks.tracks)
                    embed = discord.Embed(
                        title="📚 Dodano playlistę do kolejki (Lavalink ⚡)",
                        description=f"Załadowano **{len(tracks.tracks)}** utworów: **{tracks.name}**",
                        color=discord.Color.gold(),
                    )
                    embed.add_field(name="Łączny czas", value=format_duration(total_ms // 1000), inline=True)
                    embed.add_field(name="Łącznie w kolejce", value=str(len(player.queue)), inline=True)
                    embed.set_footer(text=f"Dodane przez: {interaction.user.display_name}")
                    return await interaction.followup.send(embed=embed)
                else:
                    track = tracks[0]
                    track.extras = {"requester": interaction.user.display_name}
                    await player.queue.put_wait(track)
                    if not player.playing:
                        await player.play(player.queue.get(), volume=config.DEFAULT_VOLUME)

                    dur_sec = (track.length // 1000) if getattr(track, "length", 0) > 0 else 0
                    embed = discord.Embed(
                        title="➕ Dodano do kolejki (Lavalink ⚡)",
                        description=f"**[{track.title}]({track.uri})**",
                        color=discord.Color.green(),
                    )
                    embed.add_field(name="Czas trwania", value=format_duration(dur_sec), inline=True)
                    embed.add_field(name="Pozycja w kolejce", value=str(len(player.queue)), inline=True)
                    if getattr(track, "artwork", None):
                        embed.set_thumbnail(url=track.artwork)
                    embed.set_footer(text=f"Dodane przez: {interaction.user.display_name}")
                    return await interaction.followup.send(embed=embed)
            else:
                cmd_logger.warning(f"⚠️ [Lavalink] Brak wyników dla '{szukaj}'. Przechodzę do fallbacku yt-dlp...")
        except Exception as e:
            cmd_logger.warning(f"⚠️ [Lavalink] Wyjątek podczas wyszukiwania '{szukaj}': {e}. Przechodzę do fallbacku yt-dlp...")

        # Próba załadowania bezpośredniego strumienia yt-dlp do Lavalink
        try:
            songs, _ = await source_manager.get_songs(szukaj, requester=interaction.user)
            if songs:
                song = songs[0]
                await song.resolve_stream()
                direct_tracks = await wavelink.Playable.search(song.stream_url)
                if direct_tracks:
                    direct_track = direct_tracks[0]
                    direct_track.title = song.title
                    direct_track.author = song.uploader
                    direct_track.extras = {"requester": interaction.user.display_name}
                    await player.queue.put_wait(direct_track)
                    if not player.playing:
                        await player.play(player.queue.get(), volume=config.DEFAULT_VOLUME)

                    embed = discord.Embed(
                        title="➕ Dodano do kolejki (yt-dlp fallback -> Lavalink 🌐)",
                        description=f"**[{song.title}]({song.web_url})**",
                        color=discord.Color.orange(),
                    )
                    embed.add_field(name="Czas trwania", value=song.formatted_duration, inline=True)
                    embed.add_field(name="Pozycja w kolejce", value=str(len(player.queue)), inline=True)
                    if song.thumbnail:
                        embed.set_thumbnail(url=song.thumbnail)
                    embed.set_footer(text=f"Dodane przez: {interaction.user.display_name}")
                    return await interaction.followup.send(embed=embed)
        except Exception as ex_http:
            cmd_logger.warning(f"Lavalink HTTP fallback nie powiódł się: {ex_http}. Przełączam na odtwarzacz lokalny...")

    # =====================================================================
    # SCIEZKA 2: LOKALNY FALLBACK (yt-dlp + FFmpeg)
    # =====================================================================
    cmd_logger.info(f"🛡️ [PLAY] Uruchamiam lokalny odtwarzacz yt-dlp + FFmpeg dla: '{szukaj}'")
    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        await vc.disconnect(force=True)
        vc = None

    if not vc or not vc.is_connected():
        try:
            vc = await user_channel.connect(reconnect=True, timeout=20.0)
        except Exception as e:
            return await interaction.followup.send(f"❌ Nie udało się połączyć z kanałem: `{e}`", ephemeral=True)

    lp = get_or_create_local_player(interaction.guild)
    lp.voice_client = vc
    lp.text_channel = interaction.channel

    try:
        songs, result_type = await source_manager.get_songs(szukaj, requester=interaction.user)
    except Exception as e:
        cmd_logger.error(f"❌ Błąd w trakcie pobierania dla '{szukaj}': {e}", exc_info=True)
        return await interaction.followup.send(f"❌ Błąd podczas wyszukiwania: `{e}`")

    if not songs:
        return await interaction.followup.send("❌ Nie znaleziono żadnych utworów.")

    if len(songs) == 1:
        song = songs[0]
        lp.queue.append(song)
        embed = discord.Embed(
            title="➕ Dodano do kolejki (Lokalny yt-dlp 🛡️)",
            description=f"**[{song.title}]({song.web_url})**",
            color=discord.Color.green(),
        )
        embed.add_field(name="Czas trwania", value=song.formatted_duration, inline=True)
        embed.add_field(name="Pozycja w kolejce", value=str(len(lp.queue)), inline=True)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.set_footer(text=f"Dodane przez: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)
    else:
        for s in songs:
            lp.queue.append(s)
        total_time = sum(s.duration for s in songs)
        embed = discord.Embed(
            title="📚 Dodano playlistę do kolejki (Lokalny yt-dlp 🛡️)",
            description=f"Załadowano **{len(songs)}** utworów ({result_type})",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Łączny czas", value=format_duration(total_time), inline=True)
        embed.add_field(name="Łącznie w kolejce", value=str(len(lp.queue)), inline=True)
        if songs[0].thumbnail:
            embed.set_thumbnail(url=songs[0].thumbnail)
        embed.set_footer(text=f"Dodane przez: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="skip", description="Przewija bieżący utwór (lub kilka utworów).")
@app_commands.describe(ile="Liczba utworów do pominięcia (domyślnie 1)")
async def skip_cmd(interaction: discord.Interaction, ile: int = 1):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ Bot nie jest połączony z kanałem.", ephemeral=True)

    count = max(1, ile)
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        if not vc.current and vc.queue.is_empty:
            return await interaction.response.send_message("❌ Kolejka jest pusta.", ephemeral=True)
        for _ in range(count - 1):
            if not vc.queue.is_empty:
                vc.queue.delete(0)
        await vc.skip(force=True)
        return await interaction.response.send_message(f"⏭️ Przewinięto {count} utwór(y) (Lavalink)!")

    lp = get_or_create_local_player(interaction.guild)
    if not lp.current_song:
        return await interaction.response.send_message("❌ Nic aktualnie nie jest odtwarzane.", ephemeral=True)
    skipped = lp.skip(count=count)
    await interaction.response.send_message(f"⏭️ Przewinięto {skipped} utwór(y)!")


@bot.tree.command(name="queue", description="Wyświetla aktualnie grany utwór oraz listę kolejki.")
@app_commands.describe(strona="Numer strony kolejki do wyświetlenia")
async def queue_cmd(interaction: discord.Interaction, strona: int = 1):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        embed = create_lavalink_queue_embed(vc, page=strona)
        return await interaction.response.send_message(embed=embed)

    lp = get_or_create_local_player(interaction.guild)
    embed = lp.create_queue_embed(page=strona)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Wyświetla informacje o aktualnie odtwarzanym utworze z przyciskami.")
async def nowplaying_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        if not vc.current:
            return await interaction.response.send_message("❌ Nic aktualnie nie jest odtwarzane.", ephemeral=True)
        embed = create_lavalink_now_playing_embed(vc, vc.current)
        view = MusicControlView()
        return await interaction.response.send_message(embed=embed, view=view)

    lp = get_or_create_local_player(interaction.guild)
    if not lp.current_song:
        return await interaction.response.send_message("❌ Nic aktualnie nie jest odtwarzane.", ephemeral=True)
    embed = lp.create_now_playing_embed(lp.current_song)
    view = MusicControlView(local_player=lp)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="volume", description="Zmienia głośność odtwarzacza (0-100%).")
@app_commands.describe(poziom="Poziom głośności w procentach (od 0 do 100)")
async def volume_cmd(interaction: discord.Interaction, poziom: int):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    if poziom < 0 or poziom > 100:
        return await interaction.response.send_message("❌ Podaj wartość z zakresu od 0 do 100.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        await vc.set_volume(poziom)
        return await interaction.response.send_message(f"🔊 Głośność ustawiona na: **{poziom}%** (Lavalink)")

    lp = get_or_create_local_player(interaction.guild)
    lp.set_volume(poziom)
    await interaction.response.send_message(f"🔊 Głośność ustawiona na: **{poziom}%**")


@bot.tree.command(name="pause", description="Wstrzymuje odtwarzanie muzyki.")
async def pause_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        if not vc.playing or vc.paused:
            return await interaction.response.send_message("❌ Nic aktualnie nie gra.", ephemeral=True)
        await vc.pause(True)
        return await interaction.response.send_message("⏸️ Wstrzymano odtwarzanie (Lavalink).")

    lp = get_or_create_local_player(interaction.guild)
    if not lp.voice_client or not lp.voice_client.is_playing():
        return await interaction.response.send_message("❌ Nic aktualnie nie gra.", ephemeral=True)
    lp.voice_client.pause()
    await interaction.response.send_message("⏸️ Wstrzymano odtwarzanie.")


@bot.tree.command(name="resume", description="Wznawia odtwarzanie muzyki.")
async def resume_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        if not vc.paused:
            return await interaction.response.send_message("❌ Odtwarzacz nie jest wstrzymany.", ephemeral=True)
        await vc.pause(False)
        return await interaction.response.send_message("▶️ Wznowiono odtwarzanie (Lavalink).")

    lp = get_or_create_local_player(interaction.guild)
    if not lp.voice_client or not lp.voice_client.is_paused():
        return await interaction.response.send_message("❌ Odtwarzacz nie jest wstrzymany.", ephemeral=True)
    lp.voice_client.resume()
    await interaction.response.send_message("▶️ Wznowiono odtwarzanie.")


@bot.tree.command(name="stop", description="Zatrzymuje odtwarzacz i czyści kolejkę.")
async def stop_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        vc.queue.clear()
        await vc.stop()
        return await interaction.response.send_message("⏹️ Zatrzymano muzykę i wyczyszczono kolejkę (Lavalink).")

    lp = get_or_create_local_player(interaction.guild)
    lp.stop()
    await interaction.response.send_message("⏹️ Zatrzymano muzykę i wyczyszczono kolejkę.")


@bot.tree.command(name="shuffle", description="Miesza kolejność utworów w kolejce.")
async def shuffle_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        if len(vc.queue) < 2:
            return await interaction.response.send_message("❌ W kolejce musi być przynajmniej 2 utwory, aby ją wymieszać.", ephemeral=True)
        vc.queue.shuffle()
        return await interaction.response.send_message(f"🔀 Wymieszano kolejność **{len(vc.queue)}** utworów w kolejce!")

    lp = get_or_create_local_player(interaction.guild)
    if len(lp.queue) < 2:
        return await interaction.response.send_message("❌ W kolejce musi być przynajmniej 2 utwory, aby ją wymieszać.", ephemeral=True)
    lp.shuffle()
    await interaction.response.send_message(f"🔀 Wymieszano kolejność **{len(lp.queue)}** utworów w kolejce!")


@bot.tree.command(name="remove", description="Usuwa konkretny utwór z kolejki według jego numeru.")
@app_commands.describe(numer="Numer utworu z komendy /queue")
async def remove_cmd(interaction: discord.Interaction, numer: int):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        if 1 <= numer <= len(vc.queue):
            removed = vc.queue[numer - 1]
            vc.queue.delete(numer - 1)
            return await interaction.response.send_message(f"🗑️ Usunięto z kolejki: **{removed.title}**")
        return await interaction.response.send_message(f"❌ Nie znaleziono utworu o numerze {numer}.", ephemeral=True)

    lp = get_or_create_local_player(interaction.guild)
    removed = lp.remove(numer)
    if removed:
        await interaction.response.send_message(f"🗑️ Usunięto z kolejki: **{removed.title}**")
    else:
        await interaction.response.send_message(f"❌ Nie znaleziono utworu o numerze {numer}.", ephemeral=True)


@bot.tree.command(name="clear", description="Czyści całą kolejkę (nie przerywając aktualnie granego utworu).")
async def clear_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        count = len(vc.queue)
        vc.queue.clear()
        return await interaction.response.send_message(f"🧹 Wyczyszczono {count} utworów z kolejki.")

    lp = get_or_create_local_player(interaction.guild)
    count = len(lp.queue)
    lp.queue.clear()
    await interaction.response.send_message(f"🧹 Wyczyszczono {count} utworów z kolejki.")


@bot.tree.command(name="loop", description="Ustawia tryb powtarzania.")
@app_commands.describe(tryb="Wybierz: wyłączone (off), bieżący utwór (song), cała kolejka (queue)")
async def loop_cmd(interaction: discord.Interaction, tryb: Literal["off", "song", "queue"]):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    labels = {
        "off": "❌ Wyłączona",
        "song": "🔂 Powtarzanie utworu",
        "queue": "🔁 Powtarzanie kolejki",
    }

    vc = interaction.guild.voice_client
    if WAVELINK_AVAILABLE and isinstance(vc, wavelink.Player):
        mode_map = {
            "off": wavelink.QueueMode.normal,
            "song": wavelink.QueueMode.loop,
            "queue": wavelink.QueueMode.loop_all,
        }
        vc.queue.mode = mode_map[tryb]
        return await interaction.response.send_message(f"Ustawiono tryb pętli (Lavalink): **{labels[tryb]}**")

    lp = get_or_create_local_player(interaction.guild)
    lp.loop_mode = tryb
    await interaction.response.send_message(f"Ustawiono tryb pętli: **{labels[tryb]}**")


# =====================================================================
# START
# =====================================================================
if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        logger.critical("[BŁĄD] Wprowadź token bota w pliku .env i spróbuj ponownie.")
        sys.exit(1)

    logger.info("🚀 Uruchamianie bota muzycznego (Lavalink v4 + yt-dlp fallback)...")
    bot.run(config.DISCORD_TOKEN)
