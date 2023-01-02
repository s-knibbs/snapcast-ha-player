# Snapcast Player Home Assistant Integration
Simple integration designed to allow TTS playback through a [snapcast](https://mjaggard.github.io/snapcast/) server (the built in [snapcast integration](https://www.home-assistant.io/integrations/snapcast/) doesn't support audio playback). Audio is streamed to snapcast using ffmpeg, which is preinstalled on HassOS.

## Installation

Install via HACS or copy `./custom_components/snapcast_player/` to `/config/custom_components/braviatv_psk/`

## Configuration Variables

|  key    | required | value             | description |
|---------|----------|-------------------|-------------|
|platform | yes      | `snapcast_player` | Platform name |
|host     | yes      | `127.0.0.1`       | Hostname or IP address of the snapcast server |
| port    | no       | `4953`            | Port to stream audio to, default is `4953` |
| start_delay | no   | `1s`              | Insert a delay at the stream start. Useful to prevent TTS from being slightly cut-off |

## Example Config

```yaml
media_player:
  - platform: snapcast_player
    host: 127.0.0.1
    start_delay: 1s
```