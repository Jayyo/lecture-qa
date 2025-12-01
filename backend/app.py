import os
import json
import subprocess
import requests
import re
import hashlib
import threading
import logging
from datetime import datetime
from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory, redirect, url_for, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Local Whisper support (optional)
try:
    import whisper
    import torch
    LOCAL_WHISPER_AVAILABLE = True
except ImportError:
    LOCAL_WHISPER_AVAILABLE = False

# Load .env from project root (one level up from backend/)
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path=env_path, override=True)

app = Flask(__name__, static_folder='../static', template_folder='../templates')
app.secret_key = os.getenv('SECRET_KEY', os.urandom(24).hex())

# Support reverse proxy (Caddy) - trust X-Forwarded-* headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

CORS(app, supports_credentials=True)

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///../data/users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Login manager
login_manager = LoginManager()
login_manager.init_app(app)

# OAuth setup
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# User model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(100))
    profile_pic = db.Column(db.String(200))
    provider = db.Column(db.String(50))  # 'google' or 'email'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Create tables
with app.app_context():
    db.create_all()

# Configuration
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '..', 'uploads')
TRANSCRIPT_FOLDER = os.path.join(os.path.dirname(__file__), '..', 'transcripts')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TRANSCRIPT_FOLDER, exist_ok=True)

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
    print(f"[Whisper] Loading local model: {WHISPER_MODEL}")
    local_whisper_model = whisper.load_model(WHISPER_MODEL)
    device = next(local_whisper_model.parameters()).device
    print(f"[Whisper] Model loaded on device: {device}")
elif USE_LOCAL_WHISPER and not LOCAL_WHISPER_AVAILABLE:
    print("[Whisper] Warning: USE_LOCAL_WHISPER=true but whisper not installed. Falling back to OpenAI API.")
    USE_LOCAL_WHISPER = False

client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Store transcription status and content
transcription_store = {}
transcription_status = {}

