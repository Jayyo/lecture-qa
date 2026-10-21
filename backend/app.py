import os
import json
import math
import re
import secrets
import subprocess
import threading
import logging
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory
from openai import OpenAI
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Local Whisper support (optional)
try:
    import whisper
    LOCAL_WHISPER_AVAILABLE = True
except ImportError:
    LOCAL_WHISPER_AVAILABLE = False

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
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
TRANSCRIPT_FOLDER.mkdir(parents=True, exist_ok=True)

# Video ids are 32 hex chars; anything else never reaches the filesystem
VIDEO_ID_RE = re.compile(r'^[0-9a-f]{32}$')

# Maximum video duration in seconds (5 minutes)
MAX_VIDEO_DURATION = 300

# Whisper configuration
# USE_LOCAL_WHISPER: Set to 'true' to use local Whisper model instead of OpenAI API
# WHISPER_MODEL: tiny, base, small, medium, large (default: small)
USE_LOCAL_WHISPER = os.getenv('USE_LOCAL_WHISPER', 'false').lower() == 'true'
WHISPER_MODEL = os.getenv('WHISPER_MODEL', 'small')

# Load local Whisper model if enabled
local_whisper_model = None
if USE_LOCAL_WHISPER and LOCAL_WHISPER_AVAILABLE:
    logger.info(f"[Whisper] Loading local model: {WHISPER_MODEL}")
    local_whisper_model = whisper.load_model(WHISPER_MODEL)
    device = next(local_whisper_model.parameters()).device
    logger.info(f"[Whisper] Model loaded on device: {device}")
elif USE_LOCAL_WHISPER and not LOCAL_WHISPER_AVAILABLE:
    logger.warning("[Whisper] USE_LOCAL_WHISPER=true but whisper not installed. Falling back to OpenAI API.")
    USE_LOCAL_WHISPER = False

client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Store transcription status and content
transcription_store = {}
transcription_status = {}


def valid_video_id(video_id):
    return isinstance(video_id, str) and bool(VIDEO_ID_RE.match(video_id))


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


def is_valid_youtube_video_url(url):
    """
    Check if URL is a valid YouTube video URL (not channel/playlist).
    Returns (is_valid, error_message)
    """
    # Patterns for invalid URLs (channels, playlists, etc.)
    invalid_patterns = [
        r'youtube\.com/channel/',
        r'youtube\.com/c/',
        r'youtube\.com/user/',
        r'youtube\.com/@',
        r'youtube\.com/playlist',
        r'youtube\.com/feed/',
        r'youtube\.com/results',
    ]

    for pattern in invalid_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return False, '채널이나 재생목록이 아닌 개별 영상 URL을 입력해주세요.'

    # Patterns for valid video URLs
    valid_patterns = [
        r'youtube\.com/watch\?v=[\w-]+',
        r'youtu\.be/[\w-]+',
        r'youtube\.com/shorts/[\w-]+',
        r'youtube\.com/embed/[\w-]+',
    ]

    for pattern in valid_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return True, None

    return False, '올바른 YouTube 영상 URL을 입력해주세요.'


def get_video_hash(video_path_or_url):
    """Generate a stable id for a YouTube URL"""
    import hashlib
    return hashlib.md5(video_path_or_url.encode()).hexdigest()


def transcribe_with_local_whisper(audio_path, video_id):
    """Transcribe using local Whisper model"""
    transcription_status[video_id] = {'status': 'processing', 'progress': 50, 'message': 'Transcribing with local Whisper...'}

    result = local_whisper_model.transcribe(
        audio_path,
        language="ko",  # Korean
        verbose=False
    )

    transcription_status[video_id] = {'status': 'processing', 'progress': 90}

    # Process segments
    segments = []
    if 'segments' in result:
        for seg in result['segments']:
            segments.append({
                'start': seg['start'],
                'end': seg['end'],
                'text': seg['text'].strip()
            })

    return {
        'full_text': result['text'].strip(),
        'segments': segments
    }


# OpenAI Whisper API file size limit (25MB)
OPENAI_FILE_SIZE_LIMIT = 25 * 1024 * 1024  # 25MB in bytes
# Chunk duration in seconds (2 minutes for safe margin)
CHUNK_DURATION_SECONDS = 120


