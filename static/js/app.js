// State
let currentVideoId = null;
let currentAnswer = '';
let currentQuestion = '';
let isTranscribing = false;
let statusCheckInterval = null;
let lastEmailContent = null;
let currentUser = null;

// User Menu Element
const userMenu = document.getElementById('userMenu');

// Fetch current user and render user menu
async function initUserMenu() {
    try {
        const response = await fetch('/api/user', { credentials: 'include' });
        const data = await response.json();

        if (data.logged_in) {
            currentUser = data;
            renderLoggedInMenu(data);
        } else {
            currentUser = null;
            renderLoginButton();
        }
    } catch (error) {
        console.error('Failed to fetch user info:', error);
        renderLoginButton();
    }
}

function renderLoginButton() {
    userMenu.innerHTML = `
        <a href="/login" class="login-btn">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>
                <polyline points="10 17 15 12 10 7"/>
                <line x1="15" y1="12" x2="3" y2="12"/>
            </svg>
            로그인
        </a>
    `;
}

function renderLoggedInMenu(user) {
    const initial = user.name ? user.name.charAt(0).toUpperCase() : user.email.charAt(0).toUpperCase();
    const displayName = user.name || user.email.split('@')[0];

    const avatarHtml = user.profile_pic
        ? `<img src="${user.profile_pic}" alt="" class="user-avatar" referrerpolicy="no-referrer">`
        : `<div class="user-avatar-placeholder">${initial}</div>`;

    userMenu.innerHTML = `
        <div class="user-profile" id="userProfileBtn">
            ${avatarHtml}
            <span class="user-name">${displayName}</span>
            <svg class="dropdown-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="6 9 12 15 18 9"/>
            </svg>
        </div>
        <div class="user-dropdown" id="userDropdown">
            <div class="dropdown-item" style="pointer-events: none; opacity: 0.7;">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                    <circle cx="12" cy="7" r="4"/>
                </svg>
                ${user.email}
            </div>
            <div class="dropdown-divider"></div>
            <a href="/auth/logout" class="dropdown-item">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
                    <polyline points="16 17 21 12 16 7"/>
                    <line x1="21" y1="12" x2="9" y2="12"/>
                </svg>
                로그아웃
            </a>
        </div>
    `;

    // Toggle dropdown
    const profileBtn = document.getElementById('userProfileBtn');
    const dropdown = document.getElementById('userDropdown');

    profileBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        profileBtn.classList.toggle('active');
        dropdown.classList.toggle('active');
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', () => {
        profileBtn.classList.remove('active');
        dropdown.classList.remove('active');
    });
}

// Initialize user menu on page load
initUserMenu();

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

// Upload handlers
uploadBox.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    videoInput.click();
});

videoInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append('video', file);

    // Show upload section with progress
    uploadSection.style.display = 'none';
    videoSection.style.display = 'flex';
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
    uploadSection.style.display = 'none';
    videoSection.style.display = 'flex';
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
    lastEmailContent = null;

    if (statusCheckInterval) {
        clearInterval(statusCheckInterval);
        statusCheckInterval = null;
    }

    // Reset UI
    videoSection.style.display = 'none';
    uploadSection.style.display = 'block';
    videoPlayer.src = '';
    videoPlayer.removeAttribute('controls');
    youtubeUrl.value = '';
    videoInput.value = '';
    chatMessages.innerHTML = `
        <div class="welcome-message">
            <p>강의 내용에 대해 궁금한 점을 질문해주세요!</p>
            <p class="hint">질문하면 영상이 자동으로 일시정지됩니다.</p>
        </div>
    `;
    answerOverlay.classList.remove('active');
    transcriptionOverlay.classList.remove('active');
    disableChat();
}

// Chat handlers
chatInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendQuestion();
    }
});

sendBtn.addEventListener('click', sendQuestion);

async function sendQuestion() {
    const question = chatInput.value.trim();
    if (!question || !currentVideoId) return;

    // Pause video
    videoPlayer.pause();

    // Store question
    currentQuestion = question;

    // Add user message
    addChatMessage(question, 'user');
    chatInput.value = '';

    // Get current timestamp
    const currentTime = videoPlayer.currentTime;

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
                current_time: currentTime
            })
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const chunk = decoder.decode(value);
            const lines = chunk.split('\n');

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const data = JSON.parse(line.slice(6));
                        if (data.content) {
                            currentAnswer += data.content;
                            answerText.textContent = currentAnswer;
                        }
                        if (data.done) {
                            feedbackButtons.style.display = 'flex';
                        }
                        if (data.error) {
                            answerText.textContent = `오류: ${data.error}`;
                            feedbackButtons.style.display = 'flex';
                        }
                    } catch (e) {
                        // Ignore parse errors
                    }
                }
            }
        }
    } catch (error) {
        answerText.textContent = '답변을 가져오는 중 오류가 발생했습니다.';
        feedbackButtons.style.display = 'flex';
        console.error(error);
    }
}

