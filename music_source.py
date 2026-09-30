import asyncio
import os
import re
import shutil
from typing import Optional, List, Dict, Any, Tuple
import yt_dlp
import discord
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
import config

def get_ffmpeg_path() -> str:
    """Finds the ffmpeg executable, checking PATH and common WinGet install locations."""
    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return which_ffmpeg
    
    # Check winget package directories if PATH was not yet reloaded in the current terminal session
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_pkgs = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_pkgs):
            for root, _, files in os.walk(winget_pkgs):
                if "ffmpeg.exe" in files:
                    return os.path.join(root, "ffmpeg.exe")
    
    return "ffmpeg"


YTDL_BASE_OPTIONS = {
    "format": "bestaudio/best",
    "extractor_args": {"youtube": ["player_client=ios"]},
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -timeout 300000000",
    "options": "-vn",
}


def format_duration(seconds: Optional[int]) -> str:
    """Formats seconds into MM:SS or HH:MM:SS."""
    if not seconds or seconds < 0:
        return "Na żywo / Nieznany"
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class Song:
    def __init__(
        self,
        title: str,
        web_url: str,
        duration: int = 0,
        thumbnail: Optional[str] = None,
        uploader: Optional[str] = None,
        requester: Optional[discord.Member] = None,
        stream_url: Optional[str] = None,
        source_type: str = "youtube",
        search_query: Optional[str] = None,
    ):
        self.title = title
        self.web_url = web_url
        self.duration = duration
        self.thumbnail = thumbnail
        self.uploader = uploader
        self.requester = requester
        self.stream_url = stream_url
        self.source_type = source_type
        self.search_query = search_query

    @property
    def formatted_duration(self) -> str:
        return format_duration(self.duration)

    async def resolve_stream(self) -> None:
        """Resolves the direct audio stream URL if not already known."""
        if self.stream_url:
            return

        loop = asyncio.get_running_loop()
        query = self.search_query or self.web_url

        def extract():
            opts = dict(YTDL_BASE_OPTIONS)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
                if "entries" in info and info["entries"]:
                    return info["entries"][0]
                return info

        data = await loop.run_in_executor(None, extract)
        if not data:
            raise RuntimeError(f"Nie udało się uzyskać strumienia audio dla: {self.title}")

        # Update metadata if resolved from search
        self.stream_url = data.get("url")
        if not self.title or self.title == "Wyszukiwanie...":
            self.title = data.get("title", self.title)
        if not self.duration:
            self.duration = int(data.get("duration", 0))
        if not self.thumbnail:
            self.thumbnail = data.get("thumbnail")
        if not self.uploader:
            self.uploader = data.get("uploader") or data.get("channel")

    def create_audio_source(self, volume: float = 0.5) -> discord.AudioSource:
        """Creates an FFmpegPCMAudio wrapped in PCMVolumeTransformer."""
        if not self.stream_url:
            raise RuntimeError("Stream URL nie został zainicjalizowany przed odtwarzaniem!")

        ffmpeg_bin = get_ffmpeg_path()
        audio = discord.FFmpegPCMAudio(
            self.stream_url,
            executable=ffmpeg_bin,
            before_options=FFMPEG_OPTIONS["before_options"],
            options=FFMPEG_OPTIONS["options"],
        )
        return discord.PCMVolumeTransformer(audio, volume=volume)


