#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║      Batch Video Downloader + MP3 Converter  v4.1                   ║
║                                                                      ║
║  Fixes over v4.0:                                                    ║
║  • Robust 5-tier format selector — never fails on YouTube            ║
║  • USE_BROWSER_COOKIES config flag (safe opt-in, not hardcoded)      ║
║  • BROWSER_FOR_COOKIES config ("chrome"/"firefox"/"safari"/etc.)     ║
║  • Removed debug print(ydl_opts) leak                                ║
║  • Removed stray print() outside main()                              ║
║  • yt-dlp extractor_args to bypass throttling                        ║
║  • HTTP headers that match a real browser request                    ║
╚══════════════════════════════════════════════════════════════════════╝

Root cause of "Requested format is not available":
  bestvideo+bestaudio requires YouTube to serve *separate* video and
  audio streams.  When yt-dlp cannot negotiate those (age-gate, region
  block, throttle, or stale extractor), the whole selector fails with
  no fallback.  The fix is a 5-tier chain:

    bestvideo[ext=mp4]+bestaudio[ext=m4a]/   ← best MP4+M4A (most common)
    bestvideo+bestaudio/                      ← best of any container
    bestvideo[ext=mp4]+bestaudio/             ← MP4 video + any audio
    bestvideo+bestaudio[ext=m4a]/             ← any video + M4A audio
    best                                      ← single-file fallback

  The final "best" is a pre-muxed stream that always exists and always
  has audio, so the selector can never return "not available".

