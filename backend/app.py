import os
import json
import math
import re
import secrets
import shutil
import threading
import logging
import importlib.util
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory
from openai import OpenAI
from werkzeug.middleware.proxy_fix import ProxyFix

from . import media, qa, transcribe

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
# Real environment variables win over .env
load_dotenv(BASE_DIR / '.env')


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


app = Flask(__name__, static_folder=str(BASE_DIR / 'static'), template_folder=str(BASE_DIR / 'templates'))

MAX_UPLOAD_MB = env_int('MAX_UPLOAD_MB', 300)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

# Only trust X-Forwarded-* when the app actually sits behind a proxy (Caddy in docker-compose)
if os.getenv('TRUST_PROXY') == '1':
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DATA_DIR = Path(os.getenv('DATA_DIR', BASE_DIR / 'data')).resolve()
UPLOAD_FOLDER = DATA_DIR / 'uploads'
TRANSCRIPT_FOLDER = DATA_DIR / 'transcripts'
WORK_FOLDER = DATA_DIR / 'work'
for folder in (UPLOAD_FOLDER, TRANSCRIPT_FOLDER, WORK_FOLDER):
    folder.mkdir(parents=True, exist_ok=True)

# Uploads get 32 random hex chars, YouTube videos "yt-" + their video id.
# Anything else never reaches the filesystem.
VIDEO_ID_RE = re.compile(r'[0-9a-f]{32}|yt-[A-Za-z0-9_-]{11}')
MEDIA_NAME_RE = re.compile(r'(?:[0-9a-f]{32}|yt-[A-Za-z0-9_-]{11})\.(?:mp4|webm)')

MAX_VIDEO_MINUTES = env_int('MAX_VIDEO_MINUTES', 20)
MAX_VIDEO_DURATION = MAX_VIDEO_MINUTES * 60
YTDLP_COOKIES = os.getenv('YTDLP_COOKIES')

# gpt-transcribe gives the best text but no timestamps; see transcribe.py
TRANSCRIBE_MODEL = os.getenv('TRANSCRIBE_MODEL', 'gpt-transcribe')
TRANSCRIBE_LANGUAGE = os.getenv('TRANSCRIBE_LANGUAGE') or None

# Answers come from the Responses API. gpt-6.1-sol only accepts low..max effort;
# gpt-6-luna with CHAT_REASONING_EFFORT=none is the faster, cheaper option.
CHAT_MODEL = os.getenv('CHAT_MODEL', 'gpt-6.1-sol')
CHAT_REASONING_EFFORT = os.getenv('CHAT_REASONING_EFFORT', 'low')
CHAT_MAX_OUTPUT_TOKENS = env_int('CHAT_MAX_OUTPUT_TOKENS', 1000)

# "Ask the professor" mails go out through Resend; the button only shows up
# when all three are set. FEEDBACK_FROM must use a domain verified in Resend.
PROFESSOR_EMAIL = os.getenv('PROFESSOR_EMAIL')
RESEND_API_KEY = os.getenv('RESEND_API_KEY')
FEEDBACK_FROM = os.getenv('FEEDBACK_FROM')
FEEDBACK_ENABLED = bool(PROFESSOR_EMAIL and RESEND_API_KEY and FEEDBACK_FROM)
EMAIL_RE = re.compile(r'[^@\s<>"]{1,64}@[^@\s<>"]{1,190}\.[A-Za-z]{2,24}')

# Cost and abuse limits for a public instance (0 disables a rate limit)
JOB_WORKERS = env_int('JOB_WORKERS', 2)
MAX_PENDING_JOBS = env_int('MAX_PENDING_JOBS', 8)
VIDEOS_PER_HOUR = env_int('RATE_LIMIT_VIDEOS_PER_HOUR', 10)
QUESTIONS_PER_HOUR = env_int('RATE_LIMIT_QUESTIONS_PER_HOUR', 60)
FEEDBACK_PER_DAY = env_int('RATE_LIMIT_FEEDBACK_PER_DAY', 10)
RETENTION_HOURS = env_int('RETENTION_HOURS', 48)

# Optional offline transcription with openai-whisper (tiny, base, small, medium, large)
USE_LOCAL_WHISPER = os.getenv('USE_LOCAL_WHISPER', 'false').lower() == 'true'
WHISPER_MODEL = os.getenv('WHISPER_MODEL', 'small')
if USE_LOCAL_WHISPER and importlib.util.find_spec('whisper') is None:
    logger.warning("USE_LOCAL_WHISPER=true but openai-whisper is not installed; using the OpenAI API")
    USE_LOCAL_WHISPER = False

