import asyncio
import logging
import os
import re
import shutil
from typing import Optional, List, Dict, Any, Tuple
import yt_dlp
import discord
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
import config

logger = logging.getLogger("mbot.source")

# Timeout for yt-dlp operations (seconds)
YTDL_TIMEOUT = 30


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


def _get_cookie_path() -> Optional[str]:
    """Returns the cookie file path if it exists and is non-empty, otherwise None."""
    cookie_env = os.environ.get("YTDL_COOKIES_PATH")
    if cookie_env and os.path.isfile(cookie_env) and os.path.getsize(cookie_env) > 0:
        return cookie_env

    # Try directory of the current script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_cookie = os.path.join(script_dir, "cookies.txt")
    if os.path.isfile(local_cookie) and os.path.getsize(local_cookie) > 0:
        return local_cookie

    if os.path.isfile("cookies.txt") and os.path.getsize("cookies.txt") > 0:
        return "cookies.txt"

    return None


def get_ytdl_base_options(use_cookies: bool = True) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "restrictfilenames": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "logtostderr": False,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        # Allow all content — no age restriction (18+ requires valid cookies or android_vr client)
        "age_limit": None,
        "extractor_args": {
            "youtube": {
                # 'web' — uses cookies for age-gate authentication
                # 'android_vr' — bypasses age verification even without cookies
                # 'ios', 'mweb' — reliable mobile clients
                "player_client": ["web", "android_vr", "ios", "mweb"],
            }
        },
        "socket_timeout": 15,
        "retries": 3,
    }
    if use_cookies:
        cookie_path = _get_cookie_path()
        if cookie_path:
            opts["cookiefile"] = cookie_path
            logger.debug(f"[YTDL] Użyto pliku ciasteczek: {cookie_path}")
    return opts