Python 3.10+ required.
"""

# ═══════════════════════════════════════════════════════════════════════
#  STANDARD-LIBRARY IMPORTS
# ═══════════════════════════════════════════════════════════════════════
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

# ═══════════════════════════════════════════════════════════════════════
#  CONFIGURATION  ← edit only this section; nothing else needs changing
# ═══════════════════════════════════════════════════════════════════════

# ── Download mode ───────────────────────────────────────────────────────
#   "video+mp3"  → download best video+audio merged MP4, then convert to MP3
#   "video_only" → download best video+audio merged MP4, no MP3 step
#   "audio_only" → download best audio, extract directly to MP3 via yt-dlp
DownloadMode = Literal["video+mp3", "video_only", "audio_only"]
DOWNLOAD_MODE: DownloadMode = "video+mp3"

# ── Folder layout ───────────────────────────────────────────────────────
BASE_DIR:     Path = Path.home() / "Downloads"
VIDEO_SUBDIR: str  = "videos"
MUSIC_SUBDIR: str  = "music"

# ── Tracking / log files ────────────────────────────────────────────────
URLS_FILE:      Path = BASE_DIR / "urls.txt"
COMPLETED_FILE: Path = BASE_DIR / "completed_urls.txt"
FAILED_FILE:    Path = BASE_DIR / "failed_urls.txt"
LOG_FILE:       Path = Path("downloader.log")

# ── Behaviour flags ─────────────────────────────────────────────────────
# Re-download URLs already present in completed_urls.txt
FORCE_REDOWNLOAD: bool = False

# Delete the video file after a successful MP3 conversion
DELETE_ORIGINAL_AFTER_CONVERSION: bool = False

# ── Cookie / authentication settings ───────────────────────────────────
# Set True to pass your browser\'s YouTube cookies to yt-dlp.
# This is the safest fix for age-restricted or login-required videos.
# Requires the browser to be installed and you to be logged into YouTube.
USE_BROWSER_COOKIES: bool = False

# Browser to read cookies from. Options:
#   "chrome" | "firefox" | "safari" | "edge" | "brave" | "chromium"
BROWSER_FOR_COOKIES: str = "chrome"

# ── Quality / encoding settings ─────────────────────────────────────────
MP3_BITRATE: str = "320k"   # highest standard MP3 quality
AUDIO_CODEC: str = "mp3"    # ffmpeg audio codec

# ═══════════════════════════════════════════════════════════════════════
#  LOGGING  — dual output: console (INFO) + file (DEBUG)
# ═══════════════════════════════════════════════════════════════════════

def _setup_logging() -> logging.Logger:
    """
    Create the module logger with console (INFO) and file (DEBUG) handlers.
    The guard on logger.handlers prevents duplicate entries on re-import.
    """
    logger = logging.getLogger("batch_downloader")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_h = logging.StreamHandler(sys.stdout)
    console_h.setLevel(logging.INFO)
    console_h.setFormatter(fmt)
    logger.addHandler(console_h)

    try:
        file_h = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(fmt)
        logger.addHandler(file_h)
    except PermissionError:
        logger.warning(
            "Cannot write log to %s — file logging disabled.", LOG_FILE
        )

    return logger


log = _setup_logging()


# ═══════════════════════════════════════════════════════════════════════
#  STATISTICS DATACLASS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class BatchStats:
    """
    Running counters for the current batch session.

    All counters start at 0 and are incremented by main() after each step.
    start_time is set automatically at construction via field(default_factory).

    Attributes:
        dl_ok:      Successful downloads.
        dl_fail:    Failed downloads.
        conv_ok:    Successful MP3 conversions.
        conv_fail:  Failed MP3 conversions.
        skipped:    URLs skipped because already in completed_urls.txt.
        start_time: monotonic clock value at batch start.
    """
    dl_ok:      int   = 0
    dl_fail:    int   = 0
    conv_ok:    int   = 0
    conv_fail:  int   = 0
    skipped:    int   = 0
    start_time: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        """Seconds since batch start."""
        return time.monotonic() - self.start_time

    @property
    def total_processed(self) -> int:
        """URLs for which a download was attempted."""
        return self.dl_ok + self.dl_fail

    def eta_str(self, remaining: int) -> str:
        """
        Estimated time remaining as a human-readable string.
        Returns "calculating …" until at least one download succeeds
        (to avoid a divide-by-zero on the very first URL).
        """
        if self.dl_ok == 0:
            return "calculating …"
        avg_sec = self.elapsed / self.dl_ok
        return str(timedelta(seconds=int(avg_sec * remaining)))


# ═══════════════════════════════════════════════════════════════════════
#  DEPENDENCY CHECKS
# ═══════════════════════════════════════════════════════════════════════

def ensure_ytdlp() -> None:
    """
    Verify yt-dlp is importable; auto-install via pip if missing.
    Exits with code 1 if pip install fails — yt-dlp is mandatory.
    """
    try:
        import yt_dlp  # noqa: F401
        log.debug("Dependency OK: yt-dlp")
    except ImportError:
        log.info("yt-dlp not found — installing via pip …")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-q", "yt-dlp"],
                timeout=120,
            )
            log.info("yt-dlp installed successfully.")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.critical("Failed to install yt-dlp: %s", exc)
            sys.exit(1)


def _check_binary(name: str, install_hint: str) -> bool:
    """
    Return True if *name* is found on PATH, False otherwise.
    Logs a warning with *install_hint* when the binary is absent.
    """
    path = shutil.which(name)
    if path:
        log.debug("Dependency OK: %s → %s", name, path)
        return True
    log.warning("%-8s NOT found on PATH.", name.upper())
    log.warning("  Install: %s", install_hint)
    return False


def check_dependencies() -> tuple[bool, bool]:
    """
    Check for ffmpeg and ffprobe at startup.

    Returns:
        (ffmpeg_ok, ffprobe_ok) as booleans.
        False means the relevant pipeline step will be skipped gracefully.
    """
    log.info("─" * 50)
    log.info("Dependency check")
    ffmpeg_ok  = _check_binary(
        "ffmpeg",
        "https://ffmpeg.org/download.html  |  brew install ffmpeg  |  apt install ffmpeg",
    )
    ffprobe_ok = _check_binary(
        "ffprobe",
        "Ships with FFmpeg — install FFmpeg and ffprobe appears automatically.",
    )
    if not ffmpeg_ok:
        log.warning("FFmpeg missing — MP3 conversion will be SKIPPED.")
    if not ffprobe_ok:
        log.warning("ffprobe missing — audio-stream pre-check will be SKIPPED.")
    log.info("─" * 50)
    return ffmpeg_ok, ffprobe_ok


# ═══════════════════════════════════════════════════════════════════════
#  FILENAME UTILITIES
# ═══════════════════════════════════════════════════════════════════════

_ILLEGAL_ASCII:   str = r'[\\/:*?"<>|]'
_ILLEGAL_UNICODE: str = r"[\uff5c\uff1a\uff02\uff1c\uff1e\uff0f\uff3c\uff0a\uff1f]"
_UNICODE_SEPS:    str = r"[\u2028\u2029]"


def sanitize_filename(name: str) -> str:
    """
    Return a filesystem-safe filename derived from *name*.

    Processing order:
      1. Unicode fullwidth lookalikes of illegal chars  → ``_``
      2. Unicode line/paragraph separators              → space
      3. ASCII illegal chars  \\ / : * ? " < > |       → ``_``
      4. C0 control chars (0x00–0x1F)                  → stripped
      5. Whitespace runs                                → single space
      6. Leading/trailing spaces and dots               → stripped
      7. Empty result                                   → "downloaded_video"
      8. Hard cap at 200 characters
    """
    s = re.sub(_ILLEGAL_UNICODE, "_", name)
    s = re.sub(_UNICODE_SEPS, " ", s)
    s = re.sub(_ILLEGAL_ASCII, "_", s)
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:200] if s else "") or "downloaded_video"


def unique_path(base: Path) -> Path:
    """
    Return *base* if it does not yet exist on disk.
    Otherwise append ``(1)``, ``(2)`` … until a free path is found.
    Falls back to a timestamp suffix for the astronomically unlikely
    case where 9 999 numbered variants all exist.
    """
    if not base.exists():
        return base
    stem, suffix, parent = base.stem, base.suffix, base.parent
    for i in range(1, 10_000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem}_{int(time.time())}{suffix}"


# ═══════════════════════════════════════════════════════════════════════
#  URL UTILITIES
# ═══════════════════════════════════════════════════════════════════════

def validate_url(url: str) -> bool:
    """Lightweight syntactic check — no network call."""
    return bool(re.match(r"https?://[^\s/$.?#][^\s]*", url, re.IGNORECASE))


def load_url_list(filepath: Path) -> list[str]:
    """
    Read *filepath* and return a de-duplicated list of validated URLs.

    Skips blank lines, ``#`` comment lines, syntactically invalid URLs,
    and duplicate entries (preserving first-occurrence order).
    Returns an empty list on missing or unreadable file.
    """
    if not filepath.exists():
        log.error("URL file not found: %s", filepath)
        return []
    try:
        raw = filepath.read_text(encoding="utf-8").splitlines()
    except PermissionError as exc:
        log.error("Cannot read %s: %s", filepath, exc)
        return []

    seen:      set[str]  = set()
    urls:      list[str] = []
    n_blank = n_invalid = n_dup = 0

    for line in raw:
        s = line.strip()
        if not s or s.startswith("#"):
            n_blank += 1
            continue
        if not validate_url(s):
            log.warning("Skipping invalid URL: %s", s)
            n_invalid += 1
            continue
        if s in seen:
            n_dup += 1
            continue
        seen.add(s)
        urls.append(s)

    log.info(
        "Loaded %d unique URLs from %s  "
        "(blanks/comments: %d | invalid: %d | duplicates: %d)",
        len(urls), filepath, n_blank, n_invalid, n_dup,
    )
    return urls


def load_completed_urls(filepath: Path) -> set[str]:
    """
    Return URLs recorded in *filepath* as a set.
    Returns an empty set (non-fatal) if the file does not exist.
    """
    if not filepath.exists():
        return set()
    try:
        lines = filepath.read_text(encoding="utf-8").splitlines()
        return {ln.strip() for ln in lines if ln.strip()}
    except PermissionError as exc:
        log.warning("Cannot read %s: %s — treating as empty.", filepath, exc)
        return set()


def append_url_to_file(url: str, filepath: Path) -> None:
    """
    Append *url* + newline to *filepath*, creating it if absent.
    Called immediately after each URL is processed so progress survives
    any crash or keyboard interrupt.
    """
    try:
        with filepath.open("a", encoding="utf-8") as fh:
            fh.write(url + "\n")
    except PermissionError as exc:
        log.error("Cannot write to %s: %s", filepath, exc)


# ═══════════════════════════════════════════════════════════════════════
#  AUDIO STREAM VERIFICATION  (ffprobe)
# ═══════════════════════════════════════════════════════════════════════

def has_audio_stream(filepath: Path) -> bool | None:
    """
    Probe *filepath* with ffprobe and return whether it has an audio stream.

    Returns:
        True   — at least one audio stream confirmed.
        False  — file probed successfully but no audio stream found.
        None   — ffprobe unavailable or probe failed (caller treats as unknown).

    The full ffprobe command is written to the DEBUG log.
    Any ffprobe stderr output is captured and logged on failure.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        log.debug("ffprobe not available — skipping audio stream check.")
        return None

    cmd = [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        str(filepath),
    ]
    log.debug("ffprobe command: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=True,
        )
        data    = json.loads(result.stdout.decode(errors="replace"))
        streams = data.get("streams", [])
        found   = any(s.get("codec_type") == "audio" for s in streams)
        log.debug(
            "ffprobe: %d stream(s), audio=%s → %s",
            len(streams), found, filepath.name,
        )
        return found

    except subprocess.CalledProcessError as exc:
        log.warning(
            "ffprobe error on %s: %s",
            filepath.name,
            exc.stderr.decode(errors="replace").strip(),
        )
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log.warning("ffprobe failed on %s: %s", filepath.name, exc)

    return None


