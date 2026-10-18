'use strict';

// ---- Session: generated once, kept in localStorage, sent with every request.
// (It only names the kitchen session; the server also checks that your signed-in cookie owns it.)
const SESSION_RE = /^[A-Za-z0-9_-]{8,64}$/;
const newId = () => (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
    : 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
let sessionId = newId();
try {
    const saved = localStorage.getItem('talksous.session');
    if (saved && SESSION_RE.test(saved)) sessionId = saved; else localStorage.setItem('talksous.session', sessionId);
} catch (e) { /* storage blocked: session lasts until reload */ }

const $ = (id) => document.getElementById(id);
const JSON_HEADERS = {'Content-Type': 'application/json'};
let busy = false;
let snapshot = {timers: [], at: Date.now()};

function rotateSession() {
    sessionId = newId();
    try { localStorage.setItem('talksous.session', sessionId); } catch (e) { /* ignore */ }
    $('tool-badge').classList.add('hidden');
    snapshot = {timers: [], at: Date.now()};
    $('step-card').classList.add('hidden');
    renderTimers();
}

// ---- Sign-in: a shared access code is exchanged for an HttpOnly cookie. The code is never stored in the page.
let loginVisible = false;
function showLogin(message) {
    loginVisible = true;
    $('login-overlay').classList.remove('hidden');
    $('login-error').innerText = message || '';
    $('login-code').focus();
}

$('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = $('login-code').value;
    if (!code) return;
    $('login-submit').disabled = true;
    try {
        const res = await fetch('/api/login', {method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({code})});
        if (res.ok) {
            $('login-code').value = '';
            $('login-error').innerText = '';
            $('login-overlay').classList.add('hidden');
            loginVisible = false;
            pollState();
        } else {
            let data = null;
            try { data = await res.json(); } catch (err) { /* non-JSON body */ }
            $('login-error').innerText = res.status === 429 ? 'Too many attempts. Wait a minute and try again.'
                : (data && typeof data.detail === 'string' ? data.detail : 'Could not sign in.');
        }
    } catch (err) {
        $('login-error').innerText = 'Could not reach the server.';
    } finally {
        $('login-submit').disabled = false;
    }
});

// ---- Clock ----
const tick = () => { $('clock').innerText = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}); };
tick(); setInterval(tick, 1000);

// ---- Speech output (queue=true lets timer alerts wait for a reply instead of cutting it off) ----
function speakText(text, queue = false) {
    if (!window.speechSynthesis) return;
    if (!queue) window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    window.speechSynthesis.speak(utterance);
}

function beep() {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        [0, 0.35, 0.7].forEach((t) => {
            const osc = ctx.createOscillator(), gain = ctx.createGain();
            osc.frequency.value = 880; osc.connect(gain); gain.connect(ctx.destination);
            gain.gain.setValueAtTime(0.15, ctx.currentTime + t);
            osc.start(ctx.currentTime + t); osc.stop(ctx.currentTime + t + 0.2);
        });
    } catch (e) { /* audio not available */ }
}

// ---- Web Speech API input ----
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition;
let isListening = false;

function showProblem(status, message) {
    $('status').innerText = status;
    $('chef-response').innerText = message;
}

if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = 'en-US';

    recognition.onstart = () => {
        isListening = true;
        $('status').innerText = 'Listening...';
        $('mic-label').innerText = 'Listening...';
    };
    recognition.onresult = async (event) => {
        const transcript = event.results[0][0].transcript;
        $('chef-response').innerText = `"${transcript}"`;
        await sendToSousChef(transcript);
    };
    recognition.onerror = (event) => {
        const messages = {
            'not-allowed': 'Microphone access is blocked. Allow it in your browser, or type below.',
            'service-not-allowed': 'Microphone access is blocked. Allow it in your browser, or type below.',
            'no-speech': "I didn't hear anything. Tap the mic and try again.",
            'audio-capture': 'I cannot find a microphone. You can type below instead.',
            'network': 'Speech recognition needs a network connection. You can type below instead.'
        };
        showProblem('Mic issue', messages[event.error] || `Voice input failed (${event.error}). You can type below instead.`);
    };
    recognition.onend = () => {
        isListening = false;
        $('mic-label').innerText = 'Tap to Speak';
        if (!busy && $('status').innerText === 'Listening...') $('status').innerText = 'Ready';
    };
}

function toggleListening() {
    if (!recognition) {
        showProblem('Voice unavailable', 'Voice input needs Chrome or Edge on localhost or HTTPS. You can type below instead.');
        return;
    }
    if (busy) return;
    try {
        if (isListening) recognition.stop(); else recognition.start();
    } catch (e) {
        showProblem('Mic issue', 'Could not start the microphone. Try again, or type below.');
    }
}
$('mic-btn').addEventListener('click', toggleListening);

// ---- Text fallback ----
$('text-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = $('text-input').value.trim();
    if (!text || busy) return;
    $('text-input').value = '';
    $('chef-response').innerText = `"${text}"`;
    await sendToSousChef(text);
});

// ---- Chat ----
function setBusy(value) {
    busy = value;
    $('mic-btn').disabled = value;
    $('send-btn').disabled = value;
}

