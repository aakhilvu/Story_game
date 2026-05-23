const API = 'http://127.0.0.1:8000';
let sessionId = null;
let turn = 1;
let selectedCharacter = null;
let allCharacters = [];
let activeSSE = null;

// Scene history for ◄ ► navigation
// Each entry: { narrative: string, imageSrc: string|null, playerAction: string|null }
let sceneHistory = [];
let sceneIndex = -1;

// ── DEBUG LOGGER ─────────────────────────────────────────────────────────────
const DBG = {
    log: (...a) => console.log('%c[GAME]', 'color:#4caf50;font-weight:bold', ...a),
    warn: (...a) => console.warn('%c[GAME]', 'color:#ff9800;font-weight:bold', ...a),
    error: (...a) => console.error('%c[GAME]', 'color:#f44336;font-weight:bold', ...a),
    group: (lbl) => console.group('%c[GAME] ' + lbl, 'color:#2196f3;font-weight:bold'),
    end: () => console.groupEnd(),
};
// ─────────────────────────────────────────────────────────────────────────────

window.addEventListener('load', function () {
    DBG.log('Page loaded. sessionId =', sessionId);
    const pdfFile = document.getElementById('pdf-file');
    if (pdfFile) {
        pdfFile.addEventListener('change', function () {
            document.getElementById('file-name').textContent = this.files[0]?.name || 'Click to choose a file';
            document.getElementById('story-text').disabled = !!this.files[0];
        });
    }
    document.getElementById('custom-input')?.addEventListener('keydown', e => {
        if (e.key === 'Enter') sendCustomAction();
    });
    loadSaves(); // populate the saves list on the upload screen
    renderEndingsGallery();
});

function switchTab(tab) {
    document.querySelectorAll('.tab-content').forEach(t => t.style.display = 'none');
    document.querySelectorAll('.tab-btn').forEach(t => t.classList.remove('active'));
    document.getElementById(tab + '-tab').style.display = 'block';
    document.querySelector(`.tab-btn[data-tab="${tab}"]`).classList.add('active');
    if (tab === 'library') loadSaves();
    if (tab === 'endings') renderEndingsGallery();
}

function showScreen(id) {
    DBG.warn('showScreen() called → switching to:', id,
        ' | sessionId =', sessionId,
        ' | selectedCharacter =', selectedCharacter?.name ?? 'null');
    ['upload-screen', 'character-screen', 'game-screen'].forEach(s =>
        document.getElementById(s).style.display = 'none'
    );
    // upload-screen and character-screen use flexbox to center content vertically
    if (id === 'upload-screen' || id === 'character-screen') {
        document.getElementById(id).style.display = 'flex';
    } else {
        document.getElementById(id).style.display = 'block';
    }
    window.scrollTo(0, 0);
}

// ── Safe fetch — always returns parsed JSON or throws a clean error ──
async function apiFetch(url, options = {}) {
    DBG.log('apiFetch →', url);
    let response;
    try {
        response = await fetch(url, options);
    } catch (networkErr) {
        DBG.error('Network error on', url, networkErr);
        throw new Error(
            'Cannot reach the server. Make sure the backend is running on ' + API +
            '\n\nDetails: ' + networkErr.message
        );
    }

    DBG.log('apiFetch ←', url, '| HTTP', response.status);

    let data;
    try {
        data = await response.json();
    } catch (parseErr) {
        DBG.error('JSON parse failed on', url, '| HTTP', response.status, parseErr);
        throw new Error(
            'Server returned an unexpected response (HTTP ' + response.status + '). ' +
            'Check the backend console for errors.'
        );
    }

    DBG.log('apiFetch data from', url, '→', JSON.stringify(data).slice(0, 300));

    if (data && data.error) {
        DBG.error('Server returned error field:', data.error, '| from', url);
        throw new Error(data.error);
    }

    return data;
}

