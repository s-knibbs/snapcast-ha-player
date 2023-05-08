# Snapcast Player Home Assistant Integration
Simple integration designed to allow playback through a [snapcast](https://mjaggard.github.io/snapcast/) server (the built in [snapcast integration](https://www.home-assistant.io/integrations/snapcast/) doesn't support audio playback).
Audio is streamed to snapcast using ffmpeg, which is preinstalled on HassOS.

## Installation

Install via HACS or copy `./custom_components/snapcast_player/` to `/config/custom_components/`

## Configuration Variables

| key         | required | example            | description                                                                                                                                                                                               |
|-------------|----------|--------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| platform    | yes      | `snapcast_player`  | Platform name                                                                                                                                                                                             |
| host        | yes      | `127.0.0.1`        | Hostname, IP address of the snapcast server. If snapcast is running on the same machine, this can also be the path to a [pipe](https://github.com/badaix/snapcast/blob/develop/doc/configuration.md#pipe) |
| name        | no       | `multiroom_player` | Entity name                                                                                                                                                                                               |
| port        | no       | `4953`             | Port to stream audio to, default is `4953`                                                                                                                                                                |
| start_delay | no       | `1s`               | Insert a delay at the stream start. This can prevent the start of TTS announcements being cut off                                                                                                         |

## Example Config

```yaml
media_player:
  - platform: snapcast_player
    host: 127.0.0.1
    start_delay: 1s
```

# TODO

- Support modifying the play queue via the `media_player.play_media` service.