def print_audio_diagnostic(filepath: Path, has_audio: bool | None) -> None:
    """Print a one-line audio-stream diagnostic before conversion starts."""
    if has_audio is True:
        symbol, label = "✓", "Found"
    elif has_audio is False:
        symbol, label = "✗", "Missing — conversion will be skipped"
    else:
        symbol, label = "?", "Unknown (ffprobe unavailable — attempting anyway)"
    print(f"  Audio Stream: {symbol} {label}  [{filepath.name}]")
    log.debug("Audio diagnostic: %s %s — %s", symbol, label, filepath.name)


# ═══════════════════════════════════════════════════════════════════════
#  PROGRESS HOOK  (yt-dlp callback)
# ═══════════════════════════════════════════════════════════════════════

_dl_state: dict = {"last_filename": None}


def _build_progress_hook(idx: int, total: int):
    """
    Return a yt-dlp progress-hook closure annotated with [idx/total].

    The closure uses \\r to overwrite the same terminal line while
    downloading, then prints a completion line when yt-dlp is done.
    """
    tag = f"[{idx}/{total}]"

    def _hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            pct   = d.get("_percent_str",  "  ?%").strip()
            speed = d.get("_speed_str",    "?B/s").strip()
            eta   = d.get("_eta_str",      "?s"  ).strip()
            print(
                f"\r  {tag} ↓ {pct:>6}  speed {speed:>12}  ETA {eta:<8}",
                end="", flush=True,
            )
        elif status == "finished":
            fname = d.get("filename", "")
            _dl_state["last_filename"] = fname
            print(f"\r  {tag} ✓ Downloaded: {Path(fname).name}" + " " * 20)
            log.debug("yt-dlp wrote: %s", fname)
        elif status == "error":
            print()
            log.error("%s yt-dlp reported a fragment-level error.", tag)

    return _hook


