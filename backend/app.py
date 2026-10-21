import os
import json
import math
import re
import secrets
import shutil
import threading
import logging
import importlib.util
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
        "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; media-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'too_large', 'message': f'{MAX_UPLOAD_MB}MB 이하의 파일만 올릴 수 있어요.'}), 413


def transcribe_audio(video_path, video_id, title=None):
    """Extract the audio track and transcribe it (runs in a background thread)."""
    wav_path = WORK_FOLDER / f"{video_id}.wav"

    def progress(fraction):
        transcription_status[video_id] = {'status': 'processing', 'progress': 30 + int(fraction * 65),
                                          'message': 'Transcribing...'}

    try:
        logger.info(f"[TRANSCRIBE] Starting | video_id: {video_id}")
        transcription_status[video_id] = {'status': 'processing', 'progress': 28, 'message': 'Extracting audio...'}
        media.extract_wav(video_path, wav_path, MAX_VIDEO_DURATION)

        if USE_LOCAL_WHISPER:
            model = f'whisper-{WHISPER_MODEL} (local)'
            transcription_status[video_id] = {'status': 'processing', 'progress': 40, 'message': 'Transcribing...'}
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
        transcription_status[video_id] = {'status': 'error', 'error': str(e)}
    except Exception:
        logger.exception(f"[TRANSCRIBE] Error | video_id: {video_id}")
        transcription_status[video_id] = {'status': 'error', 'error': '음성을 텍스트로 바꾸지 못했어요.'}
    finally:
        wav_path.unlink(missing_ok=True)


def process_youtube_job(youtube_id, video_id):
    """Download a YouTube video and transcribe it (runs in a background thread)."""
    workdir = WORK_FOLDER / video_id

    def progress(fraction):
        transcription_status[video_id] = {'status': 'downloading', 'progress': int(fraction * 25)}

    try:
        path, info = media.download_youtube(
            youtube_id, workdir, MAX_VIDEO_DURATION, app.config['MAX_CONTENT_LENGTH'],
            cookies=YTDLP_COOKIES, on_progress=progress)
        video_path = UPLOAD_FOLDER / f"{video_id}.mp4"
        shutil.move(str(path), video_path)
        logger.info(f"[YOUTUBE] Downloaded | video_id: {video_id} | title: {info.get('title')!r}")
    except media.MediaError as e:
        transcription_status[video_id] = {'status': 'error', 'error': str(e)}
        return
    except Exception:
        logger.exception(f"[YOUTUBE] Download error | video_id: {video_id}")
        transcription_status[video_id] = {'status': 'error', 'error': 'YouTube 영상을 가져오지 못했어요.'}
        return
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    transcribe_audio(video_path, video_id, title=info.get('title'))


@app.route('/')
def index():
    return send_from_directory(app.template_folder, 'index.html')


@app.route('/api/config')
def get_config():
    return jsonify({
        'max_minutes': MAX_VIDEO_MINUTES,
        'max_upload_mb': MAX_UPLOAD_MB,
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

    # Start transcription in background
    title = Path(file.filename).stem[:100] or None
    transcription_status[video_id] = {'status': 'processing', 'progress': 25}
    threading.Thread(target=transcribe_audio, args=(filepath, video_id, title), daemon=True).start()

    return jsonify({
        'video_id': video_id,
        'video_url': f'/uploads/{filename}',
        'message': 'Video uploaded, transcription started'
    })


@app.route('/api/youtube', methods=['POST'])
def process_youtube():
    """Process YouTube URL"""
    data = request.get_json(silent=True) or {}
    youtube_id = media.parse_youtube_url(data.get('url') if isinstance(data.get('url'), str) else '')
    if not youtube_id:
        return jsonify({'error': 'invalid_url',
                        'message': '올바른 YouTube 영상 주소를 입력해 주세요. 채널이나 재생목록은 안 돼요.'}), 400

    video_id = f"yt-{youtube_id}"
    video_url = f'/uploads/{video_id}.mp4'
    logger.info(f"[YOUTUBE] Request | video_id: {video_id} | IP: {request.remote_addr}")

    if load_transcript(video_id) is not None and (UPLOAD_FOLDER / f"{video_id}.mp4").exists():
        transcription_status[video_id] = {'status': 'completed', 'progress': 100}
        return jsonify({'video_id': video_id, 'video_url': video_url, 'cached': True})

    # Someone else may have asked for the same video a moment ago
    if transcription_status.get(video_id, {}).get('status') in ('downloading', 'processing'):
        return jsonify({'video_id': video_id, 'video_url': video_url})

    transcription_status[video_id] = {'status': 'downloading', 'progress': 0}
    threading.Thread(target=process_youtube_job, args=(youtube_id, video_id), daemon=True).start()

    return jsonify({'video_id': video_id, 'video_url': video_url})


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

    prompt = qa.build_input(transcript['segments'], current_time, question,
                            history=parse_history(data.get('history')), title=transcript.get('title'))

    def generate():
        try:
            for delta in qa.stream_answer(client, CHAT_MODEL, prompt, CHAT_REASONING_EFFORT, CHAT_MAX_OUTPUT_TOKENS):
                yield sse({'delta': delta})
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
    """Send feedback email to professor"""
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    question = str(data.get('question') or '')[:1000]
    answer = str(data.get('answer') or '')[:3000]
    current_time = parse_time(data.get('current_time'))

    professor_email = os.getenv('PROFESSOR_EMAIL')
    resend_api_key = os.getenv('RESEND_API_KEY')
    if not professor_email or not resend_api_key:
        return jsonify({'error': 'not_configured', 'message': '교수님 메일 전송이 설정되지 않았어요.'}), 503

    context = get_context_at_timestamp(video_id, current_time) if valid_video_id(video_id) else ''
    timestamp_str = f"{int(current_time // 60)}:{int(current_time % 60):02d}"

    body = f"""안녕하세요 교수님,

학생이 강의 영상에서 질문을 했으나, AI 답변에 만족하지 못했습니다.
확인 후 정확한 답변을 부탁드립니다.

질문 시점: {timestamp_str}

해당 시점 강의 내용:
{context[-500:]}

학생 질문:
{question}

AI 답변:
{answer}

강의 Q&A 시스템
"""

    try:
        response = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {resend_api_key}'},
            json={
                'from': 'Lecture QA <onboarding@resend.dev>',
                'to': [professor_email],
                'subject': '[강의 Q&A] 학생 질문에 대한 답변 검토 요청',
                'text': body
            },
            timeout=10
        )
    except requests.RequestException:
        logger.exception("[FEEDBACK] Resend request failed")
        return jsonify({'error': 'send_failed', 'message': '메일을 보내지 못했어요.'}), 502

    if response.status_code == 200:
        return jsonify({'message': 'Feedback sent to professor successfully'})
    logger.error(f"[FEEDBACK] Resend returned {response.status_code}: {response.text[:300]}")
    return jsonify({'error': 'send_failed', 'message': '메일을 보내지 못했어요.'}), 502


if __name__ == '__main__':
    app.run(
        host=os.getenv('HOST', '127.0.0.1'),
        port=env_int('PORT', 5000),
        debug=os.getenv('FLASK_DEBUG') == '1'
    )
