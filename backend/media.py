"""ffprobe/ffmpeg/yt-dlp helpers.

Everything that hands user input to an external tool goes through here, so
the checks live in one place.
"""
import array
import io
import json
import logging
import math
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

# ffmpeg picks a demuxer by sniffing the content, not the extension, so a
# renamed playlist or concat list could make it open other files or URLs.
# Only these two are allowed; the value is the extension we store the file as.
DEMUXERS = {'mov': '.mp4', 'matroska': '.webm'}
UPLOAD_EXTENSIONS = {'.mp4', '.m4v', '.mov', '.webm'}
_LOCKDOWN = ['-protocol_whitelist', 'file', '-format_whitelist', ','.join(DEMUXERS)]

SAMPLE_RATE = 16000
FRAME_SECONDS = 0.05


class MediaError(Exception):
    """Something is wrong with the input; the message is shown to the user."""


def probe(path, timeout=30):
    """Return (demuxer, duration_seconds) for a file we are willing to process."""
    cmd = ['ffprobe', '-v', 'error', *_LOCKDOWN,
           '-show_entries', 'format=format_name,duration:stream=codec_type',
           '-of', 'json', str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError('파일을 분석하는 데 너무 오래 걸려요.')
    if result.returncode != 0:
        raise MediaError('지원하지 않는 파일이에요. MP4 또는 WebM 영상을 올려 주세요.')

    info = json.loads(result.stdout or '{}')
    fmt = info.get('format', {})
    demuxer = (fmt.get('format_name') or '').split(',')[0]
    if demuxer not in DEMUXERS:
        raise MediaError('지원하지 않는 파일이에요. MP4 또는 WebM 영상을 올려 주세요.')
    try:
        duration = float(fmt.get('duration'))
    except (TypeError, ValueError):
        duration = math.nan
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError('영상 길이를 확인할 수 없어요.')
    if not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])):
        raise MediaError('소리가 없는 영상이에요.')
    return demuxer, duration


def extract_wav(src, dst, max_seconds, timeout=900):
    """Decode the first audio track of `src` to 16 kHz mono WAV at `dst`."""
    demuxer, _ = probe(src)
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', *_LOCKDOWN,
           '-f', demuxer, '-i', str(src),
           '-map', '0:a:0', '-vn', '-sn', '-dn', '-t', str(int(max_seconds) + 1),
           '-ac', '1', '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le', '-y', str(dst)]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning('ffmpeg failed on %s: %s', src, getattr(e, 'stderr', b'')[-500:])
        raise MediaError('오디오를 추출하지 못했어요.')


def wav_duration(path):
    with wave.open(str(path), 'rb') as w:
        return w.getnframes() / w.getframerate()


def loudness_frames(path, frame_seconds=FRAME_SECONDS):
    """RMS level (dBFS) of each frame of a 16-bit mono WAV."""
    levels = []
    with wave.open(str(path), 'rb') as w:
        size = max(1, int(w.getframerate() * frame_seconds))
        while True:
            raw = w.readframes(size)
            if not raw:
                break
            samples = array.array('h', raw)
            if sys.byteorder == 'big':
                samples.byteswap()
            power = math.sumprod(samples, samples) / len(samples)
            # 90.31 dB = 20*log10(32768), full scale for 16-bit samples
            levels.append(10 * math.log10(power) - 90.31 if power > 0 else -120.0)
    return levels


def plan_chunks(levels, total, target=12.0, min_len=5.0, max_len=20.0, frame_seconds=FRAME_SECONDS):
    """Split [0, total] seconds into chunks of at most `max_len` seconds.

    Each cut goes to the quietest moment between `min_len` and `max_len` after
    the previous cut, nudged towards `target`, so cuts land in pauses between
    sentences instead of in the middle of a word.
    """
    if total <= max_len:
        return [(0.0, round(total, 2))]

    # Average over ~250 ms; a short gap inside a word should not look like a pause
    k = 2
    smooth = []
    for i in range(len(levels)):
        window = levels[max(0, i - k):i + k + 1]
        smooth.append(sum(window) / len(window))

    cuts = [0.0]
    while total - cuts[-1] > max_len:
        start = cuts[-1]
        lo = int((start + min_len) / frame_seconds)
        hi = min(int((start + max_len) / frame_seconds), len(smooth) - 1)
        if lo > hi:
            cuts.append(start + max_len)
            continue
        best = min(range(lo, hi + 1),
                   key=lambda i: smooth[i] + 0.5 * abs(i * frame_seconds - start - target))
        cuts.append(round((best + 0.5) * frame_seconds, 2))
    cuts.append(round(total, 2))
    return list(zip(cuts, cuts[1:]))