def split_audio_into_chunks(audio_path, video_id):
    """Split large audio file into smaller chunks using ffmpeg"""
    # Get audio duration
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', audio_path
    ], capture_output=True, text=True)

    total_duration = float(result.stdout.strip())

    # Calculate number of chunks
    num_chunks = int(total_duration / CHUNK_DURATION_SECONDS) + 1

    chunk_paths = []
    base_path = audio_path.rsplit('.', 1)[0]

    for i in range(num_chunks):
        start_time = i * CHUNK_DURATION_SECONDS
        chunk_path = f"{base_path}_chunk{i:03d}.mp3"

        # Extract chunk with ffmpeg
        subprocess.run([
            'ffmpeg', '-nostdin', '-i', audio_path,
            '-ss', str(start_time),
            '-t', str(CHUNK_DURATION_SECONDS),
            '-acodec', 'libmp3lame', '-q:a', '4',
            '-y', chunk_path
        ], capture_output=True, check=True)

        chunk_paths.append({
            'path': chunk_path,
            'start_offset': start_time
        })

    return chunk_paths


def transcribe_with_openai_api(audio_path, video_id):
    """Transcribe using OpenAI Whisper API with chunking for large files"""

    # Check file size
    file_size = os.path.getsize(audio_path)

    if file_size <= OPENAI_FILE_SIZE_LIMIT:
        # Small file - transcribe directly
        transcription_status[video_id] = {'status': 'processing', 'progress': 50, 'message': 'Transcribing with OpenAI API...'}

        with open(audio_path, 'rb') as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="verbose_json",
                timestamp_granularities=["segment"]
            )

        transcription_status[video_id] = {'status': 'processing', 'progress': 90}

        # Process segments
        segments = []
        if hasattr(transcript, 'segments') and transcript.segments:
            for seg in transcript.segments:
                segments.append({
                    'start': seg.start,
                    'end': seg.end,
                    'text': seg.text
                })

        return {
            'full_text': transcript.text,
            'segments': segments
        }

    # Large file - split into chunks and transcribe each
    logger.info(f"[Whisper] Large file detected ({file_size / 1024 / 1024:.1f}MB), splitting into chunks...")
    transcription_status[video_id] = {'status': 'processing', 'progress': 40, 'message': 'Splitting audio into chunks...'}

    try:
        chunks = split_audio_into_chunks(audio_path, video_id)
        all_segments = []
        all_text = []

        for i, chunk_info in enumerate(chunks):
            chunk_path = chunk_info['path']
            start_offset = chunk_info['start_offset']

            progress = 45 + int((i / len(chunks)) * 40)
            transcription_status[video_id] = {
                'status': 'processing',
                'progress': progress,
                'message': f'Transcribing chunk {i+1}/{len(chunks)}...'
            }

            with open(chunk_path, 'rb') as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"]
                )

            # Add text
            all_text.append(transcript.text)

            # Add segments with adjusted timestamps
            if hasattr(transcript, 'segments') and transcript.segments:
                for seg in transcript.segments:
                    all_segments.append({
                        'start': seg.start + start_offset,
                        'end': seg.end + start_offset,
                        'text': seg.text
                    })

            # Cleanup chunk file
            if os.path.exists(chunk_path):
                os.remove(chunk_path)

        transcription_status[video_id] = {'status': 'processing', 'progress': 90}

        return {
            'full_text': ' '.join(all_text),
            'segments': all_segments
        }

    except Exception:
        # Cleanup any remaining chunk files
        import glob
        base_path = audio_path.rsplit('.', 1)[0]
        for chunk_file in glob.glob(f"{base_path}_chunk*.mp3"):
            try:
                os.remove(chunk_file)
            except OSError:
                pass
        raise


