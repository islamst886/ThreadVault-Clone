/**
 * validation.js
 * -------------
 * Frontend controller for the 🎯 Validation Hub tab.
 *
 * Handles:
 *   - Sub-tab switching (Find Leads / Message Builder / Pipeline)
 *   - Validation profile form submission
 *   - Polling /leads/status and /leads/results
 *   - Lead card rendering with scoring & filtering
 */

'use strict';

/* ── API base (shared with search.js) ──────────────────────────────────────── */
const VH_API = window.location.origin;

/* ── State ──────────────────────────────────────────────────────────────────── */
let _vhJobId      = null;
let _vhPollTimer  = null;
let _vhAllLeads   = [];       // full list returned by the backend
let _vhFilter     = 'all';
let _vhPipeline   = [];       // leads added to pipeline (stored locally)

/* ── Sub-tab switching ───────────────────────────────────────────────────────── */
function vhSwitchTab(name) {
    document.querySelectorAll('.vh-panel').forEach(p => p.style.display = 'none');
    document.querySelectorAll('.vh-tab-btn').forEach(b => b.classList.remove('active'));

    const panelId  = 'vh' + name.charAt(0).toUpperCase() + name.slice(1);
    const btnId    = 'vhTab' + name.charAt(0).toUpperCase() + name.slice(1);
    const panel    = document.getElementById(panelId);
    const btn      = document.getElementById(btnId);
    if (panel) panel.style.display = 'block';
    if (btn)   btn.classList.add('active');
}
window.vhSwitchTab = vhSwitchTab;

/* ── Helpers ─────────────────────────────────────────────────────────────────── */
function _esc(str) {
    return String(str || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function _setProg(pct, label, sub) {
    const bar   = document.getElementById('vhProgBar');
    const lbl   = document.getElementById('vhProgLabel');
    const slbl  = document.getElementById('vhProgSub');
    if (bar)  bar.style.width  = pct + '%';
    if (lbl)  lbl.textContent  = label || '';
    if (slbl) slbl.textContent = sub   || '';
}

function _showError(msg) {
    const el = document.getElementById('vhError');
    if (!el) return;
    el.textContent  = msg;
    el.style.display = 'block';
}
function _hideError() {
    const el = document.getElementById('vhError');
    if (el) el.style.display = 'none';
}

function _stopPoll() {
    if (_vhPollTimer) { clearInterval(_vhPollTimer); _vhPollTimer = null; }
}

/* ── Status → progress bar mapping ──────────────────────────────────────────── */
const _STATUS_PCT = {
    queued:              5,
    generating_queries: 15,
    crawling:           40,
    extracting:         65,
    analyzing:          80,
    scoring:            95,
    complete:           100,
    error:              100,
};
const _STATUS_LABEL = {
    queued:              'Queued…',
    generating_queries: '🤖 Generating smart search queries…',
    crawling:           '🌐 Crawling Google for Reddit posts…',
    extracting:         '📥 Extracting Reddit posts…',
    analyzing:          '🧠 Running AI analysis…',
    scoring:            '🎯 Scoring and deduplicating leads…',
    complete:           '✅ Done!',
    error:              '❌ Error',
};

/* ── Start a lead-finding job ────────────────────────────────────────────────── */
async function vhStartFindLeads() {
    _hideError();
    _vhAllLeads = [];
    _vhJobId    = null;

    const saasDesc   = (document.getElementById('vhSaasDesc')?.value     || '').trim();
    const targetCust = (document.getElementById('vhTargetCustomer')?.value || '').trim();
    const problems   = (document.getElementById('vhProblems')?.value      || '').trim();
    const subreddits = (document.getElementById('vhSubreddits')?.value    || '').trim();
    const depthEl    = document.querySelector('input[name="vhDepth"]:checked');
    const depth      = depthEl ? depthEl.value : 'standard';

    if (!saasDesc)   return _showError('Please describe what your SaaS does.');
    if (!targetCust) return _showError('Please describe your target customer.');
    if (!problems)   return _showError('Please list at least one problem it solves.');

    // Lock the button
    const btn = document.getElementById('vhFindBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Running…'; }

    // Show progress
    const prog = document.getElementById('vhProgress');
    if (prog) prog.style.display = 'block';
    _setProg(5, 'Starting…', '');

    // Hide empty state + summary
    const emptyState = document.getElementById('vhEmptyState');
    if (emptyState) emptyState.style.display = 'none';
    const summary = document.getElementById('vhSummary');
    if (summary) summary.style.display = 'none';

    // Clear lead list
    const leadList = document.getElementById('vhLeadList');
    if (leadList) leadList.innerHTML = '';

    try {
        const resp = await fetch(`${VH_API}/leads/start`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                saas_description: saasDesc,
                target_customer:  targetCust,
                problems,
                subreddits,
                depth,
            }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: 'Server error' }));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        _vhJobId = data.job_id;
        _startPolling();
    } catch (err) {
        _showError(err.message);
        if (btn) { btn.disabled = false; btn.textContent = '🔍 Find Leads'; }
        if (prog) prog.style.display = 'none';
    }
}
window.vhStartFindLeads = vhStartFindLeads;

/* ── Polling ─────────────────────────────────────────────────────────────────── */
function _startPolling() {
    _stopPoll();
    _vhPollTimer = setInterval(_poll, 2500);
    _poll(); // immediate first hit
}

