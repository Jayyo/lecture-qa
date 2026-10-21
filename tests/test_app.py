import json

import pytest

import backend.app as app_module
from conftest import needs_ffmpeg

VIDEO_ID = 'yt-dQw4w9WgXcQ'


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    app_module.limiter.hits.clear()
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture
def transcript():
    data = {'model': 'test', 'title': 'Binary search', 'duration': 60.0, 'segments': [
        {'start': 0.0, 'end': 30.0, 'text': 'Binary search needs sorted data.'},
        {'start': 30.0, 'end': 60.0, 'text': 'Each step halves the range.'},
    ]}
    app_module.transcript_path(VIDEO_ID).write_text(json.dumps(data), encoding='utf-8')
    app_module.transcription_store.pop(VIDEO_ID, None)
    yield data
    app_module.transcript_path(VIDEO_ID).unlink(missing_ok=True)
    app_module.transcription_store.pop(VIDEO_ID, None)


def read_events(response):
    return [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
            if line.startswith('data: ')]


def test_health_and_config(client):
    assert client.get('/healthz').data == b'ok'
    config = client.get('/api/config').get_json()
    assert set(config) == {'max_minutes', 'max_upload_mb', 'feedback_enabled'}


def test_security_headers_and_no_cors(client):
    response = client.get('/', headers={'Origin': 'https://evil.example'})
    assert response.status_code == 200
    assert 'Access-Control-Allow-Origin' not in response.headers
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert response.headers['X-Content-Type-Options'] == 'nosniff'


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.example'},
    {'Sec-Fetch-Site': 'cross-site'},
])
def test_cross_site_posts_are_rejected(client, headers):
    response = client.post('/api/youtube', json={'url': 'https://youtu.be/dQw4w9WgXcQ'}, headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize('path', [
    '/api/status/abc',
    '/api/status/yt-dQw4w9WgXcQ%0A',
    '/api/transcript/..',
    '/uploads/app.py',
    '/uploads/yt-dQw4w9WgXcQ.json',
])
def test_unknown_or_malformed_ids_are_404(client, path):
    assert client.get(path).status_code == 404


def test_ask_rejects_path_like_ids(client):
    response = client.post('/api/ask', json={'video_id': '/etc/passwd', 'question': 'hi'})
    assert response.status_code == 400


def test_youtube_rejects_non_youtube_urls(client):
    response = client.post('/api/youtube', json={'url': 'http://169.254.169.254/?v=youtube.com/watch?v=abcdefghijk'})
    assert response.status_code == 400


def test_upload_rejects_other_extensions(client, tmp_path):
    response = client.post('/api/upload', data={'video': (open(__file__, 'rb'), 'notes.txt')})
    assert response.status_code == 400


@needs_ffmpeg
def test_upload_rejects_renamed_concat_list(client):
    import io
    payload = io.BytesIO(b'ffconcat version 1.0\nfile /etc/hostname\n')
    response = client.post('/api/upload', data={'video': (payload, 'lecture.mp4')})
    assert response.status_code == 400
    assert not list(app_module.UPLOAD_FOLDER.iterdir())


def test_ask_streams_answer_and_feedback_uses_stored_answer(client, transcript, monkeypatch):
    prompts = []

    def fake_stream(_client, _model, prompt, *args):
        prompts.append(prompt)
        yield 'Sorted data [00:00]'
        yield ' is required.'

    monkeypatch.setattr(app_module.qa, 'stream_answer', fake_stream)
    response = client.post('/api/ask', json={'video_id': VIDEO_ID, 'question': 'Why sorted?', 'current_time': 12,
                                             'history': [{'question': 'q', 'answer': 'a'}]})
    events = read_events(response)
    answer_id = events[0]['answer_id']
    assert ''.join(e.get('delta', '') for e in events) == 'Sorted data [00:00] is required.'
    assert events[-1] == {'done': True}
    assert '>>> the student paused here, at 00:12 <<<' in prompts[0]
    assert 'Student: q' in prompts[0]

    sent = []

    class FakeResponse:
        status_code = 200
        text = '{"id": "x"}'

    def fake_post(url, headers, json, timeout):
        sent.append(json)
        return FakeResponse()

    monkeypatch.setattr(app_module, 'FEEDBACK_ENABLED', True)
    monkeypatch.setattr(app_module, 'PROFESSOR_EMAIL', 'prof@example.com')
    monkeypatch.setattr(app_module, 'FEEDBACK_FROM', 'QA <qa@example.com>')
    monkeypatch.setattr(app_module, 'RESEND_API_KEY', 'test')
    monkeypatch.setattr(app_module.requests, 'post', fake_post)

    payload = {'answer_id': answer_id, 'comment': 'Still unclear', 'reply_to': 'student@example.com',
               'question': 'forged', 'answer': 'forged'}
    assert client.post('/api/feedback', json=payload).status_code == 200
    mail = sent[0]
    assert mail['to'] == ['prof@example.com'] and mail['reply_to'] == 'student@example.com'
    assert 'Sorted data [00:00] is required.' in mail['text'] and 'Still unclear' in mail['text']
    assert 'forged' not in mail['text']

    # one mail per answer
    assert client.post('/api/feedback', json=payload).status_code == 409


def test_feedback_disabled_and_unknown_answers(client, monkeypatch):
    monkeypatch.setattr(app_module, 'FEEDBACK_ENABLED', False)
    assert client.post('/api/feedback', json={'answer_id': 'x'}).status_code == 503
    monkeypatch.setattr(app_module, 'FEEDBACK_ENABLED', True)
    assert client.post('/api/feedback', json={'answer_id': 'nope'}).status_code == 404
    assert client.post('/api/feedback', json={'answer_id': 'nope', 'reply_to': 'not-an-email'}).status_code == 400


def test_question_rate_limit(client, transcript, monkeypatch):
    monkeypatch.setattr(app_module, 'QUESTIONS_PER_HOUR', 2)
    monkeypatch.setattr(app_module.qa, 'stream_answer', lambda *args: iter(['ok']))
    codes = [client.post('/api/ask', json={'video_id': VIDEO_ID, 'question': 'q'}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