def transcribe_audio(video_path, video_id):
    """Transcribe video audio using local Whisper or OpenAI API"""
    audio_path = str(UPLOAD_FOLDER / f"{video_id}.mp3")
    try:
        logger.info(f"[TRANSCRIBE] Starting | video_id: {video_id}")
        transcription_status[video_id] = {'status': 'processing', 'progress': 35, 'message': 'Extracting audio...'}

        subprocess.run([
            'ffmpeg', '-nostdin', '-i', str(video_path), '-vn', '-acodec', 'libmp3lame',
            '-q:a', '4', '-y', audio_path
        ], check=True, capture_output=True)

        audio_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
        logger.info(f"[TRANSCRIBE] Audio extracted | video_id: {video_id} | size: {audio_size/1024/1024:.2f}MB")

        if USE_LOCAL_WHISPER and local_whisper_model is not None:
            transcript_data = transcribe_with_local_whisper(audio_path, video_id)
        else:
            transcript_data = transcribe_with_openai_api(audio_path, video_id)

        with open(transcript_path(video_id), 'w', encoding='utf-8') as f:
            json.dump(transcript_data, f, ensure_ascii=False, indent=2)

        transcription_store[video_id] = transcript_data
        transcription_status[video_id] = {'status': 'completed', 'progress': 100}
        logger.info(f"[TRANSCRIBE] Completed | video_id: {video_id} | segments: {len(transcript_data.get('segments', []))}")

    except Exception:
        logger.exception(f"[TRANSCRIBE] Error | video_id: {video_id}")
        transcription_status[video_id] = {'status': 'error', 'error': '음성을 텍스트로 바꾸지 못했어요.'}
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


