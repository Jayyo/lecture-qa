import math
import struct
import wave

import pytest

from backend import media
from conftest import needs_ffmpeg

VIDEO_ID = 'dQw4w9WgXcQ'


@pytest.mark.parametrize('url', [
    f'https://www.youtube.com/watch?v={VIDEO_ID}',
    f'https://m.youtube.com/watch?v={VIDEO_ID}&t=30s',
    f'https://www.youtube.com/watch?v={VIDEO_ID}&list=PLabc&index=3',
    f'youtu.be/{VIDEO_ID}?si=abc',
    f'https://youtu.be/{VIDEO_ID}',
    f'https://www.youtube.com/shorts/{VIDEO_ID}',
    f'https://www.youtube.com/embed/{VIDEO_ID}',
    f'https://youtube.com/live/{VIDEO_ID}',
    f'  https://www.youtube.com/watch?v={VIDEO_ID}  ',
])
def test_parse_youtube_url_accepts_single_videos(url):
    assert media.parse_youtube_url(url) == VIDEO_ID


@pytest.mark.parametrize('url', [
    '',
    f'http://127.0.0.1:5000/x?y=youtube.com/watch?v={VIDEO_ID}',
    f'https://www.youtube.com.evil.com/watch?v={VIDEO_ID}',
    f'https://evil.com/youtu.be/{VIDEO_ID}',
    f'https://user@www.youtube.com/watch?v={VIDEO_ID}',
    f'https://www.youtube.com:8443/watch?v={VIDEO_ID}',
    f'https://www.youtube.com/watch?v={VIDEO_ID}&v=aaaaaaaaaaa',
    'https://www.youtube.com/watch?v=tooshort',
    'https://www.youtube.com/playlist?list=PL1234567890',
    'https://www.youtube.com/@somechannel',
    f'file:///etc/passwd?v={VIDEO_ID}',
    f'ftp://www.youtube.com/watch?v={VIDEO_ID}',
])
def test_parse_youtube_url_rejects_everything_else(url):
    assert media.parse_youtube_url(url) is None


def test_plan_chunks_short_audio_is_one_chunk():
    assert media.plan_chunks([-30.0] * 200, 10.0) == [(0.0, 10.0)]


def test_plan_chunks_cuts_in_pauses():
    frame = media.FRAME_SECONDS
    total = 60.0
    levels = [-20.0] * int(total / frame)
    pauses = [11.0, 23.0, 36.0, 49.0]
    for t in pauses:
        for i in range(int((t - 0.2) / frame), int((t + 0.2) / frame)):
            levels[i] = -70.0

    chunks = media.plan_chunks(levels, total)

    assert chunks[0][0] == 0.0 and chunks[-1][1] == total
    assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))
    assert all(end - start <= 20.0 for start, end in chunks)
    for start, _ in chunks[1:]:
        assert min(abs(start - t) for t in pauses) < 0.3


def test_plan_chunks_without_pauses_still_respects_max_length():
    chunks = media.plan_chunks([-20.0] * 1200, 60.0)
    assert all(end - start <= 20.0 for start, end in chunks)
    assert chunks[-1][1] == 60.0


def write_wav(path, parts, rate=media.SAMPLE_RATE):
    """parts: list of (seconds, amplitude) runs of a 440 Hz tone."""
    frames = bytearray()
    for seconds, amplitude in parts:
        for n in range(int(seconds * rate)):
            frames += struct.pack('<h', int(amplitude * math.sin(2 * math.pi * 440 * n / rate)))
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def test_loudness_and_slice(tmp_path):
    path = tmp_path / 'a.wav'
    write_wav(path, [(1.0, 10000), (1.0, 0), (1.0, 10000)])

    levels = media.loudness_frames(path)
    assert len(levels) == 60
    assert levels[10] > -20 and levels[30] == -120.0

    chunk = tmp_path / 'b.wav'
    chunk.write_bytes(media.wav_slice(path, 0.5, 2.0))
    assert media.wav_duration(chunk) == pytest.approx(1.5)


@needs_ffmpeg
def test_probe_accepts_mp4(make_video):
    demuxer, duration = media.probe(make_video(seconds=1))
    assert demuxer == 'mov'
    assert duration == pytest.approx(1.0, abs=0.1)


@needs_ffmpeg
def test_probe_rejects_video_without_audio(make_video):
    with pytest.raises(media.MediaError):
        media.probe(make_video(audio=False))


@needs_ffmpeg
@pytest.mark.parametrize('content', [
    'ffconcat version 1.0\nfile /etc/hostname\n',
    '#EXTM3U\n#EXTINF:10,\nhttp://127.0.0.1:9/x.ts\n#EXT-X-ENDLIST\n',
    'just some text',
])
def test_probe_rejects_renamed_playlists(tmp_path, content):
    path = tmp_path / 'sneaky.mp4'
    path.write_text(content)
    with pytest.raises(media.MediaError):
        media.probe(path)


@needs_ffmpeg
def test_extract_wav(tmp_path, make_video):
    out = tmp_path / 'out.wav'
    media.extract_wav(make_video(seconds=2), out, max_seconds=60)
    with wave.open(str(out)) as w:
        assert w.getframerate() == media.SAMPLE_RATE and w.getnchannels() == 1
    assert media.wav_duration(out) == pytest.approx(2.0, abs=0.1)
