from __future__ import annotations
import asyncio
import os.path
import re
import signal
from asyncio import IncompleteReadError
from dataclasses import dataclass
from datetime import time, timedelta as delta, timedelta
from typing import TYPE_CHECKING, Any

import m3u8
import voluptuous as vol

from homeassistant.components.media_player.const import RepeatMode
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
import homeassistant.helpers.config_validation as cv
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    PLATFORM_SCHEMA,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.reload import setup_reload_service
from homeassistant.util.dt import utcnow

from .const import (
    CONF_START_DELAY,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    DEFAULT_PORT,
    DOMAIN,
)

if TYPE_CHECKING:
    from asyncio.subprocess import Process

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_START_DELAY): cv.string,
        vol.Optional(CONF_PORT): cv.string,
        vol.Optional(CONF_NAME): cv.string,
    }
)
METADATA_REGEXES = (
    re.compile(r"^(TITLE)=(.+)$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^(ARTIST)=(.+)$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^(ALBUM)=(.+)$", re.MULTILINE | re.IGNORECASE),
)
TITLE_REGEXES = (
    re.compile(r"^StreamTitle=(.+)$", re.MULTILINE),
    re.compile(r"^icy-name=(.+)$", re.MULTILINE),
)
DURATION_REGEX = re.compile(r"^ {2}Duration: ([\d:.]+),", re.MULTILINE)
PROGRESS_REGEX = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")


@dataclass
class MediaInfo:
    title: str
    artist: str | None = None
    album: str | None = None
    duration: int | None = None


@dataclass
class PlaylistInfo:
    items: list[str]
    album_art: str | None


def to_seconds(value: str) -> int:
    t = time.fromisoformat(f"{value}0000")
    return round(delta(hours=t.hour, minutes=t.minute, seconds=t.second, microseconds=t.microsecond).total_seconds())


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    setup_reload_service(hass, DOMAIN, ["media_player"])
    name = config.get(CONF_NAME, DOMAIN)
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT, DEFAULT_PORT)
    start_delay = config.get(CONF_START_DELAY)

    player_entity = SnapcastPlayer(host, name, port, start_delay, hass)

    def _shutdown(call):
        player_entity.media_stop()

    hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    add_entities([player_entity])


async def parse_playlist(hass: HomeAssistant, url: str) -> PlaylistInfo:
    dirname = os.path.dirname(url)
    if url.startswith("/media/local"):
        url = url.replace("/local", "", 1)
        with open(url) as playlist_file:
            playlist_data = playlist_file.read(64 * 1024)
    else:
        session = aiohttp_client.async_get_clientsession(hass, verify_ssl=False)
        async with session.get(url, timeout=5) as resp:
            charset = resp.charset or "utf-8"
            playlist_data = (await resp.content.read(64 * 1024)).decode(charset)

    custom_tags = {}

    def tag_handler(line: str, *_) -> bool:
        for tag in ("EXTIMG", "EXTVLCOPT"):
            if line.startswith(f"#{tag}:"):
                custom_tags[tag] = line.split(":")[1]
                return True
        return False

    playlist = m3u8.loads(playlist_data, custom_tags_parser=tag_handler)
    album_art = custom_tags.get("EXTIMG")
    return PlaylistInfo(
        [os.path.join(dirname, item.uri) for item in playlist.segments],
        os.path.join(dirname, album_art) if album_art is not None else None,
    )


