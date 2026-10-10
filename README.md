# Clipper

A small video clipping tool that runs on your own computer. Cut clips from a YouTube link or a local video, either by typing timestamps or by letting it find the best moments. It has a browser interface and a command line.

No accounts, no cloud, no AI models to download. Two tiny Python files, plus ffmpeg and yt-dlp.

![Clipper web interface](docs/screenshot.png)

## Features

- Clip from a **YouTube link** or a **local file** (video or audio)
- Cut by **timestamps**, or mark start and end while the video plays
- **Auto-find highlights**: uses YouTube's "Most replayed" graph when there is one, otherwise audio loudness
- **Vertical 9:16 export** for Reels, Shorts and TikTok, with a crop or a blurred-background option
- For YouTube, only the parts you ask for are downloaded, not the whole video
- Preview and download finished clips in the page
- Windows one-click launcher (`start.bat`)

## Requirements

- Python 3.8 or newer
- [ffmpeg](https://ffmpeg.org/) on your PATH
- yt-dlp (`pip install -r requirements.txt`)

Windows: `winget install ffmpeg`  |  macOS: `brew install ffmpeg`  |  Linux: `sudo apt install ffmpeg`

## Quick start

**Windows:** double-click `start.bat`. It checks everything, installs yt-dlp the first time, and opens the page.

**Any system:**

    pip install -r requirements.txt
    python webui.py

Then open http://127.0.0.1:8765 (it opens by itself). Finished clips are saved in the `clips` folder.

Options: `python webui.py --port 8800 -o my_clips --no-browser`

### Using the page

1. Paste a YouTube link or a file path, or drop a video on the page, then press **Load**.
2. Press **Mark start** and **Mark end** (keys `I` and `O`) while it plays, or type or paste timestamps, or press **Auto-find highlights**.
3. Pick Original or Vertical 9:16, then press **Cut clips**.

## Command line

    python clipper.py clip "https://youtu.be/XXXX" -t 1:30-2:10 -t "5:00-5:45 funny moment"
    python clipper.py clip video.mp4 -f times.txt
    python clipper.py auto video.mp4 -n 3 -l 20 --dry-run

`times.txt` has one clip per line, with an optional label and `#` for comments:

    1:30-2:10 intro hook
    12:04-12:40 best joke

| Option | What it does |
| --- | --- |
| `-o folder` | Output folder (default `clips`) |
| `--vertical [crop\|blur]` | Export 9:16 |
| `-q 1080` | Maximum YouTube resolution (default 720) |
| `--fast` | Local files only: instant stream copy, cuts snap to keyframes |
| `-n`, `-l` | (`auto`) How many clips, and how long each is in seconds |

Time formats: `90`, `1:30`, `01:02:03`, `1m30s`.

## How auto-highlights work

It is simple signal analysis, not a neural network. If the YouTube video has a "Most replayed" graph, the busiest stretches are picked. Otherwise ffmpeg measures loudness every second and the loudest non-overlapping windows are picked. It is a good starting point, so check the results before you cut.

## Security

The web interface only listens on `127.0.0.1` and rejects requests that come from other websites. Do not expose it to a network.

## Responsible use

Only clip content you made or have permission to use, and follow the terms of the sites you download from.

## License

MIT, see [LICENSE](LICENSE).