async function sendToSousChef(userText, retried = false) {
    if (busy) return;
    setBusy(true);
    $('status').innerText = 'Processing...';
    $('tool-badge').classList.add('hidden');
    try {
        const res = await fetch('/api/chat', {
            method: 'POST',
            headers: JSON_HEADERS,
            body: JSON.stringify({message: userText, session_id: sessionId})
        });
        let data = null;
        try { data = await res.json(); } catch (e) { /* non-JSON error body */ }

        if (res.status === 401) {
            $('status').innerText = 'Locked';
            showLogin('Please enter the access code to continue.');
            return;
        }
        if (res.status === 404 && !retried) {
            // This browser's saved session belongs to an earlier sign-in; start a fresh one and retry once.
            rotateSession();
            setBusy(false);
            return sendToSousChef(userText, true);
        }
        if (!res.ok) {
            let message = 'Sorry, something went wrong. Please try again.';
            if (res.status === 429) message = (data && typeof data.detail === 'string') ? data.detail
                : 'That was a lot of requests. Give me a moment and try again.';
            else if (data && typeof data.detail === 'string') message = data.detail;
            showProblem('Error', message);
            return;
        }

        $('status').innerText = 'Alexa+ Speaking';
        $('chef-response').innerText = data.response;
        if (data.tools_called && data.tools_called.length) {
            $('tool-name').innerText = [...new Set(data.tools_called)].join(', ');
            $('tool-badge').classList.remove('hidden');
        }
        if (data.state) applyState(data.state);
        speakText(data.response);
    } catch (err) {
        showProblem('Error', 'Sorry, I had trouble connecting to the backend.');
    } finally {
        setBusy(false);
    }
}

// ---- Cooking panel: step + timers (deadlines are owned by the server) ----
function applyState(state) {
    const s = state.session;
    const card = $('step-card');
    if (s && s.recipe && s.status !== 'idle') {
        card.classList.remove('hidden');
        $('recipe-title').innerText = s.recipe.title + (s.recipe.ai_generated ? ' (AI-generated)' : '');
        if (s.status === 'completed') {
            $('step-count').innerText = 'Finished';
            $('step-text').innerText = 'All done. Enjoy your meal!';
        } else if (s.step) {
            $('step-count').innerText = `Step ${s.step.step_number} of ${s.step.total_steps}`;
            $('step-text').innerText = s.step.instruction;
        }
    } else {
        card.classList.add('hidden');
    }
    snapshot = {timers: state.timers || [], at: Date.now()};
    renderTimers();
}

const fmt = (sec) => `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;

function renderTimers() {
    const elapsed = Math.floor((Date.now() - snapshot.at) / 1000);
    const chips = snapshot.timers.map((t) => {
        const expired = t.status === 'expired';
        const chip = document.createElement('div');
        chip.className = 'flex items-center gap-3 rounded-full px-4 py-2 border ' +
            (expired ? 'bg-amber-500/20 border-amber-400 text-amber-200 animate-pulse'
                     : 'bg-slate-800 border-cyan-500/40 text-cyan-200');
        const label = document.createElement('span');
        label.textContent = t.label;
        const time = document.createElement('span');
        time.className = 'font-mono text-lg';
        time.textContent = expired ? 'Done' : fmt(Math.max(0, t.remaining_s - elapsed));
        chip.append(label, time);
        if (expired) {
            const btn = document.createElement('button');
            btn.className = 'text-xs underline';
            btn.textContent = 'Dismiss';
            btn.addEventListener('click', () => dismissTimer(t.id));
            chip.append(btn);
        }
        return chip;
    });
    $('timers').replaceChildren(...chips);
}
setInterval(renderTimers, 1000);

async function dismissTimer(id) {
    try {
        const res = await fetch('/api/timers/cancel', {
            method: 'POST',
            headers: JSON_HEADERS,
            body: JSON.stringify({session_id: sessionId, timer_id: id})
        });
        if (res.status === 401) showLogin('Please enter the access code to continue.');
    } catch (e) { /* the next poll will show the real state */ }
    pollState();
}

let alertTimeout;
function announceExpiry(timer) {
    const message = `Your ${timer.label} timer is done.`;
    const banner = $('timer-alert');
    banner.innerText = `⏰ ${message}`;
    banner.classList.remove('hidden');
    clearTimeout(alertTimeout);
    alertTimeout = setTimeout(() => banner.classList.add('hidden'), 20000);
    beep();
    speakText(message, true);
}

// Poll the server; it computes expiry from stored deadlines and reports each expiry once.
let polling = false;
async function pollState() {
    if (polling || loginVisible) return;
    polling = true;
    try {
        const res = await fetch('/api/state?session_id=' + encodeURIComponent(sessionId));
        if (res.status === 401) { showLogin('Please enter the access code.'); return; }
        if (res.status === 404) { rotateSession(); return; }  // saved session belongs to an earlier sign-in
        if (!res.ok) return;
        const data = await res.json();
        applyState(data);
        (data.newly_expired || []).forEach(announceExpiry);
    } catch (e) { /* server unreachable: keep the last known state */ }
    finally { polling = false; }
}
setInterval(pollState, 2000);
pollState();

$('new-session-btn').addEventListener('click', () => {
    if (!confirm('Start a new cooking session? Your current recipe and timers will be left behind.')) return;
    rotateSession();
    $('chef-response').innerText = '"New session. What are we cooking?"';
});