class SnapcastPlayer(MediaPlayerEntity):
    _attr_media_content_type = MediaType.MUSIC

    def __init__(
        self,
        host: str,
        name: str | None,
        port: str | None,
        start_delay: str | None,
        hass: HomeAssistant,
    ) -> None:
        self._host = host
        self._port = port
        self._attr_state = MediaPlayerState.IDLE
        self._name = name
        self._start_delay = start_delay
        self._uri: str | None = None
        self._proc: Process | None = None
        self._is_stopped = False
        self._media_info: MediaInfo | None = None
        self._attr_unique_id = name
        self.hass = hass
        self._queue: list[str] = []
        self._seek_position: float | None = None

    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any) -> None:
        # TODO: Support announce
        # TODO: Support queuing items
        if media_source.is_media_source_id(media_id):
            sourced_media = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            media_id = sourced_media.url

        self._queue = []
        if media_id.endswith(".m3u8") or media_id.endswith(".m3u"):
            playlist = await parse_playlist(self.hass, media_id)
            if not playlist.items:
                return
            for item in playlist.items:
                self._queue.append(async_process_play_media_url(self.hass, item))
            self._uri = self._queue[0]
            if playlist.album_art:
                self._attr_media_image_url = async_process_play_media_url(self.hass, playlist.album_art)
        else:
            media_id = async_process_play_media_url(self.hass, media_id)
            self._uri = media_id
            self._attr_media_image_url = None

        # Already playing, terminate existing process
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            self._is_stopped = False

        self._proc = await self._start_playback()
        self.hass.async_create_task(self._on_process_complete())

    @property
    def _next_track(self) -> str | None:
        if self._uri:
            try:
                cur_index = self._queue.index(self._uri)
                return self._queue[cur_index + 1]
            except (ValueError, IndexError):
                pass
        return None

    async def _on_process_complete(self):
        while True:
            returncode = await self._proc.wait()
            repeat_modes = [RepeatMode.ONE, RepeatMode.ALL]
            if returncode != 0 or (self._attr_repeat not in repeat_modes and self._next_track is None):
                self.hass.async_create_task(self.async_update())
                return
            if self._next_track:
                self._uri = self._next_track
            self._proc = await self._start_playback()

    @property
    def _previous_track(self) -> str | None:
        if self._uri:
            try:
                cur_index = self._queue.index(self._uri)
                if cur_index > 0:
                    return self._queue[cur_index - 1]
                return self._queue[cur_index]
            except ValueError:
                pass
        return None

    async def async_media_seek(self, position: float) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
        self._proc = await self._start_playback(position)
        self.hass.async_create_task(self._on_process_complete())

    async def async_media_next_track(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
        if self._next_track:
            self._uri = self._next_track
        self._proc = await self._start_playback()
        self.hass.async_create_task(self._on_process_complete())

    async def async_media_previous_track(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
        if self._previous_track:
            self._uri = self._previous_track
        self._proc = await self._start_playback()
        self.hass.async_create_task(self._on_process_complete())

    async def _get_metadata(self) -> MediaInfo | None:
        if self._uri is None:
            return None
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-i",
            self._uri,
            "-f",
            "ffmetadata",
            "-",
            stderr=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
            close_fds=True,
        )
        stdout, stderr = await proc.communicate()
        # Parse the ffmpeg output
        if proc.returncode == 0:
            stream_info = stderr.decode("utf-8")
            duration = None
            if match := DURATION_REGEX.search(stream_info):
                duration = to_seconds(match.group(1))
            metadata = stdout.decode("utf-8")
            details = {}
            for regex in METADATA_REGEXES:
                if match := regex.search(metadata):
                    details[match.group(1).lower()] = match.group(2)
            if details:
                try:
                    return MediaInfo(duration=duration, **details)
                except TypeError:
                    pass
            for title_regex in TITLE_REGEXES:
                if match := title_regex.search(metadata):
                    return MediaInfo(match.group(1), duration=duration)
        return None

    async def _read_ffmpeg_progress(self):
        while True:
            if self._proc and self._proc.returncode is None and not self._proc.stderr.at_eof():
                try:
                    data = await self._proc.stderr.readuntil(b"\r")
                except IncompleteReadError:
                    return
                if match := PROGRESS_REGEX.search(data.decode("utf-8")):
                    position = to_seconds(match.group(1))
                    if self._seek_position:
                        position += round(self._seek_position)
                    self._attr_media_position = position
                    self._attr_media_position_updated_at = utcnow()
            else:
                self._attr_media_position = None
                return

    async def _start_playback(self, position: float | None = None) -> Process:
        if self._uri is None:
            raise ValueError("No URI set")
        format_args = [
            "-f",
            "u16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "2",
            "-ar",
            "48000",
        ]
        delay_args = [] if self._start_delay is None else ["-af", f"adelay={self._start_delay}:all=true"]
        if self._host.startswith("/"):
            out_arg = self._host
        else:
            out_arg = f"tcp://{self._host}:{self._port}"
        seek_args = ["-ss", str(timedelta(seconds=position))[:-4]] if position else []
        self._seek_position = position
        proc = await asyncio.create_subprocess_exec(
            *[
                "ffmpeg",
                "-y",
                *seek_args,
                "-i",
                self._uri,
                *format_args,
                *delay_args,
                out_arg,
            ],
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
            close_fds=True,
        )
        self._attr_state = MediaPlayerState.PLAYING
        self._is_stopped = False
        self._attr_media_position = round(self._seek_position) if self._seek_position else 0
        self._attr_media_position_updated_at = utcnow()
        self.hass.async_create_task(self._read_ffmpeg_progress())
        return proc

    @property
    def media_duration(self) -> int | None:
        return self._media_info is not None and self._media_info.duration

    @property
    def media_artist(self) -> str | None:
        return self._media_info.artist if self._media_info else None

    @property
    def media_album_name(self) -> str | None:
        return self._media_info.album if self._media_info else None

    def set_repeat(self, repeat: RepeatMode) -> None:
        self._attr_repeat = repeat

    async def async_update(self):
        if self._proc is not None and self._proc.returncode is None:
            self._attr_state = MediaPlayerState.PAUSED if self._is_stopped else MediaPlayerState.PLAYING
            self._media_info = await self._get_metadata()
        else:
            self._media_info = None
            self._attr_state = MediaPlayerState.IDLE
            self._attr_media_image_url = None

    @property
    def media_content_id(self) -> str | None:
        return self._uri

    @property
    def name(self):
        return self._name

    @property
    def media_title(self) -> str | None:
        return self._media_info.title if self._media_info else None

    def media_stop(self) -> None:
        if self._proc is not None:
            self._queue = []
            self._proc.terminate()
            self._attr_repeat = RepeatMode.OFF
            self._attr_state = MediaPlayerState.IDLE
            self._attr_media_image_url = None
            self._is_stopped = False

    def media_pause(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.send_signal(signal.SIGSTOP)
            self._is_stopped = True
            self._attr_state = MediaPlayerState.PAUSED

    def media_play(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.send_signal(signal.SIGCONT)
            self._is_stopped = False
            self._attr_state = MediaPlayerState.PLAYING

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = (
            MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.BROWSE_MEDIA
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.REPEAT_SET
        )
        if self._proc and self._proc.returncode is None and self.media_duration:
            features |= MediaPlayerEntityFeature.PLAY
            features |= MediaPlayerEntityFeature.PAUSE
            features |= MediaPlayerEntityFeature.SEEK
        if self._next_track:
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        if self._previous_track:
            features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
        return features

    async def async_browse_media(self, media_content_type: str | None = None, media_content_id: str | None = None):
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )
