"""Script the recursively creates audio playlist files from a given starting directory"""
import os
import sys
from urllib.parse import quote

from PIL import Image

PLAYLIST_TEMPLATE = """
#EXTINF:{0},{1}
{2}
""".strip()


def is_audio_file(filename: str) -> bool:
    return os.path.splitext(filename)[1] in ['.flac', '.wma', '.m4a', '.ogg', '.mp3', '.m4p']


def main():
    for path, _, files in os.walk(sys.argv[1]):
        playlist_items = sorted([file for file in files if is_audio_file(file)])
        image_files = [file for file in files if file.endswith(".jpg")]
        if len(playlist_items) < 2:
            continue

        album_image = None
        if image_files:
            max_size = 0
            # Select the largest image
            for image_name in image_files:
                image = Image.open(os.path.join(path, image_name))
                size = image.width * image.height
                if size > max_size:
                    max_size = size
                    album_image = image_name

        playlist_name = os.path.split(path)[-1] + ".m3u"
        with open(os.path.join(path, playlist_name), 'w') as playlist_file:
            print("#EXTM3U", file=playlist_file)
            if album_image is not None:
                print(f"#EXTIMG:{album_image}", file=playlist_file)
            for item in playlist_items:
                # TODO: Get correct duration
                duration = 100
                name = os.path.splitext(item)[0]
                print(PLAYLIST_TEMPLATE.format(duration, name, quote(item)), file=playlist_file)


if __name__ == "__main__":
    main()