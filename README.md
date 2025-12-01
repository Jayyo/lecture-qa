# Lecture Q&A - AI 강의 질문 도우미

강의 영상을 업로드하거나 YouTube URL을 입력하면 AI가 강의 내용을 분석하고, 학생들의 질문에 답변해주는 웹 애플리케이션입니다.

## 주요 기능

- **영상 업로드**: 로컬 강의 영상 파일 업로드 (MP4)
- **YouTube 지원**: YouTube URL 입력으로 영상 자동 다운로드
- **음성 인식**: OpenAI Whisper API를 통한 자동 자막 생성
- **AI 질문 답변**: GPT 기반 강의 내용 관련 질문 응답
- **타임스탬프 연동**: 현재 재생 위치 기준 맥락 파악
- **교수님 피드백**: AI 답변이 불만족스러울 경우 교수님께 이메일 전송
- **Google OAuth**: 구글 계정으로 로그인

## 기술 스택

- **Backend**: Flask, Gunicorn
- **Frontend**: Vanilla JavaScript, HTML/CSS
- **AI**: OpenAI GPT-4o-mini, Whisper API
- **Database**: SQLite (사용자 정보)
- **Deployment**: Docker, Caddy (HTTPS)
- **External**: yt-dlp (YouTube 다운로드), FFmpeg (오디오 추출)

## 설치 및 실행

### 환경 변수 설정

`.env` 파일을 생성하고 다음 내용을 설정:

```env
SECRET_KEY=your-secret-key
OPENAI_API_KEY=your-openai-api-key
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
RESEND_API_KEY=your-resend-api-key
PROFESSOR_EMAIL=professor@example.com
```

### Docker로 실행

```bash
docker compose up -d
```

### 로컬 개발 환경

```bash
# 가상환경 생성
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 의존성 설치
pip install -r requirements.txt

# 실행
python backend/app.py
```

## 프로젝트 구조

```
.
├── backend/
│   └── app.py          # Flask 애플리케이션
├── static/
│   ├── css/
│   │   └── style.css   # 스타일시트
│   └── js/
│       └── app.js      # 프론트엔드 로직
├── templates/
│   ├── index.html      # 메인 페이지
│   └── login.html      # 로그인 페이지
├── uploads/            # 업로드된 영상 저장
├── transcripts/        # 자막 JSON 저장
├── data/               # SQLite DB
├── Dockerfile
├── docker-compose.yml
├── Caddyfile           # Caddy 리버스 프록시 설정
└── requirements.txt
```

## API 엔드포인트

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/upload` | 영상 파일 업로드 |
| POST | `/api/youtube` | YouTube URL 처리 |
| GET | `/api/status/<video_id>` | 처리 상태 확인 |
| GET | `/api/transcript/<video_id>` | 자막 조회 |
| POST | `/api/ask` | AI 질문 (스트리밍) |
| POST | `/api/feedback` | 교수님 피드백 전송 |
| GET | `/api/user` | 현재 사용자 정보 |

## 제한 사항

- 영상 길이: 최대 5분
- 파일 크기: 25MB 이상 파일은 자동 분할 처리
- YouTube: 개별 영상만 지원 (채널/재생목록 불가)

## 라이선스

MIT License