def get_video_duration(url, yt_dlp_path='yt-dlp'):
    """Get video duration in seconds using yt-dlp"""
    try:
        result = subprocess.run([
            yt_dlp_path, '--no-playlist', '--get-duration', url
        ], capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            duration_str = result.stdout.strip()
            # Parse duration like "3:33" or "1:23:45"
            parts = duration_str.split(':')
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 1:
                return int(parts[0])
        return None
    except Exception as e:
        logger.warning(f"Duration check error: {e}")
        return None


def download_youtube_video(url, video_id, yt_dlp_path='yt-dlp'):
    """Download YouTube video using yt-dlp with progress tracking"""
    output_path = UPLOAD_FOLDER / f"{video_id}.mp4"

    # Cookie file path for YouTube authentication (to bypass bot detection)
    cookies_file = BASE_DIR / 'cookies.txt'

    try:
        cmd = [
            yt_dlp_path, '--no-playlist',
            '-f', 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b',
            '--newline', '--progress',
            '--merge-output-format', 'mp4',
        ]

        if cookies_file.exists():
            cmd.extend(['--cookies', str(cookies_file)])

        cmd.extend(['-o', str(output_path), url])

        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        for line in process.stdout:
            line = line.strip()
            # Parse download progress from yt-dlp output
            if '[download]' in line and '%' in line:
                match = re.search(r'(\d+\.?\d*)%', line)
                if match:
                    # Scale download progress to 0-25% of total
                    transcription_status[video_id] = {
                        'status': 'downloading',
                        'progress': int(float(match.group(1)) * 0.25)
                    }

        process.wait()

        if process.returncode == 0:
            transcription_status[video_id] = {'status': 'downloading', 'progress': 25}
            return output_path
        logger.warning(f"[YOUTUBE] yt-dlp exited with {process.returncode} | video_id: {video_id}")
        return None
    except Exception:
        logger.exception(f"[YOUTUBE] Download error | video_id: {video_id}")
        return None


@app.route('/')
def index():
    return send_from_directory(app.template_folder, 'index.html')


@app.route('/uploads/<filename>')
def serve_uploads(filename):
    video_id, _, ext = filename.partition('.')
    if ext != 'mp4' or not valid_video_id(video_id):
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

    video_id = secrets.token_hex(16)
    filename = f"{video_id}.mp4"
    filepath = UPLOAD_FOLDER / filename
    file.save(filepath)

    logger.info(f"[UPLOAD] Saved | video_id: {video_id} | size: {filepath.stat().st_size/1024/1024:.2f}MB | IP: {request.remote_addr}")

    # Start transcription in background
    transcription_status[video_id] = {'status': 'processing', 'progress': 10}
    threading.Thread(target=transcribe_audio, args=(filepath, video_id), daemon=True).start()

    return jsonify({
        'video_id': video_id,
        'video_url': f'/uploads/{filename}',
        'message': 'Video uploaded, transcription started'
    })


@app.route('/api/youtube', methods=['POST'])
def process_youtube():
    """Process YouTube URL"""
    data = request.get_json(silent=True) or {}
    url = data.get('url')

    if not isinstance(url, str) or not url.strip():
        return jsonify({'error': 'No URL provided'}), 400
    url = url.strip()

    logger.info(f"[YOUTUBE] Request | URL: {url} | IP: {request.remote_addr}")

    # Validate YouTube URL (reject channels, playlists, etc.)
    is_valid, error_msg = is_valid_youtube_video_url(url)
    if not is_valid:
        return jsonify({'error': 'invalid_url', 'message': error_msg}), 400

    video_id = get_video_hash(url)

    # Check if already transcribed
    if load_transcript(video_id) is not None:
        transcription_status[video_id] = {'status': 'completed', 'progress': 100}
        if (UPLOAD_FOLDER / f"{video_id}.mp4").exists():
            return jsonify({
                'video_id': video_id,
                'video_url': f'/uploads/{video_id}.mp4',
                'cached': True,
                'message': 'Video already transcribed'
            })

    # Check video duration before downloading
    duration = get_video_duration(url)
    if duration and duration > MAX_VIDEO_DURATION:
        return jsonify({
            'error': 'duration_exceeded',
            'message': '5분 이하의 동영상만 불러올 수 있습니다.',
            'duration': duration
        }), 400

    logger.info(f"[YOUTUBE] Starting download | video_id: {video_id} | duration: {duration}s")
    transcription_status[video_id] = {'status': 'downloading', 'progress': 0}

    def process():
        video_path = download_youtube_video(url, video_id)
        if video_path:
            transcribe_audio(video_path, video_id)
        else:
            transcription_status[video_id] = {'status': 'error', 'error': '영상을 다운로드하지 못했어요.'}

    threading.Thread(target=process, daemon=True).start()

    return jsonify({
        'video_id': video_id,
        'video_url': f'/uploads/{video_id}.mp4',
        'message': 'Download and transcription started'
    })


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
    """Get transcript context around current timestamp"""
    data = load_transcript(video_id)
    if not data:
        return ""

    # Segments heard so far, plus the one playing right now
    context_segments = [seg['text'] for seg in data.get('segments', []) if seg['start'] <= current_time + 10]

    # Return last ~2000 chars of context
    context = ' '.join(context_segments)
    if len(context) > 2000:
        context = context[-2000:]

    return context


def parse_time(value):
    try:
        t = float(value)
    except (TypeError, ValueError):
        return 0.0
    return t if math.isfinite(t) and t >= 0 else 0.0


@app.route('/api/ask', methods=['POST'])
def ask_question():
    """Answer question about video content - streaming response"""
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

    context = get_context_at_timestamp(video_id, current_time)
    full_transcript = transcript.get('full_text', '')

    system_prompt = """너는 이 강의를 진행하는 선생님이야. 학생이 강의 중에 손을 들고 질문한 거라고 생각해.

답변 규칙:
1. 마크다운 문법(**, ##, - 등) 절대 사용하지 마. 그냥 평문으로만 답변해.
2. 핵심만 짧게 2-4문장으로 답변해. 길게 설명하지 마.
3. 강의에서 다룬 내용 위주로, 선생님이 직접 말하듯이 친근하게 답변해.
4. 강의 내용과 관련 없으면 "그건 이 강의 내용이 아니야. 강의 관련 질문해줘!" 라고 해.
5. 반말로 답변해."""

    user_prompt = f"""[강의 내용]
{full_transcript[:2000]}

[현재까지 들은 부분]
{context}

[학생 질문]
{question}

선생님처럼 핵심만 짧게 답변해."""

    def generate():
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=300,
                stream=True
            )

            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield f"data: {json.dumps({'content': chunk.choices[0].delta.content})}\n\n"

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception:
            logger.exception(f"[ASK] Error | video_id: {video_id}")
            yield f"data: {json.dumps({'error': '답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요.'})}\n\n"

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