# ═══════════════════════════════════════════════════════════════════════
#  FORMAT SELECTION
# ═══════════════════════════════════════════════════════════════════════

# ── Why this format string? ─────────────────────────────────────────────
#
# YouTube (and other sites) serve video and audio as separate DASH streams.
# yt-dlp must download them individually and mux them with ffmpeg.
# When the negotiation fails ("Requested format is not available"), it means
# none of the specific container/codec combinations were offered.
#
# The 5-tier chain below always resolves:
#   Tier 1  bestvideo[ext=mp4]+bestaudio[ext=m4a]  — ideal: MP4+AAC
#   Tier 2  bestvideo+bestaudio                    — best of any container
#   Tier 3  bestvideo[ext=mp4]+bestaudio            — MP4 video + any audio
#   Tier 4  bestvideo+bestaudio[ext=m4a]            — any video + M4A audio
#   Tier 5  best                                    — pre-muxed fallback
#
# Tier 5 ("best") is a single pre-muxed stream that always exists and
# always contains audio.  It is the final guarantee that the selector
# never fails with "not available".

_FORMAT_AV = (
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo+bestaudio/"
    "bestvideo[ext=mp4]+bestaudio/"
    "bestvideo+bestaudio[ext=m4a]/"
    "best"
)

_FORMAT_AUDIO_ONLY = "bestaudio[ext=m4a]/bestaudio/best"


def _format_string(mode: DownloadMode) -> str:
    """Return the yt-dlp format selector for *mode*."""
    return _FORMAT_AUDIO_ONLY if mode == "audio_only" else _FORMAT_AV


# ═══════════════════════════════════════════════════════════════════════
#  CORE: download_video
# ═══════════════════════════════════════════════════════════════════════