// ── UPLOAD ──
async function beginAdventure() {
    const text = document.getElementById('story-text').value.trim();
    const file = document.getElementById('pdf-file').files[0];
    if (!text && !file) { alert('Please enter a story or upload a PDF.'); return; }

    DBG.group('beginAdventure()');
    DBG.log('Input type:', file ? 'PDF: ' + file.name : 'Custom text (' + text.length + ' chars)');

    const btn = document.getElementById('begin-btn');
    btn.disabled = true;
    btn.textContent = 'Reading story...';

    try {
        const fd = new FormData();
        if (file) fd.append('file', file);
        else fd.append('custom_text', text);

        const data = await apiFetch(`${API}/upload-story`, { method: 'POST', body: fd });

        sessionId = data.session_id;
        allCharacters = data.characters || [];

        DBG.log('Upload success | session_id =', sessionId, '| characters count =', allCharacters.length);

        if (!allCharacters.length) {
            throw new Error('No characters found in your story. Try adding more character descriptions.');
        }

        populateCharacterGrid(allCharacters);
        showScreen('character-screen');
    } catch (err) {
        DBG.error('beginAdventure() FAILED:', err.message, err);
        alert('Upload Error:\n\n' + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Create Story';
        DBG.end();
    }
}

function populateCharacterGrid(characters) {
    const grid = document.getElementById('char-grid');
    grid.innerHTML = '';
    characters.forEach((char, i) => {
        const card = document.createElement('div');
        card.className = 'char-card';
        card.onclick = () => {
            document.querySelectorAll('.char-card').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            selectCharacter(i);
        };

        const circle = document.createElement('div');
        circle.className = 'char-card-circle';

        const initial = document.createElement('span');
        initial.className = 'char-card-initial';
        initial.textContent = (char.name || '?')[0].toUpperCase();
        circle.appendChild(initial);

        const img = document.createElement('img');
        img.className = 'char-card-portrait';
        const safeName = char.name.trim().replace(/[^\w\-]/g, '_');
        const portraitSrc = `${API}/portraits/${sessionId}/${safeName}.png`;
        img.alt = char.name;
        img.style.display = 'none';
        let portraitRetries = 0;
        const maxPortraitRetries = 10;
        img.onload = function () {
            initial.style.display = 'none';
            this.style.display = '';
        };
        img.onerror = function () {
            if (portraitRetries < maxPortraitRetries) {
                portraitRetries++;
                const self = this;
                setTimeout(() => {
                    self.src = portraitSrc + '?t=' + Date.now();
                }, 3000);
            }
        };
        img.src = portraitSrc;
        circle.appendChild(img);

        const name = document.createElement('span');
        name.className = 'char-card-name';
        name.textContent = char.name;

        card.appendChild(circle);
        card.appendChild(name);
        grid.appendChild(card);
    });
    selectedCharacter = null;
    document.getElementById('char-select-area').style.display = 'flex';
    document.getElementById('char-selected-area').style.display = 'none';
    document.getElementById('char-info-section').style.display = 'none';
    document.getElementById('confirm-char-btn').style.display = 'none';
}

function openCharModal() {
    document.getElementById('char-modal').style.display = 'flex';
    document.getElementById('char-modal-overlay').style.display = 'block';
}

function closeCharModal() {
    document.getElementById('char-modal').style.display = 'none';
    document.getElementById('char-modal-overlay').style.display = 'none';
}

function selectCharacter(index) {
    const char = allCharacters[parseInt(index)];
    selectedCharacter = char;

    document.getElementById('char-select-area').style.display = 'none';
    document.getElementById('char-selected-area').style.display = 'flex';
    document.getElementById('char-selected-initials').textContent = (char.name || '?')[0].toUpperCase();
    document.getElementById('char-selected-name').textContent = char.name;

    document.getElementById('info-description').textContent = char.description || '-';
    document.getElementById('info-skills').textContent = (char.skills || []).join(', ') || '-';
    document.getElementById('info-weaknesses').textContent = (char.weaknesses || []).join(', ') || '-';
    document.getElementById('info-side').textContent = char.side || '-';
    document.getElementById('char-info-section').style.display = 'block';

    document.getElementById('confirm-char-btn').style.display = 'block';
    document.getElementById('confirm-char-btn').textContent = `PLAY AS ${char.name.toUpperCase()}`;

    closeCharModal();
}

async function confirmCharacter() {
    DBG.group('confirmCharacter()');
    DBG.log('sessionId =', sessionId, '| selectedCharacter =', selectedCharacter);

    if (!selectedCharacter) {
        DBG.error('ABORT: no selectedCharacter');
        alert('Please select a character.');
        DBG.end();
        return;
    }
    if (!sessionId) {
        DBG.error('ABORT: sessionId is null/empty — session was lost');
        alert('Session lost. Please go back and re-upload your story.');
        DBG.end();
        return;
    }

    const btn = document.getElementById('confirm-char-btn');
    btn.disabled = true;
    btn.textContent = 'Starting...';
    showCharacterLoading(true);

    try {
        DBG.log('STEP 1: POST /select-character');
        const fd = new FormData();
        fd.append('session_id', sessionId);
        fd.append('character', JSON.stringify(selectedCharacter));
        fd.append('difficulty', window._pendingDifficulty || 'easy');
        const selResp = await apiFetch(`${API}/select-character`, { method: 'POST', body: fd });
        DBG.log('STEP 1 response:', selResp);

        DBG.log('STEP 2: POST /start-game');
        const fd2 = new FormData();
        fd2.append('session_id', sessionId);
        const startData = await apiFetch(`${API}/start-game`, { method: 'POST', body: fd2 });
        DBG.log('STEP 2 response:', startData);

        if (!startData.narrative && (!startData.choices || !startData.choices.length)) {
            DBG.error('STEP 2 returned empty narrative AND empty choices — aborting');
            throw new Error('The game started but returned no content. Try again.');
        }

        DBG.log('STEP 3: Switching to game-screen');
        document.querySelector('.tb-title').textContent = selectedCharacter.name;
        document.getElementById('scene-description').textContent = '';
        document.getElementById('turn-badge').textContent = 'Turn 1';
        document.getElementById('playing-as').textContent = selectedCharacter.name;
        sceneHistory = [];
        sceneIndex = -1;
        turn = 1;

        showScreen('game-screen');

        DBG.log('STEP 4: Rendering narrative and choices');
        renderTextAndChoices(startData.narrative, startData.choices);

        DBG.log('STEP 5: Starting SSE panel stream for session', sessionId);
        startPanelStream(sessionId);

        DBG.log('confirmCharacter() completed successfully ✓');

    } catch (err) {
        DBG.error('confirmCharacter() CAUGHT ERROR:', err.message, err);
        showCharacterLoading(false);
        btn.disabled = false;
        btn.textContent = `PLAY AS ${selectedCharacter.name.toUpperCase()}`;
        showToast('Could not start game: ' + err.message, 'error');
    } finally {
        DBG.end();
    }
}

function showCharacterLoading(visible) {
    let el = document.getElementById('char-loading-msg');
    if (!el) {
        el = document.createElement('p');
        el.id = 'char-loading-msg';
        el.style.cssText = 'text-align:center;color:#555;font-size:0.88rem;margin-top:1rem;';
        document.querySelector('#character-screen .container').appendChild(el);
    }
    if (visible) {
        el.textContent = 'Generating your story opening... this may take a few seconds.';
        el.style.display = 'block';
    } else {
        el.style.display = 'none';
    }
}

// FIX: goBack now calls DELETE /end-game so the session is properly cleaned up
// and the portrait background pipeline is aborted. Previously sessionId was
// set to null without telling the server, leaving portraits generating for
// an abandoned session and leaking memory.
async function goBack() {
    DBG.warn('goBack() called — cleaning up session and returning to upload screen');
    if (activeSSE) { activeSSE.close(); activeSSE = null; }
    showCharacterLoading(false);

    if (sessionId) {
        DBG.log('Deleting session:', sessionId);
        try {
            await fetch(`${API}/end-game/${sessionId}`, { method: 'DELETE' });
        } catch (e) {
            DBG.error('end-game DELETE failed (goBack):', e);
        }
    }

    sessionId = null;
    selectedCharacter = null;
    allCharacters = [];
    document.getElementById('story-text').value = '';
    document.getElementById('story-text').disabled = false;
    document.getElementById('pdf-file').value = '';
    document.getElementById('file-name').textContent = 'Click to choose a file';

    // Reset character UI and difficulty
    document.getElementById('char-select-area').style.display = 'flex';
    document.getElementById('char-selected-area').style.display = 'none';
    document.getElementById('char-info-section').style.display = 'none';
    document.getElementById('confirm-char-btn').style.display = 'none';
    window._pendingDifficulty = 'easy';
    document.querySelectorAll('.diff-btn').forEach(b => b.classList.toggle('active', b.dataset.diff === 'easy'));

    // Reset feature state
    window._journalEntries = [];
    window._currentRelationships = {};
    if (typeof updateInventoryUI === 'function') updateInventoryUI([]);
    if (typeof updateAlignmentUI === 'function') updateAlignmentUI(0);
    if (typeof renderJournal === 'function') renderJournal([]);

    showScreen('upload-screen');
}

async function newGame() {
    DBG.warn('newGame() called — ending session');
    if (activeSSE) { activeSSE.close(); activeSSE = null; }
    if (sessionId) {
        DBG.log('Deleting session:', sessionId);
        try { await fetch(`${API}/end-game/${sessionId}`, { method: 'DELETE' }); } catch (e) {
            DBG.error('end-game DELETE failed:', e);
        }
    }
    turn = 1;
    sceneHistory = [];
    sceneIndex = -1;
    document.getElementById('turn-badge').textContent = 'Turn 1';
    sessionId = null;
    selectedCharacter = null;
    allCharacters = [];
    document.getElementById('story-text').value = '';
    document.getElementById('story-text').disabled = false;
    document.getElementById('pdf-file').value = '';
    document.getElementById('file-name').textContent = 'Click to choose a file';

    // Reset character UI and difficulty
    document.getElementById('char-select-area').style.display = 'flex';
    document.getElementById('char-selected-area').style.display = 'none';
    document.getElementById('char-info-section').style.display = 'none';
    document.getElementById('confirm-char-btn').style.display = 'none';
    window._pendingDifficulty = 'easy';
    document.querySelectorAll('.diff-btn').forEach(b => b.classList.toggle('active', b.dataset.diff === 'easy'));

    // Reset feature state
    window._journalEntries = [];
    window._currentRelationships = {};
    if (typeof updateInventoryUI === 'function') updateInventoryUI([]);
    if (typeof updateAlignmentUI === 'function') updateAlignmentUI(0);
    if (typeof renderJournal === 'function') renderJournal([]);

    showScreen('upload-screen');
}

// ── TAKE ACTION ──
async function takeAction(playerInput) {
    if (activeSSE) { activeSSE.close(); activeSSE = null; }

    // FIX: Sanitize and cap player input to prevent prompt injection and oversized payloads
    playerInput = playerInput.trim().slice(0, 200);
    if (!playerInput) return;

    hideChoices();

    const desc = document.getElementById('scene-description');
    desc.innerHTML = '';
    const pill = document.createElement('span');
    pill.className = 'player-action-pill';
    // FIX: Use textContent not innerHTML to prevent XSS from player input
    pill.textContent = '▶ ' + playerInput;
    const loading = document.createElement('em');
    loading.style.cssText = 'color:#aaa;font-size:0.85rem';
    loading.textContent = 'Loading next scene...';
    desc.appendChild(pill);
    desc.appendChild(document.createElement('br'));
    desc.appendChild(loading);

    try {
        const fd = new FormData();
        fd.append('session_id', sessionId);
        fd.append('player_input', playerInput);
        const data = await apiFetch(`${API}/take-action`, { method: 'POST', body: fd });

        turn++;
        document.getElementById('turn-badge').textContent = `Turn ${turn}`;

        renderTextAndChoices(data.narrative, data.choices, playerInput);
        startPanelStream(sessionId);

        // New feature updates
        if (data.journal_entry)                 appendJournalEntry({ turn: turn, text: data.journal_entry });
        if (Array.isArray(data.inventory))      updateInventoryUI(data.inventory);
        if (typeof data.alignment === 'number') updateAlignmentUI(data.alignment);
        if (data.relationships)                 updateRelationshipMap(data.relationships);
        if (data.ending)                        handleEnding(data.ending);

    } catch (err) {
        showToast('Action failed: ' + err.message, 'error');
        showChoices();
    }
}


function sendCustomAction() {
    const input = document.getElementById('custom-input');
    const val = input.value.trim();
    if (!val) return;
    input.value = '';
    takeAction(val);
}

// ── SSE PANEL STREAMING ──
let _imageTimeoutId = null;

function _clearImageTimeout() {
    if (_imageTimeoutId) { clearTimeout(_imageTimeoutId); _imageTimeoutId = null; }
}

function _showFrameMessage(icon, line1, line2 = '', retryFn = null) {
    const frame = document.getElementById('scene-frame');
    document.getElementById('scene-skeleton').style.display = 'none';
    document.getElementById('generating-label').style.display = 'none';
    frame.querySelectorAll('.frame-msg').forEach(el => el.remove());

    const box = document.createElement('div');
    box.className = 'frame-msg';

    const ico = document.createElement('div');
    ico.className = 'frame-msg-icon';
    ico.textContent = icon;

    const t1 = document.createElement('div');
    t1.className = 'frame-msg-title';
    t1.textContent = line1;

    box.appendChild(ico);
    box.appendChild(t1);

    if (line2) {
        const t2 = document.createElement('div');
        t2.className = 'frame-msg-sub';
        t2.textContent = line2;
        box.appendChild(t2);
    }

    if (retryFn) {
        const btn = document.createElement('button');
        btn.className = 'frame-msg-retry';
        btn.textContent = 'Retry Image';
        btn.onclick = retryFn;
        box.appendChild(btn);
    }

    frame.appendChild(box);
}

function startPanelStream(sid) {
    const frame = document.getElementById('scene-frame');
    const skeleton = document.getElementById('scene-skeleton');
    const label = document.getElementById('generating-label');

    _clearImageTimeout();
    frame.querySelectorAll('img.scene-img, .frame-msg').forEach(el => el.remove());
    skeleton.style.display = 'block';
    label.style.display = 'block';
    label.textContent = 'GENERATING SCENE...';

    if (activeSSE) activeSSE.close();
    activeSSE = new EventSource(`${API}/stream-panels/${sid}`);

    // FIX: Capture sceneIndex at stream START as a local constant.
    const targetSceneIndex = sceneIndex;

    // ── 30-second timeout: show in-frame warning ──
    _imageTimeoutId = setTimeout(() => {
        if (activeSSE) {
            _showFrameMessage(
                '⏳',
                'Image is taking longer than usual',
                'The scene will appear when ready — or click Retry.',
                () => { if (activeSSE) { activeSSE.close(); activeSSE = null; } startPanelStream(sid); }
            );
        }
    }, 60000);

    activeSSE.onmessage = function (e) {
        let event;
        try { event = JSON.parse(e.data); } catch { return; }

        if (event.type === 'grid') {
            _clearImageTimeout();
            skeleton.style.display = 'none';
            label.style.display = 'none';
            frame.querySelectorAll('.frame-msg').forEach(el => el.remove());

            // Store image in the correct history slot
            if (targetSceneIndex >= 0 && targetSceneIndex < sceneHistory.length) {
                sceneHistory[targetSceneIndex].imageSrc = event.data.startsWith('data:')
                    ? event.data
                    : 'data:image/png;base64,' + event.data;
            }

            // Only render if user is still on this scene
            if (sceneIndex === targetSceneIndex) {
                frame.querySelectorAll('img.scene-img').forEach(el => el.remove());
                const img = document.createElement('img');
                img.src = event.data.startsWith('data:') ? event.data : 'data:image/png;base64,' + event.data;
                img.className = 'scene-img';
                img.alt = 'Scene';
                frame.appendChild(img);
            }
            updateNavArrows();
        }

        if (event.type === 'grid_skipped') {
            _clearImageTimeout();
            skeleton.style.display = 'none';
            label.style.display = 'none';
        }

        if (event.type === 'image_error') {
            _clearImageTimeout();
            _showFrameMessage(
                '⚠',
                'Image generation failed',
                event.message || 'The image service is unavailable.',
                () => startPanelStream(sid)
            );
        }

        if (event.type === 'done' || event.type === 'error') {
            _clearImageTimeout();
            activeSSE.close();
            activeSSE = null;
        }
    };

    activeSSE.onerror = function () {
        _clearImageTimeout();
        _showFrameMessage(
            '⚠',
            'Connection lost',
            'Could not reach the image server.',
            () => startPanelStream(sid)
        );
        activeSSE.close();
        activeSSE = null;
    };
}


// ── SCENE HISTORY & NAVIGATION ──
function prevScene() {
    if (sceneIndex > 0) {
        sceneIndex--;
        displaySceneAt(sceneIndex);
    }
}

function nextScene() {
    if (sceneIndex < sceneHistory.length - 1) {
        sceneIndex++;
        displaySceneAt(sceneIndex);
    }
}

function displaySceneAt(idx) {
    const entry = sceneHistory[idx];
    if (!entry) return;

    const desc = document.getElementById('scene-description');
    desc.innerHTML = '';

    if (entry.playerAction) {
        const pill = document.createElement('span');
        pill.className = 'player-action-pill';
        // FIX: textContent prevents XSS from stored player actions
        pill.textContent = '► ' + entry.playerAction;
        desc.appendChild(pill);
    }

    const p = document.createElement('p');
    p.className = 'narrative-text';
    // FIX: textContent prevents XSS from LLM narrative output
    p.textContent = entry.narrative;
    desc.appendChild(p);

    const frame = document.getElementById('scene-frame');
    const skeleton = document.getElementById('scene-skeleton');
    const label = document.getElementById('generating-label');
    frame.querySelectorAll('img.scene-img').forEach(el => el.remove());

    if (entry.imageSrc) {
        skeleton.style.display = 'none';
        label.style.display = 'none';
        const img = document.createElement('img');
        img.src = entry.imageSrc;
        img.className = 'scene-img';
        img.alt = 'Scene';
        frame.appendChild(img);
    } else {
        skeleton.style.display = 'block';
        label.style.display = 'block';
    }

    if (idx === sceneHistory.length - 1) {
        showChoices();
    } else {
        hideChoices();
    }

    updateNavArrows();
}

function updateNavArrows() {
    const btnPrev = document.getElementById('btn-prev');
    const btnNext = document.getElementById('btn-next');
    if (btnPrev) btnPrev.disabled = sceneIndex <= 0;
    if (btnNext) btnNext.disabled = sceneIndex >= sceneHistory.length - 1;
}

// ── RENDER TEXT + CHOICES ──
function renderTextAndChoices(narrative, choices, playerAction = null) {
    sceneHistory.push({ narrative: narrative || '', imageSrc: null, playerAction });
    sceneIndex = sceneHistory.length - 1;

    const desc = document.getElementById('scene-description');
    desc.innerHTML = '';

    if (playerAction) {
        const pill = document.createElement('span');
        pill.className = 'player-action-pill';
        pill.textContent = '► ' + playerAction;
        desc.appendChild(pill);
    }

    const p = document.createElement('p');
    p.className = 'narrative-text';
    p.textContent = narrative || '';
    desc.appendChild(p);

    renderChoices(choices || []);
    updateNavArrows();
}

function renderChoices(choices) {
    const grid = document.getElementById('choices-grid');
    grid.innerHTML = '';
    if (!choices.length) {
        const msg = document.createElement('p');
        msg.style.cssText = 'color:#444;font-size:0.85rem;grid-column:1/-1';
        msg.textContent = 'Type your own action below.';
        grid.appendChild(msg);
    } else {
        choices.forEach((choice, i) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'choice-btn';
            btn.textContent = choice;
            btn.onclick = () => takeAction(choice);
            grid.appendChild(btn);
        });
    }
    showChoices();
}