client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Store transcription status and content
transcription_store = {}
transcription_status = {}

# Recent answers, so a mail to the professor quotes what the model really said
# instead of whatever the browser sends.
recent_answers = OrderedDict()
answers_lock = threading.Lock()
MAX_RECENT_ANSWERS = 1000

# Transcription jobs run on a small pool; keep transcription_status in one
# process (gunicorn --workers 1) so status polling sees every job.
job_pool = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix='job')
jobs_lock = threading.Lock()
pending_jobs = set()


class RateLimiter:
    """Sliding-window counter per key, kept in memory."""

    def __init__(self):
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, key, limit, window):
        if limit <= 0:
            return True
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 10000:
                self.hits = {k: q for k, q in self.hits.items() if q and q[-1] > now - 86400}
            hits = self.hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


limiter = RateLimiter()


def rate_limited(bucket, limit, window):
    """Return an error response when this client went over the limit, else None."""
    if limiter.allow(f"{bucket}:{request.remote_addr}", limit, window):
        return None
    return jsonify({'error': 'rate_limited', 'message': '요청이 너무 많아요. 잠시 후 다시 시도해 주세요.'}), 429


def queue_full():
    with jobs_lock:
        return len(pending_jobs) >= MAX_PENDING_JOBS


def submit_job(video_id, title, fn, *args):
    """Queue a background job. Returns False when the queue is full."""
    with jobs_lock:
        if video_id in pending_jobs:
            return True
        if len(pending_jobs) >= MAX_PENDING_JOBS:
            return False
        pending_jobs.add(video_id)
        transcription_status[video_id] = {'status': 'queued', 'progress': 0, 'title': title}

    def run():
        try:
            fn(*args)
        finally:
            with jobs_lock:
                pending_jobs.discard(video_id)

    job_pool.submit(run)
    cleanup_old_files()
    return True


_last_cleanup = 0.0


def cleanup_old_files():
    """Delete videos and transcripts older than RETENTION_HOURS (checked every 10 minutes)."""
    global _last_cleanup
    now = time.time()
    if RETENTION_HOURS <= 0 or now - _last_cleanup < 600:
        return
    _last_cleanup = now
    cutoff = now - RETENTION_HOURS * 3600
    for folder in (UPLOAD_FOLDER, TRANSCRIPT_FOLDER, WORK_FOLDER):
        for path in folder.iterdir():
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
                video_id = path.name.split('.')[0]
                transcription_store.pop(video_id, None)
                transcription_status.pop(video_id, None)
            except OSError:
                logger.warning(f"Could not remove {path}")


def valid_video_id(video_id):
    return isinstance(video_id, str) and VIDEO_ID_RE.fullmatch(video_id) is not None


def transcript_path(video_id):
    return TRANSCRIPT_FOLDER / f"{video_id}.json"


def load_transcript(video_id):
    """Return the transcript from memory or disk, or None."""
    if video_id in transcription_store:
        return transcription_store[video_id]
    path = transcript_path(video_id)
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            transcription_store[video_id] = json.load(f)
        return transcription_store[video_id]
    return None


@app.before_request
def reject_cross_site_posts():
    """The API is only meant to be called by our own page."""
    if request.method != 'POST':
        return None
    site = request.headers.get('Sec-Fetch-Site')
    if site and site not in ('same-origin', 'none'):
        return jsonify({'error': 'cross_site', 'message': '허용되지 않은 요청이에요.'}), 403
    origin = request.headers.get('Origin')
    if origin and urlsplit(origin).netloc != request.host:
        return jsonify({'error': 'cross_site', 'message': '허용되지 않은 요청이에요.'}), 403
    return None


@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; script-src 'self' https://www.youtube.com; "
        "frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
        "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://i.ytimg.com; media-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'too_large', 'message': f'{MAX_UPLOAD_MB}MB 이하의 파일만 올릴 수 있어요.'}), 413