FFMPEG_OPTIONS = {
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
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
        isrc: Optional[str] = None,
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
        self.isrc = isrc
        self.http_headers: Dict[str, str] = {}

    @property
    def formatted_duration(self) -> str:
        return format_duration(self.duration)

    def invalidate_stream(self) -> None:
        """Clears cached stream URL so it will be re-resolved before next play.

        Called before each playback to avoid stale/expired YouTube stream URLs
        (they expire after ~6 hours).
        """
        self.stream_url = None
        self.http_headers = {}

    async def resolve_stream(self) -> None:
        """Resolves the direct audio stream URL with timeout and smart fallback."""
        if self.stream_url:
            logger.debug(f"[RESOLVE] Utwór '{self.title}' posiada już pobrany stream URL.")
            return

        loop = asyncio.get_running_loop()
        query = self.search_query or self.web_url
        logger.info(f"⏳ [RESOLVE] Pobieranie linku audio dla: '{self.title}' (query: '{query}')")

        def extract():
            # 1. SPECIAL CASE: Spotify track or playlist item needing smart duration matching
            is_direct_url = query.startswith("http://") or query.startswith("https://")
            if self.source_type == "spotify" and not is_direct_url:
                logger.info(f"🔍 [RESOLVE-SPOTIFY] Dopasowywanie utworu Spotify: '{self.title}' (czas: {self.duration}s)...")
                clean_title = re.sub(r"[\[\(].*?[\]\)]", "", self.title).strip()
                sp_candidates = [
                    f"ytsearch5:{clean_title} official audio",
                    f"ytsearch5:{query}",
                ]
                sp_opts = get_ytdl_base_options(use_cookies=False)
                sp_opts["noplaylist"] = True

                with yt_dlp.YoutubeDL(sp_opts) as ydl_sp:
                    for sp_q in sp_candidates:
                        try:
                            sp_info = ydl_sp.extract_info(sp_q, download=False)
                            if sp_info and "entries" in sp_info and sp_info["entries"]:
                                valid_entries = [e for e in sp_info["entries"] if e and (e.get("url") or e.get("webpage_url"))]
                                if not valid_entries:
                                    continue

                                # Check exact duration match (<= 3s diff)
                                if self.duration > 0:
                                    for ent in valid_entries:
                                        dur = int(ent.get("duration", 0) or 0)
                                        if dur > 0 and abs(dur - self.duration) <= 3:
                                            logger.info(f"✅ [RESOLVE-SPOTIFY] Dokładne dopasowanie ({dur}s vs {self.duration}s): '{ent.get('title')}'")
                                            return ent

                                    # Check close match (<= 12s diff)
                                    close_list = [
                                        ent for ent in valid_entries
                                        if ent.get("duration") and abs(int(ent["duration"]) - self.duration) <= 12
                                    ]
                                    if close_list:
                                        best_ent = min(close_list, key=lambda e: abs(int(e["duration"]) - self.duration))
                                        logger.info(f"✅ [RESOLVE-SPOTIFY] Przybliżone dopasowanie ({best_ent.get('duration')}s vs {self.duration}s): '{best_ent.get('title')}'")
                                        return best_ent

                                # Fallback to first available entry
                                return valid_entries[0]
                        except Exception as e:
                            logger.warning(f"⚠️ [RESOLVE-SPOTIFY] Błąd przy '{sp_q}': {e}")
                            continue

            # 2. STANDARD / YOUTUBE EXTRACTION WITH 18+ SUPPORT
            attempts: List[Tuple[str, Dict[str, Any], str]] = []

            # 2a. Direct with cookies (authenticates age-restricted content if user account is 18+)
            if _get_cookie_path():
                attempts.append(("bezpośrednie (z cookies)", get_ytdl_base_options(use_cookies=True), query))

            # 2b. android_vr client (known to bypass YouTube age-gate even without cookies)
            opts_vr = get_ytdl_base_options(use_cookies=False)
            opts_vr["extractor_args"] = {
                "youtube": {
                    "player_client": ["android_vr", "mweb", "android"],
                }
            }
            attempts.append(("android_vr bypass (18+)", opts_vr, query))

            # 2c. Direct extraction without cookies
            attempts.append(("bezpośrednie (bez cookies)", get_ytdl_base_options(use_cookies=False), query))

            # 2d. Fallback searches for blocked videos
            fallback_queries: List[str] = []
            vid_match = re.search(r"(?:v=|\/|be\/)([0-9A-Za-z_-]{11})", query)
            if vid_match:
                vid_id = vid_match.group(1)
                fallback_queries.append(f"ytsearch1:{vid_id}")

            if self.title and self.title not in ("Wyszukiwanie...", "Nieznany utwór", "Utwór z playlisty"):
                clean_title = re.sub(r"[\[\(].*?[\]\)]", "", self.title).strip()
                if clean_title:
                    fallback_queries.append(f"ytsearch1:{clean_title} audio")
                fallback_queries.append(f"ytsearch1:{self.title}")
                fallback_queries.append(f"scsearch1:{clean_title or self.title}")

            for fb_q in fallback_queries:
                attempts.append((f"fallback ({fb_q})", get_ytdl_base_options(use_cookies=False), fb_q))

            last_error = None
            for label, opts, q in attempts:
                try:
                    logger.info(f"🔄 [RESOLVE] Próba: {label}")
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(q, download=False)
                        if info and "entries" in info and info["entries"]:
                            entry = info["entries"][0]
                            if entry and entry.get("url"):
                                logger.info(f"✅ [RESOLVE] Sukces: {label} -> '{entry.get('title')}'")
                                return entry
                        if info and info.get("url"):
                            logger.info(f"✅ [RESOLVE] Sukces: {label} -> '{info.get('title')}'")
                            return info
                except Exception as e:
                    last_error = e
                    logger.warning(f"⚠️ [RESOLVE] {label} nie powiodło się: {e}")
                    continue

            raise RuntimeError(
                f"Wszystkie metody pobrania strumienia zawiodły dla: '{self.title}'"
                + (f" (ostatni błąd: {last_error})" if last_error else "")
            )

        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, extract),
                timeout=YTDL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Timeout ({YTDL_TIMEOUT}s): yt-dlp nie odpowiada dla '{self.title}'")

        if not data or not data.get("url"):
            err_msg = f"Nie udało się uzyskać URL strumienia audio dla utworu: {self.title}"
            logger.error(f"❌ [RESOLVE] {err_msg}")
            raise RuntimeError(err_msg)

        # Update metadata if resolved from search / fallback
        self.stream_url = data.get("url")
        self.http_headers = data.get("http_headers") or {}
        if not self.title or self.title in ("Wyszukiwanie...", "Nieznany utwór"):
            self.title = data.get("title", self.title)
        if not self.duration:
            self.duration = int(data.get("duration", 0) or 0)
        if not self.thumbnail:
            self.thumbnail = data.get("thumbnail")
        if not self.uploader:
            self.uploader = data.get("uploader") or data.get("channel")

        stream_domain = self.stream_url.split('/')[2] if '://' in self.stream_url else 'nieznana'
        logger.info(f"🎉 [RESOLVE] Gotowy strumień: '{self.title}' [{self.formatted_duration}] | Serwer: {stream_domain}")

    def create_audio_source(self, volume: float = 0.5) -> discord.AudioSource:
        """Creates an FFmpegPCMAudio wrapped in PCMVolumeTransformer."""
        if not self.stream_url:
            raise RuntimeError("Stream URL nie został zainicjalizowany przed odtwarzaniem!")

        ffmpeg_bin = get_ffmpeg_path()
        logger.info(f"🎵 [FFMPEG] Inicjalizacja dla: '{self.title}' | Binarka: '{ffmpeg_bin}' | Głośność: {int(volume * 100)}%")
        before_opts = FFMPEG_OPTIONS["before_options"]
        if self.http_headers and "User-Agent" in self.http_headers:
            ua = self.http_headers["User-Agent"]
            before_opts += f' -user_agent "{ua}"'

        audio = discord.FFmpegPCMAudio(
            self.stream_url,
            executable=ffmpeg_bin,
            before_options=before_opts,
            options=FFMPEG_OPTIONS["options"],
        )
        return discord.PCMVolumeTransformer(audio, volume=volume)