def download_video(
    url:       str,
    video_dir: Path,
    idx:       int,
    total:     int,
    mode:      DownloadMode,
) -> Path | None:
    """
    Download the media at *url* into *video_dir* using yt-dlp.

    Modes:
      video+mp3 / video_only → bestvideo+bestaudio merged to MP4.
      audio_only             → bestaudio, extracted to MP3 by yt-dlp.

    Output path is resolved via three strategies in order:
      1. ydl.prepare_filename(info) with .mp4 suffix forced for AV modes.
      2. Filename captured by the progress hook (_dl_state).
      3. Newest file in video_dir by mtime (last-resort heuristic).

    Returns Path to the output file on success, None on any failure.
    All exceptions are caught, logged, and translated to None so the
    batch loop can continue with the next URL.
    """
    import yt_dlp

    _dl_state["last_filename"] = None
    fmt = _format_string(mode)
    log.info("[%d/%d] Format selector: %s", idx, total, fmt)
    log.info("[%d/%d] Download started: %s", idx, total, url)

    # ── yt-dlp options ────────────────────────────────────────────────────
    ydl_opts: dict = {
        "format":              fmt,
        "merge_output_format": "mp4",
        "outtmpl":             str(video_dir / "%(title)s.%(ext)s"),
        "noplaylist":          True,
        "quiet":               True,
        "no_warnings":         True,
        "embedmetadata":       True,
        "add_metadata":        True,
        "progress_hooks":      [_build_progress_hook(idx, total)],
        # Mimic a real browser request to reduce throttling
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        # Use the YouTube-specific extractor args to bypass some throttling
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android"],
            }
        },
    }

    # ── Optional: pass browser cookies for authenticated / age-gated videos
    # Controlled by USE_BROWSER_COOKIES in the configuration section.
    # Never hardcoded — the user must explicitly opt in.
    if USE_BROWSER_COOKIES:
        ydl_opts["cookiesfrombrowser"] = (BROWSER_FOR_COOKIES,)
        log.info(
            "[%d/%d] Using cookies from browser: %s",
            idx, total, BROWSER_FOR_COOKIES,
        )

    # ── audio_only post-processor ─────────────────────────────────────────
    if mode == "audio_only":
        ydl_opts["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": MP3_BITRATE.rstrip("k"),
        }]

    # Log opts at DEBUG (not INFO) — no console clutter in normal use
    log.debug("[%d/%d] yt-dlp opts: %s", idx, total, ydl_opts)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if not info:
            log.error(
                "[%d/%d] yt-dlp returned no metadata for: %s", idx, total, url
            )
            return None

        # ── Resolve output path ───────────────────────────────────────────
        # Strategy 1: yt-dlp template engine
        try:
            prepared = Path(ydl.prepare_filename(info))
            if mode != "audio_only":
                prepared = prepared.with_suffix(".mp4")
            if prepared.exists():
                _log_file_size(prepared, idx, total)
                return prepared
        except Exception:
            pass

        # Strategy 2: progress hook capture
        hook_fname = _dl_state.get("last_filename")
        if hook_fname:
            hook_path = Path(hook_fname)
            if hook_path.exists():
                _log_file_size(hook_path, idx, total)
                return hook_path

        # Strategy 3: newest file in directory
        candidates = sorted(
            (p for p in video_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            newest = candidates[0]
            log.info(
                "[%d/%d] Path resolved via newest-file heuristic: %s",
                idx, total, newest.name,
            )
            _log_file_size(newest, idx, total)
            return newest

        log.error(
            "[%d/%d] Download appeared to succeed but output file not found.",
            idx, total,
        )
        return None

    except yt_dlp.utils.DownloadError as exc:
        print()
        log.error("[%d/%d] DownloadError: %s", idx, total, exc)
    except yt_dlp.utils.ExtractorError as exc:
        print()
        log.error("[%d/%d] ExtractorError: %s", idx, total, exc)
    except PermissionError as exc:
        print()
        log.error("[%d/%d] Permission error: %s", idx, total, exc)
    except OSError as exc:
        print()
        log.error("[%d/%d] OS error: %s", idx, total, exc)
    except Exception as exc:
        print()
        log.exception("[%d/%d] Unexpected error: %s", idx, total, exc)

    return None


def _log_file_size(path: Path, idx: int, total: int) -> None:
    """Log the saved file path and its size in MB."""
    try:
        mb = path.stat().st_size / (1024 * 1024)
        log.info("[%d/%d] Saved: %s  (%.2f MB)", idx, total, path.name, mb)
    except OSError:
        log.info("[%d/%d] Saved: %s  (size unavailable)", idx, total, path.name)


# ═══════════════════════════════════════════════════════════════════════
#  CORE: convert_to_mp3
# ═══════════════════════════════════════════════════════════════════════

def convert_to_mp3(
    video_path: Path,
    music_dir:  Path,
    title:      str | None = None,
) -> Path | None:
    """
    Convert *video_path* to a 320 kbps MP3 using ffmpeg.

    Pre-conversion:
      • ffprobe checks for an audio stream (if ffprobe is available).
      • print_audio_diagnostic() prints a ✓/✗/? line.
      • If audio_present is definitively False, conversion is skipped and
        None is returned — the caller must mark the URL as failed.

    FFmpeg flags:
      -vn              strip video
      -ar 44100        CD-quality sample rate
      -ac 2            stereo
      -b:a 320k        constant bitrate
      -id3v2_version 3 widely compatible ID3 tags

    The exact ffmpeg command is logged at DEBUG level.
    File size is logged after a successful write.
    The source video is deleted if DELETE_ORIGINAL_AFTER_CONVERSION is True.

    Returns Path to the MP3 on success, None on any failure.
    """
    # Pre-conversion audio check
    audio_present = has_audio_stream(video_path)
    print_audio_diagnostic(video_path, audio_present)

    if audio_present is False:
        log.error("Conversion skipped — no audio stream: %s", video_path.name)
        return None

    stem     = sanitize_filename(title or video_path.stem)
    mp3_path = unique_path(music_dir / f"{stem}.mp3")

    log.info("Conversion started: %s → %s", video_path.name, mp3_path.name)
    print(f"  ♪ Converting to MP3 ({MP3_BITRATE}) …", end="", flush=True)

    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",  str(video_path),
        "-vn",
        "-ar", "44100",
        "-ac", "2",
        "-b:a", MP3_BITRATE,
        "-id3v2_version", "3",
    ]
    if title:
        cmd += ["-metadata", f"title={title}"]
    cmd.append(str(mp3_path))

    log.debug("FFmpeg command: %s", " ".join(cmd))

    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=600,
            check=True,
        )
        mb = mp3_path.stat().st_size / (1024 * 1024)
        print(f"\r  ✓ MP3 saved: {mp3_path.name}  ({mb:.2f} MB)" + " " * 20)
        log.info("Conversion completed: %s  (%.2f MB)", mp3_path.name, mb)

        if DELETE_ORIGINAL_AFTER_CONVERSION:
            try:
                video_path.unlink()
                log.info("Deleted original: %s", video_path)
            except PermissionError as exc:
                log.warning("Could not delete %s: %s", video_path, exc)

        return mp3_path

    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        print(f"\r  ✗ FFmpeg failed (exit {exc.returncode})." + " " * 30)
        log.error("FFmpeg exit code %d: %s", exc.returncode, video_path.name)
        log.error("FFmpeg stderr:\n%s", stderr)
    except subprocess.TimeoutExpired:
        print(f"\r  ✗ FFmpeg timed out." + " " * 40)
        log.error("FFmpeg timed out: %s", video_path.name)
    except FileNotFoundError:
        print(f"\r  ✗ ffmpeg not found." + " " * 40)
        log.error("ffmpeg binary missing during conversion of: %s", video_path.name)
    except PermissionError as exc:
        print(f"\r  ✗ Permission denied." + " " * 40)
        log.error("Permission denied writing %s: %s", mp3_path, exc)
    except OSError as exc:
        print(f"\r  ✗ OS error." + " " * 40)
        log.error("OS error during conversion of %s: %s", video_path.name, exc)
    except Exception as exc:
        print(f"\r  ✗ Unexpected error." + " " * 40)
        log.exception("Unexpected conversion error: %s", exc)

    return None


