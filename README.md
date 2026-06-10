# Batch Video Downloader + MP3 Converter

A production-ready Python CLI tool that reads a list of URLs, downloads each video at the highest available quality, and automatically converts every download to a 320 kbps MP3. Built for processing hundreds or thousands of URLs reliably, with crash recovery, full logging, and clean folder organisation.

---

## Features

- **Batch processing** — reads all URLs from `urls.txt`, processes them one by one automatically
- **Highest quality video** — 5-tier format selector ensures a match is always found, never errors with "format not available"
- **Automatic MP3 conversion** — every download is converted to 320 kbps MP3 via FFmpeg
- **Audio stream verification** — ffprobe checks for an audio stream before every conversion attempt
- **Three download modes** — `video+mp3`, `video_only`, or `audio_only`
- **Crash recovery** — `completed_urls.txt` is written after each URL; interrupted runs resume from where they left off
- **Duplicate and blank line skipping** — URLs are deduplicated before processing starts
- **Filename collision prevention** — appends `(1)`, `(2)` etc. rather than overwriting existing files
- **Enhanced filename sanitisation** — strips illegal characters on Windows, macOS, and Linux, including Unicode fullwidth lookalikes
- **Dual logging** — clean INFO output to the terminal, full DEBUG audit trail written to `downloader.log`
- **Separate failure tracking** — failed URLs are written to `failed_urls.txt` for easy retry
- **Optional browser cookies** — opt-in flag to pass Chrome/Firefox/Safari cookies for age-restricted content
- **Graceful dependency handling** — auto-installs `yt-dlp` via pip; clear install instructions if FFmpeg or ffprobe are missing

---

## Requirements

