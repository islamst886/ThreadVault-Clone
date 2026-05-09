/* explorer.js — Tab 2: Community Explorer */
'use strict';

(() => {

/* ── Utilities ───────────────────────────────────────────────────────────── */
const API = '';

function esc(s){ if(!s) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmtNum(n){
    if(n==null) return '—'; n=Number(n);
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
function sizeClass(m){
    if(m>=10000000) return 'massive'; if(m>=1000000) return 'large';
    if(m>=100000)   return 'medium';  if(m>=10000)   return 'small';
    if(m>=1000)     return 'micro';   return 'tiny';
}
function sizeLabelText(m){
    return {massive:'Massive',large:'Large',medium:'Medium',small:'Small',micro:'Micro',tiny:'Tiny'}[sizeClass(m)]||'Tiny';
}

/* ── State ───────────────────────────────────────────────────────────────── */
let allCommunities = [];
let compareSet     = {};
let currentView    = 'list';
let activeES       = null;
let growingTimeframe = 'monthly'; // weekly | monthly | yearly

// Context for the current fetch session
let ctx = {
    mode:       'popular',  // 'popular' | 'growing' | 'search'
    query:      '',
    cursor:     null,       // Reddit `after` cursor
    exhausted:  false,      // true when no more Reddit pages
    // Active filter params sent to the backend
    min_members: null,
    max_members: null,
    min_active:  null,
    max_active:  null,
};

/* ── Size/activity → filter params mapping ───────────────────────────────── */
const SIZE_MAP = {
    '':     {},
    '1m':   { min_members: 1000000 },
    '100k': { min_members: 100000,  max_members: 999999 },
    '10k':  { min_members: 10000,   max_members: 99999  },
    '1k':   { min_members: 1000,    max_members: 9999   },
    '100':  { min_members: 100,     max_members: 999    },
    'tiny': { max_members: 99 },
};
const ACT_MAP = {
    '':       {},
    'hot':    { min_active: 10000 },
    'high':   { min_active: 1000, max_active: 9999 },
    'active': { min_active: 100,  max_active: 999  },
    'slow':   { min_active: 10,   max_active: 99   },
    'quiet':  { max_active: 9 },
};

function getFilterParams(){
    const size = document.getElementById('ctrlSize').value;
    const act  = document.getElementById('ctrlActivity').value;
    return { ...(SIZE_MAP[size]||{}), ...(ACT_MAP[act]||{}) };
}

/* ── URL builder ─────────────────────────────────────────────────────────── */
function buildURL(after=null){
    const p = new URLSearchParams({ target: 300 });
    if (after) p.set('after', after);
    const f = getFilterParams();
    if (f.min_members != null) p.set('min_members', f.min_members);
    if (f.max_members != null) p.set('max_members', f.max_members);
    if (f.min_active  != null) p.set('min_active',  f.min_active);
    if (f.max_active  != null) p.set('max_active',  f.max_active);

    if (ctx.mode === 'growing') return `${API}/communities/growing/stream?target=500`;
    if (ctx.mode === 'search')  { p.set('q', ctx.query); return `${API}/communities/search/stream?${p}`; }
    return `${API}/communities/popular/stream?${p}`;   // popular / all
}

/* ── DOM refs ────────────────────────────────────────────────────────────── */
const explorerQuery = document.getElementById('explorerQuery');
const communityList = document.getElementById('communityList');
const communityGrid = document.getElementById('communityGrid');
const explCount     = document.getElementById('explCount');
const explSkeleton  = document.getElementById('explSkeleton');
const explEmpty     = document.getElementById('explEmpty');
const explErrAlert  = document.getElementById('explErrAlert');
const explProgress  = document.getElementById('explProgress');
const explProgFill  = document.getElementById('explProgFill');
const explProgLabel = document.getElementById('explProgLabel');

/* ── Tag / avatar helpers ────────────────────────────────────────────────── */
const AVATAR_COLORS=['#6366f1','#3b82f6','#0ea5e9','#14b8a6','#22c55e','#f59e0b','#ef4444','#8b5cf6','#ec4899'];
function avatarColor(name){ let h=0; for(let i=0;i<name.length;i++) h=(h+name.charCodeAt(i))%AVATAR_COLORS.length; return AVATAR_COLORS[h]; }
function tagClass(t){ return 'tag-'+t.toLowerCase().replace(/\s+/g,'-').replace(/[^a-z0-9-]/g,''); }
function renderTags(tags){ if(!tags?.length) return ''; return tags.map(t=>`<span class="tag ${tagClass(t)}">${esc(t)}</span>`).join(''); }

/* ── View toggle ─────────────────────────────────────────────────────────── */
function setView(v){
    currentView=v;
    document.getElementById('viewList').classList.toggle('active',v==='list');
    document.getElementById('viewCard').classList.toggle('active',v==='card');
    communityList.style.display=v==='list'?'flex':'none';
    communityGrid.style.display=v==='card'?'grid':'none';
}
window.setView = setView;

/* ── Quick-filter pill highlighter ──────────────────────────────────────── */
function setQF(id){
    document.querySelectorAll('.qf-btn').forEach(b=>b.classList.remove('active'));
    const el=document.getElementById(id);
    if(el) el.classList.add('active');
}

/* ── "Load More" button — lives at the BOTTOM of the list ───────────────── */
function refreshLoadMoreButton(){
    // Remove any existing button first
    document.getElementById('inlineLoadMore')?.remove();

    if(activeES) return;   // still loading
    if(ctx.exhausted || !ctx.cursor) return;

    const btn=document.createElement('div');
    btn.id='inlineLoadMore';
    btn.className='load-more-bar';
    btn.innerHTML=`
        <button class="btn btn-ghost" onclick="loadMore()">⬇ Load 300 More Communities</button>
        <span style="color:var(--text-dim);font-size:.78rem">${allCommunities.length} loaded so far · applying same filters</span>`;
    // Insert after the results div
    const results=document.getElementById('explResults');
    results.insertAdjacentElement('afterend', btn);
}

function removeLoadMoreButton(){
    document.getElementById('inlineLoadMore')?.remove();
}

/* ── Loading state helpers ───────────────────────────────────────────────── */
function showLoadingState(target, append){
    explErrAlert.style.display='none';
    if(!append){
        explSkeleton.style.display='block';
        explEmpty.style.display='none';
        communityList.innerHTML='';
        communityGrid.innerHTML='';
        explCount.textContent='';
    }
    explProgress.classList.add('visible');
    explProgFill.style.width='0%';
    explProgLabel.textContent=`Loading… (0 / ${target})`;
    removeLoadMoreButton();
}

function hideLoadingState(){
    explSkeleton.style.display='none';
    explProgress.classList.remove('visible');
}

function closeES(){ if(activeES){ activeES.close(); activeES=null; } }

/* ── SSE streaming connection ────────────────────────────────────────────── */
function connectSSE(url, append=false){
    closeES();
    if(!append){ allCommunities=[]; ctx.cursor=null; ctx.exhausted=false; }

    activeES=new EventSource(url);

    activeES.onmessage=(e)=>{
        let msg; try{ msg=JSON.parse(e.data); }catch{ return; }

        if(msg.type==='start'){
            showLoadingState(msg.target||300, append);
            return;
        }
        if(msg.type==='batch'){
            allCommunities.push(...msg.results);
            explSkeleton.style.display='none';
            const pct=Math.min(Math.round(msg.loaded/msg.target*100),100);
            explProgFill.style.width=pct+'%';
            explProgLabel.textContent=`Loading… (${msg.loaded} / ${msg.target})`;
            appendToDOM(msg.results);
            updateCount();
            return;
        }
        if(msg.type==='done'){
            hideLoadingState();
            activeES.close(); activeES=null;
            ctx.cursor    = msg.after||null;
            ctx.exhausted = !msg.after;
            if(allCommunities.length===0) showEmpty();
            else sortLocally();          // apply current sort on completed list
            refreshLoadMoreButton();
            return;
        }
        if(msg.type==='error'){
            hideLoadingState();
            explErrAlert.textContent='Error: '+(msg.message||'unknown');
            explErrAlert.style.display='block';
            activeES.close(); activeES=null;
        }
    };

    activeES.onerror=()=>{
        if(activeES?.readyState===EventSource.CLOSED){ hideLoadingState(); activeES=null; }
    };
}

/* ── DOM rendering ───────────────────────────────────────────────────────── */
function getMaxMembers(){ return Math.max(1,...allCommunities.map(s=>s.members||0)); }

function appendToDOM(batch){
    const maxM=getMaxMembers();
    batch.forEach(s=>{
        // Use Growing layout for growing mode
        communityList.appendChild(ctx.mode==='growing' ? buildGrowingListRow(s,maxM) : buildListRow(s,maxM));
        communityGrid.appendChild(buildCard(s));
    });
    refreshMemberBars(maxM);
}

function refreshMemberBars(maxM){
    document.querySelectorAll('.mem-bar-fill').forEach(el=>{
        const m=parseInt(el.dataset.members||0,10);
        el.style.width=Math.max(2,Math.round(m/maxM*100))+'%';
    });
}

function updateCount(){
    explCount.textContent=`${allCommunities.length} communities`;
}

function showEmpty(txt){
    explEmpty.style.display='block';
    document.getElementById('explEmptyText').textContent=txt||'No communities match. Try different filters or Load More.';
    communityList.innerHTML=''; communityGrid.innerHTML='';
    explCount.textContent='';
}

/* ── Fetch triggers ──────────────────────────────────────────────────────── */
function _startFetch(mode, query=''){
    ctx.mode=mode; ctx.query=query;
    connectSSE(buildURL(), false);
}

function loadTrending(){ setQF('qfTrending'); _hideGrowingUI(); _startFetch('popular'); }
function loadGrowing()  { setQF('qfNew');      _showGrowingUI(); _startFetch('growing'); }
function loadAll()      { setQF('qfAll');       _hideGrowingUI(); _startFetch('popular'); }
function runSearch(){
    const q=(explorerQuery.value||'').trim();
    if(!q){ explErrAlert.textContent='Please enter a search term.'; explErrAlert.style.display='block'; return; }
    explErrAlert.style.display='none';
    _hideGrowingUI();
    setQF('qfSearch');
    _startFetch('search', q);
}
function loadMore(){
    if(!ctx.cursor||activeES) return;
    connectSSE(buildURL(ctx.cursor), true /* append */);
}

function _showGrowingUI(){
    document.getElementById('growingDisclaimer').style.display='flex';
    document.getElementById('growingSortBar')?.style.setProperty('display','flex');
}
function _hideGrowingUI(){
    document.getElementById('growingDisclaimer').style.display='none';
    document.getElementById('growingSortBar')?.style.setProperty('display','none');
}

window.loadTrending = loadTrending;
window.loadGrowing  = loadGrowing;
window.loadAll      = loadAll;
window.runSearch    = runSearch;
window.loadMore     = loadMore;

document.getElementById('btnExplSearch').addEventListener('click', runSearch);
explorerQuery.addEventListener('keydown', e=>{ if(e.key==='Enter') runSearch(); });

/* ── Filter / sort controls ──────────────────────────────────────────────── */

// Apply size+activity filters to a list client-side
function _clientFilter(list){
    const f = getFilterParams();
    return list.filter(s => {
        const m = s.members, a = s.active_now;
        if(f.min_members != null && m < f.min_members) return false;
        if(f.max_members != null && m > f.max_members) return false;
        if(f.min_active  != null && a < f.min_active)  return false;
        if(f.max_active  != null && a > f.max_active)  return false;
        return true;
    });
}

// Size or activity filter changed
function onFilterChange(){
    if(!activeES && allCommunities.length === 0) return; // nothing loaded yet

    // Growing mode: all data already in memory — filter + re-render locally
    if(ctx.mode === 'growing'){
        const filtered = _clientFilter(allCommunities);
        communityList.innerHTML=''; communityGrid.innerHTML='';
        const maxM = Math.max(1, ...filtered.map(s => s.members||0));
        filtered.forEach(s => {
            communityList.appendChild(buildGrowingListRow(s, maxM));
            communityGrid.appendChild(buildCard(s));
        });
        setView(currentView);
        explCount.textContent = `${filtered.length} communities`;
        if(filtered.length === 0) showEmpty('No communities match this filter.');
        return;
    }

    // Other modes: re-fetch from server with filter params
    connectSSE(buildURL(), false);
}

// Sort dropdown → just re-sort what's already loaded (no new fetch)
function sortLocally(){
    const sort=document.getElementById('ctrlSort').value;
    const sortMap={
        // Standard sorts
        members:  (a,b)=>b.members-a.members,
        active:   (a,b)=>b.active_now-a.active_now,
        ratio:    (a,b)=>b.activity_ratio-a.activity_ratio,
        growth:   (a,b)=>b.growth_score-a.growth_score,
        newest:   (a,b)=>(b.created||'').localeCompare(a.created||''),
        alpha:    (a,b)=>a.display_name.localeCompare(b.display_name),
        // Growing-specific sorts
        momentum: (a,b)=>b.momentum_score-a.momentum_score,
        est_monthly: (a,b)=>b.approx_monthly_members-a.approx_monthly_members,
        per1k:    (a,b)=>b.size_adjusted_activity-a.size_adjusted_activity,
        engagement_pct: (a,b)=>b.engagement_pct-a.engagement_pct,
    };
    const sorted=[...allCommunities].sort(sortMap[sort]||sortMap.members);
    communityList.innerHTML=''; communityGrid.innerHTML='';
    const maxM=Math.max(1,...sorted.map(s=>s.members||0));
    sorted.forEach(s=>{
        communityList.appendChild(ctx.mode==='growing' ? buildGrowingListRow(s,maxM) : buildListRow(s,maxM));
        communityGrid.appendChild(buildCard(s));
    });
    setView(currentView);
    refreshLoadMoreButton();
}

// Expose so HTML onchange works
window.onFilterChange = onFilterChange;
window.sortLocally    = sortLocally;
// Legacy name — size/activity now trigger onFilterChange, sort triggers sortLocally
window.applyFilters   = onFilterChange;

/* ── Growing tab: momentum badge helper ──────────────────────────────────── */
function _momentumBadge(per1k){
    if(per1k >= 5.0) return `<span class="momentum-badge momentum-high">🔥 High Momentum</span>`;
    if(per1k >= 1.0) return `<span class="momentum-badge momentum-active">📈 Active</span>`;
    return `<span class="momentum-badge momentum-low">💤 Low Activity</span>`;
}
function _growthVal(s, col){
    if(col==='weekly')  return Math.round(s.approx_weekly_members  || 0);
    if(col==='monthly') return Math.round(s.approx_monthly_members || 0);
    const yearly = Math.round((s.approx_monthly_members || 0) * 12);
    return yearly;
}
function _growthCls(val){
    if(val >= 10000) return 'gval-high';
    if(val >= 1000)  return 'gval-med';
    return 'gval-low';
}

/* ── Growing tab list row builder (Change 4) ─────────────────────────────── */
function buildGrowingListRow(s, maxM){
    const sz=sizeClass(s.members);
    const color=avatarColor(s.display_name);
    const first=(s.display_name||'?').charAt(0).toUpperCase();
    const isComp=!!compareSet[s.display_name];
    const per1k = s.size_adjusted_activity || 0;
    const badge = _momentumBadge(per1k);
    const engPct = (s.engagement_pct||0).toFixed(2);

    // Growth columns — emphasize the active timeframe
    const tf = growingTimeframe;
    const wkVal  = Math.round(s.approx_weekly_members  || 0);
    const moVal  = Math.round(s.approx_monthly_members || 0);
    const yrVal  = Math.round((s.approx_monthly_members || 0) * 12);
    const perDay = Math.round(s.estimated_daily_growth || 0);

    const row=document.createElement('div');
    row.className=`list-row growing-row sz-${sz}`; row.dataset.name=s.display_name;
    row.innerHTML=`
        <div class="list-main">
            <div class="list-avatar" style="background:${color}">${first}</div>
            <div class="list-info">
                <div class="list-name">${esc(s.name)}</div>
                <div class="list-title">${esc(s.title)}</div>
                ${s.description?`<div class="list-desc">${esc(s.description)}</div>`:''}
                <div class="list-tags">${renderTags(s.tags)}</div>
                <div class="grow-metrics-row">
                    ${badge}
                    <span class="grow-per1k" title="Active users per 1,000 members">${per1k.toFixed(1)} active/1K</span>
                    <span class="grow-eng" title="Engagement %">${engPct}% engagement</span>
                </div>
                <div class="list-actions">
                    <button class="btn-sm btn-sm-orange" onclick="researchCommunity('${esc(s.display_name)}')" id="research_${esc(s.display_name)}">Research This</button>
                    <button class="btn-sm btn-sm-outline" onclick="openDrawer('${esc(s.display_name)}')">Details</button>
                    <label class="compare-wrap-sm">
                        <input type="checkbox" class="compare-cb" data-name="${esc(s.display_name)}" ${isComp?'checked':''}
                               onchange="toggleCompare('${esc(s.display_name)}',this.checked)">Compare
                    </label>
                </div>
            </div>
        </div>
        <div class="growing-stats">
            <div class="grow-col">
                <div class="grow-col-label">∼ Daily</div>
                <div class="grow-col-val ${_growthCls(perDay)}">+${fmtNum(perDay)}</div>
                <div class="grow-col-sub">members/day</div>
            </div>
            <div class="grow-col ${tf==='weekly'?'grow-col-active':''} ">
                <div class="grow-col-label">∼ Weekly</div>
                <div class="grow-col-val ${_growthCls(wkVal)}">+${fmtNum(wkVal)}</div>
                <div class="grow-col-sub">members/wk (est.)</div>
            </div>
            <div class="grow-col ${tf==='monthly'?'grow-col-active':''}">
                <div class="grow-col-label">∼ Monthly</div>
                <div class="grow-col-val ${_growthCls(moVal)}">+${fmtNum(moVal)}</div>
                <div class="grow-col-sub">members/mo (est.)</div>
            </div>
            <div class="grow-col ${tf==='yearly'?'grow-col-active':''}">
                <div class="grow-col-label">∼ Yearly</div>
                <div class="grow-col-val ${_growthCls(yrVal)}">+${fmtNum(yrVal)}</div>
                <div class="grow-col-sub">members/yr (est.)</div>
            </div>
            <div class="grow-col">
                <div class="grow-col-label">Members</div>
                <div class="grow-col-val">${fmtNum(s.members)}</div>
                <div class="grow-col-sub">${ageLabel(s.age_days)} old</div>
            </div>
        </div>`;
    return row;
}

/* ── Timeframe toggle (Change 5) ─────────────────────────────────────────── */
function setGrowingTimeframe(tf){
    growingTimeframe = tf;
    document.querySelectorAll('.tf-btn').forEach(b=>b.classList.toggle('active', b.dataset.tf===tf));
    // Re-render just the growing rows to update active column highlight
    if(ctx.mode==='growing') sortLocally();
}
window.setGrowingTimeframe = setGrowingTimeframe;

/* ── List row builder ────────────────────────────────────────────────────── */
function buildListRow(s,maxM){
    const sz=sizeClass(s.members);
    const barW=Math.max(2,Math.round((s.members/maxM)*100));
    const color=avatarColor(s.display_name);
    const first=(s.display_name||'?').charAt(0).toUpperCase();
    const isComp=!!compareSet[s.display_name];
    const row=document.createElement('div');
    row.className=`list-row sz-${sz}`; row.dataset.name=s.display_name;
    row.innerHTML=`
        <div class="list-main">
            <div class="list-avatar" style="background:${color}">${first}</div>
            <div class="list-info">
                <div class="list-name">${esc(s.name)}</div>
                <div class="list-title">${esc(s.title)}</div>
                ${s.description?`<div class="list-desc">${esc(s.description)}</div>`:''}
                <div class="list-tags">${renderTags(s.tags)}</div>
                <div class="list-actions">
                    <button class="btn-sm btn-sm-orange" onclick="researchCommunity('${esc(s.display_name)}')">Research This</button>
                    <button class="btn-sm btn-sm-outline" onclick="openDrawer('${esc(s.display_name)}')">Details</button>
                    <label class="compare-wrap-sm">
                        <input type="checkbox" class="compare-cb" data-name="${esc(s.display_name)}" ${isComp?'checked':''}
                               onchange="toggleCompare('${esc(s.display_name)}',this.checked)">Compare
                    </label>
                </div>
            </div>
        </div>
        <div class="list-stats">
            <div class="stat-col">
                <div class="stat-big">${fmtNum(s.members)}</div>
                <div class="mem-bar-wrap"><div class="mem-bar"><div class="mem-bar-fill" style="width:${barW}%" data-members="${s.members}"></div></div></div>
                <div class="stat-sm">~${fmtNum(s.estimated_daily_growth)}/day</div>
            </div>
            <div class="stat-col"><div class="stat-big">${fmtNum(s.active_now)}</div><div class="stat-sm">online now</div></div>
            <div class="stat-col"><div class="stat-big">${s.activity_ratio}%</div><div class="stat-sm">engagement</div></div>
        </div>`;
    return row;
}

/* ── Card builder ────────────────────────────────────────────────────────── */
function buildCard(s){
    const sz=sizeClass(s.members);
    const isComp=!!compareSet[s.display_name];
    const card=document.createElement('div');
    card.className=`community-card sz-${sz}`; card.dataset.name=s.display_name;
    card.innerHTML=`
        <div><div class="card-name">${esc(s.name)}</div><div class="card-title">${esc(s.title)}</div></div>
        ${s.description?`<div class="card-desc">${esc(s.description)}</div>`:''}
        <div>
            <div class="card-stat-row"><span>👥</span>${fmtNum(s.members)} members</div>
            <div class="card-stat-row"><span>🟢</span>${fmtNum(s.active_now)} online</div>
            <div class="card-stat-row"><span>📊</span>${s.activity_ratio}% eng.</div>
            <div class="card-stat-row"><span>📅</span>${s.created||'—'}</div>
        </div>
        <div class="card-tags">${renderTags(s.tags)}</div>
        <div class="card-actions">
            <button class="btn-sm btn-sm-orange" onclick="researchCommunity('${esc(s.display_name)}')">Research</button>
            <button class="btn-sm btn-sm-outline" onclick="openDrawer('${esc(s.display_name)}')">Details</button>
        </div>
        <label class="card-compare-wrap">
            <input type="checkbox" class="compare-cb" data-name="${esc(s.display_name)}" ${isComp?'checked':''}
                   onchange="toggleCompare('${esc(s.display_name)}',this.checked)">Compare
        </label>`;
    return card;
}

/* ── Research handoff ────────────────────────────────────────────────────── */
function researchCommunity(name){
    window.switchTab('search');
    window._showSearchSection?.();
    document.getElementById('query').value=`site:reddit.com/r/${name}`;
    window.showInfo?.(`ℹ Searching within r/${name} only.`);
    window.scrollTo({top:0,behavior:'smooth'});
}
window.researchCommunity = researchCommunity;

/* ── Drawer ──────────────────────────────────────────────────────────────── */
function openDrawer(name){
    const overlay=document.getElementById('drawerOverlay'), drawer=document.getElementById('drawer'), content=document.getElementById('drawerContent');
    content.innerHTML=`<div class="drawer-name">Loading…</div><div style="color:var(--text-dim);margin-top:8px">Fetching r/${esc(name)}</div>`;
    overlay.classList.add('open'); drawer.classList.add('open');
    fetch(`${API}/communities/details/${encodeURIComponent(name)}`)
        .then(r=>r.ok?r.json():Promise.reject(r.status))
        .then(s=>renderDrawer(content,s))
        .catch(e=>{ content.innerHTML=`<div class="drawer-name">Error ${e}</div>`; });
}
function renderDrawer(el,s){
    const sz=sizeClass(s.members);
    el.innerHTML=`
        <div class="drawer-name">${esc(s.name)}</div>
        <div class="drawer-title">${esc(s.title)}</div>
        <div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:4px">${renderTags(s.tags)}</div>
        <hr class="drawer-divider">
        <div class="drawer-stat-row"><span class="ds-label">👥 Members</span><span class="ds-value">${fmtNum(s.members)} <span class="size-badge sb-${sz}">${sizeLabelText(s.members)}</span></span></div>
        <div class="drawer-stat-row"><span class="ds-label">🟢 Online Now</span><span class="ds-value">${fmtNum(s.active_now)}</span></div>
        <div class="drawer-stat-row"><span class="ds-label">📊 Engagement %</span><span class="ds-value">${s.activity_ratio}%</span></div>
        <div class="drawer-stat-row"><span class="ds-label">📅 Community Age</span><span class="ds-value">${ageLabel(s.age_days)}</span></div>
        <div class="drawer-stat-row"><span class="ds-label">📈 Avg Growth</span><span class="ds-value">~${fmtNum(s.estimated_daily_growth)} members/day</span></div>
        <div class="drawer-stat-row"><span class="ds-label">🚀 Growth Score</span><span class="ds-value">${s.growth_score?.toLocaleString()||'—'}</span></div>
        ${s.posts_per_day!=null?`<div class="drawer-stat-row"><span class="ds-label">✍ Posts/Day</span><span class="ds-value">~${s.posts_per_day}</span></div>`:''}
        ${s.moderators_count!=null?`<div class="drawer-stat-row"><span class="ds-label">🛡 Moderators</span><span class="ds-value">${s.moderators_count}</span></div>`:''}
        ${s.rules_count!=null?`<div class="drawer-stat-row"><span class="ds-label">📋 Rules</span><span class="ds-value">${s.rules_count}</span></div>`:''}
        ${s.long_description?`<hr class="drawer-divider"><div style="font-size:.75rem;font-weight:700;text-transform:uppercase;color:var(--text-dim);margin-bottom:6px">About</div><div class="drawer-desc">${esc(s.long_description)}</div>`:''}
        <hr class="drawer-divider">
        <a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" style="color:var(--blue);font-size:.88rem;font-weight:600">Open on Reddit ↗</a>
        <button class="btn btn-primary" style="margin-top:14px" onclick="researchCommunity('${esc(s.display_name)}');closeDrawer()">🔍 Research This Community</button>`;
}
function closeDrawer(){ document.getElementById('drawerOverlay').classList.remove('open'); document.getElementById('drawer').classList.remove('open'); }
window.closeDrawer = closeDrawer;
window.openDrawer  = openDrawer;
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeDrawer(); });

/* ── Compare ─────────────────────────────────────────────────────────────── */
function toggleCompare(name, checked){
    const sub=allCommunities.find(s=>s.display_name===name);
    if(!sub) return;
    if(checked){
        if(Object.keys(compareSet).length>=4){ alert('Max 4 communities.'); event.target.checked=false; return; }
        compareSet[name]=sub;
    } else {
        delete compareSet[name];
        if(Object.keys(compareSet).length<2) document.getElementById('compareSection').classList.remove('visible');
    }
    updateCompareBar();
}
window.toggleCompare = toggleCompare;

function updateCompareBar(){
    const names=Object.keys(compareSet);
    document.getElementById('compareCount').textContent=names.length;
    document.getElementById('compareNames').innerHTML=names.map(n=>`<span class="compare-pill">r/${esc(n)}</span>`).join('');
    document.getElementById('compareBar').classList.toggle('visible',names.length>=2);
}
document.getElementById('btnCompareGo').addEventListener('click', ()=>{
    const subs=Object.values(compareSet); if(subs.length<2) return;
    buildCompareTable(subs);
    document.getElementById('compareSection').classList.add('visible');
    document.getElementById('compareSection').scrollIntoView({behavior:'smooth'});
});
function buildCompareTable(subs){
    const metrics=[
        ['Members',s=>s.members,fmtNum,true],['Active Now',s=>s.active_now,fmtNum,true],
        ['Engagement %',s=>s.activity_ratio,v=>v+'%',true],
        ['Growth/day',s=>s.estimated_daily_growth,v=>v!=null?'~'+fmtNum(v):'—',true],
        ['Growth Score',s=>s.growth_score,v=>v!=null?v.toLocaleString():'—',true],
        ['Age',s=>s.age_days,ageLabel,false],['Posts/Day',s=>s.posts_per_day,v=>v!=null?'~'+v:'—',true],
        ['Size',s=>sizeLabelText(s.members),v=>v,false],
    ];
    let html=`<thead><tr><th>Metric</th>${subs.map(s=>`<th>${esc(s.name)}</th>`).join('')}</tr></thead><tbody>`;
    metrics.forEach(([lbl,getter,fmt,higher])=>{
        const vals=subs.map(s=>getter(s)), nums=vals.filter(v=>typeof v==='number');
        const best=higher&&nums.length?Math.max(...nums):null;
        html+=`<tr><td>${lbl}</td>`;
        vals.forEach(v=>{ const win=higher&&typeof v==='number'&&v===best&&nums.length>1; html+=`<td class="${win?'compare-winner':''}">${fmt(v)}</td>`; });
        html+='</tr>';
    });
    html+=`<tr><td>Research</td>${subs.map(s=>`<td><button class="btn-sm btn-sm-orange" onclick="researchCommunity('${esc(s.display_name)}')">Go →</button></td>`).join('')}</tr></tbody>`;
    document.getElementById('compareTable').innerHTML=html;
}
function clearComparison(){
    compareSet={};
    document.querySelectorAll('.compare-cb').forEach(cb=>cb.checked=false);
    document.getElementById('compareBar').classList.remove('visible');
    document.getElementById('compareSection').classList.remove('visible');
    document.getElementById('compareCount').textContent='0';
    document.getElementById('compareNames').innerHTML='';
}
window.clearComparison = clearComparison;

/* ── Lazy init ───────────────────────────────────────────────────────────── */
window._explorerInit=()=>{ if(!allCommunities.length) loadTrending(); window._explorerInit=null; };

/* ── Boot ────────────────────────────────────────────────────────────────── */
setView('list');

})();