function hideChoices() { document.getElementById('choices-panel').style.display = 'none'; }
function showChoices() { document.getElementById('choices-panel').style.display = 'flex'; }

// ── TOAST NOTIFICATION ──
// FIX: Rebuilt to use DOM methods instead of innerHTML — prevents XSS from
// error message strings that could contain HTML (e.g. from malformed API responses)
function showToast(message, type = 'error') {
    const existing = document.getElementById('toast-notification');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.id = 'toast-notification';
    toast.className = 'toast toast-' + type;

    const icon = document.createElement('span');
    icon.className = 'toast-icon';
    icon.textContent = type === 'error' ? '⚠' : '✓';

    const msg = document.createElement('span');
    msg.className = 'toast-msg';
    // FIX: textContent not innerHTML — safe against XSS
    msg.textContent = message;

    const closeBtn = document.createElement('button');
    closeBtn.className = 'toast-close';
    closeBtn.textContent = '✕';
    closeBtn.onclick = () => toast.remove();

    toast.appendChild(icon);
    toast.appendChild(msg);
    toast.appendChild(closeBtn);

    document.body.appendChild(toast);
    setTimeout(() => { if (toast.parentElement) toast.remove(); }, 8000);
}


// ── SAVE / LOAD ──────────────────────────────────────────────────────────────

