/* youtube.js — Tab 5: YouTube Comment Archiver */
'use strict';

(() => {
const API = '';

// ── State ────────────────────────────────────────────────────────────────────
let ytTags     = [];
let ytJobId    = null;
let ytPollTimer = null;

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function ytFmt(n) {
    n = Number(n);
    if (n >= 1e9) return (n/1e9).toFixed(1).replace(/\.0$/,'') + 'B';
    if (n >= 1e6) return (n/1e6).toFixed(1).replace(/\.0$/,'') + 'M';
    if (n >= 1e3) return (n/1e3).toFixed(1).replace(/\.0$/,'') + 'K';
    return n.toLocaleString();
}

// ── Tag pill input ────────────────────────────────────────────────────────────
const MAX_CHANNELS = 20;

function ytRenderTags() {
    const wrap = document.getElementById('ytTags');
    if (!wrap) return;
    wrap.innerHTML = ytTags.map((t, i) =>
        `<span class="ba-tag">${esc(t)}<button class="ba-tag-remove" onclick="ytRemoveTag(${i})">×</button></span>`
    ).join('');
    const counter = document.getElementById('ytCounter');
    if (counter) counter.textContent = `${ytTags.length} / ${MAX_CHANNELS} channels`;
}

window.ytRemoveTag = function(idx) {
    ytTags.splice(idx, 1);
    ytRenderTags();
};

function ytAddTag(raw) {
    const name = raw.trim().replace(/^@/, '').replace(/^https?:\/\/(www\.)?youtube\.com\/(channel\/|@)?/, '').replace(/\/$/, '').trim();
    if (!name || ytTags.includes(name) || ytTags.length >= MAX_CHANNELS) return;
    ytTags.push(name);
    ytRenderTags();
}

document.addEventListener('DOMContentLoaded', () => {
    const field = document.getElementById('ytTagField');
    if (!field) return;

    field.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            const val = field.value.trim().replace(/,$/, '');
            if (val) { ytAddTag(val); field.value = ''; }
        } else if (e.key === 'Backspace' && !field.value && ytTags.length) {
            ytTags.pop();
            ytRenderTags();
        }
    });

    field.addEventListener('paste', e => {
        e.preventDefault();
        const pasted = (e.clipboardData || window.clipboardData).getData('text');
        pasted.split(/[\n,]+/).forEach(p => ytAddTag(p));
        field.value = '';
    });
});

window.ytParsePaste = function() {
    const area = document.getElementById('ytPasteArea');
    if (!area) return;
    area.value.split(/[\n,]+/).forEach(p => ytAddTag(p));
    area.value = '';
};

// ── Alerts ────────────────────────────────────────────────────────────────────
function ytShowError(msg) {
    const el = document.getElementById('ytAlert');
    if (!el) return;
    el.textContent = msg;
    el.style.display = msg ? 'block' : 'none';
    el.className = 'alert alert-error';
}
function ytHideError() { ytShowError(''); }

// ── Start job ─────────────────────────────────────────────────────────────────
window.ytStartJob = async function() {
    ytHideError();
    if (!ytTags.length) { ytShowError('Add at least one YouTube channel name.'); return; }

    const maxComments = document.getElementById('ytCommentLimit')?.value ?? 'all';
    const maxVideos   = parseInt(document.getElementById('ytMaxVideos')?.value || '25', 10);

    const btn = document.getElementById('ytStartBtn');
    btn.disabled = true;
    btn.textContent = 'Starting…';

    try {
        const res = await fetch(`${API}/youtube/extract`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({
                channels:               ytTags,
                max_videos_per_channel: maxVideos,
                max_comments_per_video: maxComments === 'all' ? 'all' : parseInt(maxComments, 10),
            }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `Server error ${res.status}`);
        }
        const data = await res.json();
        ytJobId = data.job_id;

        // Switch to progress view
        document.getElementById('ytInputCard').style.display    = 'none';
        document.getElementById('ytProgressCard').style.display = 'block';
        document.getElementById('ytDownloadCard').style.display = 'none';

        ytStartPolling();

    } catch (err) {
        ytShowError('Failed to start: ' + err.message);
        btn.disabled = false;
        btn.textContent = '📹 Start YouTube Archive';
    }
};

// ── Polling ────────────────────────────────────────────────────────────────────
function ytStartPolling() {
    ytStopPolling();
    ytPollStatus();
    ytPollTimer = setInterval(ytPollStatus, 2000);
}
function ytStopPolling() {
    if (ytPollTimer !== null) { clearInterval(ytPollTimer); ytPollTimer = null; }
}