async function _poll() {
    if (!_vhJobId) return;
    try {
        const resp = await fetch(`${VH_API}/leads/status/${_vhJobId}`);
        if (!resp.ok) return;
        const d = await resp.json();

        const pct   = _STATUS_PCT[d.status]  || 5;
        const label = _STATUS_LABEL[d.status] || d.status;
        const sub   = d.substatus || _buildSubLabel(d);
        _setProg(pct, label, sub);

        // Live lead count badge during run
        if (d.leads_found > 0) {
            _updateSummaryCount(d.leads_found);
        }

        if (d.status === 'complete') {
            _stopPoll();
            await _fetchResults();
        } else if (d.status === 'error') {
            _stopPoll();
            _showError(d.error || 'The lead-finding job encountered an error.');
            _unlockBtn();
        }
    } catch { /* network glitch — just retry next tick */ }
}

function _buildSubLabel(d) {
    const parts = [];
    if (d.queries_done && d.total_queries) {
        parts.push(`Query ${d.queries_done}/${d.total_queries}`);
    }
    if (d.urls_found)      parts.push(`${d.urls_found} URLs found`);
    if (d.posts_analyzed)  parts.push(`${d.posts_analyzed} posts analyzed`);
    if (d.leads_found)     parts.push(`${d.leads_found} leads so far`);
    return parts.join(' · ');
}

function _updateSummaryCount(n) {
    const summary = document.getElementById('vhSummary');
    const text    = document.getElementById('vhSummaryText');
    if (summary) summary.style.display = 'flex';
    if (text)    text.textContent = `Found ${n} lead${n !== 1 ? 's' : ''} so far…`;
}

