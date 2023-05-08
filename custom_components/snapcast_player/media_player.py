from __future__ import annotations
import asyncio
import logging
import os.path
import re
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import m3u8
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
    async_process_play_media_url
)
import voluptuous as vol
from homeassistant.helpers import aiohttp_client

if TYPE_CHECKING:
    from asyncio.subprocess import Process

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

DOMAIN = "snapcast_player"
DEFAULT_PORT = "4953"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "name"
CONF_START_DELAY = "start_delay"
CONF_START_DELAY_TEMPLATE = "start_delay_template"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_START_DELAY): cv.string,
        vol.Optional(CONF_PORT): cv.string,
        vol.Optional(CONF_NAME): cv.string,
    }
)
METADATA_REGEXES = (
    re.compile(r'^(TITLE)=(.+)$', re.MULTILINE | re.IGNORECASE),
    re.compile(r'^(ARTIST)=(.+)$', re.MULTILINE | re.IGNORECASE),
    re.compile(r'^(ALBUM)=(.+)$', re.MULTILINE | re.IGNORECASE)
)
TITLE_REGEXES = (
    re.compile(r'^StreamTitle=(.+)$', re.MULTILINE),
    re.compile(r'^icy-name=(.+)$', re.MULTILINE)
)


@dataclass
class MediaInfo:
    title: str
    artist: str | None = None
    album: str | None = None


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    name = config.get(CONF_NAME, DOMAIN)
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT, DEFAULT_PORT)
    start_delay = config.get(CONF_START_DELAY)

    player_entity = SnapcastPlayer(host, name, port, start_delay, hass)

    def _shutdown(call):
        player_entity.media_stop()

    hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    add_entities([player_entity])


async def parse_playlist(hass: HomeAssistant, url: str) -> list[str]:
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

    def ignore_vlc_opts(line: str, *_) -> bool:
        return line.startswith("#EXTVLCOPT")

    playlist = m3u8.loads(playlist_data, custom_tags_parser=ignore_vlc_opts)

    def _convert(uri: str) -> str:
        return os.path.join(dirname, uri)

    return [_convert(item.uri) for item in playlist.segments]


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
        self._uri = None
        self._proc: Process | None = None
        self._is_stopped = False
        self._media_info: MediaInfo | None = None
        self._attr_unique_id = name
        self.hass = hass
        self._queue: list[str] = []

    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any) -> None:
        # TODO: Support announce
        # TODO: Support queuing items
        if media_source.is_media_source_id(media_id):
            sourced_media = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = sourced_media.url

        self._queue = []
        if media_id.endswith('.m3u8') or media_id.endswith(".m3u"):
            playlist = await parse_playlist(self.hass, media_id)
            if not playlist:
                return
            for item in playlist:
                self._queue.append(async_process_play_media_url(self.hass, item))
            self._uri = self._queue[0]
        else:
            media_id = async_process_play_media_url(self.hass, media_id)
            self._uri = media_id

        # Already playing, terminate existing process
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            self._is_stopped = False

        self._proc = await self._start_playback()
        self.hass.async_create_task(self._on_process_complete())

    @property
    def _next_track(self) -> str | None:
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
        try:
            cur_index = self._queue.index(self._uri)
            if cur_index > 0:
                return self._queue[cur_index - 1]
            return self._queue[cur_index]
        except ValueError:
            pass
        return None

    async def async_media_next_track(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            self._is_stopped = False
        if self._next_track:
            self._uri = self._next_track
        self._proc = await self._start_playback()
        self.hass.async_create_task(self._on_process_complete())

    async def async_media_previous_track(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            self._is_stopped = False
        if self._previous_track:
            self._uri = self._previous_track
        self._proc = await self._start_playback()
        self.hass.async_create_task(self._on_process_complete())

    async def _get_metadata(self) -> MediaInfo | None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", self._uri, "-f", "ffmetadata", "-",
            stderr=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
            close_fds=True,
        )
        stdout, stderr = await proc.communicate()
        # Parse the ffmpeg output
        if proc.returncode == 0:
            metadata = stdout.decode('utf-8')
            details = {}
            for regex in METADATA_REGEXES:
                if match := regex.search(metadata):
                    details[match.group(1).lower()] = match.group(2)
            if details:
                try:
                    return MediaInfo(**details)
                except TypeError:
                    pass
            for title_regex in TITLE_REGEXES:
                if match := title_regex.search(metadata):
                    return MediaInfo(match.group(1))
        return None

    async def _start_playback(self) -> Process:
        format_args = ["-f", "u16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", "48000"]
        delay_args = [] if self._start_delay is None else ["-af", f"adelay={self._start_delay}:all=true"]
        if self._host.startswith("/"):
            out_arg = self._host
        else:
            out_arg = f"tcp://{self._host}:{self._port}"
        proc = await asyncio.create_subprocess_exec(
            *["ffmpeg", "-y", "-i", self._uri, *format_args, *delay_args, out_arg],
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
            close_fds=True,
            )
        self._attr_state = MediaPlayerState.PLAYING
        return proc

    @property
    def media_artist(self) -> str | None:
        return self._media_info and self._media_info.artist

    @property
    def media_album_name(self) -> str | None:
        return self._media_info and self._media_info.album

    def set_repeat(self, repeat: RepeatMode) -> None:
        self._attr_repeat = repeat

    async def async_update(self):
        if self._proc is not None and self._proc.returncode is None:
            self._attr_state = MediaPlayerState.PAUSED if self._is_stopped else MediaPlayerState.PLAYING
            self._media_info = await self._get_metadata()
        else:
            self._media_info = None
            self._attr_state = MediaPlayerState.IDLE

    @property
    def media_content_id(self) -> str | None:
        return self._uri

    @property
    def name(self):
        return self._name

    @property
    def media_title(self) -> str | None:
        return self._media_info and self._media_info.title

    def media_stop(self) -> None:
        if self._proc is not None:
            self._queue = []
            self._proc.terminate()
            self._attr_repeat = RepeatMode.OFF
            self._attr_state = MediaPlayerState.IDLE
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
        if self._proc and self._proc.returncode is None:
            features |= MediaPlayerEntityFeature.PLAY
            features |= MediaPlayerEntityFeature.PAUSE
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