class MusicSourceManager:
    def __init__(self):
        self.spotify: Optional[Spotify] = None
        if config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET:
            try:
                auth = SpotifyClientCredentials(
                    client_id=config.SPOTIFY_CLIENT_ID,
                    client_secret=config.SPOTIFY_CLIENT_SECRET,
                )
                self.spotify = Spotify(auth_manager=auth)
            except Exception as e:
                print(f"[OSTRZEŻENIE] Błąd inicjalizacji Spotify API: {e}")

    def is_spotify_url(self, query: str) -> bool:
        return "spotify.com" in query.lower()

    def is_soundcloud_url(self, query: str) -> bool:
        return "soundcloud.com" in query.lower()

    def is_url(self, query: str) -> bool:
        return query.startswith("http://") or query.startswith("https://")

    async def parse_spotify(
        self, url: str, requester: Optional[discord.Member]
    ) -> Tuple[List[Song], str]:
        """Parses track, album, or playlist from Spotify."""
        if not self.spotify:
            raise ValueError(
                "Spotify nie jest skonfigurowane! Aby odtwarzać utwory ze Spotify, "
                "wprowadź `SPOTIFY_CLIENT_ID` oraz `SPOTIFY_CLIENT_SECRET` w pliku `.env`."
            )

        loop = asyncio.get_running_loop()

        # Extract Spotify ID and type
        track_match = re.search(r"spotify\.com/track/([a-zA-Z0-9]+)", url)
        playlist_match = re.search(r"spotify\.com/playlist/([a-zA-Z0-9]+)", url)
        album_match = re.search(r"spotify\.com/album/([a-zA-Z0-9]+)", url)

        if track_match:
            track_id = track_match.group(1)
            track_data = await loop.run_in_executor(None, lambda: self.spotify.track(track_id))
            artists = ", ".join(a["name"] for a in track_data.get("artists", []))
            title = track_data.get("name", "Nieznany utwór")
            search_query = f"{title} {artists}"
            thumbnail = None
            if track_data.get("album", {}).get("images"):
                thumbnail = track_data["album"]["images"][0]["url"]

            duration = int(track_data.get("duration_ms", 0) / 1000)
            song = Song(
                title=f"{artists} - {title}",
                web_url=track_data.get("external_urls", {}).get("spotify", url),
                duration=duration,
                thumbnail=thumbnail,
                uploader=artists,
                requester=requester,
                source_type="spotify",
                search_query=search_query,
            )
            return [song], "track"

        elif album_match:
            album_id = album_match.group(1)
            album_data = await loop.run_in_executor(None, lambda: self.spotify.album(album_id))
            album_title = album_data.get("name", "Album")
            thumbnail = album_data["images"][0]["url"] if album_data.get("images") else None
            songs: List[Song] = []

            for item in album_data.get("tracks", {}).get("items", []):
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                title = item.get("name", "")
                duration = int(item.get("duration_ms", 0) / 1000)
                search_query = f"{title} {artists}"
                songs.append(
                    Song(
                        title=f"{artists} - {title}",
                        web_url=item.get("external_urls", {}).get("spotify", url),
                        duration=duration,
                        thumbnail=thumbnail,
                        uploader=artists,
                        requester=requester,
                        source_type="spotify",
                        search_query=search_query,
                    )
                )
            return songs, f"album: {album_title}"

        elif playlist_match:
            playlist_id = playlist_match.group(1)
            playlist_data = await loop.run_in_executor(
                None, lambda: self.spotify.playlist(playlist_id)
            )
            playlist_name = playlist_data.get("name", "Playlista")
            thumbnail = playlist_data["images"][0]["url"] if playlist_data.get("images") else None

            # Fetch items (up to 100 or pagination)
            tracks_result = playlist_data.get("tracks", {})
            items = list(tracks_result.get("items", []))
            
            # Fetch remaining items if any (limit to max 200 to prevent huge memory overhead)
            while tracks_result.get("next") and len(items) < 200:
                tracks_result = await loop.run_in_executor(
                    None, lambda: self.spotify.next(tracks_result)
                )
                items.extend(tracks_result.get("items", []))

            songs: List[Song] = []
            for entry in items:
                track = entry.get("track")
                if not track or not track.get("name"):
                    continue
                artists = ", ".join(a["name"] for a in track.get("artists", []))
                title = track.get("name", "")
                duration = int(track.get("duration_ms", 0) / 1000)
                track_thumb = thumbnail
                if track.get("album", {}).get("images"):
                    track_thumb = track["album"]["images"][0]["url"]

                search_query = f"{title} {artists}"
                songs.append(
                    Song(
                        title=f"{artists} - {title}",
                        web_url=track.get("external_urls", {}).get("spotify", url),
                        duration=duration,
                        thumbnail=track_thumb,
                        uploader=artists,
                        requester=requester,
                        source_type="spotify",
                        search_query=search_query,
                    )
                )
            return songs, f"playlista: {playlist_name}"

        else:
            raise ValueError("Nieobsługiwany format linku Spotify! Podaj link do utworu, playlisty lub albumu.")

    async def parse_ytdlp(
        self, query: str, requester: Optional[discord.Member]
    ) -> Tuple[List[Song], str]:
        """Parses YouTube / SoundCloud link, search query, or playlist via yt-dlp."""
        loop = asyncio.get_running_loop()

        is_link = self.is_url(query)
        is_search = not is_link

        # Use extract_flat to avoid freezing when encountering big playlists
        def extract():
            opts = {
                "format": "bestaudio/best",
                "extractor_args": {"youtube": ["player_client=ios"]},
                "extract_flat": "in_playlist",
                "noplaylist": False,
                "nocheckcertificate": True,
                "ignoreerrors": False,
                "quiet": False,
                "no_warnings": True,
                "default_search": "ytsearch1" if is_search else "auto",
            }
            target_query = f"ytsearch1:{query}" if is_search else query
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(target_query, download=False)

        data = await loop.run_in_executor(None, extract)
        if not data:
            raise ValueError("Nie znaleziono wyników dla podanego zapytania.")

        # Check if it's a playlist or search result list
        if "entries" in data and data["entries"]:
            entries = [e for e in data["entries"] if e]
            if not entries:
                raise ValueError("Brak dostępnych utworów.")

            if is_search or len(entries) == 1:
                # Single search result
                entry = entries[0]
                url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry.get('id')}"
                song = Song(
                    title=entry.get("title", "Nieznany tytuł"),
                    web_url=url,
                    duration=int(entry.get("duration", 0) or 0),
                    thumbnail=entry.get("thumbnail"),
                    uploader=entry.get("uploader") or entry.get("channel"),
                    requester=requester,
                    source_type="soundcloud" if "soundcloud" in url else "youtube",
                    search_query=url,
                )
                return [song], "track"
            else:
                # Full playlist / set
                playlist_title = data.get("title", "Playlista")
                songs: List[Song] = []
                for entry in entries:
                    url = entry.get("url") or entry.get("webpage_url")
                    if not url and entry.get("id"):
                        url = f"https://www.youtube.com/watch?v={entry.get('id')}"
                    if not url:
                        continue

                    songs.append(
                        Song(
                            title=entry.get("title", "Utwór z playlisty"),
                            web_url=url,
                            duration=int(entry.get("duration", 0) or 0),
                            thumbnail=entry.get("thumbnail"),
                            uploader=entry.get("uploader") or entry.get("channel"),
                            requester=requester,
                            source_type="soundcloud" if "soundcloud" in url else "youtube",
                            search_query=url,
                        )
                    )
                return songs, f"playlista: {playlist_title}"
        else:
            # Single direct link
            url = data.get("webpage_url") or data.get("url") or query
            song = Song(
                title=data.get("title", "Nieznany utwór"),
                web_url=url,
                duration=int(data.get("duration", 0) or 0),
                thumbnail=data.get("thumbnail"),
                uploader=data.get("uploader") or data.get("channel"),
                requester=requester,
                stream_url=data.get("url") if data.get("is_live") or not data.get("extractor", "").startswith("youtube") else None,
                source_type="soundcloud" if "soundcloud" in url else "youtube",
                search_query=url,
            )
            return [song], "track"

    async def get_songs(
        self, query: str, requester: Optional[discord.Member]
    ) -> Tuple[List[Song], str]:
        """Extracts songs from Spotify, YouTube, SoundCloud, or search query."""
        query = query.strip()
        if self.is_spotify_url(query):
            return await self.parse_spotify(query, requester)
        else:
            return await self.parse_ytdlp(query, requester)