/* ── Fetch + render final results ───────────────────────────────────────────── */
async function _fetchResults() {
    try {
        const resp = await fetch(`${VH_API}/leads/results/${_vhJobId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        _vhAllLeads = data.leads || [];
        _renderLeads(_vhAllLeads);
        _unlockBtn();
    } catch (err) {
        _showError('Could not load results: ' + err.message);
        _unlockBtn();
    }
}

function _unlockBtn() {
    const btn = document.getElementById('vhFindBtn');
    if (btn) { btn.disabled = false; btn.textContent = '🔍 Find Leads'; }
}

/* ── Render leads ────────────────────────────────────────────────────────────── */
function _renderLeads(leads) {
    const list    = document.getElementById('vhLeadList');
    const summary = document.getElementById('vhSummary');
    const sumText = document.getElementById('vhSummaryText');

    if (!list) return;
    list.innerHTML = '';

    if (!leads || leads.length === 0) {
        list.innerHTML = `
            <div class="vh-empty-state">
                <div class="vh-empty-icon">😕</div>
                <div class="vh-empty-text">No qualifying leads found. Try using a different depth or broadening your problem descriptions.</div>
            </div>`;
        if (summary) summary.style.display = 'none';
        return;
    }

    // Summary stats
    const high   = leads.filter(l => l.score >= 8).length;
    const good   = leads.filter(l => l.score >= 5 && l.score < 8).length;
    const weak   = leads.filter(l => l.score <  5).length;
    const subs   = new Set(leads.map(l => l.subreddit).filter(Boolean));

    if (summary)  summary.style.display = 'flex';
    if (sumText)  sumText.textContent =
        `Found ${leads.length} leads across ${subs.size} subreddit${subs.size !== 1 ? 's' : ''} · ` +
        `${high} High Priority · ${good} Good · ${weak} Weak`;

    leads.forEach(lead => list.appendChild(_buildLeadCard(lead)));

    // Show batch generate bar if there are high-priority leads
    const batchBar = _el('vhBatchBar');
    if (batchBar) batchBar.style.display = high > 0 ? 'flex' : 'none';
}

/* ── Build a single lead card ───────────────────────────────────────────────── */
function _buildLeadCard(lead) {
    const score    = lead.score || 0;
    const priority = lead.priority || 'WEAK LEAD';

    let borderClass = 'lead-weak';
    let scoreClass  = 'lscore-low';
    if      (score >= 8) { borderClass = 'lead-high';   scoreClass = 'lscore-high'; }
    else if (score >= 5) { borderClass = 'lead-mid';    scoreClass = 'lscore-mid';  }

    const appearances = lead.appearances || 1;
    const multiLabel  = appearances > 1
        ? `<span class="lead-multi-badge">🔁 Mentioned ${appearances}× across multiple posts</span>` : '';

    const date = lead.post_date
        ? `<span>${_esc(lead.post_date.slice(0, 10))}</span>` : '';

    const card = document.createElement('div');
    card.className = `lead-card ${borderClass}`;
    card.dataset.category = lead.category || '';
    card.dataset.priority = priority;
    card.innerHTML = `
        <div class="lead-card-header">
            <div class="lead-score-badge ${scoreClass}">
                🎯 ${score}/10
                <span class="lead-priority-label">${_esc(priority)}</span>
            </div>
            <div class="lead-type-badge">${lead.lead_type === 'commenter' ? '💬 Commenter' : '✍️ Poster'}</div>
        </div>

        <div class="lead-username">u/${_esc(lead.reddit_username || 'unknown')}</div>

        <div class="lead-meta">
            <span class="lead-sub">r/${_esc(lead.subreddit || '?')}</span>
            ${date}
            <span class="lead-score-num">Post score: ${lead.post_score ?? '?'}</span>
        </div>

        ${lead.key_quote ? `<blockquote class="lead-quote">"${_esc(lead.key_quote.slice(0, 200))}"</blockquote>` : ''}

        <div class="lead-pain">${_esc(lead.pain_point_summary || '')}</div>

        <div class="lead-tags">
            <span class="lead-category-tag">${_esc(lead.category || '')}</span>
            ${multiLabel}
        </div>

        <div class="lead-title-row">
            <a class="lead-post-link" href="${_esc(lead.post_url || '#')}" target="_blank" rel="noopener">
                ${_esc((lead.post_title || '').slice(0, 80))}${(lead.post_title || '').length > 80 ? '…' : ''}
            </a>
        </div>

        <div class="lead-actions">
            <button class="btn-sm btn-sm-orange" onclick="vhBuildMessage(${JSON.stringify(_esc(JSON.stringify(lead)))})">✉️ Build Message</button>
            <button class="btn-sm btn-sm-outline" onclick="vhAddToPipeline(${JSON.stringify(_esc(JSON.stringify(lead)))})">➕ Add to Pipeline</button>
        </div>`;
    return card;
}

/* ── Filter buttons ──────────────────────────────────────────────────────────── */
function vhApplyFilter(filter) {
    _vhFilter = filter;
    document.querySelectorAll('.vhf-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === filter));

    let filtered = _vhAllLeads;
    if      (filter === 'high')     filtered = _vhAllLeads.filter(l => l.score >= 8);
    else if (filter === 'solution') filtered = _vhAllLeads.filter(l => l.category === 'Solution Request');
    else if (filter === 'money')    filtered = _vhAllLeads.filter(l => l.category === 'Money Talk');
    else if (filter === 'pain')     filtered = _vhAllLeads.filter(l => l.category === 'Pain Point');

    const list = document.getElementById('vhLeadList');
    if (!list) return;
    list.innerHTML = '';
    if (filtered.length === 0) {
        list.innerHTML = `<div class="vh-empty-state"><div class="vh-empty-icon">🔍</div><div class="vh-empty-text">No leads match this filter.</div></div>`;
    } else {
        filtered.forEach(lead => list.appendChild(_buildLeadCard(lead)));
    }
}
window.vhApplyFilter = vhApplyFilter;

/* ── Message Builder ─────────────────────────────────────────────────────────── */

// State for the currently loaded lead and its generated versions
let _vmbCurrentLead     = null;
let _vmbVersions        = [];  // array of 3 version objects
let _vmbActiveVersion   = 0;

/**
 * Called from a lead card "Build Message" button.
 * Switches to the Message Builder tab and kicks off generation.
 */
async function vhBuildMessage(encodedLead) {
    let lead;
    try { lead = JSON.parse(encodedLead); } catch { return; }

    _vmbCurrentLead   = lead;
    _vmbVersions      = [];
    _vmbActiveVersion = 0;

    // Switch tab
    vhSwitchTab('messageBuilder');

    // Show builder, hide empty state
    _el('vhMsgEmpty').style.display  = 'none';
    _el('vhMsgBuilder').style.display = 'block';

    // Populate context header
    _el('vmbLeadName').textContent = `u/${lead.reddit_username || 'unknown'}`;
    _el('vmbLeadSub').textContent  = `r/${lead.subreddit || '?'}  ·  Score: ${lead.score || '?'}/10  ·  ${lead.category || ''}`;
    _el('vmbQuoteText').textContent = lead.key_quote || '(No quote available)';
    _el('vmbPain').textContent      = lead.pain_point_summary ? `🔥 Pain: ${lead.pain_point_summary}` : '';

    // Show loading, hide versions
    _el('vmbLoading').style.display  = 'flex';
    _el('vmbVersions').style.display = 'none';
    _el('vmbError').style.display    = 'none';

    await _vmbGenerate(lead);
}
window.vhBuildMessage = vhBuildMessage;

/**
 * Call backend to generate 3 message versions.
 */
async function _vmbGenerate(lead) {
    const saasDesc = (_el('vhSaasDesc')?.value || '').trim()
        || 'a SaaS product for market validation';

    try {
        const resp = await fetch(`${VH_API}/leads/generate-message`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lead, saas_description: saasDesc }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        _vmbVersions = data.versions || [];
        _el('vmbLoading').style.display  = 'none';
        _el('vmbVersions').style.display = 'block';
        vmbShowVersion(0);
    } catch (err) {
        _el('vmbLoading').style.display = 'none';
        _el('vmbError').textContent     = `Generation failed: ${err.message}`;
        _el('vmbError').style.display   = 'block';
    }
}

/**
 * Helper: get element by id.
 */
function _el(id) { return document.getElementById(id); }

/**
 * Show one of the 3 generated versions.
 */
function vmbShowVersion(idx) {
    _vmbActiveVersion = idx;

    // Update tab buttons
    ['vmbVerA', 'vmbVerB', 'vmbVerC'].forEach((id, i) => {
        _el(id)?.classList.toggle('active', i === idx);
    });

    const v = _vmbVersions[idx];
    if (!v) return;

    _el('vmbSubject').textContent    = v.subject || '';
    _el('vmbMessageText').value      = v.message || '';
    _el('vmbReadTime').textContent   = v.estimated_read_time ? `⏱ ${v.estimated_read_time} read` : '';
    _el('vmbNote').innerHTML         = v.personalization_note
        ? `<strong>ℹ Personalized by referencing:</strong> ${_esc(v.personalization_note)}`
        : '';
    _el('vmbValQ').innerHTML         = v.validation_question
        ? `<strong>❓ Validation question:</strong> <em>${_esc(v.validation_question)}</em>`
        : '';
    vmbUpdateCount();
}
window.vmbShowVersion = vmbShowVersion;

/**
 * Update character count under the textarea.
 */
function vmbUpdateCount() {
    const ta = _el('vmbMessageText');
    if (!ta) return;
    const n = ta.value.length;
    _el('vmbCharCount').textContent = `${n} char${n !== 1 ? 's' : ''}`;
}
window.vmbUpdateCount = vmbUpdateCount;

/**
 * Copy current message to clipboard.
 */
async function vmbCopy() {
    const ta = _el('vmbMessageText');
    if (!ta) return;
    try {
        await navigator.clipboard.writeText(ta.value);
        const btn = document.querySelector('.vmb-actions .btn-sm-orange');
        if (btn) {
            const prev = btn.textContent;
            btn.textContent = '✅ Copied!';
            setTimeout(() => btn.textContent = prev, 1800);
        }
    } catch {
        ta.select();
        document.execCommand('copy');
    }
}
window.vmbCopy = vmbCopy;

/**
 * Regenerate messages for the current lead.
 */
async function vmbRegenerate() {
    if (!_vmbCurrentLead) return;
    _vmbVersions = [];
    _el('vmbVersions').style.display = 'none';
    _el('vmbLoading').style.display  = 'flex';
    _el('vmbError').style.display    = 'none';
    await _vmbGenerate(_vmbCurrentLead);
}
window.vmbRegenerate = vmbRegenerate;

/**
 * Mark current lead as sent → add to pipeline with "Contacted" status.
 */
function vmbMarkSent() {
    if (!_vmbCurrentLead) return;
    const lead = { ..._vmbCurrentLead, pipeline_status: 'Contacted' };
    const exists = _vhPipeline.some(l => l.reddit_username === lead.reddit_username);
    if (!exists) _vhPipeline.push(lead);
    else {
        const idx = _vhPipeline.findIndex(l => l.reddit_username === lead.reddit_username);
        if (idx >= 0) _vhPipeline[idx].pipeline_status = 'Contacted';
    }
    _renderPipeline();
    vhSwitchTab('pipeline');
}
window.vmbMarkSent = vmbMarkSent;


/* ── Batch Message Generation ────────────────────────────────────────────────── */

async function vhGenerateAllMessages() {
    if (!_vhJobId) {
        return _showError('No lead search job found. Run Find Leads first.');
    }
    const saasDesc = (_el('vhSaasDesc')?.value || '').trim()
        || 'a SaaS product for market validation';

    const btn    = _el('vhBatchBtn');
    const status = _el('vhBatchStatus');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Generating…'; }
    if (status) { status.textContent = 'Asking Gemini to write personalized messages for your top leads…'; status.className = 'vh-batch-status running'; }

    try {
        const resp = await fetch(`${VH_API}/leads/generate-all-messages`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ job_id: _vhJobId, saas_description: saasDesc }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const data = await resp.json();

        if (status) {
            status.textContent = `✅ ${data.message}`;
            status.className   = 'vh-batch-status done';
        }

        // Show export button
        const bar = _el('vhBatchBar');
        if (bar && !_el('vhExportBtn')) {
            const exportBtn = document.createElement('button');
            exportBtn.id        = 'vhExportBtn';
            exportBtn.className = 'btn-sm btn-sm-outline';
            exportBtn.textContent = '⬇ Export All as DOCX';
            exportBtn.onclick   = vhExportMessages;
            bar.appendChild(exportBtn);
        }

        if (btn) { btn.disabled = false; btn.textContent = '🔄 Regenerate All Messages'; }

    } catch (err) {
        if (status) { status.textContent = `❌ ${err.message}`; status.className = 'vh-batch-status error'; }
        if (btn) { btn.disabled = false; btn.textContent = '🚀 Generate Messages for All High Priority Leads'; }
    }
}
window.vhGenerateAllMessages = vhGenerateAllMessages;

async function vhExportMessages() {
    if (!_vhJobId) return;
    const exportBtn = _el('vhExportBtn');
    if (exportBtn) { exportBtn.disabled = true; exportBtn.textContent = '⏳ Building DOCX…'; }
    try {
        const resp = await fetch(`${VH_API}/leads/export-messages/${_vhJobId}`, { method: 'POST' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement('a');
        a.href     = url;
        a.download = `outreach_messages.docx`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 2000);
    } catch (err) {
        alert('Export failed: ' + err.message);
    } finally {
        if (exportBtn) { exportBtn.disabled = false; exportBtn.textContent = '⬇ Export All as DOCX'; }
    }
}
window.vhExportMessages = vhExportMessages;




/* ═══════════════════════════════════════════════════════════════════════════
   VALIDATION PROFILES  — localStorage-backed project switcher
   ═══════════════════════════════════════════════════════════════════════════ */

const _VP_KEY    = 'tv_validation_profiles';
const _VP_ACTIVE = 'tv_active_profile';

const _PIPE_STAGES = ['new','contacted','followup','replied','validated','rejected'];
const _STAGE_LABELS = {
    new:       '📋 New Leads',
    contacted: '✉️ Contacted',
    followup:  '👁️ Follow-up Due',
    replied:   '💬 Replied',
    validated: '✅ Validated',
    rejected:  '❌ Not Interested',
};
// 3 days in ms
const _FOLLOWUP_MS  = 3 * 24 * 60 * 60 * 1000;

// ── Profile CRUD ─────────────────────────────────────────────────────────────

function _vpLoad() {
    try { return JSON.parse(localStorage.getItem(_VP_KEY) || '{}'); } catch { return {}; }
}
function _vpSave(p) { localStorage.setItem(_VP_KEY, JSON.stringify(p)); }
function _vpActiveName() { return localStorage.getItem(_VP_ACTIVE) || ''; }
function _vpSetActive(name) { localStorage.setItem(_VP_ACTIVE, name); }

function _vpGet(name) {
    const all = _vpLoad();
    return all[name] || null;
}

function _vpEnsureDefault() {
    const all = _vpLoad();
    if (Object.keys(all).length === 0) {
        const def = 'My Validation Project';
        all[def] = _vpEmpty(def);
        _vpSave(all);
        _vpSetActive(def);
    }
    if (!_vpActiveName() || !_vpLoad()[_vpActiveName()]) {
        _vpSetActive(Object.keys(_vpLoad())[0]);
    }
}

function _vpEmpty(name) {
    return {
        name,
        saas_description: '',
        target_customer:  '',
        problems:         '',
        pipeline: { new:[], contacted:[], followup:[], replied:[], validated:[], rejected:[] },
    };
}

function _vpActive() {
    _vpEnsureDefault();
    return _vpLoad()[_vpActiveName()] || _vpEmpty(_vpActiveName());
}

function _vpSaveActive(updated) {
    const all = _vpLoad();
    all[_vpActiveName()] = updated;
    _vpSave(all);
}

// ── Profile UI ───────────────────────────────────────────────────────────────

function _renderProfileSelector() {
    const sel = _el('vhProfileSelect');
    if (!sel) return;
    const all    = _vpLoad();
    const active = _vpActiveName();
    sel.innerHTML = Object.keys(all).map(name =>
        `<option value="${_esc(name)}" ${name === active ? 'selected' : ''}>${_esc(name)}</option>`
    ).join('');
}

function vhSwitchProfile(name) {
    if (!name) return;
    _vpSetActive(name);
    _renderProfileSelector();
    // Reload form fields from profile
    const prof = _vpActive();
    const f = (id, val) => { const e = _el(id); if (e) e.value = val || ''; };
    f('vhSaasDesc',       prof.saas_description);
    f('vhTargetCustomer', prof.target_customer);
    f('vhProblems',       prof.problems);
    _renderPipeline();
}
window.vhSwitchProfile = vhSwitchProfile;

// ── Custom Modal ─────────────────────────────────────────────────────────────

function _vhShowModal(title, { isConfirm = false, isAlert = false, defaultValue = '', placeholder = '' } = {}) {
    return new Promise(resolve => {
        const overlay = _el('vhModal');
        const titleEl = _el('vhModalTitle');
        const input   = _el('vhModalInput');
        const btnOk   = _el('vhModalOkBtn');
        const btnCancel = _el('vhModalCancelBtn');

        titleEl.textContent = title;
        input.value = defaultValue;
        input.placeholder = placeholder;
        
        if (isConfirm || isAlert) {
            input.style.display = 'none';
        } else {
            input.style.display = 'block';
        }

        if (isAlert) {
            btnCancel.style.display = 'none';
        } else {
            btnCancel.style.display = 'block';
        }

        overlay.style.display = 'flex';
        if (!isConfirm && !isAlert) input.focus();

        const close = (val) => {
            overlay.style.display = 'none';
            btnOk.onclick = null;
            btnCancel.onclick = null;
            input.onkeydown = null;
            resolve(val);
        };

        btnOk.onclick = () => close((isConfirm || isAlert) ? true : input.value);
        btnCancel.onclick = () => close((isConfirm || isAlert) ? false : null);
        input.onkeydown = (e) => {
            if (e.key === 'Enter') btnOk.click();
            if (e.key === 'Escape') btnCancel.click();
        };
    });
}
function _vhAlert(title) {
    return _vhShowModal(title, { isAlert: true });
}


async function vhNewProfile() {
    let name = await _vhShowModal('Name for the new validation project:', { placeholder: 'e.g. Fitness CRM' });
    if (name === null) return;
    name = name.trim();
    if (!name) return;
    const all = _vpLoad();
    if (all[name]) { _vhAlert('A project with that name already exists.'); return; }
    all[name] = _vpEmpty(name);
    _vpSave(all);
    _vpSetActive(name);
    _renderProfileSelector();
    vhSwitchProfile(name);
}
window.vhNewProfile = vhNewProfile;

async function vhRenameProfile() {
    const old  = _vpActiveName();
    let name = await _vhShowModal('Rename project to:', { defaultValue: old });
    if (name === null) return;
    name = name.trim();
    if (!name || name === old) return;
    const all = _vpLoad();
    if (all[name]) { _vhAlert('A project with that name already exists.'); return; }
    all[name] = { ...all[old], name };
    delete all[old];
    _vpSave(all);
    _vpSetActive(name);
    _renderProfileSelector();
}
window.vhRenameProfile = vhRenameProfile;

async function vhDeleteProfile() {
    const name = _vpActiveName();
    const isYes = await _vhShowModal(`Delete project "${name}" and all its leads? This cannot be undone.`, { isConfirm: true });
    if (!isYes) return;
    const all = _vpLoad();
    delete all[name];
    if (Object.keys(all).length === 0) {
        const def = 'My Validation Project';
        all[def] = _vpEmpty(def);
    }
    _vpSave(all);
    _vpSetActive(Object.keys(_vpLoad())[0]);
    _renderProfileSelector();
    vhSwitchProfile(_vpActiveName());
}
window.vhDeleteProfile = vhDeleteProfile;


// Sync form fields → active profile on change
function _vpSyncFormToProfile() {
    const prof = _vpActive();
    prof.saas_description = (_el('vhSaasDesc')?.value || '').trim();
    prof.target_customer  = (_el('vhTargetCustomer')?.value || '').trim();
    prof.problems         = (_el('vhProblems')?.value || '').trim();
    _vpSaveActive(prof);
}

// Hook form fields to auto-save profile data
document.addEventListener('DOMContentLoaded', () => {
    _vpEnsureDefault();
    _renderProfileSelector();
    const prof = _vpActive();
    const f = (id, val) => { const e = _el(id); if (e) e.value = val || ''; };
    f('vhSaasDesc',       prof.saas_description);
    f('vhTargetCustomer', prof.target_customer);
    f('vhProblems',       prof.problems);

    ['vhSaasDesc','vhTargetCustomer','vhProblems'].forEach(id => {
        const e = _el(id);
        if (e) e.addEventListener('input', _vpSyncFormToProfile);
    });
});

/* ═══════════════════════════════════════════════════════════════════════════
   PIPELINE — Kanban board with analytics, auto follow-up, DOCX export
   ═══════════════════════════════════════════════════════════════════════════ */

// ── Add to pipeline (from lead card or Mark as Sent) ─────────────────────────

function vhAddToPipeline(encodedLead) {
    try {
        const lead = JSON.parse(encodedLead);
        _pipeAddLead(lead, 'new');
        vhSwitchTab('pipeline');
    } catch { /* ignore */ }
}
window.vhAddToPipeline = vhAddToPipeline;

function vmbMarkSent() {
    if (!_vmbCurrentLead) return;
    _pipeAddLead(_vmbCurrentLead, 'contacted');
    vhSwitchTab('pipeline');
}
window.vmbMarkSent = vmbMarkSent;

function _pipeAddLead(lead, stage) {
    const prof     = _vpActive();
    const pipeline = prof.pipeline;
    // Dedup by username across all stages
    const username = (lead.reddit_username || '').toLowerCase();
    const exists   = _PIPE_STAGES.some(s => pipeline[s]?.some(l => l.reddit_username?.toLowerCase() === username));
    if (exists) { _renderPipeline(); return; }

    const entry = {
        ...lead,
        id:             `lead_${Date.now()}_${Math.random().toString(36).slice(2,7)}`,
        stage,
        added_date:     Date.now(),
        contacted_date: stage === 'contacted' ? Date.now() : null,
        notes:          '',
    };
    if (!pipeline[stage]) pipeline[stage] = [];
    pipeline[stage].push(entry);
    _vpSaveActive(prof);
    _renderPipeline();
}

// ── Move a lead between stages ────────────────────────────────────────────────

function pipeMoveStage(id, newStage) {
    const prof     = _vpActive();
    const pipeline = prof.pipeline;
    let lead       = null;

    // Find and remove from current stage
    for (const s of _PIPE_STAGES) {
        const idx = (pipeline[s] || []).findIndex(l => l.id === id);
        if (idx >= 0) {
            [lead] = pipeline[s].splice(idx, 1);
            break;
        }
    }
    if (!lead) return;
    lead.stage = newStage;
    if (newStage === 'contacted' && !lead.contacted_date) {
        lead.contacted_date = Date.now();
    }
    if (!pipeline[newStage]) pipeline[newStage] = [];
    pipeline[newStage].push(lead);
    _vpSaveActive(prof);
    _renderPipeline();
}
window.pipeMoveStage = pipeMoveStage;

// ── Update notes inline ───────────────────────────────────────────────────────

function pipeUpdateNotes(id, notes) {
    const prof = _vpActive();
    for (const s of _PIPE_STAGES) {
        const lead = (prof.pipeline[s] || []).find(l => l.id === id);
        if (lead) { lead.notes = notes; break; }
    }
    _vpSaveActive(prof);
}
window.pipeUpdateNotes = pipeUpdateNotes;

// ── Auto-move contacted → follow-up after 3 days ─────────────────────────────

function _checkFollowup() {
    const prof = _vpActive();
    const pipeline = prof.pipeline;
    const now  = Date.now();
    let changed = false;

    (pipeline.contacted || []).forEach(lead => {
        if (lead.contacted_date && (now - lead.contacted_date) >= _FOLLOWUP_MS) {
            lead.stage = 'followup';
            if (!pipeline.followup) pipeline.followup = [];
            pipeline.followup.push(lead);
            changed = true;
        }
    });
    if (changed) {
        pipeline.contacted = (pipeline.contacted || []).filter(l => l.stage !== 'followup');
        _vpSaveActive(prof);
    }
    return changed;
}

// ── Generate follow-up message ────────────────────────────────────────────────

async function pipeGenerateFollowup(id, btnEl) {
    const prof = _vpActive();
    const saasDesc = prof.saas_description || (_el('vhSaasDesc')?.value || '').trim() || 'my SaaS';
    let lead = null;
    for (const s of _PIPE_STAGES) {
        lead = (prof.pipeline[s] || []).find(l => l.id === id);
        if (lead) break;
    }
    if (!lead) return;

    if (btnEl) { btnEl.disabled = true; btnEl.textContent = '⏳ Generating…'; }

    try {
        const resp = await fetch(`${VH_API}/leads/generate-followup`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ lead, saas_description: saasDesc }),
        });
        const data = await resp.json();
        const msg  = data.message || '';

        // Show in a textarea below the card
        const card = document.getElementById(`pcard-${id}`);
        if (card) {
            let box = card.querySelector('.pipe-followup-msg');
            if (!box) {
                box = document.createElement('div');
                box.className = 'pipe-followup-msg';
                card.appendChild(box);
            }
            box.innerHTML = `
                <textarea class="pipe-followup-ta" rows="3">${_esc(msg)}</textarea>
                <div style="display:flex;gap:6px;margin-top:6px">
                    <button class="btn-sm btn-sm-orange" onclick="navigator.clipboard.writeText(this.closest('.pipe-followup-msg').querySelector('textarea').value)">📋 Copy</button>
                    <button class="btn-sm btn-sm-outline" onclick="this.closest('.pipe-followup-msg').remove()">✕</button>
                </div>`;
        }
    } catch (err) {
        alert('Follow-up generation failed: ' + err.message);
    } finally {
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = '✍️ Generate Follow-up'; }
    }
}
window.pipeGenerateFollowup = pipeGenerateFollowup;

// ── Analytics ─────────────────────────────────────────────────────────────────

function _updateAnalytics(pipeline) {
    const total       = _PIPE_STAGES.reduce((n, s) => n + (pipeline[s]?.length || 0), 0);
    const contacted   = ['contacted','followup','replied','validated'].reduce((n,s) => n + (pipeline[s]?.length||0), 0);
    const replied     = ['replied','validated'].reduce((n,s) => n + (pipeline[s]?.length||0), 0);
    const validated   = pipeline.validated?.length || 0;
    const replyRate   = contacted > 0 ? Math.round(replied / contacted * 100) : null;
    const yourPct     = contacted > 0 ? Math.round(replied / contacted * 100) : 0;

    const setText = (id, val) => { const e = _el(id); if (e) e.textContent = val; };
    setText('vhStatTotalN',     total);
    setText('vhStatContactedN', contacted);
    setText('vhStatRepliedN',   replied);
    setText('vhStatValidatedN', validated);
    setText('vhStatRateN',      replyRate !== null ? `${replyRate}%` : '—');

    // Funnel
    const funnelEl = _el('vhFunnel');
    if (funnelEl && total > 0) {
        const steps = [
            { label: 'Found',     n: total,     color: 'var(--blue)' },
            { label: 'Contacted', n: contacted, color: 'var(--orange)' },
            { label: 'Replied',   n: replied,   color: 'var(--gold)' },
            { label: 'Validated', n: validated,  color: 'var(--green)' },
        ];
        funnelEl.innerHTML = steps.map((s, i) => {
            const pct = Math.round(s.n / total * 100);
            return `<div class="vh-funnel-step">
                <div class="vh-funnel-bar" style="width:${Math.max(pct,4)}%;background:${s.color}">
                    <span class="vh-funnel-n">${s.n}</span>
                </div>
                <div class="vh-funnel-label">${s.label}</div>
                ${i < steps.length-1 ? '<div class="vh-funnel-arrow">→</div>' : ''}
            </div>`;
        }).join('');
    } else if (funnelEl) {
        funnelEl.innerHTML = '';
    }

    // Benchmark
    const benchEl = _el('vhBenchmark');
    if (benchEl && contacted > 0) {
        const icon  = yourPct >= 10 ? '🔥' : yourPct >= 3 ? '✅' : '⚠️';
        const msg   = yourPct >= 10 ? 'above average — great outreach!'
                     : yourPct >= 3 ? 'within industry benchmark (3–10%)'
                     : 'below benchmark — try varying your opener';
        benchEl.textContent = `💡 Industry benchmark: 3–10% reply rate. You're at ${yourPct}% — ${icon} ${msg}`;
        benchEl.style.display = 'block';
    } else if (benchEl) {
        benchEl.style.display = 'none';
    }
}

// ── Follow-up banner ──────────────────────────────────────────────────────────

function _updateFollowupBanner(pipeline) {
    const count  = pipeline.followup?.length || 0;
    const banner = _el('vhFollowBanner');
    const text   = _el('vhFollowBannerText');
    if (!banner) return;
    if (count > 0) {
        if (text) text.textContent = `⏰ ${count} lead${count !== 1 ? 's' : ''} due for follow-up`;
        banner.style.display = 'flex';
    } else {
        banner.style.display = 'none';
    }
}

function vhScrollToFollowup() {
    const col = _el('pipeCol-followup');
    if (col) {
        col.scrollIntoView({ behavior: 'smooth', block: 'start' });
        col.classList.add('vh-col-highlight');
        setTimeout(() => col.classList.remove('vh-col-highlight'), 2000);
    }
}
window.vhScrollToFollowup = vhScrollToFollowup;

// ── Kanban card builder ───────────────────────────────────────────────────────

function _buildPipeCard(lead, stage) {
    const div  = document.createElement('div');
    div.className = 'pipe-card';
    div.id        = `pcard-${lead.id}`;

    const scoreClass = lead.score >= 8 ? 'lscore-high' : lead.score >= 5 ? 'lscore-mid' : 'lscore-low';
    const daysSince  = lead.contacted_date
        ? Math.floor((Date.now() - lead.contacted_date) / 86400000)
        : null;

    // Stage move buttons (show next/prev)
    const stageIdx   = _PIPE_STAGES.indexOf(stage);
    const prevStage  = stageIdx > 0 ? _PIPE_STAGES[stageIdx - 1] : null;
    const nextStage  = stageIdx < _PIPE_STAGES.length - 1 ? _PIPE_STAGES[stageIdx + 1] : null;
    const moveButtons = [
        prevStage ? `<button class="pipe-move-btn" onclick="pipeMoveStage('${lead.id}','${prevStage}')">← ${_STAGE_LABELS[prevStage]?.split(' ')[1] || prevStage}</button>` : '',
        nextStage ? `<button class="pipe-move-btn pipe-move-next" onclick="pipeMoveStage('${lead.id}','${nextStage}')">${_STAGE_LABELS[nextStage]?.split(' ')[1] || nextStage} →</button>` : '',
    ].filter(Boolean).join('');

    const followupBtn = (stage === 'followup')
        ? `<button class="btn-sm btn-sm-outline pipe-followup-btn" onclick="pipeGenerateFollowup('${lead.id}', this)">✍️ Generate Follow-up</button>`
        : '';

    const msgBtn = `<button class="btn-sm btn-sm-outline" style="font-size:.72rem" onclick="vhBuildMessage(${JSON.stringify(_esc(JSON.stringify(lead)))})">✉️ Message</button>`;

    div.innerHTML = `
        <div class="pipe-card-header">
            <a class="pipe-username" href="https://reddit.com/user/${_esc(lead.reddit_username || '')}" target="_blank" rel="noopener">
                u/${_esc(lead.reddit_username || 'unknown')}
            </a>
            <span class="lead-score-badge ${scoreClass}" style="font-size:.7rem;padding:2px 8px">🎯 ${lead.score || '?'}/10</span>
        </div>
        <div class="pipe-sub">r/${_esc(lead.subreddit || '?')}  ·  ${_esc(lead.category || '')}</div>
        ${lead.key_quote ? `<div class="pipe-quote">"${_esc(lead.key_quote.slice(0, 120))}"</div>` : ''}
        <div class="pipe-meta">
            <span>Added ${_formatDate(lead.added_date)}</span>
            ${daysSince !== null ? `<span class="${daysSince >= 3 ? 'pipe-days-warn' : ''}">${daysSince}d since contacted</span>` : ''}
        </div>
        <textarea class="pipe-notes" placeholder="Add notes…" rows="2"
            onchange="pipeUpdateNotes('${lead.id}', this.value)">${_esc(lead.notes || '')}</textarea>
        ${followupBtn}
        <div class="pipe-card-actions">
            ${msgBtn}
            <div class="pipe-move-btns">${moveButtons}</div>
        </div>`;
    return div;
}

function _formatDate(ts) {
    if (!ts) return '?';
    return new Date(ts).toLocaleDateString(undefined, { month:'short', day:'numeric' });
}

// ── Main render ───────────────────────────────────────────────────────────────

function _renderPipeline() {
    _vpEnsureDefault();
    const prof     = _vpActive();
    const pipeline = prof.pipeline;

    // Auto-move follow-up due leads
    _checkFollowup();

    // Analytics
    _updateAnalytics(pipeline);
    _updateFollowupBanner(pipeline);

    // Render columns
    for (const stage of _PIPE_STAGES) {
        const body  = _el(`pipeBody-${stage}`);
        const count = _el(`pipeCount-${stage}`);
        const leads = pipeline[stage] || [];
        if (count) count.textContent = leads.length;
        if (!body) continue;
        body.innerHTML = '';
        if (leads.length === 0) {
            body.innerHTML = `<div class="pipe-empty">No leads here yet</div>`;
        } else {
            leads.forEach(lead => body.appendChild(_buildPipeCard(lead, stage)));
        }
    }
}
window._renderPipeline = _renderPipeline;

// ── Export pipeline as DOCX ───────────────────────────────────────────────────

async function vhExportPipeline() {
    const prof = _vpActive();
    const btn  = document.querySelector('[onclick="vhExportPipeline()"]');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Building…'; }

    try {
        const resp = await fetch(`${VH_API}/leads/export-pipeline`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({
                stages:           prof.pipeline,
                profile_name:     prof.name,
                saas_description: prof.saas_description || (_el('vhSaasDesc')?.value || ''),
                target_customer:  prof.target_customer  || (_el('vhTargetCustomer')?.value || ''),
            }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement('a');
        a.href     = url;
        a.download = `pipeline_${(prof.name || 'export').replace(/\s+/g,'_')}.docx`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 2000);
    } catch (err) {
        alert('Export failed: ' + err.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '📥 Export as DOCX'; }
    }
}
window.vhExportPipeline = vhExportPipeline;