def get_request_info():
    """Get request info for logging"""
    return {
        'ip': request.headers.get('X-Forwarded-For', request.remote_addr),
        'user_agent': request.headers.get('User-Agent', 'Unknown')[:100],
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

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
    """Generate a hash for video identification"""
    return hashlib.md5(video_path_or_url.encode()).hexdigest()

def transcribe_with_local_whisper(audio_path, video_id):
    """Transcribe using local Whisper model"""
    global local_whisper_model

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
    import glob

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
            'ffmpeg', '-i', audio_path,
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
    print(f"[Whisper] Large file detected ({file_size / 1024 / 1024:.1f}MB), splitting into chunks...")
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

            print(f"[Whisper] Transcribing chunk {i+1}/{len(chunks)}...")

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

    except Exception as e:
        # Cleanup any remaining chunk files
        import glob
        base_path = audio_path.rsplit('.', 1)[0]
        for chunk_file in glob.glob(f"{base_path}_chunk*.mp3"):
            try:
                os.remove(chunk_file)
            except:
                pass
        raise e

def transcribe_audio(video_path, video_id):
    """Transcribe video audio using local Whisper or OpenAI API"""
    try:
        logger.info(f"[TRANSCRIBE] Starting | video_id: {video_id} | video_path: {video_path}")
        transcription_status[video_id] = {'status': 'processing', 'progress': 30}

        # Extract audio from video using ffmpeg
        audio_path = os.path.join(UPLOAD_FOLDER, f"{video_id}.mp3")

        transcription_status[video_id] = {'status': 'processing', 'progress': 35, 'message': 'Extracting audio...'}
        logger.info(f"[TRANSCRIBE] Extracting audio | video_id: {video_id}")

        # Extract audio
        subprocess.run([
            'ffmpeg', '-i', video_path, '-vn', '-acodec', 'libmp3lame',
            '-q:a', '4', '-y', audio_path
        ], check=True, capture_output=True)

        # Get audio file size for logging
        audio_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
        logger.info(f"[TRANSCRIBE] Audio extracted | video_id: {video_id} | size: {audio_size/1024/1024:.2f}MB")

        # Choose transcription method
        if USE_LOCAL_WHISPER and local_whisper_model is not None:
            logger.info(f"[TRANSCRIBE] Using local Whisper | video_id: {video_id}")
            transcript_data = transcribe_with_local_whisper(audio_path, video_id)
        else:
            logger.info(f"[TRANSCRIBE] Using OpenAI API | video_id: {video_id}")
            transcript_data = transcribe_with_openai_api(audio_path, video_id)

        # Save transcript
        transcript_path = os.path.join(TRANSCRIPT_FOLDER, f"{video_id}.json")
        with open(transcript_path, 'w', encoding='utf-8') as f:
            json.dump(transcript_data, f, ensure_ascii=False, indent=2)

        transcription_store[video_id] = transcript_data
        transcription_status[video_id] = {'status': 'completed', 'progress': 100}

        logger.info(f"[TRANSCRIBE] Completed | video_id: {video_id} | segments: {len(transcript_data.get('segments', []))}")

        # Cleanup audio file
        if os.path.exists(audio_path):
            os.remove(audio_path)

    except Exception as e:
        transcription_status[video_id] = {'status': 'error', 'error': str(e)}
        logger.error(f"[TRANSCRIBE] Error | video_id: {video_id} | error: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())

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
        print(f"Duration check error: {e}")
        return None

def download_youtube_video(url, video_id, yt_dlp_path='yt-dlp'):
    """Download YouTube video using yt-dlp with progress tracking"""
    output_path = os.path.join(UPLOAD_FOLDER, f"{video_id}.mp4")

    # Cookie file path for YouTube authentication (to bypass bot detection)
    cookies_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cookies.txt')

    try:
        print(f"[DEBUG] Starting download for {video_id}")
        print(f"[DEBUG] URL: {url}")
        print(f"[DEBUG] Output path: {output_path}")
        print(f"[DEBUG] Cookies file: {cookies_file}, exists: {os.path.exists(cookies_file)}")

        # Build yt-dlp command with cookies if available
        cmd = [
            yt_dlp_path, '--no-playlist',
            '-f', 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b',
            '--newline', '--progress',
            '--merge-output-format', 'mp4',
        ]

        # Add cookies file if it exists
        if os.path.exists(cookies_file):
            cmd.extend(['--cookies', cookies_file])
            print(f"[DEBUG] Using cookies file for authentication")

        cmd.extend(['-o', output_path, url])

        # Use yt-dlp with progress output and better format selection
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        for line in process.stdout:
            line = line.strip()
            print(f"[DEBUG] yt-dlp: {line}")
            # Parse download progress from yt-dlp output
            if '[download]' in line and '%' in line:
                try:
                    match = re.search(r'(\d+\.?\d*)%', line)
                    if match:
                        percent = float(match.group(1))
                        # Scale download progress to 0-25% of total
                        scaled_progress = int(percent * 0.25)
                        transcription_status[video_id] = {
                            'status': 'downloading',
                            'progress': scaled_progress
                        }
                        print(f"[DEBUG] Progress: {scaled_progress}%")
                except Exception as parse_err:
                    print(f"[DEBUG] Parse error: {parse_err}")

        process.wait()
        print(f"[DEBUG] Process finished with return code: {process.returncode}")

        if process.returncode == 0:
            transcription_status[video_id] = {'status': 'downloading', 'progress': 25}
            print(f"[DEBUG] Download successful, file exists: {os.path.exists(output_path)}")
            return output_path
        else:
            print(f"[DEBUG] Download failed with return code: {process.returncode}")
            return None
    except Exception as e:
        print(f"[DEBUG] Download error: {e}")
        import traceback
        traceback.print_exc()
        return None

@app.route('/')
def index():
    # Check if user has visited before or is logged in
    if current_user.is_authenticated or session.get('guest_access'):
        return send_from_directory('../templates', 'index.html')
    # First time visitor - redirect to login
    return redirect('/login')

@app.route('/login')
def login_page():
    # If already logged in or guest, redirect to main
    if current_user.is_authenticated or session.get('guest_access'):
        return redirect('/')
    return send_from_directory('../templates', 'login.html')

@app.route('/guest')
def guest_access():
    session['guest_access'] = True
    return redirect('/')

# Auth routes
@app.route('/auth/google')
def google_login():
    redirect_uri = url_for('google_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/google/callback')
def google_callback():
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')

        if not user_info:
            app.logger.error("No userinfo in token")
            return redirect('/login?error=failed')

        # Find or create user
        user = User.query.filter_by(email=user_info['email']).first()
        if not user:
            user = User(
                email=user_info['email'],
                name=user_info.get('name', ''),
                profile_pic=user_info.get('picture', ''),
                provider='google'
            )
            db.session.add(user)
            db.session.commit()

        login_user(user)
        return redirect('/')
    except Exception as e:
        import traceback
        app.logger.error(f"Google auth error: {e}")
        app.logger.error(traceback.format_exc())
        return redirect('/login?error=failed')

@app.route('/auth/logout')
def logout():
    logout_user()
    return redirect('/')

@app.route('/api/user')
def get_current_user():
    if current_user.is_authenticated:
        return jsonify({
            'logged_in': True,
            'id': current_user.id,
            'email': current_user.email,
            'name': current_user.name,
            'profile_pic': current_user.profile_pic
        })
    return jsonify({'logged_in': False})

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('../static', filename)

@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    return send_from_directory('../uploads', filename)

@app.route('/api/upload', methods=['POST'])
def upload_video():
    """Handle video file upload"""
    req_info = get_request_info()

    if 'video' not in request.files:
        logger.warning(f"[UPLOAD] No video file | IP: {req_info['ip']} | UA: {req_info['user_agent']}")
        return jsonify({'error': 'No video file provided'}), 400

    file = request.files['video']
    if file.filename == '':
        logger.warning(f"[UPLOAD] Empty filename | IP: {req_info['ip']} | UA: {req_info['user_agent']}")
        return jsonify({'error': 'No selected file'}), 400

    # Get file size
    file.seek(0, 2)  # Seek to end
    file_size = file.tell()
    file.seek(0)  # Seek back to start

    video_id = get_video_hash(file.filename + str(os.urandom(8)))
    filename = f"{video_id}.mp4"
    filepath = os.path.join(UPLOAD_FOLDER, filename)

    logger.info(f"[UPLOAD] Started | video_id: {video_id} | filename: {file.filename} | size: {file_size/1024/1024:.2f}MB | IP: {req_info['ip']} | UA: {req_info['user_agent']}")

    file.save(filepath)

    logger.info(f"[UPLOAD] File saved | video_id: {video_id} | path: {filepath}")

    # Start transcription in background
    transcription_status[video_id] = {'status': 'processing', 'progress': 10}
    thread = threading.Thread(target=transcribe_audio, args=(filepath, video_id))
    thread.start()

    logger.info(f"[UPLOAD] Transcription started | video_id: {video_id}")

    return jsonify({
        'video_id': video_id,
        'video_url': f'/uploads/{filename}',
        'message': 'Video uploaded, transcription started'
    })

@app.route('/api/youtube', methods=['POST'])
def process_youtube():
    """Process YouTube URL"""
    req_info = get_request_info()
    data = request.json
    url = data.get('url')

    if not url:
        logger.warning(f"[YOUTUBE] No URL provided | IP: {req_info['ip']} | UA: {req_info['user_agent']}")
        return jsonify({'error': 'No URL provided'}), 400

    logger.info(f"[YOUTUBE] Request | URL: {url} | IP: {req_info['ip']} | UA: {req_info['user_agent']}")

    # Validate YouTube URL (reject channels, playlists, etc.)
    is_valid, error_msg = is_valid_youtube_video_url(url)
    if not is_valid:
        logger.warning(f"[YOUTUBE] Invalid URL rejected | URL: {url} | Reason: {error_msg} | IP: {req_info['ip']}")
        return jsonify({'error': 'invalid_url', 'message': error_msg}), 400

    video_id = get_video_hash(url)

    # Check if already transcribed
    transcript_path = os.path.join(TRANSCRIPT_FOLDER, f"{video_id}.json")
    if os.path.exists(transcript_path):
        logger.info(f"[YOUTUBE] Cache hit | video_id: {video_id} | URL: {url}")
        with open(transcript_path, 'r', encoding='utf-8') as f:
            transcription_store[video_id] = json.load(f)
        transcription_status[video_id] = {'status': 'completed', 'progress': 100}

        video_path = os.path.join(UPLOAD_FOLDER, f"{video_id}.mp4")
        if os.path.exists(video_path):
            return jsonify({
                'video_id': video_id,
                'video_url': f'/uploads/{video_id}.mp4',
                'cached': True,
                'message': 'Video already transcribed'
            })

    # Check video duration before downloading
    duration = get_video_duration(url)
    if duration and duration > MAX_VIDEO_DURATION:
        logger.warning(f"[YOUTUBE] Duration exceeded | URL: {url} | duration: {duration}s | IP: {req_info['ip']}")
        return jsonify({
            'error': 'duration_exceeded',
            'message': '5분 이하의 동영상만 불러올 수 있습니다.',
            'duration': duration
        }), 400

    logger.info(f"[YOUTUBE] Starting download | video_id: {video_id} | URL: {url} | duration: {duration}s")

    transcription_status[video_id] = {'status': 'downloading', 'progress': 0}

    # Download and transcribe in background
    def process():
        video_path = download_youtube_video(url, video_id)
        if video_path:
            logger.info(f"[YOUTUBE] Download complete | video_id: {video_id}")
            transcribe_audio(video_path, video_id)
        else:
            logger.error(f"[YOUTUBE] Download failed | video_id: {video_id} | URL: {url}")
            transcription_status[video_id] = {'status': 'error', 'error': 'Failed to download video'}

    thread = threading.Thread(target=process)
    thread.start()

    return jsonify({
        'video_id': video_id,
        'video_url': f'/uploads/{video_id}.mp4',
        'message': 'Download and transcription started'
    })

@app.route('/api/status/<video_id>')
def get_status(video_id):
    """Get transcription status"""
    status = transcription_status.get(video_id, {'status': 'unknown'})
    return jsonify(status)

@app.route('/api/transcript/<video_id>')
def get_transcript(video_id):
    """Get transcript for a video"""
    if video_id in transcription_store:
        return jsonify(transcription_store[video_id])

    transcript_path = os.path.join(TRANSCRIPT_FOLDER, f"{video_id}.json")
    if os.path.exists(transcript_path):
        with open(transcript_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            transcription_store[video_id] = data
            return jsonify(data)

    return jsonify({'error': 'Transcript not found'}), 404

def get_context_at_timestamp(video_id, current_time):
    """Get transcript context around current timestamp"""
    if video_id not in transcription_store:
        return ""

    segments = transcription_store[video_id].get('segments', [])

    # Get segments before current time (context)
    context_segments = []
    for seg in segments:
        if seg['end'] <= current_time + 10:  # Include current and a bit ahead
            context_segments.append(seg['text'])

    # Return last ~2000 chars of context
    context = ' '.join(context_segments)
    if len(context) > 2000:
        context = context[-2000:]

    return context

@app.route('/api/ask', methods=['POST'])
def ask_question():
    """Answer question about video content - streaming response"""
    data = request.json
    video_id = data.get('video_id')
    question = data.get('question')
    current_time = data.get('current_time', 0)

    if not video_id or not question:
        return jsonify({'error': 'Missing video_id or question'}), 400

    # Load transcript from file if not in memory
    if video_id not in transcription_store:
        transcript_path = os.path.join(TRANSCRIPT_FOLDER, f"{video_id}.json")
        if os.path.exists(transcript_path):
            with open(transcript_path, 'r', encoding='utf-8') as f:
                transcription_store[video_id] = json.load(f)

    # Get context from transcript
    context = get_context_at_timestamp(video_id, current_time)
    full_transcript = transcription_store.get(video_id, {}).get('full_text', '')

    if not context and not full_transcript:
        return jsonify({'error': 'Transcript not available'}), 404

    # Build prompt
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
                if chunk.choices[0].delta.content:
                    yield f"data: {json.dumps({'content': chunk.choices[0].delta.content})}\n\n"

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

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
    data = request.json
    video_id = data.get('video_id')
    question = data.get('question')
    answer = data.get('answer')
    current_time = data.get('current_time', 0)
    feedback_type = data.get('feedback_type', 'negative')

    if feedback_type != 'negative':
        return jsonify({'message': 'Positive feedback recorded'}), 200

    # Get logged-in user info
    student_name = "익명 학생"
    student_email = None
    if current_user.is_authenticated:
        student_name = current_user.name or current_user.email.split('@')[0]
        student_email = current_user.email

    # Get context
    context = get_context_at_timestamp(video_id, current_time)

    # Prepare email
    professor_email = os.getenv('PROFESSOR_EMAIL', 'gracekim7765@gmail.com')

    resend_api_key = os.getenv('RESEND_API_KEY')

    if not resend_api_key:
        return jsonify({'error': 'Email not configured', 'message': 'RESEND_API_KEY not set'}), 500

    # Format timestamp
    minutes = int(current_time // 60)
    seconds = int(current_time % 60)
    timestamp_str = f"{minutes}:{seconds:02d}"

    # Include student info in subject if logged in
    if student_email:
        subject = f"[강의 Q&A] {student_name} 학생의 질문 - 답변 검토 요청"
    else:
        subject = f"[강의 Q&A] 학생 질문에 대한 답변 검토 요청"

    # Build student info section
    student_info = f"👤 학생: {student_name}"
    if student_email:
        student_info += f" ({student_email})"

    body = f"""안녕하세요 교수님,

학생이 강의 영상에서 질문을 했으나, AI 답변에 만족하지 못했습니다.
확인 후 정확한 답변을 부탁드립니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{student_info}

📍 질문 시점: {timestamp_str}

📝 해당 시점 강의 내용:
{context[:500]}...

❓ 학생 질문:
{question}

🤖 AI 답변 (학생이 불만족):
{answer}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

감사합니다.
강의 Q&A 시스템
"""

    try:
        email_data = {
            'from': 'Lecture QA <onboarding@resend.dev>',
            'to': [professor_email],
            'subject': subject,
            'text': body
        }

        # Add reply_to if student is logged in (so professor can reply directly)
        if student_email:
            email_data['reply_to'] = student_email

        response = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {resend_api_key}',
                'Content-Type': 'application/json'
            },
            json=email_data
        )

        if response.status_code == 200:
            return jsonify({'message': 'Feedback sent to professor successfully'})
        else:
            return jsonify({'error': response.json()}), 500

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
