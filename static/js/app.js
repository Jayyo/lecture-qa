// Lecture Q&A front end: pick a video, follow the transcription job, ask questions.

const $ = (id) => document.getElementById(id);

const startSection = $('startSection');
const workspace = $('workspace');
const dropZone = $('dropZone');
const videoInput = $('videoInput');
const youtubeForm = $('youtubeForm');
const youtubeUrl = $('youtubeUrl');
const loadYoutubeBtn = $('loadYoutube');
const videoTitle = $('videoTitle');
const newVideoBtn = $('newVideoBtn');
const videoPlayer = $('videoPlayer');
const ytMount = $('ytMount');
const jobStatus = $('jobStatus');
const jobStatusText = $('jobStatusText');
const jobStatusPercent = $('jobStatusPercent');
const progressFill = $('progressFill');
const transcriptList = $('transcriptList');
const transcriptMeta = $('transcriptMeta');
const chatMessages = $('chatMessages');
const chatForm = $('chatForm');
const chatInput = $('chatInput');
const sendBtn = $('sendBtn');

const welcomeMessage = chatMessages.querySelector('.welcome-message').cloneNode(true);
const emptyTranscript = transcriptList.querySelector('.transcript-empty').cloneNode(true);

let config = { max_minutes: 20, max_upload_mb: 300 };
let job = null;            // { id, kind: 'upload' | 'youtube', ... }
let segments = [];
let history = [];          // last questions and answers, sent along for follow-ups
let pollTimer = null;
let askController = null;
let uploadRequest = null;
let activeIndex = -1;

fetch('/api/config')
    .then((response) => response.json())
    .then((data) => {
        config = data;
        $('limitHint').textContent = `MP4 · WebM, ${data.max_minutes}분 · ${data.max_upload_mb}MB까지`;
    })
    .catch(() => {});

// ---------------------------------------------------------------- helpers

