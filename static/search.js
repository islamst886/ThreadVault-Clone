/* search.js — Tab 1: Search Posts + Research Dashboard */
'use strict';

(() => {
const API = '';

/* ── Shared utils (also used by explorer.js via window.*) ────────────────── */
function esc(s){ if(!s) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function fmtNum(n){
    if(n==null||n===undefined) return '—';
    n=Number(n);
    if(n>=1000000) return (n/1000000).toFixed(1).replace(/\.0$/,'')+'M';
    if(n>=1000)    return (n/1000).toFixed(1).replace(/\.0$/,'')+'K';
    return n.toLocaleString();
}

function ageLabel(days){
    if(!days) return '—';
    if(days<30)  return days+'d';
    if(days<365) return Math.floor(days/30)+'mo';
    const y=Math.floor(days/365), mo=Math.floor((days%365)/30);
    return mo>0?`${y}yr ${mo}mo`:`${y}yr`;
}

function sizeClass(members){
    if(members>=10000000) return 'massive';
    if(members>=1000000)  return 'large';
    if(members>=100000)   return 'medium';
    if(members>=10000)    return 'small';
    if(members>=1000)     return 'micro';
    return 'tiny';
}

function sizeLabelText(m){
    return {massive:'Massive',large:'Large',medium:'Medium',small:'Small',micro:'Micro',tiny:'Tiny'}[sizeClass(m)]||'Tiny';
}

/* ── Tab switching ───────────────────────────────────────────────────────── */
function switchTab(name){
    // Set display:none on every panel (overcomes any inline style="" on the element)
    document.querySelectorAll('.tab-panel').forEach(p => {
        p.classList.remove('active');
        p.style.display = 'none';
    });
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

    const cap   = name.charAt(0).toUpperCase() + name.slice(1);
    const panel = document.getElementById('tab' + cap);
    const btn   = document.getElementById('tabBtn' + cap);
    if (panel) { panel.classList.add('active'); panel.style.display = ''; }
    if (btn)     btn.classList.add('active');

    // Lazy-load explorer on first visit
    if (name === 'explorer') window._explorerInit?.();
}
window.switchTab = switchTab;

/* ── Search tab state ────────────────────────────────────────────────────── */
let currentJobId = null;
let pollInterval = null;
let logs         = [];
let activeFilter = null;

function showS(el){ document.querySelectorAll('#tabSearch .section').forEach(s=>s.classList.remove('active')); el.classList.add('active'); }
function showError(m){ const a=document.getElementById('errorAlert'); a.textContent=m; a.style.display='block'; }
function showWarn(m) { const a=document.getElementById('warningAlert'); a.textContent=m; a.style.display='block'; }
function showInfo(m) { const a=document.getElementById('infoAlert'); a.textContent=m; a.style.display='block'; }
function hideAlerts(){ ['errorAlert','warningAlert','infoAlert'].forEach(id=>document.getElementById(id).style.display='none'); }
window.showInfo = showInfo;  // used by explorer.js handoff

function addLog(m){
    const t=new Date().toLocaleTimeString();
    logs.push(`[${t}] ${m}`);
    if(logs.length>6) logs.shift();
    const la=document.getElementById('logArea');
    la.innerHTML=logs.map(l=>`<div class="log-line">${l}</div>`).join('');
    la.scrollTop=la.scrollHeight;
}

/* ── Form wiring ─────────────────────────────────────────────────────────── */
document.getElementById('limitInput').addEventListener('change', e=>{
    document.getElementById('limitWarning').style.display = e.target.value==='all'?'block':'none';
});

document.getElementById('searchForm').addEventListener('submit', async e=>{
    e.preventDefault();
    hideAlerts();
    const query    = document.getElementById('query').value.trim();
    const maxPages = parseInt(document.getElementById('maxPages').value, 10);
    const sortMode = document.getElementById('sortInput').value;
    const limitRaw = document.getElementById('limitInput').value;
    const limitVal = limitRaw==='all' ? 'all' : parseInt(limitRaw, 10);
    if(!query) return;

    const btnSubmit = document.getElementById('btnSubmit');
    btnSubmit.disabled=true; btnSubmit.textContent='Starting…';

    try{
        const res = await fetch(`${API}/search`, {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({ query, max_pages:maxPages, sort:sortMode, limit:limitVal }),
        });
        if(!res.ok){ const err=await res.json(); throw new Error(err.detail||`Error ${res.status}`); }
        const data = await res.json();
        currentJobId=data.job_id; logs=[]; addLog(`Job: ${currentJobId}`);
        showS(document.getElementById('progressSection'));
        startPolling();
    } catch(err){
        showError('Failed to start: '+err.message);
        btnSubmit.disabled=false; btnSubmit.textContent='Start Research';
    }
});

/* ── Polling ─────────────────────────────────────────────────────────────── */
function startPolling(){ stopPolling(); pollStatus(); pollInterval=setInterval(pollStatus,2000); }
function stopPolling(){ if(pollInterval!==null){ clearInterval(pollInterval); pollInterval=null; } }

document.addEventListener('visibilitychange', ()=>{
    if(!document.hidden && currentJobId && pollInterval!==null) pollStatus();
});

async function pollStatus(){
    if(!currentJobId) return;
    try{
        const res  = await fetch(`${API}/status/${currentJobId}`);
        if(!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        updateProgressUI(data);
        if(['complete','error','blocked'].includes(data.status)){ stopPolling(); finishJob(data); }
    } catch(err){ addLog('Warning: '+err.message); }
}

function updateProgressUI(d){
    const pb=document.getElementById('progressBar'), st=document.getElementById('statusText');
    let msg='';
    if(d.status==='queued')      msg='Job queued…';
    else if(d.status==='crawling')    msg=d.urls_found>0?`Scanning Google… found ${d.urls_found} URLs`:'Scanning Google…';
    else if(d.status==='extracting'){ msg=`Extracting post ${d.posts_done} of ${d.total_posts}…`; if(d.substatus) msg+=`<br><span style="color:#f97316">⚠ ${d.substatus}</span>`; pb.style.width=`${d.percent}%`; }
    else if(d.status==='analyzing'){ msg=d.substatus?`🤖 ${d.substatus}`:'🤖 Running AI analysis…'; pb.style.width='90%'; }
    else if(d.status==='generating'){ msg=d.substatus||'Compiling DOCX…'; pb.style.width='97%'; }
    else if(d.status==='complete'){   msg='Finished!'; pb.style.width='100%'; }
    if(msg!==st.innerHTML){ st.innerHTML=msg; addLog(msg.replace(/<[^>]*>/gm,'')); }
}


// ── Download helper — fetch → Blob → blob: URL, triggered outside the DOM.
// Resets form dirty state so Chrome's built-in unload guard never fires.
async function _triggerDownload(url){
    try {
        const btnDL = document.getElementById('btnDownload');
        const origText = btnDL.textContent;
        btnDL.textContent = '⏳ Preparing…';
        btnDL.disabled = true;

        // Clear form dirty flag: Chrome shows "Leave site?" when an input
        // has been typed into and an anchor click is detected. Resetting
        // the form removes the dirty flag silently.
        const form = document.getElementById('searchForm');
        const savedQuery    = document.getElementById('query')?.value || '';
        const savedMaxPages = document.getElementById('maxPages')?.value || '30';
        const savedSort     = document.getElementById('sortInput')?.value || 'best';
        const savedLimit    = document.getElementById('limitInput')?.value || 'all';
        if(form) form.reset();
        // Restore field values after reset so the UI looks unchanged
        if(document.getElementById('query'))    document.getElementById('query').value    = savedQuery;
        if(document.getElementById('maxPages')) document.getElementById('maxPages').value = savedMaxPages;
        if(document.getElementById('sortInput'))document.getElementById('sortInput').value= savedSort;
        if(document.getElementById('limitInput'))document.getElementById('limitInput').value= savedLimit;

        const response = await fetch(url);
        if(!response.ok) throw new Error(`Server error ${response.status}`);

        // Read filename from Content-Disposition if the server sends it
        const cd = response.headers.get('Content-Disposition') || '';
        const fnMatch = cd.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
        const filename = fnMatch ? fnMatch[1].replace(/['"]/g,'') : 'ThreadVault_report.docx';

        const blob    = await response.blob();
        const blobUrl = URL.createObjectURL(blob);

        // Build anchor but never attach it to the DOM — avoids all Chrome guards
        const a    = document.createElement('a');
        a.href     = blobUrl;
        a.download = filename;
        // Use a MouseEvent so it behaves like a real click
        a.dispatchEvent(new MouseEvent('click', { bubbles: false, cancelable: false, view: window }));

        setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);

        btnDL.textContent = '✅ Downloaded!';
        btnDL.disabled    = false;
        setTimeout(() => { btnDL.textContent = origText; }, 3000);

    } catch(err) {
        const btnDL = document.getElementById('btnDownload');
        btnDL.textContent = '⬇ Download DOCX';
        btnDL.disabled    = false;
        console.error('Download failed:', err);
    }
}

// State: has user downloaded at least once?
let _downloadedOnce = false;
let _pendingDownloadUrl = '';

// Show the custom modal (replaces browser beforeunload)
function _showLeaveModal(downloadUrl, onLeave){
    const overlay = document.getElementById('leaveModalOverlay');
    overlay.style.display = 'flex';

    const dlBtn    = document.getElementById('modalDownloadBtn');
    const leaveBtn = document.getElementById('modalLeaveBtn');

    // Clone to clear old listeners
    const dlBtnNew    = dlBtn.cloneNode(true);
    const leaveBtnNew = leaveBtn.cloneNode(true);
    dlBtn.replaceWith(dlBtnNew);
    leaveBtn.replaceWith(leaveBtnNew);

    document.getElementById('modalDownloadBtn').onclick = () => {
        overlay.style.display = 'none';
        _triggerDownload(downloadUrl);
        _unlockReset();
    };
    document.getElementById('modalLeaveBtn').onclick = () => {
        overlay.style.display = 'none';
        _unlockReset();
        if(onLeave) onLeave();
    };
}

function _unlockReset(){
    _downloadedOnce = true;
    const btnReset = document.getElementById('btnReset');
    btnReset.disabled = false;
    btnReset.title = '';
}

async function finishJob(d){
    if(d.status==='complete'){
        document.getElementById('resultIcon').textContent='✅';
        document.getElementById('resultHeadline').textContent='Research Complete';
        const succeeded=d.posts_done??d.total_posts, total=d.total_posts, skipped=total-succeeded;
        document.getElementById('resultStats').textContent=`Extracted ${succeeded} of ${total} posts.`+(skipped>0?` (${skipped} skipped)`:'');
        if(d.warning) showWarn(d.warning);

        const btnDL    = document.getElementById('btnDownload');
        const btnReset = document.getElementById('btnReset');

        _downloadedOnce = false;
        _pendingDownloadUrl = `${API}${d.download_url}`;

        // Keep reset enabled but intercept via modal if not yet downloaded
        btnReset.disabled = false;
        btnReset.title = '';

        btnDL.style.display = 'block';
        btnDL.onclick = () => {
            // Download via hidden <a> — no page navigation, no beforeunload
            _triggerDownload(_pendingDownloadUrl);
            _unlockReset();
        };

        showS(document.getElementById('resultSection'));
        await loadDashboard(currentJobId);
    } else if(d.status==='blocked'){
        document.getElementById('resultIcon').textContent='⚠️';
        document.getElementById('resultHeadline').textContent='Blocked by Google';
        document.getElementById('resultStats').textContent='Google returned 0 results — likely CAPTCHA.';
        showWarn(d.warning||'Google CAPTCHA detected.');
        document.getElementById('btnDownload').style.display='none';
        document.getElementById('dashLoader').textContent='No posts to analyse.';
        showS(document.getElementById('resultSection'));
    } else if(d.status==='error'){
        showError('Job failed: '+d.error);
        showS(document.getElementById('searchSection'));
        const b=document.getElementById('btnSubmit'); b.disabled=false; b.textContent='Start Research';
    }
}

function _doReset(){
    stopPolling(); currentJobId=null; activeFilter=null; logs=[];
    document.getElementById('searchForm').reset();
    document.getElementById('maxPages').value=30;
    hideAlerts();
    document.getElementById('progressBar').style.width='0%';
    document.getElementById('statusText').textContent='Initialising…';
    const b=document.getElementById('btnSubmit'); b.disabled=false; b.textContent='Start Research';
    document.getElementById('btnDownload').style.display='none';
    document.getElementById('dashLoader').style.display='block';
    document.getElementById('dashLoader').textContent='Loading insights…';
    document.getElementById('dashContent').style.display='none';
    _downloadedOnce=false;
    showS(document.getElementById('searchSection'));
}

document.getElementById('btnReset').addEventListener('click', ()=>{
    // If there's a pending download not yet done, show the custom modal instead
    if(!_downloadedOnce && _pendingDownloadUrl){
        _showLeaveModal(_pendingDownloadUrl, _doReset);
        return;
    }
    _doReset();
});

/* ── Research Dashboard ──────────────────────────────────────────────────── */
const CAT_COLORS = {
    'pain-point':'#ef4444','solution-request':'#3b82f6','money-talk':'#22c55e',
    'positive-experience':'#10b981','negative-experience':'#f97316',
    'hot-discussion':'#ec4899','question':'#8b5cf6','news-update':'#14b8a6','other':'#6b7280',
};

function catSlug(c){ return (c||'other').toLowerCase().replace(/[^a-z ]/g,'').trim().replace(/ +/g,'-'); }

async function loadDashboard(jobId){
    const dl=document.getElementById('dashLoader'), dc=document.getElementById('dashContent');
    dl.style.display='block'; dc.style.display='none';
    let posts;
    try{
        const r=await fetch(`${API}/results/${jobId}`);
        if(!r.ok) throw new Error();
        const b=await r.json();
        posts=b.posts||[];
    } catch(e){ dl.textContent='Could not load dashboard.'; return; }

    const analysed=posts.filter(p=>p.ai_analysis&&typeof p.ai_analysis==='object');
    if(!analysed.length){ dl.textContent='No AI analysis data available.'; return; }
    dl.style.display='none'; dc.style.display='block';
    buildCategoryChart(analysed);
    buildList('painList',    analysed, p=>p.ai_analysis.pain_points||[],     'No pain points.');
    buildList('productList', analysed, p=>p.ai_analysis.mentioned_products||[],'No products.');
    buildList('solutionList',analysed, p=>p.ai_analysis.solution_requests||[], 'No requests.');
    buildWTP(analysed);
    buildPostCards(posts);
}

function buildCategoryChart(posts){
    const counts={};
    posts.forEach(p=>{ const c=p.ai_analysis.category||'Other'; counts[c]=(counts[c]||0)+1; });
    const sorted=Object.entries(counts).sort((a,b)=>b[1]-a[1]), max=sorted[0]?.[1]||1;
    const el=document.getElementById('chartBars'); el.innerHTML='';
    sorted.forEach(([cat,count])=>{
        const slug=catSlug(cat), color=CAT_COLORS[slug]||'#6b7280', pct=Math.round(count/max*100);
        const row=document.createElement('div'); row.className='chart-bar-row'; row.dataset.cat=cat;
        row.innerHTML=`<div class="chart-bar-label">${cat}</div><div class="chart-bar-track"><div class="chart-bar-fill" style="width:0%;background:${color}"></div></div><div class="chart-bar-count">${count}</div>`;
        row.addEventListener('click', ()=>{
            activeFilter=activeFilter===cat?null:cat;
            document.querySelectorAll('.chart-bar-row').forEach(r=>r.classList.toggle('active',r.dataset.cat===activeFilter&&activeFilter!==null));
            applyPostFilter();
        });
        el.appendChild(row);
        requestAnimationFrame(()=>requestAnimationFrame(()=>row.querySelector('.chart-bar-fill').style.width=pct+'%'));
    });
}

function buildList(id, posts, getter, empty){
    const freq={};
    posts.forEach(p=>(getter(p)||[]).forEach(i=>{ const k=(i||'').trim(); if(k) freq[k]=(freq[k]||0)+1; }));
    const sorted=Object.entries(freq).sort((a,b)=>b[1]-a[1]);
    const el=document.getElementById(id); el.innerHTML='';
    if(!sorted.length){ el.innerHTML=`<div class="empty-state">${empty}</div>`; return; }
    sorted.forEach(([text,count])=>{
        const d=document.createElement('div'); d.className='list-item';
        d.innerHTML=`<span class="list-item-text">• ${esc(text)}</span><span class="list-item-count">${count>1?count+' posts':'1 post'}</span>`;
        el.appendChild(d);
    });
}

function buildWTP(posts){
    const total=posts.length;
    const count=posts.filter(p=>p.ai_analysis.willingness_to_pay===true).length;
    const pct=total>0?Math.round(count/total*100):0;
    const strong=pct>20;
    const panel=document.getElementById('wtpPanel');
    if(strong) panel.classList.add('signal-strong');
    document.getElementById('wtpContent').innerHTML=
        `<div class="wtp-stat"><span class="wtp-num">${count}</span><span class="wtp-denom"> / ${total}</span></div>`+
        `<div class="wtp-detail">posts mention pricing or budget</div>`+
        `<div class="wtp-detail" style="margin-top:5px;font-size:.9rem;font-weight:700">${pct}%</div>`+
        `<div><span class="wtp-badge ${strong?'wtp-strong':'wtp-weak'}">${strong?'✅ Strong signal':'💬 Low signal'}</span></div>`;
}

function buildPostCards(posts){
    const cats=[...new Set(posts.filter(p=>p.ai_analysis?.category).map(p=>p.ai_analysis.category))].sort();
    const pillRow=document.getElementById('filterPills'); pillRow.innerHTML='';
    const allPill=document.createElement('button'); allPill.className='filter-pill active'; allPill.textContent='All'; allPill.dataset.cat='';
    allPill.addEventListener('click', ()=>{
        activeFilter=null;
        document.querySelectorAll('.filter-pill').forEach(p=>p.classList.remove('active'));
        allPill.classList.add('active');
        document.querySelectorAll('.chart-bar-row').forEach(r=>r.classList.remove('active'));
        applyPostFilter();
    });
    pillRow.appendChild(allPill);
    cats.forEach(cat=>{
        const pill=document.createElement('button'); pill.className='filter-pill'; pill.textContent=cat; pill.dataset.cat=cat;
        pill.addEventListener('click', ()=>{
            activeFilter=cat;
            document.querySelectorAll('.filter-pill').forEach(p=>p.classList.remove('active'));
            pill.classList.add('active');
            document.querySelectorAll('.chart-bar-row').forEach(r=>r.classList.toggle('active',r.dataset.cat===cat));
            applyPostFilter();
        });
        pillRow.appendChild(pill);
    });

    const container=document.getElementById('postCards'); container.innerHTML='';
    posts.forEach(post=>{
        const info=post.post||{}, ai=post.ai_analysis||{};
        const cat=ai.category||'Other', slug=catSlug(cat);
        const sent=(ai.sentiment||'neutral').toLowerCase();
        const title=info.title||'(untitled)', url=info.url||'#';
        const summary=ai.summary||'', kq=ai.key_quote||'';
        const card=document.createElement('div'); card.className='post-card'; card.dataset.cat=cat;
        card.innerHTML=
            `<div class="post-card-top"><div class="post-card-badges"><span class="badge badge-${slug}">${esc(cat)}</span><span class="badge badge-sentiment-${sent}">${sent}</span></div></div>`+
            `<div class="post-card-title"><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a></div>`+
            (summary?`<div class="post-card-summary">${esc(summary)}</div>`:'')+
            (kq?`<div class="post-card-quote">"${esc(kq)}"</div>`:'');
        container.appendChild(card);
    });
    updatePostCount(posts.length, posts.length);
}

function applyPostFilter(){
    const cards=document.querySelectorAll('.post-card'); let visible=0;
    cards.forEach(c=>{ const show=!activeFilter||c.dataset.cat===activeFilter; c.classList.toggle('hidden',!show); if(show) visible++; });
    document.querySelectorAll('.filter-pill').forEach(p=>p.classList.toggle('active',p.dataset.cat===''?!activeFilter:p.dataset.cat===activeFilter));
    updatePostCount(visible, cards.length);
}

function updatePostCount(v,t){
    const el=document.getElementById('postCount');
    if(el) el.textContent=activeFilter?`${v} of ${t} posts`:`${t} posts`;
}

/* ── expose showS for explorer.js handoff ───────────────────────────────── */
window._showSearchSection = ()=>showS(document.getElementById('searchSection'));
})();