class MusicSourceManager:
    def __init__(self):
        self.spotify: Optional[Spotify] = None
        spotify_id = getattr(config, "SPOTIFY_CLIENT_ID", "")
        spotify_secret = getattr(config, "SPOTIFY_CLIENT_SECRET", "")
        if spotify_id and spotify_secret:
            try:
                auth = SpotifyClientCredentials(
                    client_id=spotify_id,
                    client_secret=spotify_secret,
                )
                self.spotify = Spotify(auth_manager=auth)
                logger.info("🟢 [INIT] Integracja Spotify API została poprawnie skonfigurowana.")
            except Exception as e:
                logger.warning(f"⚠️ [INIT] Błąd inicjalizacji Spotify API: {e}")
        else:
            logger.info("ℹ️ [INIT] Spotify API nie zostało skonfigurowane w .env (linki Spotify będą ignorowane).")

    def is_spotify_url(self, query: str) -> bool:
        return "spotify.com" in query.lower()

    def is_soundcloud_url(self, query: str) -> bool:
        return "soundcloud.com" in query.lower()

    def is_url(self, query: str) -> bool:
        return query.startswith("http://") or query.startswith("https://")

    # ─── Spotify → YouTube smart matching ───────────────────────────────

    async def _resolve_spotify_to_youtube(
        self, title: str, artists: str, isrc: Optional[str], duration_s: int
    ) -> Optional[Dict[str, Any]]:
        """Finds the best YouTube match for a Spotify track using single multi-result query with duration check."""
        loop = asyncio.get_running_loop()
        clean_title = re.sub(r"[\[\(].*?[\]\)]", "", title).replace('"', '').strip()
        clean_artists = artists.replace('"', '').strip()

        # Build prioritized search queries
        queries: List[str] = []
        if isrc:
            queries.append(f"ytsearch1:{isrc}")
        queries.append(f"ytsearch5:{clean_artists} - {clean_title}")
        queries.append(f'ytsearch3:"{clean_artists}" "{clean_title}" official audio')

        def search():
            opts = get_ytdl_base_options(use_cookies=False)
            opts["noplaylist"] = True

            best_entry: Optional[Dict[str, Any]] = None
            best_diff = 9999

            with yt_dlp.YoutubeDL(opts) as ydl:
                for q in queries:
                    try:
                        logger.info(f"🔍 [SPOTIFY→YT] Zapytanie: '{q}'")
                        info = ydl.extract_info(q, download=False)
                        if not info:
                            continue

                        entries = info.get("entries") or [info]
                        for entry in entries:
                            if not entry or not entry.get("title"):
                                continue

                            entry_dur = int(entry.get("duration", 0) or 0)
                            if duration_s > 0 and entry_dur > 0:
                                diff = abs(duration_s - entry_dur)
                                if diff <= 3:
                                    logger.info(f"✅ [SPOTIFY→YT] Dokładne dopasowanie ({entry_dur}s vs {duration_s}s): '{entry['title']}'")
                                    return entry
                                if diff < best_diff:
                                    best_diff = diff
                                    best_entry = entry
                            elif best_entry is None:
                                best_entry = entry

                        # If found candidate within 10s difference after checking candidates
                        if best_entry and best_diff <= 10:
                            logger.info(f"✅ [SPOTIFY→YT] Przybliżone dopasowanie (różnica {best_diff}s): '{best_entry['title']}'")
                            return best_entry

                    except Exception as e:
                        logger.warning(f"⚠️ [SPOTIFY→YT] Błąd przy '{q}': {e}")
                        continue

            if best_entry:
                logger.info(f"ℹ️ [SPOTIFY→YT] Wybrano najlepszy dostępny wynik: '{best_entry.get('title')}'")
                return best_entry
            return None

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, search),
                timeout=YTDL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"❌ [SPOTIFY→YT] Timeout wyszukiwania dla: '{artists} - {title}'")
            return None

    # ─── Spotify parser ─────────────────────────────────────────────────

    async def parse_spotify(
        self, url: str, requester: Optional[discord.Member]
    ) -> Tuple[List[Song], str]:
        """Parses track, album, or playlist from Spotify with ISRC-based YouTube matching."""
        if not self.spotify:
            raise ValueError(
                "Spotify nie jest skonfigurowane! Aby odtwarzać utwory ze Spotify, "
                "wprowadź `SPOTIFY_CLIENT_ID` oraz `SPOTIFY_CLIENT_SECRET` w pliku `.env`."
            )

        logger.info(f"🔍 [SPOTIFY] Parsowanie linku: {url} | Użytkownik: {requester.display_name if requester else 'nieznany'}")
        loop = asyncio.get_running_loop()

        track_match = re.search(r"spotify\.com/track/([a-zA-Z0-9]+)", url)
        playlist_match = re.search(r"spotify\.com/playlist/([a-zA-Z0-9]+)", url)
        album_match = re.search(r"spotify\.com/album/([a-zA-Z0-9]+)", url)

        try:
            if track_match:
                track_id = track_match.group(1)
                track_data = await loop.run_in_executor(None, lambda: self.spotify.track(track_id))
                artists = ", ".join(a["name"] for a in track_data.get("artists", []))
                title = track_data.get("name", "Nieznany utwór")
                isrc = track_data.get("external_ids", {}).get("isrc")
                duration = int(track_data.get("duration_ms", 0) / 1000)
                thumbnail = None
                if track_data.get("album", {}).get("images"):
                    thumbnail = track_data["album"]["images"][0]["url"]

                # Smart YouTube matching with duration verification
                search_query = f"{title} {artists}"
                yt_match = await self._resolve_spotify_to_youtube(title, artists, isrc, duration)
                if yt_match:
                    yt_url = (
                        yt_match.get("webpage_url")
                        or yt_match.get("url")
                        or f"https://www.youtube.com/watch?v={yt_match.get('id')}"
                    )
                    search_query = yt_url
                    logger.info(f"✅ [SPOTIFY] Dopasowano do YouTube: '{yt_match.get('title')}' | URL: {yt_url}")

                song = Song(
                    title=f"{artists} - {title}",
                    web_url=track_data.get("external_urls", {}).get("spotify", url),
                    duration=duration,
                    thumbnail=thumbnail,
                    uploader=artists,
                    requester=requester,
                    source_type="spotify",
                    search_query=search_query,
                    isrc=isrc,
                )
                logger.info(f"✅ [SPOTIFY] Utwór: '{song.title}' [{song.formatted_duration}] | ISRC: {isrc or 'brak'}")
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
                logger.info(f"✅ [SPOTIFY] Pomyślnie załadowano album '{album_title}' ({len(songs)} utworów)")
                return songs, f"album: {album_title}"

            elif playlist_match:
                playlist_id = playlist_match.group(1)
                playlist_data = await loop.run_in_executor(
                    None, lambda: self.spotify.playlist(playlist_id)
                )
                playlist_name = playlist_data.get("name", "Playlista")
                thumbnail = playlist_data["images"][0]["url"] if playlist_data.get("images") else None

                tracks_result = playlist_data.get("tracks", {})
                items = list(tracks_result.get("items", []))

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
                    isrc = track.get("external_ids", {}).get("isrc")
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
                            isrc=isrc,
                        )
                    )
                logger.info(f"✅ [SPOTIFY] Pomyślnie załadowano playlistę '{playlist_name}' ({len(songs)} utworów)")
                return songs, f"playlista: {playlist_name}"

            else:
                err_msg = "Nieobsługiwany format linku Spotify! Podaj link do utworu, playlisty lub albumu."
                logger.warning(f"⚠️ [SPOTIFY] {err_msg} (URL: {url})")
                raise ValueError(err_msg)
        except Exception as e:
            logger.error(f"❌ [SPOTIFY] Błąd pobierania danych ze Spotify: {e}", exc_info=True)
            raise e

    # ─── YouTube / SoundCloud parser ────────────────────────────────────

    async def parse_ytdlp(
        self, query: str, requester: Optional[discord.Member]
    ) -> Tuple[List[Song], str]:
        """Parses YouTube / SoundCloud link, search query, or playlist via yt-dlp."""
        loop = asyncio.get_running_loop()

        is_link = self.is_url(query)
        is_search = not is_link
        target_query = f"ytsearch1:{query}" if is_search else query

        logger.info(f"🔍 [YTDL] Rozpoczynam parsowanie: '{query}' (tryb: {'link bezpośredni' if is_link else f'wyszukiwanie YouTube: {target_query}'}) | Użytkownik: {requester.display_name if requester else 'brak'}")

        def extract():
            opts = get_ytdl_base_options()
            opts.update({
                "extract_flat": "in_playlist",
                "noplaylist": False,
                "ignoreerrors": True,
                "default_search": "ytsearch1" if is_search else "auto",
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                res = ydl.extract_info(target_query, download=False)
                if res and (("entries" in res and res["entries"]) or res.get("url") or res.get("webpage_url") or res.get("title")):
                    return res

            # Jeśli pobranie z plikiem cookies nie powiodło się, ponów bez ciasteczek
            if _get_cookie_path():
                logger.info(f"🔄 [YTDL] Pobranie z cookies nie powiodło się dla '{target_query}', ponawiam bez cookies...")
                opts_nc = get_ytdl_base_options(use_cookies=False)
                opts_nc.update({
                    "extract_flat": "in_playlist",
                    "noplaylist": False,
                    "ignoreerrors": True,
                    "default_search": "ytsearch1" if is_search else "auto",
                })
                with yt_dlp.YoutubeDL(opts_nc) as ydl_nc:
                    res = ydl_nc.extract_info(target_query, download=False)
                    if res and (("entries" in res and res["entries"]) or res.get("url") or res.get("webpage_url") or res.get("title")):
                        return res

            # Fallback dla bezpośredniego linku do YouTube
            if is_link and ("youtu.be" in query or "youtube.com" in query):
                vid_match = re.search(r"(?:v=|\/|be\/)([0-9A-Za-z_-]{11})", query)
                if vid_match:
                    vid_id = vid_match.group(1)
                    search_fallback = f"ytsearch1:{vid_id}"
                    logger.info(f"🔄 [YTDL] Link zablokowany. Próba wyszukiwania ID: '{search_fallback}'...")
                    opts_fb = get_ytdl_base_options(use_cookies=False)
                    opts_fb.update({
                        "extract_flat": "in_playlist",
                        "noplaylist": False,
                        "ignoreerrors": True,
                        "default_search": "ytsearch1",
                    })
                    with yt_dlp.YoutubeDL(opts_fb) as ydl_fb:
                        res = ydl_fb.extract_info(search_fallback, download=False)
                        if res and (("entries" in res and res["entries"]) or res.get("url") or res.get("webpage_url") or res.get("title")):
                            return res

            return None

        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, extract),
                timeout=YTDL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Timeout ({YTDL_TIMEOUT}s): yt-dlp nie odpowiada dla '{target_query}'")
        except Exception as e:
            logger.error(f"❌ [YTDL] Błąd podczas pobierania informacji dla '{target_query}': {e}", exc_info=True)
            raise e

        if not data:
            logger.warning(f"⚠️ [YTDL] Brak wyników dla: '{target_query}'")
            raise ValueError("Nie znaleziono wyników dla podanego zapytania.")

        # Check if it's a playlist or search result list
        if "entries" in data and data["entries"]:
            entries = [e for e in data["entries"] if e]
            if not entries:
                logger.warning(f"⚠️ [YTDL] Lista entries jest pusta dla: '{target_query}'")
                raise ValueError("Brak dostępnych utworów.")

            if is_search or len(entries) == 1:
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
                logger.info(f"✅ [YTDL] Pomyślnie znaleziono utwór: '{song.title}' [{song.formatted_duration}] | Kanał: {song.uploader} | URL: {song.web_url}")
                return [song], "track"
            else:
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
                logger.info(f"✅ [YTDL] Pomyślnie sparsowano playlistę '{playlist_title}' z {len(songs)} utworami.")
                return songs, f"playlista: {playlist_title}"
        else:
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
            logger.info(f"✅ [YTDL] Bezpośredni link: '{song.title}' [{song.formatted_duration}] | Kanał: {song.uploader} | URL: {song.web_url}")
            return [song], "track"

    async def get_songs(
        self, query: str, requester: Optional[discord.Member]
    ) -> Tuple[List[Song], str]:
        """Extracts songs from Spotify, YouTube, SoundCloud, or search query."""
        query = query.strip()
        user_str = f"{requester} (ID: {requester.id})" if requester else "nieznany"
        logger.info(f"📥 [MANAGER] Nowe zapytanie od {user_str}: '{query}'")
        if self.is_spotify_url(query):
            return await self.parse_spotify(query, requester)
        else:
            return await self.parse_ytdlp(query, requester)