| Dependency | Version | Install |
|---|---|---|
| Python | 3.10+ | [python.org](https://www.python.org/downloads/) |
| yt-dlp | latest | Auto-installed on first run |
| FFmpeg | any recent | See below |
| ffprobe | any recent | Bundled with FFmpeg |

### Install FFmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg

# Fedora
sudo dnf install ffmpeg

# Windows (winget)
winget install ffmpeg

# Windows (manual)
# https://ffmpeg.org/download.html
```

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/your-username/batch-downloader.git
cd batch-downloader

# 2. (Optional) create a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows

# 3. Install yt-dlp
pip install yt-dlp

# 4. Confirm FFmpeg is available
ffmpeg -version
```

> **yt-dlp** will also be installed automatically on the first run if it is missing.

---

## Usage

### 1. Add your URLs

Create `~/Downloads/urls.txt` (the script creates a placeholder automatically on first run) and add one URL per line:

```
# Lines starting with # are ignored
https://www.youtube.com/watch?v=dQw4w9WgXcQ
https://www.youtube.com/watch?v=9bZkp7q19f0
https://vimeo.com/123456789
```

Blank lines, comment lines, invalid URLs, and duplicates are all skipped automatically.

### 2. Run the script

```bash
python batch_downloader.py
```

### 3. Find your files

```
~/Downloads/
├── urls.txt
├── completed_urls.txt
├── failed_urls.txt
│
├── videos/
│   ├── Video Title One.mp4
│   └── Video Title Two.mp4
│
└── music/
    ├── Video Title One.mp3
    └── Video Title Two.mp3
```

---

## Configuration

All settings live at the top of `batch_downloader.py` under the `CONFIGURATION` section. No other code needs to change.

```python
# ── Download mode ──────────────────────────────────────────────────────
#   "video+mp3"  → download video+audio MP4, then convert to MP3
#   "video_only" → download video+audio MP4, no MP3 step
#   "audio_only" → download best audio, extract directly to MP3
DOWNLOAD_MODE = "video+mp3"

# ── Folder layout ──────────────────────────────────────────────────────
BASE_DIR     = Path.home() / "Downloads"   # root for all output
VIDEO_SUBDIR = "videos"
MUSIC_SUBDIR = "music"

# ── Behaviour flags ────────────────────────────────────────────────────
FORCE_REDOWNLOAD                = False   # set True to ignore completed_urls.txt
DELETE_ORIGINAL_AFTER_CONVERSION = False  # set True to remove .mp4 after conversion

# ── Browser cookies (for age-restricted or login-required videos) ──────
USE_BROWSER_COOKIES  = False      # set True to enable
BROWSER_FOR_COOKIES  = "chrome"   # "chrome" | "firefox" | "safari" | "edge" | "brave"

# ── Quality ────────────────────────────────────────────────────────────
MP3_BITRATE = "320k"
```

---

## Download Modes

| Mode | What happens |
|---|---|
| `video+mp3` | Downloads best video+audio merged to MP4, then converts to 320 kbps MP3. Both files are kept (unless `DELETE_ORIGINAL_AFTER_CONVERSION = True`). |
| `video_only` | Downloads best video+audio merged to MP4. No conversion step. |
| `audio_only` | Downloads best audio stream and extracts directly to MP3 via yt-dlp's built-in processor. No video file is created. |

---

## How the Format Selector Works

YouTube and other sites serve video and audio as separate DASH streams that must be downloaded and merged. A simple two-tier selector can fail when a site throttles or restricts specific container/codec combinations.

This tool uses a 5-tier chain that exhausts every reasonable combination before falling back to a guaranteed pre-muxed stream:

```
bestvideo[ext=mp4]+bestaudio[ext=m4a]   ← tier 1: ideal MP4 + AAC
bestvideo+bestaudio                      ← tier 2: best of any container
bestvideo[ext=mp4]+bestaudio             ← tier 3: MP4 video + any audio
bestvideo+bestaudio[ext=m4a]             ← tier 4: any video + M4A audio
best                                     ← tier 5: pre-muxed fallback (always has audio)
```

Tier 5 (`best`) is a single pre-muxed file that always exists and always contains an audio stream, so the selector can never return "format not available."

---

## URL Tracking and Recovery

Progress is saved to disk after every URL, so an interrupted run (network drop, Ctrl-C, power cut) resumes automatically from the next unprocessed URL.

| File | Purpose |
|---|---|
| `urls.txt` | Your input list — never modified by the script |
| `completed_urls.txt` | Written after each successful full-pipeline completion |
| `failed_urls.txt` | Written when a download or conversion fails |

**To retry failed URLs:**
```bash
cp ~/Downloads/failed_urls.txt ~/Downloads/urls.txt
python batch_downloader.py
```

**To re-download everything from scratch:**
```python
FORCE_REDOWNLOAD = True   # in the configuration section
```

---

## Logging

Two log streams run in parallel:

| Stream | Level | Content |
|---|---|---|
| Terminal | INFO | Download progress, conversion status, per-URL results, final summary |
| `downloader.log` | DEBUG | Everything above plus exact yt-dlp format selected, exact FFmpeg command, file sizes in MB, ffprobe stream details, full FFmpeg stderr on failure |

The log file is written to the directory where the script is run from.

---

## Example Output

```
╔══════════════════════════════════════════════════════════════════════╗
║  Batch Video Downloader + MP3 Converter  v4.1                        ║
╠══════════════════════════════════════════════════════════════════════╣
║  Mode:         video+mp3                                             ║
║  Videos →      /Users/you/Downloads/videos                           ║
║  Music →       /Users/you/Downloads/music                            ║
║  ffmpeg:       ✓ Available                                           ║
║  ffprobe:      ✓ Available                                           ║
║  Cookies:      ✗ disabled                                            ║
╠══════════════════════════════════════════════════════════════════════╣
║  Total URLs:   5                                                     ║
║  Already done: 0                                                     ║
║  To process:   5                                                     ║
╚══════════════════════════════════════════════════════════════════════╝

──────────────────────────────────────────────────────────────────────
  [1/5]  https://www.youtube.com/watch?v=dQw4w9WgXcQ
  dl_ok=0  dl_fail=0  conv_ok=0  conv_fail=0  remaining=4  ETA=calculating …
──────────────────────────────────────────────────────────────────────
  [1/5] ↓  100%  speed    4.20MiB/s  ETA 0s
  [1/5] ✓ Downloaded: Rick Astley - Never Gonna Give You Up.mp4
  Audio Stream: ✓ Found  [Rick Astley - Never Gonna Give You Up.mp4]
  ♪ Converting to MP3 (320k) …
  ✓ MP3 saved: Rick Astley - Never Gonna Give You Up.mp3  (9.42 MB)
```

---

## Supported Sites

Any site supported by yt-dlp — including YouTube, Vimeo, Twitter/X, Instagram, Facebook, TikTok, SoundCloud, Twitch, and [hundreds more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

---

## Troubleshooting

**"Requested format is not available"**
The 5-tier format selector should prevent this. If it still occurs, enable browser cookies:
```python
USE_BROWSER_COOKIES = True
BROWSER_FOR_COOKIES = "chrome"   # or whichever browser you use
```

**"Audio Stream: ✗ Missing"**
The downloaded file contains no audio track. This can happen with video-only streams from some platforms. Enable `USE_BROWSER_COOKIES` or check that the URL points to a video with audio.

**FFmpeg conversion fails**
Check `downloader.log` — the full FFmpeg stderr is recorded there. Ensure FFmpeg is installed and reachable: `ffmpeg -version`.

**Downloads are slow**
yt-dlp throttle is a server-side limitation. The script sends a real browser User-Agent and uses the `web` + `android` player clients to minimise throttling, but some rate-limiting is outside the script's control.

**Resume after interruption**
Just run the script again. URLs already in `completed_urls.txt` are skipped automatically.

---

## Project Structure

```
batch-downloader/
├── batch_downloader.py   # main script — the only file you need
├── README.md
└── .gitignore
```

A minimal `.gitignore` for this project:

```gitignore
# Downloaded media
Downloads/

# Tracking files
completed_urls.txt
failed_urls.txt

# Logs
downloader.log
*.log

# Python
__pycache__/
*.pyc
.venv/
```

---

## Dependencies

| Package | Purpose | Auto-installed |
|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Video downloading and format selection | ✓ Yes |
| [FFmpeg](https://ffmpeg.org) | Video/audio muxing and MP3 conversion | Manual |
| [ffprobe](https://ffmpeg.org) | Audio stream verification (bundled with FFmpeg) | Manual |

All other imports (`json`, `logging`, `re`, `shutil`, `subprocess`, `pathlib`, etc.) are Python standard library — no additional packages required.

---

## License

MIT License — see [LICENSE](LICENSE) for details.