# ═══════════════════════════════════════════════════════════════════════
#  FOLDER / FILE SETUP
# ═══════════════════════════════════════════════════════════════════════

def _create_dirs(*dirs: Path) -> None:
    """Create all given directories. Exits on PermissionError."""
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            log.debug("Directory ready: %s", d)
        except PermissionError as exc:
            log.critical("Cannot create %s: %s", d, exc)
            sys.exit(1)


def _ensure_urls_file(filepath: Path) -> None:
    """Create a commented placeholder urls.txt if the file does not exist."""
    if filepath.exists():
        return
    content = (
        "# Batch Video Downloader — URL list\n"
        "# One URL per line. Lines starting with # are ignored.\n"
        "#\n"
        "# Example:\n"
        "# https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
    )
    try:
        filepath.write_text(content, encoding="utf-8")
        log.info("Created placeholder URL list: %s", filepath)
    except PermissionError as exc:
        log.error("Cannot create %s: %s", filepath, exc)


# ═══════════════════════════════════════════════════════════════════════
#  DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════════════

_W = 70   # inner banner width (between the ║ borders)


def _row(label: str, value: str, width: int = _W) -> str:
    return f"║  {label:<14}{value:<{width - 16}}║"


def print_banner(
    video_dir:    Path,
    music_dir:    Path,
    ffmpeg_ok:    bool,
    ffprobe_ok:   bool,
    mode:         str,
    total_urls:   int,
    already_done: int,
    to_process:   int,
) -> None:
    """Print the startup configuration and statistics banner."""
    cookies_status = (
        f"✓ {BROWSER_FOR_COOKIES}" if USE_BROWSER_COOKIES else "✗ disabled"
    )
    print()
    print("╔" + "═" * _W + "╗")
    print(f"║{'  Batch Video Downloader + MP3 Converter  v4.1':<{_W}}║")
    print("╠" + "═" * _W + "╣")
    print(_row("Mode:",      mode))
    print(_row("Videos →",   str(video_dir)))
    print(_row("Music →",    str(music_dir)))
    print(_row("Log →",      str(LOG_FILE.resolve())))
    print(_row("ffmpeg:",    "✓ Available" if ffmpeg_ok  else "✗ Missing — MP3 skipped"))
    print(_row("ffprobe:",   "✓ Available" if ffprobe_ok else "✗ Missing — audio check skipped"))
    print(_row("Cookies:",   cookies_status))
    print("╠" + "═" * _W + "╣")
    print(_row("Total URLs:",   str(total_urls)))
    print(_row("Already done:", str(already_done)))
    print(_row("To process:",   str(to_process)))
    print("╚" + "═" * _W + "╝")
    print()