async function saveGame() {
    const btn = document.getElementById('tb-save-btn');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
        const fd = new FormData();
        fd.append('session_id', sessionId);
        const data = await apiFetch(`${API}/save-game`, { method: 'POST', body: fd });
        showToast(data.success ? '\uD83D\uDCBE Game saved!' : 'Save failed', data.success ? 'info' : 'error');
    } catch (err) {
        showToast('Save failed: ' + err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '\uD83D\uDCBE Save';
    }
}

async function loadSaves() {
    try {
        const data = await apiFetch(`${API}/list-saves`);
        const saves = data.saves || [];
        const container = document.getElementById('library-saves-list');
        if (!container) return;
        container.innerHTML = '';
        if (!saves.length) {
            container.innerHTML = '<p style="color:#444;font-size:0.85rem;text-align:center;">No saved games yet.</p>';
            return;
        }
        saves.forEach(save => {
            const card = document.createElement('div');
            card.className = 'save-card';
            const date = save.saved_at ? new Date(save.saved_at).toLocaleDateString() : '';
            const nameEl = document.createElement('strong');
            nameEl.textContent = save.character_name;
            const metaEl = document.createElement('span');
            metaEl.className = 'save-meta';
            metaEl.textContent = `Turn ${save.turn_count} \u00B7 ${save.difficulty} \u00B7 ${date}`;
            const loadBtn = document.createElement('button');
            loadBtn.type = 'button';
            loadBtn.className = 'save-load-btn';
            loadBtn.textContent = 'Load';
            loadBtn.onclick = () => loadGame(save.session_id);
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'save-delete-btn';
            delBtn.textContent = '\u2715';
            delBtn.onclick = async (e) => {
                e.stopPropagation();
                await apiFetch(`${API}/delete-save/${save.session_id}`, { method: 'DELETE' });
                card.remove();
            };
            card.appendChild(nameEl);
            card.appendChild(metaEl);
            card.appendChild(loadBtn);
            card.appendChild(delBtn);
            container.appendChild(card);
        });
    } catch (err) {
        DBG.error('loadSaves failed:', err);
    }
}