function addChatMessage(text, sender) {
    // Remove welcome message if exists
    const welcome = chatMessages.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const messageDiv = document.createElement('div');
    messageDiv.className = `chat-message ${sender}`;

    const label = sender === 'user' ? '나' : 'AI';
    messageDiv.innerHTML = `
        <div class="message-label">${label}</div>
        <div class="message-bubble">${text}</div>
    `;

    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Close answer overlay
closeAnswer.addEventListener('click', () => {
    answerOverlay.classList.remove('active');
});

// Feedback handlers
thumbsUp.addEventListener('click', async () => {
    // Add AI response to chat
    addChatMessage(currentAnswer, 'assistant');

    // Close overlay and resume video
    answerOverlay.classList.remove('active');
    videoPlayer.play();

    showToast('감사합니다! 영상이 이어서 재생됩니다', 'success');
});

// Thumbs down - show feedback modal
thumbsDown.addEventListener('click', () => {
    showFeedbackModal();
});

function showFeedbackModal() {
    // Create modal
    const modal = document.createElement('div');
    modal.className = 'feedback-modal';
    modal.id = 'feedbackModal';
    modal.innerHTML = `
        <div class="feedback-modal-content">
            <h3>어떤 점이 불만족스러웠나요?</h3>
            <textarea id="feedbackText" placeholder="답변의 어떤 부분이 부족했는지 알려주세요..."></textarea>
            <div class="feedback-modal-buttons">
                <button class="btn-cancel" id="feedbackCancel">취소</button>
                <button class="btn-submit" id="feedbackSubmit">교수님께 문의</button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    // Event listeners
    document.getElementById('feedbackCancel').addEventListener('click', () => {
        modal.remove();
    });

    document.getElementById('feedbackSubmit').addEventListener('click', () => {
        const feedbackText = document.getElementById('feedbackText').value.trim();
        modal.remove();
        submitFeedback(feedbackText);
    });

    // Focus textarea
    setTimeout(() => {
        document.getElementById('feedbackText').focus();
    }, 100);
}

async function submitFeedback(userFeedback) {
    const currentTime = videoPlayer.currentTime;
    const minutes = Math.floor(currentTime / 60);
    const seconds = Math.floor(currentTime % 60);
    const timestamp = `${minutes}:${seconds.toString().padStart(2, '0')}`;

    // Get student info from currentUser
    const studentName = currentUser ? (currentUser.name || currentUser.email.split('@')[0]) : '익명 학생';
    const studentEmail = currentUser ? currentUser.email : null;

    // Build email content for preview
    lastEmailContent = {
        timestamp: timestamp,
        question: currentQuestion,
        answer: currentAnswer,
        feedback: userFeedback || '(피드백 없음)',
        studentName: studentName,
        studentEmail: studentEmail
    };

    // Send feedback to backend
    try {
        await fetch('/api/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({
                video_id: currentVideoId,
                question: currentQuestion,
                answer: currentAnswer,
                current_time: currentTime,
                feedback_type: 'negative',
                user_feedback: userFeedback
            })
        });
    } catch (error) {
        console.error('Failed to send feedback:', error);
    }

    // Add AI response to chat with email preview
    addChatMessage(currentAnswer, 'assistant');

    // Show email sent notification with preview option
    showEmailSentNotification();

    // Close overlay and resume video
    answerOverlay.classList.remove('active');
    videoPlayer.play();
}

function showEmailSentNotification() {
    // Remove existing notification if any
    const existing = document.getElementById('emailNotification');
    if (existing) existing.remove();

    const notification = document.createElement('div');
    notification.className = 'email-notification';
    notification.id = 'emailNotification';
    notification.innerHTML = `
        <div class="email-notification-content">
            <div class="email-notification-header">
                <span class="email-icon">📧</span>
                <span>교수님께 문의가 전송되었습니다</span>
            </div>
            <button class="email-preview-btn" id="showEmailPreview">전송된 내용 보기</button>
        </div>
    `;

    document.body.appendChild(notification);

    // Auto hide after 5 seconds
    setTimeout(() => {
        notification.classList.add('fade-out');
        setTimeout(() => notification.remove(), 300);
    }, 5000);

    // Preview button
    document.getElementById('showEmailPreview').addEventListener('click', (e) => {
        e.stopPropagation();
        showEmailPreviewModal();
    });
}

function showEmailPreviewModal() {
    const modal = document.createElement('div');
    modal.className = 'email-preview-modal';
    modal.id = 'emailPreviewModal';

    // Build subject with student name if logged in
    const subject = lastEmailContent.studentEmail
        ? `[강의 Q&A] ${lastEmailContent.studentName} 학생의 질문 - 답변 검토 요청`
        : '[강의 Q&A] 학생 질문에 대한 답변 검토 요청';

    // Build student info display
    const studentInfo = lastEmailContent.studentEmail
        ? `${lastEmailContent.studentName} (${lastEmailContent.studentEmail})`
        : '익명 학생';

    modal.innerHTML = `
        <div class="email-preview-content">
            <div class="email-preview-header">
                <h3>📧 전송된 문의 내용</h3>
                <button class="close-btn" id="closeEmailPreview">×</button>
            </div>
            <div class="email-preview-body">
                <div class="email-field">
                    <label>받는 사람</label>
                    <p>교수님</p>
                </div>
                <div class="email-field">
                    <label>보내는 사람</label>
                    <p>${studentInfo}</p>
                </div>
                <div class="email-field">
                    <label>제목</label>
                    <p>${subject}</p>
                </div>
                <div class="email-divider"></div>
                <div class="email-field">
                    <label>📍 질문 시점</label>
                    <p>${lastEmailContent.timestamp}</p>
                </div>
                <div class="email-field">
                    <label>❓ 학생 질문</label>
                    <p>${lastEmailContent.question}</p>
                </div>
                <div class="email-field">
                    <label>🤖 AI 답변</label>
                    <p>${lastEmailContent.answer}</p>
                </div>
                <div class="email-field">
                    <label>💬 학생 피드백</label>
                    <p>${lastEmailContent.feedback}</p>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    document.getElementById('closeEmailPreview').addEventListener('click', () => {
        modal.remove();
    });

    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            modal.remove();
        }
    });
}

// Initialize
disableChat();