def transcribe_audio(video_path, video_id, title=None):
    """Extract the audio track and transcribe it (runs in a background thread)."""
    wav_path = WORK_FOLDER / f"{video_id}.wav"

    def set_status(progress):
        transcription_status[video_id] = {'status': 'processing', 'progress': progress, 'title': title}

    def progress(fraction):
        set_status(30 + int(fraction * 65))

    try:
        logger.info(f"[TRANSCRIBE] Starting | video_id: {video_id}")
        set_status(28)
        media.extract_wav(video_path, wav_path, MAX_VIDEO_DURATION)

        if USE_LOCAL_WHISPER:
            model = f'whisper-{WHISPER_MODEL} (local)'
            set_status(40)
            segments = transcribe.transcribe_local(wav_path, WHISPER_MODEL, TRANSCRIBE_LANGUAGE)
        else:
            model = TRANSCRIBE_MODEL
            segments = transcribe.transcribe_openai(client, wav_path, TRANSCRIBE_MODEL,
                                                    language=TRANSCRIBE_LANGUAGE, on_progress=progress)
        if not segments:
            raise media.MediaError('영상에서 말소리를 찾지 못했어요.')

        transcript_data = {
            'model': model,
            'title': title,
            'duration': round(media.wav_duration(wav_path), 2),
            'segments': segments,
        }
        tmp = transcript_path(video_id).with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(transcript_data, f, ensure_ascii=False, indent=1)
        tmp.replace(transcript_path(video_id))

        transcription_store[video_id] = transcript_data
        transcription_status[video_id] = {'status': 'completed', 'progress': 100}
        logger.info(f"[TRANSCRIBE] Completed | video_id: {video_id} | model: {model} | segments: {len(segments)}")

    except media.MediaError as e:
        logger.info(f"[TRANSCRIBE] Rejected | video_id: {video_id} | {e}")
        transcription_status[video_id] = {'status': 'error', 'error': str(e)}
    except Exception:
        logger.exception(f"[TRANSCRIBE] Error | video_id: {video_id}")
        transcription_status[video_id] = {'status': 'error', 'error': '음성을 텍스트로 바꾸지 못했어요.'}
    finally:
        wav_path.unlink(missing_ok=True)


def process_youtube_job(youtube_id, video_id):
    """Fetch the audio of a YouTube video and transcribe it (runs in a background thread)."""
    workdir = WORK_FOLDER / video_id

    def progress(fraction):
        transcription_status[video_id] = {'status': 'downloading', 'progress': int(fraction * 25)}

    try:
        audio_path, info = media.download_youtube_audio(
            youtube_id, workdir, MAX_VIDEO_DURATION, app.config['MAX_CONTENT_LENGTH'],
            cookies=YTDLP_COOKIES, on_progress=progress)
        logger.info(f"[YOUTUBE] Downloaded audio | video_id: {video_id} | title: {info.get('title')!r}")
        transcribe_audio(audio_path, video_id, title=info.get('title'))
    except media.MediaError as e:
        transcription_status[video_id] = {'status': 'error', 'error': str(e)}
    except Exception:
        logger.exception(f"[YOUTUBE] Download error | video_id: {video_id}")
        transcription_status[video_id] = {'status': 'error', 'error': 'YouTube 영상을 가져오지 못했어요.'}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.route('/')
def index():
    return send_from_directory(app.template_folder, 'index.html')


@app.route('/api/config')
def get_config():
    return jsonify({
        'max_minutes': MAX_VIDEO_MINUTES,
        'max_upload_mb': MAX_UPLOAD_MB,
        'feedback_enabled': FEEDBACK_ENABLED,
    })


@app.route('/uploads/<filename>')
def serve_uploads(filename):
    if not MEDIA_NAME_RE.fullmatch(filename):
        return jsonify({'error': 'Not found'}), 404
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/api/upload', methods=['POST'])
def upload_video():
    """Handle video file upload"""
    if 'video' not in request.files:
        return jsonify({'error': 'No video file provided'}), 400

    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if Path(file.filename).suffix.lower() not in media.UPLOAD_EXTENSIONS:
        return jsonify({'error': 'unsupported', 'message': 'MP4 또는 WebM 영상만 올릴 수 있어요.'}), 400
    if queue_full():
        return jsonify({'error': 'busy', 'message': '지금 처리 중인 영상이 많아요. 잠시 후 다시 시도해 주세요.'}), 503
    limited = rate_limited('videos', VIDEOS_PER_HOUR, 3600)
    if limited:
        return limited

    video_id = secrets.token_hex(16)
    tmp_path = WORK_FOLDER / f"{video_id}.upload"
    file.save(tmp_path)
    try:
        demuxer, duration = media.probe(tmp_path)
        if duration > MAX_VIDEO_DURATION:
            raise media.MediaError(f'{MAX_VIDEO_MINUTES}분 이하의 영상만 처리할 수 있어요.')
    except media.MediaError as e:
        tmp_path.unlink(missing_ok=True)
        return jsonify({'error': 'invalid_media', 'message': str(e)}), 400

    filename = f"{video_id}{media.DEMUXERS[demuxer]}"
    filepath = UPLOAD_FOLDER / filename
    tmp_path.replace(filepath)

    logger.info(f"[UPLOAD] Saved | video_id: {video_id} | {duration:.0f}s | {filepath.stat().st_size/1024/1024:.2f}MB | IP: {request.remote_addr}")

    title = Path(file.filename).stem[:100] or None
    if not submit_job(video_id, title, transcribe_audio, filepath, video_id, title):
        filepath.unlink(missing_ok=True)
        return jsonify({'error': 'busy', 'message': '지금 처리 중인 영상이 많아요. 잠시 후 다시 시도해 주세요.'}), 503

    return jsonify({
        'video_id': video_id,
        'video_url': f'/uploads/{filename}',
        'message': 'Video uploaded, transcription started'
    })