async function loadGame(saveSessionId) {
    try {
        const fd = new FormData();
        fd.append('session_id', saveSessionId);
        const data = await apiFetch(`${API}/load-game`, { method: 'POST', body: fd });
        if (data.error) { showToast(data.error, 'error'); return; }

        sessionId = saveSessionId;
        selectedCharacter = data.character;
        allCharacters = data.characters || [];
        turn = data.turn_count + 1;

        // Restore UI state
        document.querySelector('.tb-title').textContent = selectedCharacter.name || '';
        document.getElementById('playing-as').textContent = selectedCharacter.name || '';
        document.getElementById('turn-badge').textContent = `Turn ${turn}`;
        sceneHistory = [];
        sceneIndex = -1;

        // Restore feature UI (stubs — will be wired in later steps)
        if (typeof updateInventoryUI   === 'function') updateInventoryUI(data.inventory || []);
        if (typeof updateAlignmentUI   === 'function') updateAlignmentUI(data.alignment || 0);
        if (typeof updateRelationshipMap === 'function') updateRelationshipMap(data.relationships || {});
        if (typeof renderJournal       === 'function') renderJournal(data.journal || []);

        showScreen('game-screen');

        // Show the last scene from the save — no LLM call needed
        renderTextAndChoices(data.narrative, data.choices);
        if (data.narrative) startPanelStream(sessionId);

        showToast('Game loaded!', 'info');
    } catch (err) {
        showToast('Load failed: ' + err.message, 'error');
    }
}

