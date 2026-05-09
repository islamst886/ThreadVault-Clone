/* ============================================================
   bulk.js — Subreddit Bulk Archive feature
   Handles: tag input, settings, ETA estimate, job start,
            status polling, progress display, downloads.
   ============================================================ */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let baTags      = [];        // array of subreddit name strings
let baJobId     = null;
let baPollingId = null;
const BA_MAX    = 20;
const BA_SECS_PER_POST = 7;  // conservative ETA estimate

// ── Tag input ─────────────────────────────────────────────────────────────────
function baInitTagInput() {
  const field = document.getElementById('baTagField');
  if (!field) return;

  field.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      const val = field.value.trim().replace(/^r\//, '').replace(/,/g, '').trim();
      if (val) baAddTag(val);
      field.value = '';
    } else if (e.key === 'Backspace' && field.value === '' && baTags.length) {
      baTags.pop();
      baRenderTags();
    }
  });
}

function baAddTag(name) {
  name = name.trim().replace(/^r\//, '').replace(/[^a-zA-Z0-9_]/g, '').slice(0, 50);
  if (!name) return;
  if (baTags.includes(name)) { baFlashDupe(name); return; }
  if (baTags.length >= BA_MAX) { baShowAlert(`Maximum ${BA_MAX} subreddits per job.`); return; }
  baTags.push(name);
  baRenderTags();
}

function baRemoveTag(idx) {
  baTags.splice(idx, 1);
  baRenderTags();
}

function baRenderTags() {
  const container = document.getElementById('baTags');
  const counter   = document.getElementById('baCounter');
  const field     = document.getElementById('baTagField');
  if (!container) return;

  container.innerHTML = baTags.map((name, i) =>
    `<span class="ba-pill" id="baPill${i}">r/${name}
      <button class="ba-pill-x" onclick="baRemoveTag(${i})" aria-label="Remove r/${name}">×</button>
    </span>`
  ).join('');

  if (counter) {
    counter.textContent = `${baTags.length} / ${BA_MAX} subreddits`;
    counter.className   = 'ba-counter' + (baTags.length >= BA_MAX ? ' ba-counter-max' : '');
  }
  if (field) field.placeholder = baTags.length ? 'Add another…' : 'Type a subreddit name and press Enter…';

  baUpdateEta();
}

function baFlashDupe(name) {
  const idx  = baTags.indexOf(name);
  const pill = document.getElementById(`baPill${idx}`);
  if (pill) { pill.classList.add('ba-pill-dupe'); setTimeout(() => pill.classList.remove('ba-pill-dupe'), 800); }
}

// ── Paste helper ──────────────────────────────────────────────────────────────
function baParsePaste() {
  const area = document.getElementById('baPasteArea');
  if (!area) return;
  const names = area.value.split(/[\s,]+/).map(n => n.trim().replace(/^r\//, '')).filter(Boolean);
  names.forEach(baAddTag);
  area.value = '';
}

// ── Settings & ETA ────────────────────────────────────────────────────────────
function baGetSettings() {
  const yearsEl = document.querySelector('input[name="baYears"]:checked');
  const limitEl = document.querySelector('input[name="baLimit"]:checked');
  return {
    years_back:              parseFloat(yearsEl ? yearsEl.value : 2),
    post_limit_per_subreddit: parseInt(limitEl  ? limitEl.value : 500),
    comment_sort:            (document.getElementById('baSort')         || {}).value || 'top',
    comment_limit:           (document.getElementById('baCommentLimit') || {}).value || '25',
  };
}

function baUpdateEta() {
  const bar = document.getElementById('baEtaBar');
  if (!bar) return;

  const { post_limit_per_subreddit } = baGetSettings();
  const numSubs  = baTags.length;

  if (numSubs === 0) {
    bar.innerHTML  = '⏱ Add subreddits above to see time estimate.';
    bar.className  = 'ba-eta-bar';
    return;
  }

  const totalPosts = post_limit_per_subreddit * numSubs;
  const etaSecs    = totalPosts * BA_SECS_PER_POST;
  const etaHrs     = Math.floor(etaSecs / 3600);
  const etaMins    = Math.round((etaSecs % 3600) / 60);
  const etaStr     = etaHrs > 0 ? `~${etaHrs}h ${etaMins}m` : `~${etaMins} min`;

  const estFiles   = Math.ceil(totalPosts / 200);

  let cls  = 'ba-eta-bar';
  let warn = '';
  if (etaSecs > 5 * 3600) {
    cls  += ' ba-eta-danger';
    warn  = '<div class="ba-eta-warn">⚠ This is a very large extraction. Consider reducing posts per subreddit or splitting into separate jobs.</div>';
  } else if (etaSecs > 2 * 3600) {
    cls  += ' ba-eta-caution';
  }

  bar.className = cls;
  bar.innerHTML = `⏱ Estimated time: <strong>${etaStr}</strong>
    <span class="ba-eta-detail">(${totalPosts.toLocaleString()} posts × ${numSubs} subreddit${numSubs>1?'s':''} at ~${BA_SECS_PER_POST}s/post · ~${estFiles} DOCX file${estFiles>1?'s':''})</span>
    ${warn}`;
}

// ── Confirm modal ─────────────────────────────────────────────────────────────
function baConfirmStart() {
  if (baTags.length === 0) { baShowAlert('Add at least one subreddit.'); return; }

  const { years_back, post_limit_per_subreddit, comment_sort, comment_limit } = baGetSettings();
  const totalPosts = post_limit_per_subreddit * baTags.length;
  const etaSecs    = totalPosts * BA_SECS_PER_POST;
  const etaHrs     = Math.floor(etaSecs / 3600);
  const etaMins    = Math.round((etaSecs % 3600) / 60);
  const etaStr     = etaHrs > 0 ? `~${etaHrs} hours ${etaMins} min` : `~${etaMins} min`;
  const estFiles   = Math.ceil(totalPosts / 200);
  const yrsLabel   = years_back < 1 ? `${Math.round(years_back*12)} months` : `${years_back} year${years_back>1?'s':''}`;

  document.getElementById('baConfirmBody').innerHTML = `
    <div class="ba-confirm-list">
      <div class="ba-confirm-row"><span>📋 Subreddits:</span><strong>${baTags.map(t=>'r/'+t).join(', ')}</strong></div>
      <div class="ba-confirm-row"><span>📦 Posts total (max):</span><strong>Up to ${totalPosts.toLocaleString()}</strong></div>
      <div class="ba-confirm-row"><span>📅 Time range:</span><strong>Past ${yrsLabel}</strong></div>
      <div class="ba-confirm-row"><span>💬 Comments:</span><strong>${comment_limit} per post · ${comment_sort} sort</strong></div>
      <div class="ba-confirm-row"><span>⏱ Estimated time:</span><strong>${etaStr}</strong></div>
      <div class="ba-confirm-row"><span>📄 Estimated DOCX files:</span><strong>~${estFiles} file${estFiles>1?'s':''}</strong></div>
    </div>
    <p class="ba-confirm-note">This will run in the background. You can close this tab and come back later.</p>`;

  document.getElementById('baConfirmModal').style.display = 'flex';
}

function baCloseConfirm() {
  document.getElementById('baConfirmModal').style.display = 'none';
}

async function baStartJob() {
  baCloseConfirm();
  const { years_back, post_limit_per_subreddit, comment_sort, comment_limit } = baGetSettings();

  let climit = comment_limit === 'all' ? 'all' : parseInt(comment_limit);

  try {
    const resp = await fetch('/bulk-extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subreddits:               baTags,
        comment_sort,
        comment_limit:            climit,
        post_limit_per_subreddit: Math.min(post_limit_per_subreddit, 1000),
        years_back:               Math.min(years_back, 5),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Failed to start job');

    baJobId = data.job_id;
    baShowProgress();
    baPollStatus();
  } catch (err) {
    baShowAlert(`Could not start job: ${err.message}`);
  }
}

// ── Polling ───────────────────────────────────────────────────────────────────
function baPollStatus() {
  if (baPollingId) clearInterval(baPollingId);
  baPollingId = setInterval(baFetchStatus, 3000);
  baFetchStatus();   // immediate first call
}

async function baFetchStatus() {
  if (!baJobId) return;
  try {
    const resp = await fetch(`/bulk-extract/status/${baJobId}`);
    if (!resp.ok) return;
    const data = await resp.json();
    baUpdateProgress(data);

    if (data.status === 'complete' || data.status === 'error') {
      clearInterval(baPollingId);
      baPollingId = null;
      if (data.status === 'complete') baShowDownloads(data);
      else baShowAlert(`Job failed: ${data.error || 'Unknown error'}`);
    }
  } catch (_) {}
}

// ── Progress UI ───────────────────────────────────────────────────────────────
function baShowProgress() {
  document.getElementById('baProgressCard').style.display = '';
  document.getElementById('baDownloadsCard').style.display = 'none';
  baRenderSubRows();
}

function baRenderSubRows() {
  const container = document.getElementById('baSubRows');
  if (!container) return;
  container.innerHTML = baTags.map(name =>
    `<div class="ba-sub-row" id="baRow_${name}">
       <div class="ba-sub-name">r/${name}</div>
       <div class="ba-sub-right">
         <div class="ba-sub-bar-wrap"><div class="ba-sub-bar-fill" id="baFill_${name}" style="width:0%"></div></div>
         <div class="ba-sub-info" id="baInfo_${name}">⏳ Queued</div>
       </div>
     </div>`
  ).join('');
}

const BA_PHASE_LABELS = {
  scanning_posts:  '🔍 Scanning posts',
  extracting_posts:'📥 Extracting posts',
  writing_docx:    '✍️ Writing DOCX',
  complete:        '✅ Complete',
  error:           '❌ Error',
};

function baUpdateProgress(data) {
  // Overall bar
  const pct = data.percent || 0;
  const fill = document.getElementById('baMainFill');
  if (fill) fill.style.width = pct + '%';

  const pctEl = document.getElementById('baPercent');
  if (pctEl) pctEl.textContent = pct.toFixed(1) + '%';

  const tp = document.getElementById('baTotalPosts');
  if (tp) tp.textContent = `${(data.total_posts_done||0).toLocaleString()} / ${(data.total_posts_all_subs||0).toLocaleString()} posts extracted`;

  const tc = document.getElementById('baTotalComments');
  if (tc) tc.textContent = `${(data.total_comments_done||0).toLocaleString()} comments`;

  const eta = document.getElementById('baEtaRemaining');
  if (eta) {
    if (data.status === 'generating' || data.status === 'complete') {
      eta.textContent = data.status === 'complete' ? 'Done!' : '✍️ Generating DOCX…';
    } else {
      const m = data.eta_minutes || 0;
      eta.textContent = m > 60
        ? `ETA: ~${Math.floor(m/60)}h ${m%60}m remaining`
        : `ETA: ~${m} min remaining`;
    }
  }

  // Per-subreddit rows
  const cur  = data.current_subreddit || '';
  const done = data.posts_done_this_sub || 0;
  const tot  = data.total_posts_this_sub || 0;
  const phase = data.phase || '';

  baTags.forEach(name => {
    const row  = document.getElementById(`baRow_${name}`);
    const bar  = document.getElementById(`baFill_${name}`);
    const info = document.getElementById(`baInfo_${name}`);
    if (!row) return;

    if (data.status === 'complete') {
      row.classList.add('ba-row-done');
      if (bar)  bar.style.width = '100%';
      if (info) info.textContent = '✅ Complete';
    } else if (name === cur) {
      row.classList.add('ba-row-active');
      const rowPct = tot > 0 ? (done / tot * 100).toFixed(1) : 0;
      if (bar)  bar.style.width = rowPct + '%';
      const phaseLabel = BA_PHASE_LABELS[phase] || phase;
      if (info) info.textContent = `${phaseLabel}  ${done.toLocaleString()}/${tot.toLocaleString()}`;
    } else if (data.subreddits_skipped && data.subreddits_skipped.some(s => s.name === name)) {
      const skip = data.subreddits_skipped.find(s => s.name === name);
      if (info) info.textContent = `❌ Skipped: ${skip.reason}`;
      row.classList.add('ba-row-skip');
    }
    // else: still queued — leave as-is
  });

  // Log
  const logBox = document.getElementById('baLogBox');
  if (logBox && Array.isArray(data.log) && data.log.length) {
    logBox.innerHTML = data.log.map(l => `<div>${baEsc(l)}</div>`).join('');
    logBox.scrollTop = logBox.scrollHeight;
  }
  // Also show substatus as latest log entry
  if (logBox && data.substatus) {
    const last = document.createElement('div');
    last.textContent = data.substatus;
    last.className = 'ba-log-live';
    if (!logBox.querySelector('.ba-log-live')) logBox.appendChild(last);
    else logBox.querySelector('.ba-log-live').textContent = data.substatus;
    logBox.scrollTop = logBox.scrollHeight;
  }
}

// ── Downloads panel ───────────────────────────────────────────────────────────
function baShowDownloads(data) {
  document.getElementById('baProgressCard').style.display = 'none';
  document.getElementById('baDownloadsCard').style.display = '';

  const stats = document.getElementById('baDoneStats');
  if (stats) {
    const mins = data.time_taken_minutes || '?';
    stats.textContent = `Extracted ${(data.total_posts_extracted||0).toLocaleString()} posts and ${(data.total_comments_extracted||0).toLocaleString()} comments across ${data.total_subreddits||baTags.length} subreddit(s) in ${mins} minutes.`;
  }

  const skipWarn = document.getElementById('baSkippedWarn');
  if (skipWarn && data.subreddits_skipped && data.subreddits_skipped.length) {
    skipWarn.style.display = '';
    skipWarn.innerHTML = `⚠ ${data.subreddits_skipped.length} subreddit${data.subreddits_skipped.length>1?'s':''} skipped: ` +
      data.subreddits_skipped.map(s => `r/${s.name} (${s.reason})`).join(', ');
  }

  const fileList = document.getElementById('baFileList');
  if (fileList && data.download_urls) {
    fileList.innerHTML = data.download_urls.map((url, i) => {
      const fname = (data.docx_files && data.docx_files[i])
        ? data.docx_files[i].split(/[\\/]/).pop()
        : `archive_file_${i+1}.docx`;
      return `<a class="ba-dl-btn" href="${url}" download="${fname}">
        <span class="ba-dl-icon">📥</span>
        <span class="ba-dl-name">${baEsc(fname)}</span>
      </a>`;
    }).join('');
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function baShowAlert(msg) {
  const el = document.getElementById('baAlert');
  if (!el) return;
  el.textContent = msg;
  el.style.display = '';
  setTimeout(() => { el.style.display = 'none'; }, 8000);
}

function baRestart() {
  baTags      = [];
  baJobId     = null;
  if (baPollingId) { clearInterval(baPollingId); baPollingId = null; }
  baRenderTags();
  document.getElementById('baProgressCard').style.display  = 'none';
  document.getElementById('baDownloadsCard').style.display = 'none';
  document.getElementById('baAlert').style.display         = 'none';
  document.getElementById('baPasteArea').value             = '';
}

function baEsc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Wire settings change listeners ────────────────────────────────────────────
function baWireSettingsListeners() {
  document.querySelectorAll('input[name="baYears"], input[name="baLimit"]').forEach(el => {
    el.addEventListener('change', baUpdateEta);
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  baInitTagInput();
  baWireSettingsListeners();
  baUpdateEta();
});