@app.route('/api/youtube', methods=['POST'])
def process_youtube():
    """Start transcribing a YouTube video (or reuse an earlier transcript)."""
    data = request.get_json(silent=True) or {}
    youtube_id = media.parse_youtube_url(data.get('url') if isinstance(data.get('url'), str) else '')
    if not youtube_id:
        return jsonify({'error': 'invalid_url',
                        'message': '올바른 YouTube 영상 주소를 입력해 주세요. 채널이나 재생목록은 안 돼요.'}), 400

    video_id = f"yt-{youtube_id}"
    result = {'video_id': video_id, 'youtube_id': youtube_id}
    logger.info(f"[YOUTUBE] Request | video_id: {video_id} | IP: {request.remote_addr}")

    transcript = load_transcript(video_id)
    if transcript is not None:
        return jsonify({**result, 'cached': True, 'title': transcript.get('title')})

    # Someone may have asked for the same video a moment ago; join that job
    with jobs_lock:
        if video_id in pending_jobs:
            return jsonify(result)
    limited = rate_limited('videos', VIDEOS_PER_HOUR, 3600)
    if limited:
        return limited
    if not submit_job(video_id, None, process_youtube_job, youtube_id, video_id):
        return jsonify({'error': 'busy', 'message': '지금 처리 중인 영상이 많아요. 잠시 후 다시 시도해 주세요.'}), 503
    return jsonify(result)


@app.route('/api/status/<video_id>')
def get_status(video_id):
    """Get transcription status"""
    if not valid_video_id(video_id):
        return jsonify({'status': 'unknown'}), 404
    status = transcription_status.get(video_id)
    if status is None:
        if load_transcript(video_id) is not None:
            return jsonify({'status': 'completed', 'progress': 100})
        return jsonify({'status': 'unknown'}), 404
    return jsonify(status)


@app.route('/api/transcript/<video_id>')
def get_transcript(video_id):
    """Get transcript for a video"""
    data = load_transcript(video_id) if valid_video_id(video_id) else None
    if data is None:
        return jsonify({'error': 'Transcript not found'}), 404
    return jsonify(data)


def get_context_at_timestamp(video_id, current_time):
    """The last ~500 characters of the lecture before `current_time` (used in the professor mail)."""
    data = load_transcript(video_id)
    if not data:
        return ""
    text = ' '.join(seg['text'] for seg in data.get('segments', []) if seg['start'] <= current_time)
    return text[-500:]


def parse_time(value):
    try:
        t = float(value)
    except (TypeError, ValueError):
        return 0.0
    return t if math.isfinite(t) and t >= 0 else 0.0


def parse_history(value):
    """Up to three earlier question/answer pairs sent back by the page."""
    turns = []
    if isinstance(value, list):
        for turn in value[-3:]:
            if isinstance(turn, dict) and isinstance(turn.get('question'), str) and isinstance(turn.get('answer'), str):
                turns.append({'question': turn['question'][:1000], 'answer': turn['answer'][:2000]})
    return turns