async function ytPollStatus() {
    if (!ytJobId) return;
    try {
        const res  = await fetch(`${API}/youtube/status/${ytJobId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        ytUpdateProgressUI(data);
        if (['complete', 'error'].includes(data.status)) {
            ytStopPolling();
            ytHandleFinished(data);
        }
    } catch (err) {
        ytAddLog('Warning: ' + err.message);
    }
}

function ytUpdateProgressUI(d) {
    const done  = d.channels_done  || 0;
    const total = d.total_channels || 1;
    const pct   = Math.round((done / total) * 100);

    const fill  = document.getElementById('ytProgFill');
    const label = document.getElementById('ytProgLabel');
    const sub   = document.getElementById('ytProgSub');

    if (fill)  fill.style.width  = pct + '%';
    if (label) {
        if (d.status === 'queued')     label.textContent = 'Queued — waiting to start…';
        else if (d.status === 'generating') label.textContent = 'Building DOCX report…';
        else if (d.status === 'complete')   label.textContent = 'Done!';
        else if (d.status === 'error')      label.textContent = '⚠ Error';
        else label.textContent = `Channel ${done} / ${total} — ${d.channel_name || '…'}`;
    }
    if (sub) sub.textContent = d.substatus || '';

    if (d.substatus) ytAddLog(d.substatus);
}

function ytHandleFinished(d) {
    if (d.status === 'complete') {
        const downloadUrl = `${API}${d.download_url}`;
        document.getElementById('ytProgressCard').style.display = 'none';
        const card = document.getElementById('ytDownloadCard');
        card.style.display = 'block';

        const btn = document.getElementById('ytDownloadBtn');
        btn.onclick = () => ytTriggerDownload(downloadUrl);
        btn.style.display = 'block';

        document.getElementById('ytDoneStats').textContent =
            `Successfully processed ${d.channels_done} channel(s).`;

    } else {
        ytShowError('Job failed: ' + (d.error || 'Unknown error'));
        document.getElementById('ytProgressCard').style.display = 'none';
        document.getElementById('ytInputCard').style.display    = 'block';
        document.getElementById('ytStartBtn').disabled   = false;
        document.getElementById('ytStartBtn').textContent = '📹 Start YouTube Archive';
    }
}

// ── Download ───────────────────────────────────────────────────────────────────
async function ytTriggerDownload(url) {
    const btn = document.getElementById('ytDownloadBtn');
    const orig = btn.textContent;
    btn.textContent = '⏳ Preparing…';
    btn.disabled = true;
    try {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`Server error ${response.status}`);
        const cd = response.headers.get('Content-Disposition') || '';
        const fnMatch = cd.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
        const filename = fnMatch ? fnMatch[1].replace(/['"]/g,'') : 'YouTube_Comments.docx';
        const blob    = await response.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename;
        a.dispatchEvent(new MouseEvent('click', { bubbles: false, cancelable: false, view: window }));
        setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);
        btn.textContent = '✅ Downloaded!';
        btn.disabled = false;
        setTimeout(() => { btn.textContent = orig; }, 3000);
    } catch (err) {
        btn.textContent = orig;
        btn.disabled = false;
        ytShowError('Download failed: ' + err.message);
    }
}

// ── Log ────────────────────────────────────────────────────────────────────────
const ytLogs = [];
function ytAddLog(msg) {
    if (!msg) return;
    const t = new Date().toLocaleTimeString();
    ytLogs.push(`[${t}] ${msg}`);
    if (ytLogs.length > 8) ytLogs.shift();
    const box = document.getElementById('ytLogBox');
    if (box) {
        box.innerHTML = ytLogs.map(l => `<div class="log-line">${esc(l)}</div>`).join('');
        box.scrollTop = box.scrollHeight;
    }
}

// ── Reset ──────────────────────────────────────────────────────────────────────
window.ytRestart = function() {
    ytStopPolling();
    ytJobId = null;
    ytTags  = [];
    ytRenderTags();
    ytHideError();
    ytLogs.length = 0;
    const logBox = document.getElementById('ytLogBox');
    if (logBox) logBox.innerHTML = '';
    document.getElementById('ytInputCard').style.display    = 'block';
    document.getElementById('ytProgressCard').style.display = 'none';
    document.getElementById('ytDownloadCard').style.display = 'none';
    const btn = document.getElementById('ytStartBtn');
    if (btn) { btn.disabled = false; btn.textContent = '📹 Start YouTube Archive'; }
};

})();