function setDifficulty(value, btn) {
    window._pendingDifficulty = value;
    document.querySelectorAll('.diff-btn').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
}


// ── JOURNAL ──────────────────────────────────────────────────────────────────

function toggleJournal() {
    const drawer  = document.getElementById('journal-drawer');
    const overlay = document.getElementById('journal-overlay');
    const open    = drawer.style.display !== 'none';
    drawer.style.display  = open ? 'none' : 'block';
    overlay.style.display = open ? 'none' : 'block';
}

function renderJournal(entries) {
    const container = document.getElementById('journal-entries');
    if (!container) return;
    container.innerHTML = '';
    if (!entries.length) {
        const empty = document.createElement('p');
        empty.style.cssText = 'color:#444;font-size:0.85rem;text-align:center;padding:1rem;';
        empty.textContent = 'No entries yet. Play some turns!';
        container.appendChild(empty);
        return;
    }
    [...entries].reverse().forEach(entry => {
        const row = document.createElement('div');
        row.className = 'journal-entry';
        const badge = document.createElement('span');
        badge.className = 'journal-turn-badge';
        badge.textContent = `T${entry.turn}`;
        const text = document.createElement('span');
        text.className = 'journal-text';
        text.textContent = entry.text;  // textContent prevents XSS
        row.appendChild(badge);
        row.appendChild(text);
        container.appendChild(row);
    });
}

function appendJournalEntry(entry) {
    if (!entry || !entry.text) return;
    const session = window._journalEntries = window._journalEntries || [];
    session.push(entry);
    renderJournal(session);
}


// ── INVENTORY ────────────────────────────────────────────────────────────────

function toggleInventory() {
    const drawer  = document.getElementById('inventory-drawer');
    const overlay = document.getElementById('inventory-overlay');
    const open    = drawer.style.display !== 'none';
    drawer.style.display  = open ? 'none' : 'block';
    overlay.style.display = open ? 'none' : 'block';
    if (!open) renderInventoryDrawer();
}

const INVENTORY_ICONS = {
    potion: '🧪', elixir: '🧪', vial: '🧪', poison: '☠️',
    wand: '🪄', staff: '🪄', rod: '🪄',
    book: '📖', tome: '📕', scroll: '📜', letter: '✉️',
    sword: '⚔️', dagger: '🗡️', blade: '🗡️', axe: '🪓', weapon: '⚔️',
    shield: '🛡️', armor: '🛡️', robe: '👘', cloak: '🧥',
    ring: '💍', amulet: '📿', necklace: '📿', jewel: '💎', gem: '💎',
    key: '🔑', map: '🗺️', compass: '🧭',
    coin: '🪙', gold: '🪙', money: '💰', treasure: '💰',
    flower: '🌺', herb: '🌿', plant: '🌱', seed: '🌰',
    food: '🍞', bread: '🍞', fruit: '🍎', drink: '🥤',
    feather: '🪶', quill: '🪶', ink: '🖋️',
    candle: '🕯️', lantern: '🏮', torch: '🔥',
    crystal: '🔮', orb: '🔮', glass: '🧊',
    bone: '🦴', skull: '💀', tooth: '🦷',
    pot: '🍲', cauldron: '🍲', cup: '🥃',
    star: '⭐', moon: '🌙', sun: '☀️',
};

function getItemIcon(item) {
    const lower = item.toLowerCase();
    for (const [keyword, icon] of Object.entries(INVENTORY_ICONS)) {
        if (lower.includes(keyword)) return icon;
    }
    return '📦';
}

function getItemRarity(item) {
    const lower = item.toLowerCase();
    const rare = ['gem', 'jewel', 'ring', 'crystal', 'orb', 'wand', 'elixir', 'crown', 'amulet', 'necklace'];
    const uncommon = ['sword', 'dagger', 'blade', 'shield', 'armor', 'scroll', 'tome', 'key', 'map', 'compass', 'lantern', 'torch', 'cauldron'];
    for (const kw of rare) { if (lower.includes(kw)) return 'rare'; }
    for (const kw of uncommon) { if (lower.includes(kw)) return 'uncommon'; }
    return 'common';
}

