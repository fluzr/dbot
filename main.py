import asyncio
import os
import sys
from typing import Optional, Literal
import discord
from discord import app_commands
from discord.ext import commands

import config
from music_source import MusicSourceManager, Song, format_duration
from player import GuildMusicPlayer, MusicControlView

# Validate config before running
config_errors = config.validate_config()
if config_errors:
    print("\n" + "=" * 60)
    print("BŁĘDY KONFIGURACJI (.env):")
    for err in config_errors:
        print(f" - {err}")
    print("=" * 60 + "\n")

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
source_manager = MusicSourceManager()
music_player: Optional[GuildMusicPlayer] = None


def get_or_create_player(guild: discord.Guild) -> GuildMusicPlayer:
    global music_player
    if music_player is None:
        music_player = GuildMusicPlayer(bot, guild.id)
        music_player.start()
    return music_player


# Check for single-server restriction
def guild_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if config.DISCORD_GUILD_ID and interaction.guild.id != config.DISCORD_GUILD_ID:
        return False
    return True


@bot.event
async def on_ready():
    print(f"\n✅ Zalogowano jako: {bot.user} (ID: {bot.user.id})")
    
    # Sync slash commands for the specified guild (instant sync!)
    if config.DISCORD_GUILD_ID:
        guild_obj = discord.Object(id=config.DISCORD_GUILD_ID)
        try:
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"🚀 Zsynchronizowano {len(synced)} komend slash dla serwera ID: {config.DISCORD_GUILD_ID}")
        except Exception as e:
            print(f"❌ Błąd podczas synchronizacji komend slash: {e}")
    else:
        print("⚠️ Brak DISCORD_GUILD_ID w konfiguracji. Komendy nie zostały zsynchronizowane.")

    activity = discord.Activity(type=discord.ActivityType.listening, name="/play | Czekolada, smaczne orzechy")
    await bot.change_presence(activity=activity)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    global music_player
    # If the bot itself was disconnected from voice channel
    if member == bot.user and before.channel is not None and after.channel is None:
        if music_player:
            music_player.stop()
            music_player.voice_client = None


# Helper to ensure user is in voice and connect bot
async def ensure_voice_connection(
    interaction: discord.Interaction,
) -> Optional[discord.VoiceClient]:
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Nie można określić Twojego profilu użytkownika.", ephemeral=True)
        return None

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ Musisz być na kanale głosowym, aby użyć tej komendy!", ephemeral=True)
        return None

    user_channel = interaction.user.voice.channel
    player = get_or_create_player(interaction.guild)
    player.text_channel = interaction.channel

    # Check bot permissions
    permissions = user_channel.permissions_for(interaction.guild.me)
    if not permissions.connect or not permissions.speak:
        await interaction.followup.send(
            f"❌ Bot nie ma uprawnień do połączenia lub mówienia na kanale {user_channel.mention}!",
            ephemeral=True,
        )
        return None

    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_client = await user_channel.connect()
        player.voice_client = voice_client
    elif voice_client.channel != user_channel:
        # Move to user channel if empty or requested
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

    await interaction.response.defer()
    target_channel = kanal or (interaction.user.voice.channel if interaction.user.voice else None)
    if not target_channel:
        return await interaction.followup.send("❌ Musisz być na kanale głosowym lub wskazać kanał w parametrze!", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    player.text_channel = interaction.channel

    voice_client = interaction.guild.voice_client
    if voice_client:
        if voice_client.channel == target_channel:
            return await interaction.followup.send(f"Jestem już na kanale {target_channel.mention}.")
        await voice_client.move_to(target_channel)
        player.voice_client = voice_client
        return await interaction.followup.send(f"Przeniesiono na kanał {target_channel.mention}.")

    voice_client = await target_channel.connect()
    player.voice_client = voice_client
    await interaction.followup.send(f"🔊 Połączono z {target_channel.mention}!")


@bot.tree.command(name="leave", description="Rozłącza bota i czyści kolejkę.")
async def leave_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    await interaction.response.defer()
    player = get_or_create_player(interaction.guild)
    if not interaction.guild.voice_client:
        return await interaction.followup.send("❌ Bot nie znajduje się na żadnym kanale głosowym.")

    await player.disconnect()
    await interaction.followup.send("👋 Rozłączono z kanału i wyczyszczono kolejkę.")


@bot.tree.command(name="play", description="Odtwarza utwór lub playlistę z YouTube, Spotify lub SoundCloud.")
@app_commands.describe(szukaj="Tytuł utworu lub link (YouTube, Spotify, SoundCloud)")
async def play_cmd(interaction: discord.Interaction, szukaj: str):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    await interaction.response.defer()

    # Ensure voice connection
    voice_client = await ensure_voice_connection(interaction)
    if not voice_client:
        return

    player = get_or_create_player(interaction.guild)
    player.voice_client = voice_client
    player.text_channel = interaction.channel

    # Extract songs
    try:
        songs, result_type = await source_manager.get_songs(szukaj, requester=interaction.user)
    except Exception as e:
        return await interaction.followup.send(f"❌ Błąd podczas wyszukiwania: `{e}`")

    if not songs:
        return await interaction.followup.send("❌ Nie znaleziono żadnych utworów.")

    # Add songs to player queue
    if len(songs) == 1:
        song = songs[0]
        player.queue.append(song)
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

    player = get_or_create_player(interaction.guild)
    if not player.current_song:
        return await interaction.response.send_message("❌ Nic aktualnie nie jest odtwarzane.", ephemeral=True)

    count = max(1, ile)
    skipped = player.skip(count=count)
    await interaction.response.send_message(f"⏭️ Przewinięto {skipped} utwór(y)!")


@bot.tree.command(name="queue", description="Wyświetla aktualnie grany utwór oraz listę kolejki.")
@app_commands.describe(strona="Numer strony kolejki do wyświetlenia")
async def queue_cmd(interaction: discord.Interaction, strona: int = 1):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    embed = player.create_queue_embed(page=strona)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Wyświetla informacje o aktualnie odtwarzanym utworze z przyciskami.")
async def nowplaying_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    if not player.current_song:
        return await interaction.response.send_message("❌ Nic aktualnie nie jest odtwarzane.", ephemeral=True)

    embed = player.create_now_playing_embed(player.current_song)
    view = MusicControlView(player)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="volume", description="Zmienia głośność odtwarzacza (0-100%).")
