from __future__ import annotations
import asyncio
import logging
from pipes import Template
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.template import Template
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

if TYPE_CHECKING:
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
        vol.Optional(CONF_START_DELAY_TEMPLATE): cv.template,
    }
)

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
    start_delay_template = config.get(CONF_START_DELAY_TEMPLATE)
    if start_delay_template is not None:
        start_delay_template.hass = hass

    player_entity = SnapcastPlayer(host, name, port, start_delay, start_delay_template)

    def _shutdown(call):
        player_entity.media_stop()

    hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    add_entities([player_entity])


class SnapcastPlayer(MediaPlayerEntity):

    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.STOP
    )

    def __init__(
        self,
        host: str,
        name: str | None,
        port: str | None,
        start_delay: str | None,
        start_delay_template: Template | None,
    ) -> None:
        self._host = host
        self._port = port
        self._attr_state = MediaPlayerState.IDLE
        self._name = name
        self._start_delay = start_delay
        self._start_delay_template = start_delay_template
        self._uri = None
        self._proc = None

    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any) -> None:
        if media_source.is_media_source_id(media_id):
            sourced_media = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = sourced_media.url

        media_id = async_process_play_media_url(self.hass, media_id)
        self._uri = media_id
        format_args = ["-f", "u16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", "48000"]
        delay = self._start_delay
        if self._start_delay_template is not None:
            delay = self._start_delay_template.async_render()
        delay_args = [] if delay is None else ["-af", f"adelay={delay}:all=true"]
        out_arg = f"tcp://{self._host}:{self._port}"
        # Already playing, terminate existing process
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
        self._proc = await asyncio.create_subprocess_exec(
            *["ffmpeg", "-y", "-i", media_id, *format_args, *delay_args, out_arg],
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
            close_fds=True,
        )
        self._attr_state = MediaPlayerState.PLAYING

    def update(self):
        if self._proc is not None and self._proc.returncode is None:
            self._attr_state = MediaPlayerState.PLAYING
        else:
            self._attr_state = MediaPlayerState.IDLE

    @property
    def media_content_id(self) -> str | None:
        return self._uri

    @property
    def name(self):
        return self._name

    def media_stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()

    async def async_browse_media(self, media_content_type: str | None = None, media_content_id: str | None = None):
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )