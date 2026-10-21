// State
let currentVideoId = null;
let currentAnswer = '';
let currentQuestion = '';
let isTranscribing = false;
let statusCheckInterval = null;
let questionTime = 0;
let isAsking = false;

// DOM Elements
const uploadBox = document.getElementById('uploadBox');
const videoInput = document.getElementById('videoInput');
const youtubeUrl = document.getElementById('youtubeUrl');
const loadYoutube = document.getElementById('loadYoutube');
const uploadSection = document.getElementById('uploadSection');
const videoSection = document.getElementById('videoSection');
const videoPlayer = document.getElementById('videoPlayer');
const videoContainer = document.getElementById('videoContainer');
const answerOverlay = document.getElementById('answerOverlay');
const answerText = document.getElementById('answerText');
const transcriptionOverlay = document.getElementById('transcriptionOverlay');
const transcriptionStatus = document.getElementById('transcriptionStatus');
const progressFill = document.getElementById('progressFill');
const progressText = document.getElementById('progressText');
const chatInput = document.getElementById('chatInput');
const sendBtn = document.getElementById('sendBtn');
const chatMessages = document.getElementById('chatMessages');
const newVideoBtn = document.getElementById('newVideoBtn');
const closeAnswer = document.getElementById('closeAnswer');
const thumbsUp = document.getElementById('thumbsUp');
const thumbsDown = document.getElementById('thumbsDown');
const feedbackButtons = document.getElementById('feedbackButtons');
const welcomeMessage = chatMessages.querySelector('.welcome-message').cloneNode(true);

// Toast notification
function showToast(message, type = 'default') {
    const toast = document.getElementById('toast');
    const toastMessage = document.getElementById('toastMessage');
    toastMessage.textContent = message;
    toast.className = 'toast show';
    if (type) toast.classList.add(type);

    setTimeout(() => {
        toast.className = 'toast';
    }, 3000);
}

// Upload handlers - using label[for] natively triggers file input
// No click handler needed for uploadBox since it's now a <label for="videoInput">

videoInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append('video', file);

    // Show upload section with progress
    uploadSection.hidden = true;
    videoSection.hidden = false;
    transcriptionOverlay.classList.add('active');
    transcriptionStatus.textContent = '영상을 업로드하고 있습니다...';
    progressFill.style.width = '0%';
    progressText.textContent = '0%';

    try {
        const response = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });

        const data = await response.json();
        if (response.ok) {
            currentVideoId = data.video_id;
            videoPlayer.src = data.video_url;
            videoPlayer.load();
            startStatusCheck();
        } else {
            showToast(data.error || '업로드 실패', 'error');
            resetToUpload();
        }
    } catch (error) {
        showToast('업로드 중 오류가 발생했습니다', 'error');
        console.error(error);
        resetToUpload();
    }
});

// YouTube URL handler
loadYoutube.addEventListener('click', async () => {
    const url = youtubeUrl.value.trim();
    if (!url) {
        showToast('YouTube URL을 입력해주세요', 'error');
        return;
    }

    // Show loading state immediately
    uploadSection.hidden = true;
    videoSection.hidden = false;
    transcriptionOverlay.classList.add('active');
    transcriptionStatus.textContent = '영상을 다운로드하고 있습니다...';
    progressFill.style.width = '0%';
    progressText.textContent = '0%';

    try {
        const response = await fetch('/api/youtube', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });

        const data = await response.json();
        if (response.ok) {
            currentVideoId = data.video_id;
            videoPlayer.src = data.video_url;
            videoPlayer.load();

            if (data.cached) {
                transcriptionOverlay.classList.remove('active');
                videoPlayer.setAttribute('controls', '');
                enableChat();
                showToast('캐시된 영상을 불러왔습니다', 'success');
            } else {
                startStatusCheck();
            }
        } else {
            // Handle specific error types
            if (data.error === 'duration_exceeded') {
                showToast(data.message || '5분 이하의 동영상만 불러올 수 있습니다.', 'error');
            } else {
                showToast(data.message || data.error || '불러오기 실패', 'error');
            }
            resetToUpload();
        }
    } catch (error) {
        showToast('영상을 불러오는 중 오류가 발생했습니다', 'error');
        console.error(error);
        resetToUpload();
    }
});

// Status check for transcription
function startStatusCheck() {
    if (statusCheckInterval) {
        clearInterval(statusCheckInterval);
    }

    statusCheckInterval = setInterval(async () => {
        try {
            const response = await fetch(`/api/status/${currentVideoId}`);
            const data = await response.json();

            if (response.status === 404) {
                clearInterval(statusCheckInterval);
                statusCheckInterval = null;
                showToast('작업을 찾을 수 없어요. 영상을 다시 올려 주세요.', 'error');
                resetToUpload();
                return;
            }

            updateTranscriptionProgress(data);

            if (data.status === 'completed') {
                clearInterval(statusCheckInterval);
                statusCheckInterval = null;
                transcriptionComplete();
            } else if (data.status === 'error') {
                clearInterval(statusCheckInterval);
                statusCheckInterval = null;
                showToast(`오류: ${data.error}`, 'error');
                resetToUpload();
            }
        } catch (error) {
            console.error('Status check error:', error);
        }
    }, 1000);
}

function updateTranscriptionProgress(data) {
    const progress = data.progress || 0;

    // More detailed status messages based on progress
    let statusMessage;
    if (data.status === 'downloading') {
        statusMessage = `영상을 다운로드하고 있습니다... (${progress}%)`;
    } else if (data.status === 'processing') {
        if (progress < 40) {
            statusMessage = '오디오를 추출하고 있습니다...';
        } else if (progress < 80) {
            statusMessage = '음성을 분석하고 있습니다...';
        } else {
            statusMessage = '분석 결과를 저장하고 있습니다...';
        }
    } else if (data.status === 'completed') {
        statusMessage = '분석이 완료되었습니다!';
    } else {
        statusMessage = '처리 중...';
    }

    transcriptionStatus.textContent = statusMessage;
    progressFill.style.width = `${progress}%`;
    progressText.textContent = `${progress}%`;
}

function transcriptionComplete() {
    isTranscribing = false;
    transcriptionOverlay.classList.remove('active');

    // Reload video to ensure it's playable
    const currentSrc = videoPlayer.src;
    videoPlayer.src = '';
    videoPlayer.load();
    setTimeout(() => {
        videoPlayer.src = currentSrc;
        videoPlayer.load();
        videoPlayer.setAttribute('controls', '');
    }, 100);

    enableChat();
    showToast('분석 완료! 이제 영상을 재생하고 질문할 수 있습니다', 'success');
}

function enableChat() {
    chatInput.disabled = false;
    sendBtn.disabled = false;
    chatInput.placeholder = '질문을 입력하세요...';
}

function disableChat() {
    chatInput.disabled = true;
    sendBtn.disabled = true;
    chatInput.placeholder = '분석 중입니다...';
}

// New video handler
newVideoBtn.addEventListener('click', () => {
    resetToUpload();
});

function resetToUpload() {
    // Clear state
    currentVideoId = null;
    currentAnswer = '';
    currentQuestion = '';
    isTranscribing = false;

    if (statusCheckInterval) {
        clearInterval(statusCheckInterval);
        statusCheckInterval = null;
    }

    // Reset UI
    videoSection.hidden = true;
    uploadSection.hidden = false;
    videoPlayer.src = '';
    videoPlayer.removeAttribute('controls');
    youtubeUrl.value = '';
    videoInput.value = '';
    chatMessages.replaceChildren(welcomeMessage.cloneNode(true));
    answerOverlay.classList.remove('active');
    transcriptionOverlay.classList.remove('active');
    disableChat();
}

// Chat handlers
chatInput.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendQuestion();
    }
});

sendBtn.addEventListener('click', sendQuestion);