@app_commands.describe(poziom="Poziom głośności w procentach (od 0 do 100)")
async def volume_cmd(interaction: discord.Interaction, poziom: int):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    if poziom < 0 or poziom > 100:
        return await interaction.response.send_message("❌ Podaj wartość z zakresu od 0 do 100.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    player.set_volume(poziom)
    await interaction.response.send_message(f"🔊 Głośność ustawiona na: **{poziom}%**")


@bot.tree.command(name="pause", description="Wstrzymuje odtwarzanie muzyki.")
async def pause_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    if not player.voice_client or not player.voice_client.is_playing():
        return await interaction.response.send_message("❌ Nic aktualnie nie gra.", ephemeral=True)

    player.voice_client.pause()
    await interaction.response.send_message("⏸️ Wstrzymano odtwarzanie.")


@bot.tree.command(name="resume", description="Wznawia odtwarzanie muzyki.")
async def resume_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    if not player.voice_client or not player.voice_client.is_paused():
        return await interaction.response.send_message("❌ Odtwarzacz nie jest wstrzymany.", ephemeral=True)

    player.voice_client.resume()
    await interaction.response.send_message("▶️ Wznowiono odtwarzanie.")


@bot.tree.command(name="stop", description="Zatrzymuje odtwarzacz i czyści kolejkę.")
async def stop_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    player.stop()
    await interaction.response.send_message("⏹️ Zatrzymano muzykę i wyczyszczono kolejkę.")


@bot.tree.command(name="shuffle", description="Miesza kolejność utworów w kolejce.")
async def shuffle_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    if len(player.queue) < 2:
        return await interaction.response.send_message("❌ W kolejce musi być przynajmniej 2 utwory, aby ją wymieszać.", ephemeral=True)

    player.shuffle()
    await interaction.response.send_message(f"🔀 Wymieszano kolejność **{len(player.queue)}** utworów w kolejce!")


@bot.tree.command(name="remove", description="Usuwa konkretny utwór z kolejki według jego numeru.")
@app_commands.describe(numer="Numer utworu z komendy /queue")
async def remove_cmd(interaction: discord.Interaction, numer: int):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    removed = player.remove(numer)
    if removed:
        await interaction.response.send_message(f"🗑️ Usunięto z kolejki: **{removed.title}**")
    else:
        await interaction.response.send_message(f"❌ Nie znaleziono utworu o numerze {numer}.", ephemeral=True)


@bot.tree.command(name="clear", description="Czyści całą kolejkę (nie przerywając aktualnie granego utworu).")
async def clear_cmd(interaction: discord.Interaction):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

    player = get_or_create_player(interaction.guild)
    count = len(player.queue)
    player.queue.clear()
    await interaction.response.send_message(f"🧹 Wyczyszczono {count} utworów z kolejki.")


@bot.tree.command(name="loop", description="Ustawia tryb powtarzania.")
@app_commands.describe(tryb="Wybierz: wyłączone (off), bieżący utwór (song), cała kolejka (queue)")
async def loop_cmd(interaction: discord.Interaction, tryb: Literal["off", "song", "queue"]):
    if not guild_check(interaction):
        return await interaction.response.send_message("Ta komenda nie jest dostępna na tym serwerze.", ephemeral=True)

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
        print("[BŁĄD] Wprowadź token bota w pliku .env i spróbuj ponownie.")
        sys.exit(1)

    print("Uruchamianie bota muzycznego...")
    bot.run(config.DISCORD_TOKEN)
