import os
import sys
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID_RAW = os.getenv("DISCORD_GUILD_ID", "").strip()
DISCORD_GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else None

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

try:
    DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME", "50"))
except ValueError:
    DEFAULT_VOLUME = 50

try:
    AUTO_LEAVE_SECONDS = int(os.getenv("AUTO_LEAVE_SECONDS", "300"))
except ValueError:
    AUTO_LEAVE_SECONDS = 300

# Lavalink settings
LAVALINK_URI = os.getenv("LAVALINK_URI", "http://127.0.0.1:2333").strip()
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass").strip()
LAVALINK_ENABLED = os.getenv("LAVALINK_ENABLED", "true").lower() in ("true", "1", "yes")


def validate_config():
    errors = []
    if not DISCORD_TOKEN or DISCORD_TOKEN == "twoj_discord_token_tutaj":
        errors.append("Brak DISCORD_TOKEN w pliku .env! Uzupełnij go przed uruchomieniem bota.")
    if not DISCORD_GUILD_ID:
        errors.append("Brak poprawnego DISCORD_GUILD_ID (ID serwera) w pliku .env! Bot ma działać tylko na jednym serwerze.")
    return errors