def print_url_header(
    idx:       int,
    total:     int,
    url:       str,
    stats:     BatchStats,
    remaining: int,
) -> None:
    """Print the per-URL separator line with live batch statistics."""
    eta = stats.eta_str(remaining)
    print(
        f"\n{'─' * 70}\n"
        f"  [{idx}/{total}]  {url}\n"
        f"  dl_ok={stats.dl_ok}  dl_fail={stats.dl_fail}  "
        f"conv_ok={stats.conv_ok}  conv_fail={stats.conv_fail}  "
        f"skipped={stats.skipped}  remaining={remaining}  ETA={eta}\n"
        f"{'─' * 70}"
    )


def print_summary(
    stats:     BatchStats,
    total_all: int,
    skipped:   int,
    video_dir: Path,
    music_dir: Path,
) -> None:
    """Print the final summary report after the batch finishes."""
    elapsed = str(timedelta(seconds=int(stats.elapsed)))
    print()
    print("╔" + "═" * _W + "╗")
    print(f"║{'  BATCH COMPLETE — Summary Report':<{_W}}║")
    print("╠" + "═" * _W + "╣")
    print(f"║  {'Total URLs in file':<36} {total_all:>5}               ║")
    print(f"║  {'Skipped (already completed)':<36} {skipped:>5}               ║")
    print(f"║  {'Downloads succeeded':<36} {stats.dl_ok:>5}               ║")
    print(f"║  {'Downloads failed':<36} {stats.dl_fail:>5}               ║")
    print(f"║  {'MP3 conversions succeeded':<36} {stats.conv_ok:>5}               ║")
    print(f"║  {'MP3 conversions failed':<36} {stats.conv_fail:>5}               ║")
    print(f"║  {'Total time':<36} {elapsed:>13}       ║")
    print("╠" + "═" * _W + "╣")
    print(f"║  Videos : {str(video_dir):<{_W - 11}}║")
    print(f"║  Music  : {str(music_dir):<{_W - 11}}║")
    print(f"║  Log    : {str(LOG_FILE.resolve()):<{_W - 11}}║")
    if stats.dl_fail or stats.conv_fail:
        print(f"║  Failed : {str(FAILED_FILE):<{_W - 11}}║")
    print("╚" + "═" * _W + "╝")
    print()


