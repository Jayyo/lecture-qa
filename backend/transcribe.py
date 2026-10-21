"""Speech-to-text backends.

Each backend returns a list of {"start", "end", "text"} segments (seconds).
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import media

logger = logging.getLogger(__name__)

# Models that return their own segment timestamps, and the response format
# that carries them. OpenAI plans to shut all of them down on 2027-02-26.
SEGMENT_FORMATS = {
    'whisper-1': 'verbose_json',
    'gpt-4o-transcribe-diarize': 'diarized_json',
}


def transcribe_openai(client, wav_path, model, language=None, on_progress=None, workers=6):
    """Transcribe a WAV file with the OpenAI API.

    gpt-transcribe returns plain text only, so the audio is cut at pauses into
    5-20 second chunks and every chunk becomes one timestamped segment. Much
    shorter chunks lose context (numbers start coming back spelled out). Models
    in SEGMENT_FORMATS return their own timestamps; for those the chunks only
    keep each request under the 25 MB upload limit.
    """
    native = model in SEGMENT_FORMATS
    total = media.wav_duration(wav_path)
    levels = media.loudness_frames(wav_path)
    if native:
        chunks = media.plan_chunks(levels, total, target=540, min_len=240, max_len=600)
    else:
        chunks = media.plan_chunks(levels, total)

    def run(chunk):
        start, end = chunk
        audio = ('chunk.wav', media.wav_slice(wav_path, start, end), 'audio/wav')
        if native:
            kwargs = {'response_format': SEGMENT_FORMATS[model]}
            if model == 'whisper-1':
                kwargs['timestamp_granularities'] = ['segment']
            else:
                kwargs['chunking_strategy'] = 'auto'
            if language:
                kwargs['language'] = language
            result = client.audio.transcriptions.create(model=model, file=audio, **kwargs)
            return [{'start': round(start + s.start, 2), 'end': round(start + s.end, 2), 'text': s.text.strip()}
                    for s in (result.segments or []) if s.text.strip()]

        kwargs = {'response_format': 'json'}
        if language:
            kwargs['extra_body'] = {'languages': [language]}
        result = client.audio.transcriptions.create(model=model, file=audio, **kwargs)
        text = (result.text or '').strip()
        return [{'start': round(start, 2), 'end': round(end, 2), 'text': text}] if text else []

    results = [[] for _ in chunks]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, chunk): i for i, chunk in enumerate(chunks)}
        for done, future in enumerate(as_completed(futures), 1):
            results[futures[future]] = future.result()
            if on_progress:
                on_progress(done / len(chunks))
    return [segment for part in results for segment in part]


_local_model = None
_local_lock = threading.Lock()


def transcribe_local(wav_path, model_name, language=None):
    """Transcribe with openai-whisper on this machine (requirements-local-whisper.txt)."""
    global _local_model
    import whisper

    with _local_lock:
        if _local_model is None:
            logger.info('Loading local Whisper model %r', model_name)
            _local_model = whisper.load_model(model_name)
        result = _local_model.transcribe(str(wav_path), language=language, verbose=None)
    return [{'start': round(s['start'], 2), 'end': round(s['end'], 2), 'text': s['text'].strip()}
            for s in result['segments'] if s['text'].strip()]