def sse(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.route('/api/ask', methods=['POST'])
def ask_question():
    """Answer a question about the lecture, streamed as server-sent events."""
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    question = data.get('question')
    current_time = parse_time(data.get('current_time'))

    if not valid_video_id(video_id) or not isinstance(question, str) or not question.strip():
        return jsonify({'error': 'Missing video_id or question'}), 400
    question = question.strip()[:1000]

    transcript = load_transcript(video_id)
    if transcript is None:
        return jsonify({'error': 'Transcript not available'}), 404
    limited = rate_limited('questions', QUESTIONS_PER_HOUR, 3600)
    if limited:
        return limited

    prompt = qa.build_input(transcript['segments'], current_time, question,
                            history=parse_history(data.get('history')), title=transcript.get('title'))

    answer_id = secrets.token_urlsafe(12)

    def generate():
        parts = []
        try:
            yield sse({'answer_id': answer_id})
            for delta in qa.stream_answer(client, CHAT_MODEL, prompt, CHAT_REASONING_EFFORT, CHAT_MAX_OUTPUT_TOKENS):
                parts.append(delta)
                yield sse({'delta': delta})
            with answers_lock:
                recent_answers[answer_id] = {
                    'video_id': video_id, 'question': question, 'answer': ''.join(parts),
                    'time': current_time, 'title': transcript.get('title'), 'sent': False,
                }
                while len(recent_answers) > MAX_RECENT_ANSWERS:
                    recent_answers.popitem(last=False)
            yield sse({'done': True})
        except Exception:
            logger.exception(f"[ASK] Error | video_id: {video_id}")
            yield sse({'error': '답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요.'})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/feedback', methods=['POST'])
def send_feedback():
    """Mail a question and the AI answer to the professor."""
    if not FEEDBACK_ENABLED:
        return jsonify({'error': 'not_configured', 'message': '교수님께 메일 보내기가 설정되지 않았어요.'}), 503

    data = request.get_json(silent=True) or {}
    answer_id = data.get('answer_id')
    comment = str(data.get('comment') or '').strip()[:2000]
    reply_to = str(data.get('reply_to') or '').strip()
    if reply_to and not EMAIL_RE.fullmatch(reply_to):
        return jsonify({'error': 'bad_email', 'message': '이메일 주소를 확인해 주세요.'}), 400
    limited = rate_limited('feedback', FEEDBACK_PER_DAY, 86400)
    if limited:
        return limited

    with answers_lock:
        item = recent_answers.get(answer_id) if isinstance(answer_id, str) else None
        if item is None:
            return jsonify({'error': 'not_found', 'message': '답변을 찾을 수 없어요. 다시 질문한 뒤 보내 주세요.'}), 404
        if item['sent']:
            return jsonify({'error': 'already_sent', 'message': '이미 교수님께 보낸 질문이에요.'}), 409
        item['sent'] = True

    when = qa.fmt_time(item['time'])
    body = f"""교수님, 강의 영상 Q&A에서 학생이 AI 답변을 확인해 달라고 요청했습니다.

영상: {item['title'] or '(제목 없음)'}
질문 시점: {when}

[학생 질문]
{item['question']}

[AI 답변]
{item['answer']}

[학생 의견]
{comment or '(없음)'}

[질문 시점까지의 강의 내용 (자동 자막)]
{get_context_at_timestamp(item['video_id'], item['time'])}

{'이 메일에 회신하면 학생에게 전달됩니다.' if reply_to else '학생이 연락처를 남기지 않았습니다.'}
"""
    email = {
        'from': FEEDBACK_FROM,
        'to': [PROFESSOR_EMAIL],
        'subject': f"[강의 Q&A] 학생 질문 확인 요청 ({when})",
        'text': body,
    }
    if reply_to:
        email['reply_to'] = reply_to

    try:
        response = requests.post('https://api.resend.com/emails',
                                 headers={'Authorization': f'Bearer {RESEND_API_KEY}'},
                                 json=email, timeout=10)
        ok = response.status_code == 200
        if not ok:
            logger.error(f"[FEEDBACK] Resend returned {response.status_code}: {response.text[:300]}")
    except requests.RequestException:
        logger.exception("[FEEDBACK] Resend request failed")
        ok = False

    if not ok:
        with answers_lock:
            item['sent'] = False
        return jsonify({'error': 'send_failed', 'message': '메일을 보내지 못했어요. 잠시 후 다시 시도해 주세요.'}), 502
    logger.info(f"[FEEDBACK] Sent | answer_id: {answer_id}")
    return jsonify({'message': 'sent'})


if __name__ == '__main__':
    app.run(
        host=os.getenv('HOST', '127.0.0.1'),
        port=env_int('PORT', 5000),
        debug=os.getenv('FLASK_DEBUG') == '1'
    )