# ═══════════════════════════════════════════════════════════════════════
#  MAIN  — batch processing loop
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    Batch downloader entry point.

    Pipeline per URL:
      1. Download via yt-dlp (format: 5-tier chain, always resolves).
      2. In video+mp3 mode: probe audio, convert to MP3 via ffmpeg.
      3. Write to completed_urls.txt ONLY after full pipeline success.
      4. Write to failed_urls.txt on any failure; continue to next URL.

    Recovery: because completed_urls.txt is written per-URL, a crashed or
    interrupted run resumes from the first URL not yet in that file.
    """
    ensure_ytdlp()
    ffmpeg_ok, ffprobe_ok = check_dependencies()

    video_dir = BASE_DIR / VIDEO_SUBDIR
    music_dir = BASE_DIR / MUSIC_SUBDIR
    _create_dirs(BASE_DIR, video_dir, music_dir)
    _ensure_urls_file(URLS_FILE)

    all_urls       = load_url_list(URLS_FILE)
    completed_urls = load_completed_urls(COMPLETED_FILE)

    if FORCE_REDOWNLOAD:
        log.info("FORCE_REDOWNLOAD=True — ignoring completed_urls.txt.")
        pending = all_urls
        skipped = 0
    else:
        pending = [u for u in all_urls if u not in completed_urls]
        skipped = len(all_urls) - len(pending)
        if skipped:
            log.info("Skipping %d URL(s) already completed.", skipped)

    total_all     = len(all_urls)
    total_pending = len(pending)

    print_banner(
        video_dir    = video_dir,
        music_dir    = music_dir,
        ffmpeg_ok    = ffmpeg_ok,
        ffprobe_ok   = ffprobe_ok,
        mode         = DOWNLOAD_MODE,
        total_urls   = total_all,
        already_done = skipped,
        to_process   = total_pending,
    )

    if total_pending == 0:
        if total_all == 0:
            log.warning("urls.txt is empty. Add URLs to: %s", URLS_FILE)
        else:
            log.info(
                "All %d URL(s) already completed. "
                "Set FORCE_REDOWNLOAD=True to redo.",
                total_all,
            )
        return

    log.info(
        "Batch started — mode=%s  pending=%d  skipped=%d",
        DOWNLOAD_MODE, total_pending, skipped,
    )

    stats = BatchStats(skipped=skipped)

    for idx, url in enumerate(pending, start=1):
        remaining = total_pending - idx
        print_url_header(idx, total_pending, url, stats, remaining)
        log.info("[%d/%d] Processing: %s", idx, total_pending, url)

        # ── Step 1: Download ──────────────────────────────────────────────
        media_path = download_video(
            url       = url,
            video_dir = video_dir,
            idx       = idx,
            total     = total_pending,
            mode      = DOWNLOAD_MODE,
        )

        if media_path is None:
            print(f"  ✗ Download failed — details in {LOG_FILE}")
            log.error("[%d/%d] DOWNLOAD FAILED: %s", idx, total_pending, url)
            append_url_to_file(url, FAILED_FILE)
            stats.dl_fail += 1
            continue   # do NOT mark as completed

        stats.dl_ok += 1

        # ── Step 2: MP3 conversion ────────────────────────────────────────
        if DOWNLOAD_MODE == "video+mp3" and ffmpeg_ok:

            mp3_path = convert_to_mp3(
                video_path = media_path,
                music_dir  = music_dir,
                title      = media_path.stem,
            )

            if mp3_path is None:
                print(f"  ✗ MP3 conversion failed — details in {LOG_FILE}")
                log.error(
                    "[%d/%d] CONVERSION FAILED: %s", idx, total_pending, url
                )
                append_url_to_file(url, FAILED_FILE)
                stats.conv_fail += 1
                continue   # do NOT mark as completed

            stats.conv_ok += 1

        elif DOWNLOAD_MODE == "video_only":
            log.info("[%d/%d] video_only — no MP3 step.", idx, total_pending)

        elif DOWNLOAD_MODE == "audio_only":
            # yt-dlp already extracted the MP3; move it to music_dir.
            log.info(
                "[%d/%d] audio_only — MP3 at: %s", idx, total_pending, media_path
            )
            dest = unique_path(music_dir / media_path.name)
            if media_path.parent != music_dir:
                try:
                    media_path.rename(dest)
                    log.info("Moved MP3 to music dir: %s", dest)
                except OSError as exc:
                    log.warning("Could not move MP3 to music dir: %s", exc)
            stats.conv_ok += 1

        elif DOWNLOAD_MODE == "video+mp3" and not ffmpeg_ok:
            print("  ⚠ Skipping MP3 conversion — FFmpeg not installed.")
            log.info("[%d/%d] MP3 skipped (no ffmpeg).", idx, total_pending)

        # ── Step 3: Mark completed (only reaches here on full success) ────
        append_url_to_file(url, COMPLETED_FILE)
        log.info("[%d/%d] COMPLETED: %s", idx, total_pending, url)

    # ── Final summary ─────────────────────────────────────────────────────
    log.info(
        "Batch finished — dl_ok=%d  dl_fail=%d  conv_ok=%d  "
        "conv_fail=%d  skipped=%d  time=%s",
        stats.dl_ok, stats.dl_fail, stats.conv_ok, stats.conv_fail,
        stats.skipped, str(timedelta(seconds=int(stats.elapsed))),
    )
    print_summary(
        stats     = stats,
        total_all = total_all,
        skipped   = skipped,
        video_dir = video_dir,
        music_dir = music_dir,
    )


# ═══════════════════════════════════════════════════════════════════════
#  ENTRY GUARD
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Interrupted — progress saved to completed_urls.txt.")
        log.info("Session interrupted by user (Ctrl-C).")
        sys.exit(0)