const RARITY_STYLES = {
    rare: { border: '#b8860b', bg: '#fffbe6', iconBg: '#fff7cc', color: '#8a6d00' },
    uncommon: { border: '#4a90d9', bg: '#f0f8ff', iconBg: '#dceaff', color: '#1a5a9a' },
    common: { border: '#eaeaea', bg: '#f8f8fa', iconBg: '#f0f5ff', color: '#1677ff' },
};

function renderInventoryDrawer() {
    const container = document.getElementById('inventory-items-list');
    if (!container) return;
    const session = window._inventoryItems || [];
    container.innerHTML = '';
    if (!session.length) {
        const empty = document.createElement('p');
        empty.style.cssText = 'color:#8c8c8c;font-size:0.85rem;text-align:center;padding:2rem;';
        empty.textContent = 'No items collected yet.';
        container.appendChild(empty);
        return;
    }
    session.forEach(item => {
        const raw = typeof item === 'string' ? item : (item.name || item.item || '');
        const qty = typeof item === 'object' && item.quantity ? item.quantity : 1;
        const rarity = getItemRarity(raw);
        const style = RARITY_STYLES[rarity];
        const row = document.createElement('div');
        row.className = 'inventory-drawer-item';
        row.style.cssText = `border-color:${style.border};background:${style.bg};`;
        row.setAttribute('data-rarity', rarity);
        const icon = document.createElement('span');
        icon.className = 'inv-item-icon';
        icon.style.background = style.iconBg;
        icon.style.color = style.color;
        icon.textContent = getItemIcon(raw);
        const name = document.createElement('span');
        name.textContent = raw;
        name.style.flex = '1';
        row.appendChild(icon);
        row.appendChild(name);
        if (qty > 1) {
            const badge = document.createElement('span');
            badge.className = 'inv-item-qty';
            badge.textContent = `×${qty}`;
            row.appendChild(badge);
        }
        const rarityTag = document.createElement('span');
        rarityTag.className = 'inv-item-rarity';
        rarityTag.textContent = rarity;
        rarityTag.style.color = style.color;
        row.appendChild(rarityTag);
        container.appendChild(row);
    });
}

function updateInventoryUI(items) {
    window._inventoryItems = items || [];
}


// ── ALIGNMENT ────────────────────────────────────────────────────────────────

function updateAlignmentUI(value) {
    // value is -100 to +100
    const indicator = document.getElementById('alignment-indicator');
    const fillEl    = document.getElementById('alignment-fill');
    const labelEl   = document.getElementById('align-label-text');
    if (!indicator) return;

    // Convert to 0-100% position
    const pct = (value + 100) / 2;  // -100→0%, 0→50%, +100→100%
    indicator.style.left = pct + '%';

    // Color the fill
    if (value > 0) {
        fillEl.style.background = `linear-gradient(90deg, #2a2a3a 50%, #2d6a3f ${pct}%)`;
    } else {
        fillEl.style.background = `linear-gradient(90deg, #6a1a1a ${pct}%, #2a2a3a 50%)`;
    }

    // Label
    let label = 'Neutral';
    if (value >= 80)       label = 'Champion';
    else if (value >= 40)  label = 'Hero';
    else if (value >= 10)  label = 'Good';
    else if (value <= -80) label = 'Dark Lord';
    else if (value <= -40) label = 'Villain';
    else if (value <= -10) label = 'Ruthless';
    labelEl.textContent = label;
}


// ── RELATIONSHIP MAP ─────────────────────────────────────────────────────────

let _currentRelationships = {};

function toggleRelMap() {
    const modal   = document.getElementById('rel-modal');
    const overlay = document.getElementById('rel-overlay');
    const open    = modal.style.display !== 'none';
    modal.style.display   = open ? 'none' : 'flex';
    overlay.style.display = open ? 'none' : 'block';
    if (!open) renderRelMap(_currentRelationships);
}

function updateRelationshipMap(relationships) {
    _currentRelationships = relationships;
}

function renderRelMap(relationships) {
    const container = document.getElementById('rel-map-container');
    if (!container) return;
    container.innerHTML = '';

    const playerName = selectedCharacter ? selectedCharacter.name : null;
    const chars = Object.entries(relationships).filter(([name]) => name !== playerName);
    if (!chars.length) {
        container.innerHTML = '<p style="color:#8c8c8c;font-size:0.85rem;text-align:center;padding:2rem;">No relationships yet. Keep playing!</p>';
        return;
    }

    const list = document.createElement('div');
    list.className = 'rel-bar-list';

    chars.forEach(([name, data]) => {
        const score = data.score || 0;
        const label = data.label || 'Acquaintance';
        const pct = (score + 100) / 2;

        const row = document.createElement('div');
        row.className = 'rel-bar-row';

        const top = document.createElement('div');
        top.className = 'rel-bar-top';

        const nameEl = document.createElement('span');
        nameEl.className = 'rel-bar-name';
        nameEl.textContent = name;

        const labelEl = document.createElement('span');
        labelEl.className = 'rel-bar-label';
        labelEl.textContent = label;
        labelEl.style.color = score > 20 ? '#2d6a3f' : score < -20 ? '#9a2a2a' : '#8c8c8c';

        top.appendChild(nameEl);
        top.appendChild(labelEl);

        const track = document.createElement('div');
        track.className = 'rel-bar-track';

        const fill = document.createElement('div');
        fill.className = 'rel-bar-fill';
        fill.style.width = pct + '%';
        fill.style.background = score > 0
            ? `linear-gradient(90deg, #2d6a3f, #4a9a5a)`
            : score < 0
                ? `linear-gradient(90deg, #9a2a2a, #6a1a1a)`
                : '#8c8c8c';
        track.appendChild(fill);

        const scoreEl = document.createElement('span');
        scoreEl.className = 'rel-bar-score';
        scoreEl.textContent = `${score > 0 ? '+' : ''}${score}`;
        scoreEl.style.color = score > 0 ? '#4a9a5a' : score < 0 ? '#9a4a4a' : '#8c8c8c';

        const bottom = document.createElement('div');
        bottom.className = 'rel-bar-bottom';
        if (data.last_reason) {
            const reasonEl = document.createElement('span');
            reasonEl.className = 'rel-bar-reason';
            reasonEl.textContent = data.last_reason;
            bottom.appendChild(reasonEl);
        }

        row.appendChild(top);
        row.appendChild(track);
        row.appendChild(scoreEl);
        row.appendChild(bottom);

        row.addEventListener('click', () => {
            const expanded = row.classList.toggle('rel-bar-expanded');
            bottom.style.display = expanded && data.last_reason ? 'block' : 'none';
        });

        list.appendChild(row);
    });

    container.appendChild(list);
}