async function sendQuestion() {
    const question = chatInput.value.trim();
    if (!question || !currentVideoId || isAsking) return;
    isAsking = true;
    sendBtn.disabled = true;

    // Pause video
    videoPlayer.pause();

    // Remember what was asked and when, for the feedback mail
    currentQuestion = question;
    questionTime = videoPlayer.currentTime;

    addChatMessage(question, 'user');
    chatInput.value = '';

    // Show answer overlay
    answerOverlay.classList.add('active');
    answerText.textContent = '';
    feedbackButtons.style.display = 'none';
    currentAnswer = '';

    try {
        const response = await fetch('/api/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                video_id: currentVideoId,
                question: question,
                current_time: questionTime
            })
        });

        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.message || data.error || `HTTP ${response.status}`);
        }

        await readEventStream(response, (data) => {
            if (data.content) {
                currentAnswer += data.content;
                answerText.textContent = currentAnswer;
            }
            if (data.error) {
                answerText.textContent = data.error;
            }
        });
    } catch (error) {
        answerText.textContent = '답변을 가져오는 중 오류가 발생했습니다.';
        console.error(error);
    } finally {
        feedbackButtons.style.display = 'flex';
        isAsking = false;
        sendBtn.disabled = false;
    }
}

// Server-sent events arrive in arbitrary network chunks, so buffer until a blank line
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
                .filter(line => line.startsWith('data: '))
                .map(line => line.slice(6))
                .join('\n');
            if (payload) {
                try {
                    onEvent(JSON.parse(payload));
                } catch (e) {
                    console.warn('Bad event', payload);
                }
            }
        }
        if (done) break;
    }
}

function addChatMessage(text, sender) {
    // Remove welcome message if exists
    const welcome = chatMessages.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const messageDiv = document.createElement('div');
    messageDiv.className = `chat-message ${sender}`;

    const label = document.createElement('div');
    label.className = 'message-label';
    label.textContent = sender === 'user' ? '나' : 'AI';

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.textContent = text;

    messageDiv.append(label, bubble);
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Close answer overlay
closeAnswer.addEventListener('click', () => {
    answerOverlay.classList.remove('active');
});

// Feedback handlers
thumbsUp.addEventListener('click', () => {
    addChatMessage(currentAnswer, 'assistant');
    answerOverlay.classList.remove('active');
    videoPlayer.play();
    showToast('감사합니다! 영상이 이어서 재생됩니다', 'success');
});

// Thumbs down - show feedback modal
thumbsDown.addEventListener('click', () => {
    showFeedbackModal();
});

function showFeedbackModal() {
    const modal = document.createElement('div');
    modal.className = 'feedback-modal';
    modal.innerHTML = `
        <div class="feedback-modal-content">
            <h3>어떤 점이 불만족스러웠나요?</h3>
            <textarea placeholder="답변의 어떤 부분이 부족했는지 알려주세요..."></textarea>
            <div class="feedback-modal-buttons">
                <button class="btn-cancel">취소</button>
                <button class="btn-submit">교수님께 문의</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    const textarea = modal.querySelector('textarea');
    modal.querySelector('.btn-cancel').addEventListener('click', () => modal.remove());
    modal.querySelector('.btn-submit').addEventListener('click', () => {
        const feedbackText = textarea.value.trim();
        modal.remove();
        submitFeedback(feedbackText);
    });

    setTimeout(() => textarea.focus(), 100);
}

async function submitFeedback(userFeedback) {
    let ok = false;
    let message = '교수님께 보내지 못했어요.';
    try {
        const response = await fetch('/api/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                video_id: currentVideoId,
                question: currentQuestion,
                answer: currentAnswer,
                current_time: questionTime,
                user_feedback: userFeedback
            })
        });
        const data = await response.json().catch(() => ({}));
        ok = response.ok;
        if (!ok && data.message) message = data.message;
    } catch (error) {
        console.error('Failed to send feedback:', error);
    }

    addChatMessage(currentAnswer, 'assistant');
    answerOverlay.classList.remove('active');

    if (ok) {
        showToast('교수님께 문의를 보냈어요', 'success');
        videoPlayer.play();
    } else {
        showToast(message, 'error');
    }
}

// Initialize
disableChat();