def wav_slice(path, start, end):
    """Return the [start, end) part of a WAV file as WAV bytes."""
    with wave.open(str(path), 'rb') as src:
        rate = src.getframerate()
        src.setpos(min(int(start * rate), src.getnframes()))
        frames = src.readframes(max(0, int((end - start) * rate)))
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as dst:
            dst.setnchannels(src.getnchannels())
            dst.setsampwidth(src.getsampwidth())
            dst.setframerate(rate)
            dst.writeframes(frames)
    return buf.getvalue()


# --- YouTube ---------------------------------------------------------------

_YOUTUBE_ID = re.compile(r'[A-Za-z0-9_-]{11}')
_YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com'}


def parse_youtube_url(text):
    """Return the 11-character id of a single YouTube video, or None.

    Only the id survives; the server builds its own URL from it, so nothing
    the user typed is ever passed to yt-dlp.
    """
    text = (text or '').strip()
    if not re.match(r'^https?://', text, re.IGNORECASE):
        text = 'https://' + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if parts.username or parts.password or port not in (None, 80, 443):
        return None

    host = (parts.hostname or '').lower()
    if host == 'youtu.be':
        candidate = parts.path.lstrip('/').split('/')[0]
    elif host in _YOUTUBE_HOSTS:
        if parts.path.rstrip('/') == '/watch':
            values = parse_qs(parts.query).get('v', [])
            if len(values) != 1:
                return None
            candidate = values[0]
        else:
            match = re.fullmatch(r'/(?:shorts|embed|live)/([^/]+)/?', parts.path)
            candidate = match.group(1) if match else ''
    else:
        return None
    return candidate if _YOUTUBE_ID.fullmatch(candidate) else None


class _YtLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        logger.info('yt-dlp: %s', msg)

    def error(self, msg):
        logger.warning('yt-dlp: %s', msg)


def _youtube_error(err):
    text = str(err).lower()
    if 'not a bot' in text or 'sign in' in text:
        return 'YouTube가 서버의 요청을 막았어요. 잠시 후 다시 시도하거나 영상 파일을 직접 올려 주세요.'
    if 'private' in text or 'unavailable' in text or 'removed' in text:
        return '볼 수 없는 영상이에요. 비공개이거나 삭제되었을 수 있어요.'
    return 'YouTube 영상을 가져오지 못했어요.'


def download_youtube_audio(video_id, workdir, max_seconds, max_bytes, cookies=None, on_progress=None):
    """Download only the audio track of a YouTube video into `workdir`.

    The page plays the video through YouTube's own embed, so the server never
    stores or re-serves the video itself. Returns (path, info).
    """
    import yt_dlp

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if on_progress and d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total:
                on_progress(min(d.get('downloaded_bytes', 0) / total, 1.0))

    opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio',
        'outtmpl': {'default': str(workdir / 'audio.%(ext)s')},
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'socket_timeout': 20,
        'retries': 3,
        'max_filesize': max_bytes,
        'cachedir': False,
        'logger': _YtLogger(),
        'progress_hooks': [hook],
    }
    if cookies and Path(cookies).is_file():
        # yt-dlp writes the cookie jar back on exit, so give it a private copy
        jar = workdir / 'cookies.txt'
        shutil.copyfile(cookies, jar)
        opts['cookiefile'] = str(jar)

    url = f'https://www.youtube.com/watch?v={video_id}'
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            # ie_key pins the YouTube extractor; the generic one never runs
            info = ydl.extract_info(url, download=False, ie_key='Youtube')
        except yt_dlp.utils.DownloadError as e:
            raise MediaError(_youtube_error(e))

        if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
            raise MediaError('라이브 방송은 처리할 수 없어요.')
        duration = info.get('duration')
        if not duration:
            raise MediaError('영상 길이를 확인할 수 없어요.')
        if duration > max_seconds:
            raise MediaError(f'{int(max_seconds // 60)}분 이하의 영상만 처리할 수 있어요.')
        if info.get('playable_in_embed') is False:
            raise MediaError('영상 소유자가 다른 사이트에서의 재생을 막아 두었어요. 영상 파일을 직접 올려 주세요.')

        try:
            info = ydl.process_ie_result(info, download=True)
        except yt_dlp.utils.DownloadError as e:
            raise MediaError(_youtube_error(e))

    downloads = info.get('requested_downloads') or []
    path = Path(downloads[0]['filepath']) if downloads else None
    if not path or not path.is_file():
        raise MediaError('YouTube 오디오를 가져오지 못했어요. 파일이 너무 클 수 있어요.')
    return path, {'title': info.get('title'), 'duration': duration}