// ── ENDINGS ──────────────────────────────────────────────────────────────────

function handleEnding(ending) {
    if (!ending) return;

    if (ending.is_new) saveEndingLocally(ending);

    const overlay    = document.getElementById('ending-overlay');
    const badge      = document.getElementById('ending-type-badge');
    const titleEl    = document.getElementById('ending-title');
    const summaryEl  = document.getElementById('ending-summary-text');
    const alignStat  = document.getElementById('ending-align-stat');
    const turnStat   = document.getElementById('ending-turn-stat');
    const newBadge   = document.getElementById('ending-new-badge');

    const typeColors = {
        victory:  '#2d6a3f', defeat: '#6a1a1a', sacrifice: '#4a1a6a',
        betrayal: '#6a3a1a', escape: '#1a3a6a', ambiguous: '#3a3a3a',
    };
    const color = typeColors[ending.type] || '#2a2a3a';

    badge.textContent = (ending.type || 'ENDING').toUpperCase();
    badge.style.background = color;

    const titles = {
        victory: 'Victory!', defeat: 'Defeated...', sacrifice: 'A Noble Sacrifice',
        betrayal: 'Betrayed!', escape: 'Escaped!', ambiguous: 'An Uncertain End...',
    };
    titleEl.textContent   = titles[ending.type] || 'The End';
    summaryEl.textContent = ending.summary || '';
    alignStat.textContent = `Alignment: ${ending.alignment > 0 ? '+' : ''}${ending.alignment}`;
    turnStat.textContent  = `Turns: ${ending.turn_count}`;
    newBadge.style.display = ending.is_new ? 'inline' : 'none';

    overlay.style.display = 'flex';

    // Update gallery
    if (ending.is_new) renderEndingsGallery();
}

async function restartSameStory() {
    document.getElementById('ending-overlay').style.display = 'none';
    if (activeSSE) { activeSSE.close(); activeSSE = null; }
    // Reset game state but keep session (story stays loaded)
    const fd = new FormData();
    fd.append('session_id', sessionId);
    fd.append('character', JSON.stringify(selectedCharacter));
    fd.append('difficulty', window._pendingDifficulty || 'easy');
    await apiFetch(`${API}/select-character`, { method: 'POST', body: fd });
    const fd2 = new FormData();
    fd2.append('session_id', sessionId);
    const startData = await apiFetch(`${API}/start-game`, { method: 'POST', body: fd2 });
    turn = 1;
    sceneHistory = [];
    sceneIndex = -1;
    window._journalEntries = [];
    document.getElementById('turn-badge').textContent = 'Turn 1';
    updateInventoryUI([]);
    updateAlignmentUI(0);
    renderJournal([]);
    renderTextAndChoices(startData.narrative, startData.choices);
    startPanelStream(sessionId);
}

function renderEndingsGallery() {
    const gallery = document.getElementById('ending-gallery');
    if (!gallery) return;
    const stored = JSON.parse(localStorage.getItem('storygame_endings') || '[]');
    gallery.innerHTML = '';
    if (!stored.length) {
        gallery.innerHTML = '<p style="color:#444;font-size:0.85rem;text-align:center;">No endings discovered yet.</p>';
        return;
    }
    const typeColors = {
        victory: '#2d6a3f', defeat: '#6a1a1a', sacrifice: '#4a1a6a',
        betrayal: '#6a3a1a', escape: '#1a3a6a', ambiguous: '#3a3a3a',
    };
    stored.forEach(ending => {
        const card = document.createElement('div');
        card.className = 'ending-gallery-card';
        const badge = document.createElement('span');
        badge.className = 'ending-gallery-badge';
        badge.textContent = (ending.type || 'ENDING').toUpperCase();
        badge.style.background = typeColors[ending.type] || '#2a2a3a';
        const summary = document.createElement('p');
        summary.className = 'ending-gallery-summary';
        summary.textContent = ending.summary || '';
        card.appendChild(badge);
        card.appendChild(summary);
        gallery.appendChild(card);
    });
}

// Save newly discovered endings to localStorage for gallery persistence
function saveEndingLocally(ending) {
    try {
        const stored = JSON.parse(localStorage.getItem('storygame_endings') || '[]');
        const fps = stored.map(e => e.fingerprint);
        if (!fps.includes(ending.fingerprint)) {
            stored.push(ending);
            localStorage.setItem('storygame_endings', JSON.stringify(stored));
        }
    } catch (e) { /* localStorage may be unavailable */ }
}