let toastTimer = null;
function showToast(message, type = '') {
    const toast = $('toast');
    toast.textContent = message;
    toast.className = `toast show ${type}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3500);
}

function formatTime(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const h = Math.floor(total / 3600);
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
    const s = String(total % 60).padStart(2, '0');
    return h ? `${h}:${m}:${s}` : `${m}:${s}`;
}

async function postJSON(url, body, options = {}) {
    return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        ...options,
    });
}

// ---------------------------------------------------------------- player
// One interface over the <video> element (uploads) and the YouTube iframe.

const player = {
    kind: null,
    yt: null,

    time() {
        if (this.kind === 'youtube') {
            return (this.yt && this.yt.getCurrentTime && this.yt.getCurrentTime()) || 0;
        }
        return videoPlayer.currentTime || 0;
    },
    play() {
        if (this.kind === 'youtube') {
            if (this.yt && this.yt.playVideo) this.yt.playVideo();
        } else {
            videoPlayer.play().catch(() => {});
        }
    },
    pause() {
        if (this.kind === 'youtube') {
            if (this.yt && this.yt.pauseVideo) this.yt.pauseVideo();
        } else {
            videoPlayer.pause();
        }
    },
    seek(seconds) {
        if (this.kind === 'youtube') {
            if (this.yt && this.yt.seekTo) this.yt.seekTo(seconds, true);
        } else {
            videoPlayer.currentTime = seconds;
        }
    },
    destroy() {
        if (this.yt) {
            try { this.yt.destroy(); } catch (e) { /* already gone */ }
            this.yt = null;
        }
        ytMount.replaceChildren();
        ytMount.hidden = true;
        videoPlayer.pause();
        videoPlayer.removeAttribute('src');
        videoPlayer.load();
        videoPlayer.hidden = true;
        this.kind = null;
    },
};

let youtubeApi = null;
function loadYouTubeApi() {
    if (!youtubeApi) {
        youtubeApi = new Promise((resolve, reject) => {
            if (window.YT && window.YT.Player) {
                resolve(window.YT);
                return;
            }
            window.onYouTubeIframeAPIReady = () => resolve(window.YT);
            const script = document.createElement('script');
            script.src = 'https://www.youtube.com/iframe_api';
            script.onerror = () => {
                youtubeApi = null;
                reject(new Error('YouTube API failed to load'));
            };
            document.head.appendChild(script);
        });
    }
    return youtubeApi;
}

async function mountYouTube(youtubeId) {
    const YT = await loadYouTubeApi();
    const target = document.createElement('div');
    ytMount.replaceChildren(target);
    ytMount.hidden = false;
    player.kind = 'youtube';
    player.yt = new YT.Player(target, {
        host: 'https://www.youtube-nocookie.com',
        videoId: youtubeId,
        playerVars: { rel: 0, playsinline: 1 },
        events: {
            onError: (event) => {
                if (event.data === 101 || event.data === 150) {
                    showToast('영상 소유자가 다른 사이트에서의 재생을 막아 두었어요. 영상 파일을 직접 올려 주세요.', 'error');
                }
            },
        },
    });
}

function mountUpload(url) {
    player.kind = 'html5';
    videoPlayer.src = url;
    videoPlayer.hidden = false;
}

videoPlayer.addEventListener('error', () => {
    if (videoPlayer.getAttribute('src')) {
        showToast('브라우저가 이 영상을 재생하지 못해요. H.264 MP4로 변환해서 올려 주세요.', 'error');
    }
});

// ---------------------------------------------------------------- start screen

videoInput.addEventListener('change', () => {
    if (videoInput.files[0]) uploadFile(videoInput.files[0]);
});

['dragenter', 'dragover'].forEach((type) => dropZone.addEventListener(type, (event) => {
    event.preventDefault();
    dropZone.classList.add('dragging');
}));
['dragleave', 'drop'].forEach((type) => dropZone.addEventListener(type, (event) => {
    event.preventDefault();
    dropZone.classList.remove('dragging');
}));
dropZone.addEventListener('drop', (event) => {
    const file = event.dataTransfer.files[0];
    if (file) uploadFile(file);
});

function uploadFile(file) {
    const ext = file.name.split('.').pop().toLowerCase();
    if (!['mp4', 'm4v', 'mov', 'webm'].includes(ext)) {
        showToast('MP4 또는 WebM 영상만 올릴 수 있어요.', 'error');
        return;
    }
    if (file.size > config.max_upload_mb * 1024 * 1024) {
        showToast(`${config.max_upload_mb}MB 이하의 파일만 올릴 수 있어요.`, 'error');
        return;
    }

    openWorkspace(file.name);
    setStatus('영상을 올리는 중', 0);

    const form = new FormData();
    form.append('video', file);
    const xhr = new XMLHttpRequest();
    uploadRequest = xhr;
    xhr.open('POST', '/api/upload');
    xhr.responseType = 'json';
    xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) setStatus('영상을 올리는 중', Math.round((event.loaded / event.total) * 100));
    });
    xhr.addEventListener('load', () => {
        uploadRequest = null;
        const data = xhr.response || {};
        if (xhr.status !== 200) {
            resetToStart();
            showToast(data.message || '업로드에 실패했어요.', 'error');
            return;
        }
        startJob({ id: data.video_id, kind: 'upload', mediaUrl: data.video_url });
    });
    xhr.addEventListener('error', () => {
        uploadRequest = null;
        resetToStart();
        showToast('업로드 중에 연결이 끊겼어요.', 'error');
    });
    xhr.send(form);
}

youtubeForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const url = youtubeUrl.value.trim();
    if (!url) {
        showToast('YouTube 영상 주소를 입력해 주세요.', 'error');
        return;
    }
    loadYoutubeBtn.disabled = true;
    try {
        const response = await postJSON('/api/youtube', { url });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            showToast(data.message || '영상을 불러오지 못했어요.', 'error');
            return;
        }
        openWorkspace(data.title || 'YouTube 영상');
        startJob({ id: data.video_id, kind: 'youtube', youtubeId: data.youtube_id, ready: data.cached });
    } catch (error) {
        showToast('영상을 불러오지 못했어요.', 'error');
    } finally {
        loadYoutubeBtn.disabled = false;
    }
});

// ---------------------------------------------------------------- job

function openWorkspace(title) {
    startSection.hidden = true;
    workspace.hidden = false;
    newVideoBtn.hidden = false;
    videoTitle.textContent = title;
}

function setStatus(text, percent) {
    jobStatus.hidden = false;
    jobStatus.classList.remove('error');
    jobStatusText.textContent = text;
    jobStatusPercent.textContent = `${percent}%`;
    progressFill.style.width = `${percent}%`;
}

function failJob(message) {
    clearTimeout(pollTimer);
    jobStatus.hidden = false;
    jobStatus.classList.add('error');
    jobStatusText.textContent = message;
    transcriptList.replaceChildren();
    showToast(message, 'error');
}

function startJob(next) {
    job = next;
    if (job.kind === 'upload') {
        mountUpload(job.mediaUrl);
    } else {
        mountYouTube(job.youtubeId).catch(() => showToast('YouTube 플레이어를 불러오지 못했어요.', 'error'));
    }
    if (job.ready) {
        loadTranscript();
    } else {
        setStatus('작업을 준비하는 중', 0);
        pollStatus(job.id);
    }
}

function statusText(data) {
    if (data.status === 'downloading') return 'YouTube에서 오디오를 받는 중';
    if ((data.progress || 0) < 30) return '오디오를 추출하는 중';
    return '음성을 자막으로 바꾸는 중';
}

function pollStatus(id) {
    let failures = 0;
    const tick = async () => {
        if (!job || job.id !== id) return;
        let response = null;
        let data = null;
        try {
            response = await fetch(`/api/status/${encodeURIComponent(id)}`);
            data = await response.json();
        } catch (error) {
            data = null;
        }
        if (!job || job.id !== id) return;

        if (response && response.status === 404) {
            failJob('작업을 찾을 수 없어요. 영상을 다시 열어 주세요.');
            return;
        }
        if (!data) {
            if (++failures >= 10) {
                failJob('서버에 연결할 수 없어요.');
                return;
            }
        } else {
            failures = 0;
            if (data.title) videoTitle.textContent = data.title;
            if (data.status === 'completed') {
                loadTranscript();
                return;
            }
            if (data.status === 'error') {
                failJob(data.error || '영상을 처리하지 못했어요.');
                return;
            }
            setStatus(statusText(data), data.progress || 0);
        }
        pollTimer = setTimeout(tick, 1000);
    };
    tick();
}

async function loadTranscript() {
    const id = job.id;
    let data = null;
    try {
        const response = await fetch(`/api/transcript/${encodeURIComponent(id)}`);
        if (response.ok) data = await response.json();
    } catch (error) {
        data = null;
    }
    if (!job || job.id !== id) return;
    if (!data) {
        failJob('자막을 불러오지 못했어요.');
        return;
    }

    segments = data.segments || [];
    if (data.title) videoTitle.textContent = data.title;
    renderTranscript(data);
    jobStatus.hidden = true;
    chatInput.disabled = false;
    sendBtn.disabled = false;
    chatInput.placeholder = '질문을 입력하세요';
}

// ---------------------------------------------------------------- transcript

function renderTranscript(data) {
    transcriptMeta.textContent = [`${segments.length}개 구간`, data.model].filter(Boolean).join(' · ');
    activeIndex = -1;
    const items = segments.map((segment, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'segment';
        button.dataset.index = index;

        const time = document.createElement('span');
        time.className = 'segment-time';
        time.textContent = formatTime(segment.start);
        const text = document.createElement('span');
        text.textContent = segment.text;
        button.append(time, text);
        button.addEventListener('click', () => {
            player.seek(segment.start);
            player.play();
        });

        const item = document.createElement('li');
        item.appendChild(button);
        return item;
    });
    transcriptList.replaceChildren(...items);
}

// Highlight the segment that is playing and keep it in view
setInterval(() => {
    if (!segments.length || workspace.hidden) return;
    const now = player.time();
    let index = -1;
    for (let i = 0; i < segments.length && segments[i].start <= now + 0.1; i++) index = i;
    if (index === activeIndex) return;

    const previous = transcriptList.querySelector('.segment.active');
    if (previous) previous.classList.remove('active');
    activeIndex = index;
    const current = transcriptList.querySelector(`.segment[data-index="${index}"]`);
    if (!current) return;
    current.classList.add('active');
    const top = current.offsetTop;
    const bottom = top + current.offsetHeight;
    if (top < transcriptList.scrollTop || bottom > transcriptList.scrollTop + transcriptList.clientHeight) {
        transcriptList.scrollTo({ top: top - 8, behavior: 'smooth' });
    }
}, 300);

// ---------------------------------------------------------------- chat

chatForm.addEventListener('submit', (event) => {
    event.preventDefault();
    ask();
});

// Enter that only finishes a Korean IME composition must not submit
chatInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.isComposing || event.keyCode === 229)) event.preventDefault();
});

function addMessage(sender, text, at) {
    const welcome = chatMessages.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const message = document.createElement('div');
    message.className = `chat-message ${sender}`;
    const label = document.createElement('div');
    label.className = 'message-label';
    label.textContent = sender === 'user' ? `나 · ${formatTime(at)}` : 'AI';
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.textContent = text;
    message.append(label, bubble);
    chatMessages.appendChild(message);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return bubble;
}

// Answers cite moments like [03:12]; those become buttons that jump there.
const TIMESTAMP_RE = /\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\s*[-–~]\s*(?:\d{1,2}:)?\d{1,2}:\d{2})?\]/g;

function segmentStartFor(seconds) {
    if (!segments.length) return null;
    const last = segments[segments.length - 1];
    if (seconds > Math.max(last.end || 0, last.start) + 1) return null;
    let start = segments[0].start;
    for (const segment of segments) {
        if (segment.start > seconds + 0.5) break;
        start = segment.start;
    }
    return start;
}

function renderAnswer(element, text) {
    const nodes = [];
    let last = 0;
    for (const match of text.matchAll(TIMESTAMP_RE)) {
        const cited = Number(match[1] || 0) * 3600 + Number(match[2]) * 60 + Number(match[3]);
        const target = segmentStartFor(cited);
        if (target === null) continue;
        nodes.push(document.createTextNode(text.slice(last, match.index)));
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'timestamp-chip';
        chip.textContent = formatTime(cited);
        chip.title = '이 장면으로 이동';
        chip.addEventListener('click', () => {
            player.seek(target);
            player.play();
        });
        nodes.push(chip);
        last = match.index + match[0].length;
    }
    nodes.push(document.createTextNode(text.slice(last)));
    element.replaceChildren(...nodes);
}

function addAnswerActions(bubble, item) {
    const actions = document.createElement('div');
    actions.className = 'message-actions';

    const resume = document.createElement('button');
    resume.type = 'button';
    resume.className = 'link-btn';
    resume.textContent = '이어서 보기';
    resume.addEventListener('click', () => player.play());
    actions.appendChild(resume);

    const feedback = document.createElement('button');
    feedback.type = 'button';
    feedback.className = 'link-btn';
    feedback.textContent = '교수님께 질문 보내기';
    feedback.addEventListener('click', () => openFeedback(item));
    actions.appendChild(feedback);

    bubble.parentElement.appendChild(actions);
}

async function ask() {
    const question = chatInput.value.trim();
    if (!question || !job || askController) return;

    const at = player.time();
    player.pause();
    chatInput.value = '';
    addMessage('user', question, at);
    const bubble = addMessage('assistant', '');
    bubble.classList.add('pending');
    sendBtn.disabled = true;

    const controller = new AbortController();
    askController = controller;
    let answer = '';
    try {
        const response = await postJSON('/api/ask', {
            video_id: job.id,
            question,
            current_time: at,
            history: history.slice(-3),
        }, { signal: controller.signal });
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.message || '답변을 가져오지 못했어요.');
        }

        let failure = null;
        await readEventStream(response, (event) => {
            if (event.delta) {
                answer += event.delta;
                renderAnswer(bubble, answer);
                chatMessages.scrollTop = chatMessages.scrollHeight;
            }
            if (event.error) failure = event.error;
        });
        if (failure) throw new Error(failure);
        if (!answer) throw new Error('빈 답변이 왔어요. 다시 질문해 주세요.');

        history.push({ question, answer });
        addAnswerActions(bubble, { question, answer, at });
    } catch (error) {
        if (error.name !== 'AbortError') bubble.textContent = error.message || '답변을 가져오지 못했어요.';
    } finally {
        bubble.classList.remove('pending');
        if (askController === controller) {
            askController = null;
            sendBtn.disabled = !job;
        }
    }
}

// Server-sent events arrive in arbitrary network chunks; split on blank lines.
async function readEventStream(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        let boundary;
        while ((boundary = buffer.indexOf('\n\n')) !== -1) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const payload = block.split('\n')
                .filter((line) => line.startsWith('data: '))
                .map((line) => line.slice(6))
                .join('\n');
            if (!payload) continue;
            try {
                onEvent(JSON.parse(payload));
            } catch (error) {
                console.warn('Bad event', payload);
            }
        }
        if (done) break;
    }
}

// ---------------------------------------------------------------- professor feedback

function openFeedback(item) {
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.innerHTML = `
        <div class="modal-content" role="dialog" aria-modal="true">
            <h3>교수님께 질문 보내기</h3>
            <p class="modal-hint">질문, AI 답변, 질문한 시점의 강의 내용이 함께 전달돼요.</p>
            <textarea placeholder="AI 답변에서 부족했던 점이나 더 궁금한 점을 적어 주세요"></textarea>
            <div class="modal-buttons">
                <button type="button" class="btn btn-ghost" data-action="cancel">취소</button>
                <button type="button" class="btn btn-primary" data-action="send">보내기</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    const textarea = modal.querySelector('textarea');
    const sendButton = modal.querySelector('[data-action="send"]');
    modal.querySelector('[data-action="cancel"]').addEventListener('click', () => modal.remove());
    modal.addEventListener('click', (event) => {
        if (event.target === modal) modal.remove();
    });
    sendButton.addEventListener('click', async () => {
        sendButton.disabled = true;
        try {
            const response = await postJSON('/api/feedback', {
                video_id: job.id,
                question: item.question,
                answer: item.answer,
                current_time: item.at,
                user_feedback: textarea.value.trim(),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.message || '보내지 못했어요.');
            modal.remove();
            showToast('교수님께 보냈어요.', 'success');
        } catch (error) {
            sendButton.disabled = false;
            showToast(error.message, 'error');
        }
    });
    textarea.focus();
}

// ---------------------------------------------------------------- reset

function resetToStart() {
    clearTimeout(pollTimer);
    if (askController) askController.abort();
    if (uploadRequest) uploadRequest.abort();
    askController = null;
    uploadRequest = null;
    job = null;
    segments = [];
    history = [];
    activeIndex = -1;

    player.destroy();
    chatMessages.replaceChildren(welcomeMessage.cloneNode(true));
    transcriptList.replaceChildren(emptyTranscript.cloneNode(true));
    transcriptMeta.textContent = '';
    videoTitle.textContent = '';
    jobStatus.hidden = true;
    chatInput.value = '';
    chatInput.disabled = true;
    sendBtn.disabled = true;
    chatInput.placeholder = '자막을 만드는 중이에요...';

    workspace.hidden = true;
    startSection.hidden = false;
    newVideoBtn.hidden = true;
    videoInput.value = '';
    youtubeUrl.value = '';
}

newVideoBtn.addEventListener('click', resetToStart);
