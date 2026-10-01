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
from player import GuildMusicPlayer, MusicControlView

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
music_player: Optional[GuildMusicPlayer] = None


def get_or_create_player(guild: discord.Guild) -> GuildMusicPlayer:
    global music_player
    if music_player is None:
        logger.info(f"[INIT] Tworzenie nowego GuildMusicPlayer dla serwera: '{guild.name}' (ID: {guild.id})")
        music_player = GuildMusicPlayer(bot, guild.id)
    music_player.start()
    return music_player


# Check for single-server restriction
def guild_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        cmd_logger.warning(f"Odrzucono interakcję od {interaction.user}: brak kontekstu serwera (DM).")
        return False
    if config.DISCORD_GUILD_ID and interaction.guild.id != config.DISCORD_GUILD_ID:
        cmd_logger.warning(
            f"Odrzucono interakcję od {interaction.user}: próba użycia na nieautoryzowanym serwerze ID {interaction.guild.id} "
            f"(dozwolony serwer: {config.DISCORD_GUILD_ID})."
        )
        return False
    return True


@bot.event
async def on_ready():
    logger.info(f"✅ Zalogowano pomyślnie jako: {bot.user} (ID: {bot.user.id}) | Serwerów: {len(bot.guilds)}")
    
    # Sync slash commands for the specified guild (instant sync!)
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
    global music_player
    # If the bot itself was disconnected from voice channel
    if member == bot.user:
        if before.channel is not None and after.channel is None:
            voice_logger.warning(f"🤖 Bot został odłączony od kanału głosowego '{before.channel.name}' (ID: {before.channel.id})!")
            if music_player:
                music_player.stop()
                music_player.voice_client = None
        elif before.channel != after.channel and after.channel is not None:
            voice_logger.info(f"🤖 Bot przeniósł się na kanał głosowy: '{after.channel.name}'")
        return

    # User join / leave / move tracking for clear debug visibility
    if before.channel != after.channel:
        if before.channel is None and after.channel is not None:
            voice_logger.info(f"👤 [VOICE: JOIN] {member.display_name} ({member}) wszedł na kanał '{after.channel.name}'")
        elif before.channel is not None and after.channel is None:
            voice_logger.info(f"👤 [VOICE: LEAVE] {member.display_name} ({member}) opuścił kanał '{before.channel.name}'")
        elif before.channel is not None and after.channel is not None:
            voice_logger.info(f"👤 [VOICE: MOVE] {member.display_name} ({member}) przeszedł z '{before.channel.name}' na '{after.channel.name}'")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Globalny handler błędów komend Slash - loguje pełny traceback każdego błędu."""
    cmd_name = interaction.command.name if interaction.command else "nieznana"
    cmd_logger.error(
        f"💥 [BŁĄD KOMENDY] /{cmd_name} wywołana przez {interaction.user} (ID: {interaction.user.id}) "
        f"w kanale #{interaction.channel}: {error}",
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


# Helper to ensure user is in voice and connect bot
async def ensure_voice_connection(
    interaction: discord.Interaction,
) -> Optional[discord.VoiceClient]:
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        cmd_logger.warning(f"[VOICE CONNECT] Nie można ustalić profilu użytkownika dla: {interaction.user}")
        await interaction.followup.send("❌ Nie można określić Twojego profilu użytkownika.", ephemeral=True)
        return None

    if not interaction.user.voice or not interaction.user.voice.channel:
        cmd_logger.info(f"[VOICE CONNECT] Użytkownik {interaction.user.display_name} użył komendy muzycznej nie będąc na kanale głosowym.")
        await interaction.followup.send("❌ Musisz być na kanale głosowym, aby użyć tej komendy!", ephemeral=True)
        return None

    user_channel = interaction.user.voice.channel
    player = get_or_create_player(interaction.guild)
    player.text_channel = interaction.channel

    # Check bot permissions
    permissions = user_channel.permissions_for(interaction.guild.me)
    if not permissions.connect or not permissions.speak:
        cmd_logger.error(
            f"❌ [UPRAWNIENIA] Bot nie ma uprawnień do połączenia lub mówienia na kanale '{user_channel.name}' "
            f"(connect={permissions.connect}, speak={permissions.speak})!"
        )
        await interaction.followup.send(
            f"❌ Bot nie ma uprawnień do połączenia lub mówienia na kanale {user_channel.mention}!",
            ephemeral=True,
        )
        return None

    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_logger.info(f"🔊 Łączenie bota z kanałem głosowym: '{user_channel.name}' (ID: {user_channel.id})")
        voice_client = await user_channel.connect()
        player.voice_client = voice_client
        voice_logger.info(f"✅ Połączono z kanałem: '{user_channel.name}'")
    elif voice_client.channel != user_channel:
        voice_logger.info(f"🔄 Przenoszenie bota z '{voice_client.channel.name}' na kanał użytkownika: '{user_channel.name}'")
        await voice_client.move_to(user_channel)
        player.voice_client = voice_client
    else:
        player.voice_client = voice_client

    return voice_client


# ==========================================
# KOMENDY SLASH
# ==========================================


@bot.tree.command(name="join", description="Dołącza bota do Twojego kanału głosowego.")
async def join_cmd(interaction: discord.Interaction, kanal: Optional[discord.VoiceChannel] = None):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /join | Wskazany kanał: {kanal.name if kanal else 'automatyczny (kanał usera)'}")
    await interaction.response.defer()
    target_channel = kanal or (interaction.user.voice.channel if interaction.user.voice else None)
    if not target_channel:
        cmd_logger.warning(f"⚠️ [USER: {interaction.user}] /join odrzucone: brak kanału głosowego użytkownika lub parametru.")
        return await interaction.followup.send("❌ Musisz być na kanale głosowym lub wskazać kanał w parametrze!", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    player.text_channel = interaction.channel

    voice_client = interaction.guild.voice_client
    if voice_client:
        if voice_client.channel == target_channel:
            cmd_logger.info(f"ℹ️ [USER: {interaction.user}] Bot znajduje się już na kanale '{target_channel.name}'.")
            return await interaction.followup.send(f"Jestem już na kanale {target_channel.mention}.")
        cmd_logger.info(f"🔄 [USER: {interaction.user}] Przenoszenie bota z '{voice_client.channel.name}' na '{target_channel.name}'.")
        await voice_client.move_to(target_channel)
        player.voice_client = voice_client
        return await interaction.followup.send(f"Przeniesiono na kanał {target_channel.mention}.")

    cmd_logger.info(f"🔊 [USER: {interaction.user}] Łączenie z kanałem: '{target_channel.name}' (ID: {target_channel.id})")
    voice_client = await target_channel.connect()
    player.voice_client = voice_client
    cmd_logger.info(f"✅ [USER: {interaction.user}] Pomyślnie połączono z kanałem '{target_channel.name}'.")
    await interaction.followup.send(f"🔊 Połączono z {target_channel.mention}!")


@bot.tree.command(name="leave", description="Rozłącza bota i czyści kolejkę.")
async def leave_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /leave")
    await interaction.response.defer()
    player = get_or_create_player(interaction.guild)
    if not interaction.guild.voice_client:
        cmd_logger.warning(f"⚠️ [USER: {interaction.user}] /leave: Bot nie był połączony z żadnym kanałem głosowym.")
        return await interaction.followup.send("❌ Bot nie znajduje się na żadnym kanale głosowym.")

    cmd_logger.info(f"🔌 [USER: {interaction.user}] Rozłączanie bota i czyszczenie kolejki przez /leave.")
    await player.disconnect()
    await interaction.followup.send("👋 Rozłączono z kanału i wyczyszczono kolejkę.")


@bot.tree.command(name="play", description="Odtwarza utwór lub playlistę z YouTube, Spotify lub SoundCloud.")
@app_commands.describe(szukaj="Tytuł utworu lub link (YouTube, Spotify, SoundCloud)")
async def play_cmd(interaction: discord.Interaction, szukaj: str):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user} (ID: {interaction.user.id})] Wywołał /play -> szukaj='{szukaj}' | Kanał: #{interaction.channel}")
    await interaction.response.defer()

    # Ensure voice connection
    voice_client = await ensure_voice_connection(interaction)
    if not voice_client:
        cmd_logger.warning(f"⚠️ [USER: {interaction.user}] /play przerwane: brak połączenia z kanałem głosowym.")
        return

    player = get_or_create_player(interaction.guild)
    player.voice_client = voice_client
    player.text_channel = interaction.channel

    # Extract songs
    try:
        cmd_logger.info(f"🔍 [USER: {interaction.user}] Rozpoczynam wyszukiwanie i ekstrakcję dla: '{szukaj}'")
        songs, result_type = await source_manager.get_songs(szukaj, requester=interaction.user)
    except Exception as e:
        cmd_logger.error(f"❌ [USER: {interaction.user}] Błąd w trakcie pobierania dla '{szukaj}': {e}", exc_info=True)
        return await interaction.followup.send(f"❌ Błąd podczas wyszukiwania: `{e}`")

    if not songs:
        cmd_logger.warning(f"⚠️ [USER: {interaction.user}] Nie znaleziono żadnych utworów dla: '{szukaj}'")
        return await interaction.followup.send("❌ Nie znaleziono żadnych utworów.")

    # Add songs to player queue
    if len(songs) == 1:
        song = songs[0]
        player.queue.append(song)
        cmd_logger.info(
            f"➕ [USER: {interaction.user}] Dodano utwór do kolejki: '{song.title}' [{song.formatted_duration}] "
            f"| Pozycja: {len(player.queue)} | Źródło: {song.source_type} | URL: {song.web_url}"
        )
        embed = discord.Embed(
            title="➕ Dodano do kolejki",
            description=f"**[{song.title}]({song.web_url})**",
            color=discord.Color.green(),
        )
        embed.add_field(name="Czas trwania", value=song.formatted_duration, inline=True)
        embed.add_field(name="Pozycja w kolejce", value=str(len(player.queue)), inline=True)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.set_footer(text=f"Dodane przez: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)
    else:
        # Playlist / Album
        for s in songs:
            player.queue.append(s)
        total_time = sum(s.duration for s in songs)
        cmd_logger.info(
            f"📚 [USER: {interaction.user}] Dodano playlistę do kolejki: {len(songs)} utworów ({result_type}) "
            f"| Łączny czas: {format_duration(total_time)} | Łącznie w kolejce: {len(player.queue)}"
        )
        embed = discord.Embed(
            title="📚 Dodano playlistę do kolejki",
            description=f"Załadowano **{len(songs)}** utworów ({result_type})",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Łączny czas", value=format_duration(total_time), inline=True)
        embed.add_field(name="Łącznie w kolejce", value=str(len(player.queue)), inline=True)
        if songs[0].thumbnail:
            embed.set_thumbnail(url=songs[0].thumbnail)
        embed.set_footer(text=f"Dodane przez: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="skip", description="Przewija bieżący utwór (lub kilka utworów).")
@app_commands.describe(ile="Liczba utworów do pominięcia (domyślnie 1)")
async def skip_cmd(interaction: discord.Interaction, ile: int = 1):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /skip (ile={ile})")
    player = get_or_create_player(interaction.guild)
    if not player.current_song:
        cmd_logger.info(f"ℹ️ [USER: {interaction.user}] /skip: Nic aktualnie nie jest odtwarzane.")
        return await interaction.response.send_message("❌ Nic aktualnie nie jest odtwarzane.", ephemeral=True)

    count = max(1, ile)
    cur_title = player.current_song.title
    skipped = player.skip(count=count)
    cmd_logger.info(f"⏭️ [USER: {interaction.user}] Pomyślnie pominięto {skipped} utwór(y) (bieżący: '{cur_title}').")
    await interaction.response.send_message(f"⏭️ Przewinięto {skipped} utwór(y)!")


@bot.tree.command(name="queue", description="Wyświetla aktualnie grany utwór oraz listę kolejki.")
@app_commands.describe(strona="Numer strony kolejki do wyświetlenia")
async def queue_cmd(interaction: discord.Interaction, strona: int = 1):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /queue (strona={strona})")
    player = get_or_create_player(interaction.guild)
    embed = player.create_queue_embed(page=strona)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Wyświetla informacje o aktualnie odtwarzanym utworze z przyciskami.")
async def nowplaying_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /nowplaying")
    player = get_or_create_player(interaction.guild)
    if not player.current_song:
        cmd_logger.info(f"ℹ️ [USER: {interaction.user}] /nowplaying: Kolejka pusta, nic nie gra.")
        return await interaction.response.send_message("❌ Nic aktualnie nie jest odtwarzane.", ephemeral=True)

    cmd_logger.info(f"🎶 [USER: {interaction.user}] Aktualnie odtwarzany utwór: '{player.current_song.title}'")
    embed = player.create_now_playing_embed(player.current_song)
    view = MusicControlView(player)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="volume", description="Zmienia głośność odtwarzacza (0-100%).")
@app_commands.describe(poziom="Poziom głośności w procentach (od 0 do 100)")
async def volume_cmd(interaction: discord.Interaction, poziom: int):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /volume -> zadana wartość: {poziom}%")
    if poziom < 0 or poziom > 100:
        cmd_logger.warning(f"⚠️ [USER: {interaction.user}] Nieprawidłowa głośność: {poziom}% (musi być 0-100).")
        return await interaction.response.send_message("❌ Podaj wartość z zakresu od 0 do 100.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    player.set_volume(poziom)
    cmd_logger.info(f"🔊 [USER: {interaction.user}] Zaktualizowano głośność do {poziom}%.")
    await interaction.response.send_message(f"🔊 Głośność ustawiona na: **{poziom}%**")


@bot.tree.command(name="pause", description="Wstrzymuje odtwarzanie muzyki.")
async def pause_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /pause")
    player = get_or_create_player(interaction.guild)
    if not player.voice_client or not player.voice_client.is_playing():
        cmd_logger.info(f"ℹ️ [USER: {interaction.user}] /pause: Brak aktywnego odtwarzania.")
        return await interaction.response.send_message("❌ Nic aktualnie nie gra.", ephemeral=True)

    player.voice_client.pause()
    cmd_logger.info(f"⏸️ [USER: {interaction.user}] Wstrzymano odtwarzanie.")
    await interaction.response.send_message("⏸️ Wstrzymano odtwarzanie.")


@bot.tree.command(name="resume", description="Wznawia odtwarzanie muzyki.")
async def resume_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /resume")
    player = get_or_create_player(interaction.guild)
    if not player.voice_client or not player.voice_client.is_paused():
        cmd_logger.info(f"ℹ️ [USER: {interaction.user}] /resume: Odtwarzacz nie jest wstrzymany.")
        return await interaction.response.send_message("❌ Odtwarzacz nie jest wstrzymany.", ephemeral=True)

    player.voice_client.resume()
    cmd_logger.info(f"▶️ [USER: {interaction.user}] Wznowiono odtwarzanie.")
    await interaction.response.send_message("▶️ Wznowiono odtwarzanie.")


@bot.tree.command(name="stop", description="Zatrzymuje odtwarzacz i czyści kolejkę.")
async def stop_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /stop")
    player = get_or_create_player(interaction.guild)
    player.stop()
    cmd_logger.info(f"⏹️ [USER: {interaction.user}] Zatrzymano odtwarzacz i wyczyszczono kolejkę.")
    await interaction.response.send_message("⏹️ Zatrzymano muzykę i wyczyszczono kolejkę.")


@bot.tree.command(name="shuffle", description="Miesza kolejność utworów w kolejce.")
async def shuffle_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /shuffle")
    player = get_or_create_player(interaction.guild)
    if len(player.queue) < 2:
        cmd_logger.info(f"ℹ️ [USER: {interaction.user}] Za mało utworów w kolejce do wymieszania ({len(player.queue)}).")
        return await interaction.response.send_message("❌ W kolejce musi być przynajmniej 2 utwory, aby ją wymieszać.", ephemeral=True)

    player.shuffle()
    cmd_logger.info(f"🔀 [USER: {interaction.user}] Wymieszano {len(player.queue)} utworów w kolejce.")
    await interaction.response.send_message(f"🔀 Wymieszano kolejność **{len(player.queue)}** utworów w kolejce!")


@bot.tree.command(name="remove", description="Usuwa konkretny utwór z kolejki według jego numeru.")
@app_commands.describe(numer="Numer utworu z komendy /queue")
async def remove_cmd(interaction: discord.Interaction, numer: int):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /remove -> pozycja {numer}")
    player = get_or_create_player(interaction.guild)
    removed = player.remove(numer)
    if removed:
        cmd_logger.info(f"🗑️ [USER: {interaction.user}] Usunięto z kolejki: '{removed.title}'")
        await interaction.response.send_message(f"🗑️ Usunięto z kolejki: **{removed.title}**")
    else:
        cmd_logger.warning(f"⚠️ [USER: {interaction.user}] Nie znaleziono utworu o numerze {numer} w kolejce.")
        await interaction.response.send_message(f"❌ Nie znaleziono utworu o numerze {numer}.", ephemeral=True)


@bot.tree.command(name="clear", description="Czyści całą kolejkę (nie przerywając aktualnie granego utworu).")
async def clear_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /clear")
    player = get_or_create_player(interaction.guild)
    count = len(player.queue)
    player.queue.clear()
    cmd_logger.info(f"🧹 [USER: {interaction.user}] Wyczyszczono {count} utworów z kolejki.")
    await interaction.response.send_message(f"🧹 Wyczyszczono {count} utworów z kolejki.")


@bot.tree.command(name="loop", description="Ustawia tryb powtarzania.")
@app_commands.describe(tryb="Wybierz: wyłączone (off), bieżący utwór (song), cała kolejka (queue)")
async def loop_cmd(interaction: discord.Interaction, tryb: Literal["off", "song", "queue"]):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    cmd_logger.info(f"👤 [USER: {interaction.user}] Wywołał /loop -> nowy tryb: {tryb}")
    player = get_or_create_player(interaction.guild)
    player.loop_mode = tryb
    labels = {
        "off": "❌ Wyłączona",
        "song": "🔂 Powtarzanie utworu",
        "queue": "🔁 Powtarzanie kolejki",
    }
    await interaction.response.send_message(f"Ustawiono tryb pętli: **{labels[tryb]}**")


# ==========================================
# RUN
# ==========================================

if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        logger.critical("[BŁĄD] Wprowadź token bota w pliku .env i spróbuj ponownie.")
        sys.exit(1)

    logger.info("🚀 Uruchamianie bota muzycznego...")
    bot.run(config.DISCORD_TOKEN)
