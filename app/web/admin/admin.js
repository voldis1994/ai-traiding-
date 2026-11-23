'use strict';
const $=id=>document.getElementById(id);
let pollTimer=null;
let lastOverview=null;
let chartRange='1W';
let overviewInFlight=null;
let lastSyncAt=null;
let deskFocusId=null;
let deskStructure=null;
let deskStructureInFlight=null;
let lastDeskChartKey='';
const deskPrevFields=new Map(); // clientId → {field: previous string}
const deskPrevTicks=new Map();  // clientId → {tick, at}
const POLL_IDLE_MS=10000;
const POLL_LIVE_MS=5000;
const POLL_DESK_MS=2500;
// Popup OHLC refresh (only when operator opens a client window — no main-board poll).
const OHLC_REFRESH_MS=2000;
// While a LIVE deal is open, freeze displayed BUY/HOLD/SELL per client so a
// lagging backend cannot make the Admin card dance mid-trade.
const frozenScoresByClient=new Map();
let currentPollMs=null;
let lastPerfKey='';

/** Only rewrite DOM when markup actually changed — stops poll flicker. */
function stableHtml(el,html){
  if(!el)return false;
  if(el._stableHtml===html)return false;
  el._stableHtml=html;
  el.innerHTML=html;
  return true;
}

async function api(path,method='GET',payload){
  const opts={method,credentials:'same-origin',headers:{'Content-Type':'application/json'}};
  if(payload!==undefined)opts.body=JSON.stringify(payload);
  const res=await fetch(path,opts);let body={};try{body=await res.json()}catch(_){}
  if(!res.ok)throw Error(typeof body.detail==='string'?body.detail:`HTTP ${res.status}`);
  return body;
}
function show(login){
  $('login').classList.toggle('hidden',!login);
  $('dash').classList.toggle('hidden',login);
  if(!login){
    requestAnimationFrame(()=>{deskAutoFit();termViewsAutoFit();});
  }
}

/** Clamp helper for Auto-Fit sizing. */
function clampNum(n,lo,hi){return Math.max(lo,Math.min(hi,Number(n)||lo))}

/**
 * Desk Auto-Fit — size every window to the viewport WITHOUT CSS scale()/zoom.
 * Layout order stays fixed; only CSS custom properties (fonts, gaps, tracks) change.
 */
function deskAutoFit(){
  const root=$('psDesk');
  if(!root)return;
  const deskView=document.querySelector('.view[data-view="desk"]');
  if(deskView&&!deskView.classList.contains('active'))return;
  root.dataset.autofit='1';
  const w=Math.max(root.clientWidth||0,window.innerWidth-90);
  const h=Math.max(root.clientHeight||0,window.innerHeight-40);
  // Readable floor first — never crush bottom labels into mid-word wraps.
  const fs=clampNum(Math.round(10.5+h/110+w/480),11,15);
  const fsSm=clampNum(fs-1,10,13);
  const fsXs=clampNum(fs-2,9,12);
  const fsMd=clampNum(fs,11,14);
  const fsLg=clampNum(fs+2,13,19);
  const fsXl=clampNum(fs+4,15,22);
  const gap=clampNum(Math.round(3+h/280),3,7);
  const pad=clampNum(gap+1,4,9);
  const headH=clampNum(Math.round(22+fs*0.35),24,28);
  const tapeH=clampNum(Math.round(18+fs*0.25),20,24);
  const statusMin=clampNum(Math.round(44+fs),48,64);
  // Side windows: keep center chart dominant, but never starve learn/closes.
  let side=clampNum(Math.round(w*0.15),150,260);
  if(w<1200)side=clampNum(Math.round(w*0.135),130,200);
  if(w<980)side=clampNum(Math.round(w*0.12),112,160);
  // Bottom row needs real height — previous autofit starved LEARN/CLOSES.
  const bottomMin=clampNum(Math.round(h*0.34),210,340);
  const topMin=clampNum(Math.round(h*0.28),150,280);
  const set=(k,v)=>root.style.setProperty(k,v);
  set('--desk-fs',fs+'px');
  set('--desk-fs-xs',fsXs+'px');
  set('--desk-fs-sm',fsSm+'px');
  set('--desk-fs-md',fsMd+'px');
  set('--desk-fs-lg',fsLg+'px');
  set('--desk-fs-xl',fsXl+'px');
  set('--desk-gap',gap+'px');
  set('--desk-pad',pad+'px');
  set('--desk-head-h',headH+'px');
  set('--desk-tape-h',tapeH+'px');
  set('--desk-status-min',statusMin+'px');
  set('--desk-col-intel',side+'px');
  set('--desk-col-right',side+'px');
  // Give LEARN + CLOSES equal readable width (were too narrow → "Libra/ry").
  set('--desk-grid-c1','minmax(110px,1fr)');
  set('--desk-grid-c2','minmax(140px,1.2fr)');
  set('--desk-grid-c3','minmax(170px,1.25fr)');
  set('--desk-grid-c4','minmax(170px,1.25fr)');
  set('--desk-grid-r1','minmax('+topMin+'px,1.1fr)');
  set('--desk-grid-r2','minmax('+bottomMin+'px,1.25fr)');
  root.dataset.autofitW=String(Math.round(w));
  root.dataset.autofitH=String(Math.round(h));
  root.dataset.autofitFs=String(fs);
}

/** Non-desk modules: fill the shell; only inner .fill-scroll scrolls. */
function termViewsAutoFit(){
  const shell=$('termShell')||document.querySelector('.term');
  if(!shell)return;
  const h=window.innerHeight;
  const fs=clampNum(Math.round(9+h/110),9,13);
  shell.style.setProperty('--term-fit-fs',fs+'px');
  document.querySelectorAll('.term-main>.view:not([data-view="desk"]) .term-view').forEach(el=>{
    el.style.minHeight='0';
    el.style.height='100%';
  });
}

/** Browser fullscreen for the Admin terminal (desktop). Needs a user gesture. */
let deskFsArmed=false;
async function enterAdminFullscreen(){
  try{
    const root=document.documentElement;
    if(document.fullscreenElement)return true;
    if(root.requestFullscreen){
      await root.requestFullscreen({navigationUI:'hide'});
      return true;
    }
    if(root.webkitRequestFullscreen){root.webkitRequestFullscreen();return true}
  }catch(err){
    console.warn('fullscreen',err);
  }
  return false;
}
function armFullscreenOnNextGesture(){
  if(deskFsArmed||document.fullscreenElement)return;
  deskFsArmed=true;
  const once=async()=>{
    document.removeEventListener('pointerdown',once,true);
    document.removeEventListener('keydown',once,true);
    deskFsArmed=false;
    await enterAdminFullscreen();
  };
  document.addEventListener('pointerdown',once,true);
  document.addEventListener('keydown',once,true);
}
function pct(v){return Math.round((Number(v)||0)*100)+'%'}
function num(v,d=2){const n=Number(v);return Number.isFinite(n)?n.toFixed(d):'—'}
function pnlClass(v){const n=Number(v);if(!Number.isFinite(n)||n===0)return '';return n>0?'pnl-pos':'pnl-neg'}
function money(v,ccy){
  if(v==null||v===''||!Number.isFinite(Number(v)))return '—';
  const n=Number(v);
  const sign=n>0?'+':'';
  return sign+num(n,2)+(ccy?' '+ccy:'');
}
function esc(s){
  return String(s??'').replace(/[&<>"']/g,c=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function icon(name){return `<svg class="ico" aria-hidden="true"><use href="#i-${esc(name)}"></use></svg>`}

function sourceLabel(src){
  const s=String(src||'').toLowerCase();
  if(s==='broker')return 'Capital £';
  if(s==='upl_estimate')return '≈UPL (nav Capital £)';
  if(s==='open_upl')return 'live UPL';
  if(s==='working')return 'working';
  if(s==='none'||s==='empty')return 'nav skaitļu';
  if(s==='pending_broker_pnl'||s==='pending_close_sync')return 'gaida Capital £';
  return src||'—';
}
function closePnlSrcBadge(src,pending){
  const s=String(src||'').toLowerCase();
  if(pending||s==='pending_broker_pnl'||s==='pending_close_sync'){
    return {cls:'psx-pnl-src wait',text:'GAIDA'};
  }
  if(s==='upl_estimate')return {cls:'psx-pnl-src est',text:'≈UPL'};
  if(s==='broker'||s===''||!s)return {cls:'psx-pnl-src ok',text:'CAPITAL'};
  if(s==='ticks_only'||s==='price_size'||s==='position_disappeared'){
    return {cls:'psx-pnl-src wait',text:'PROXY'};
  }
  return {cls:'psx-pnl-src wait',text:String(src).slice(0,8).toUpperCase()};
}
function closeTimeLabel(t){
  const raw=(t&&(t.closed||t.closed_at||t.updated_at||t.at))||'';
  const s=String(raw);
  if(!s)return '—';
  // ISO …T14:10:00… → 14:10
  const m=s.match(/T(\d{2}:\d{2})/);
  if(m)return m[1];
  if(s.length>=19)return s.slice(11,16);
  if(s.length>=8)return s.slice(11,19)||s;
  return s;
}

function hasRealWorkingPnl(r){
  if(r==null||r.working_pnl==null||!Number.isFinite(Number(r.working_pnl)))return false;
  const src=String(r.working_pnl_source||'').toLowerCase();
  // Backend used to emit working_pnl=0 with source empty/none — treat as missing.
  if(src==='none'||src==='empty')return false;
  return true;
}

/** Format P&L for Desk — gaida only when there is literally no number. */
function formatPnlDisplay(pnl,src,ccy){
  const s=String(src||'').toLowerCase();
  if(pnl!=null&&Number.isFinite(Number(pnl))){
    const m=money(Number(pnl),ccy);
    if(s==='upl_estimate')return '≈'+m;
    return m;
  }
  if(s==='upl_estimate'||s==='pending_broker_pnl'||s==='pending_close_sync'){
    return 'gaida Capital £';
  }
  return '—';
}

/** Capital £ → live open UPL → ≈UPL closes. Never wall the desk on blank gaida. */
function scoreboard(r){
  r=r||{};
  const ccy=r.pnl_currency||'';
  const ledger=Array.isArray(r.pnl_ledger)?r.pnl_ledger:[];
  const recent=Array.isArray(r.recent_trades)?r.recent_trades:[];
  let pnl=null,src='none',wins=0,losses=0;
  const wsrc=String(r.working_pnl_source||'').toLowerCase();
  const estN=Number(r.estimate_trades)||0;

  if(hasRealWorkingPnl(r) && wsrc==='broker'){
    pnl=Number(r.working_pnl);src='broker';
  }else if((r.cash_trades||0)>0 && r.realized_pnl!=null && Number.isFinite(Number(r.realized_pnl))){
    pnl=Number(r.realized_pnl);src='broker';
  }else if(r.open_upl!=null && Number.isFinite(Number(r.open_upl))){
    // Live Capital open UPL must win — stuck ≈UPL closes must not hide +£0.31.
    pnl=Number(r.open_upl);src='open_upl';
  }else if(wsrc==='upl_estimate' || estN>0){
    const est=r.estimate_pnl!=null?Number(r.estimate_pnl):null;
    if(est!=null&&Number.isFinite(est)){pnl=est;src='upl_estimate';}
    else{
      let sum=0,n=0;
      for(const row of ledger){
        const v=Number(row.pnl);
        if(!Number.isFinite(v))continue;
        if(String(row.source||'').toLowerCase()!=='upl_estimate')continue;
        sum+=v;n++;
      }
      if(!n){
        for(const t of recent){
          const v=Number(t.pnl!=null?t.pnl:(t.profit!=null?t.profit:t.upl_hint));
          const s=String(t.pnl_source||'').toLowerCase();
          if(!Number.isFinite(v))continue;
          if(s!=='upl_estimate'&&s!=='pending_broker_pnl'&&!t.pending_cash)continue;
          sum+=v;n++;
        }
      }
      src='upl_estimate';
      if(n)pnl=sum;
    }
  }

  if(src==='broker'){
    if(r.display_wins!=null || r.display_losses!=null){
      wins=Number(r.display_wins)||0;losses=Number(r.display_losses)||0;
    }else{
      wins=Number(r.wins)||0;losses=Number(r.losses)||0;
    }
  }

  if(pnl==null && src!=='upl_estimate' && ledger.length){
    let sum=0,w=0,l=0,nBroker=0;
    for(const row of ledger){
      const v=Number(row.pnl);
      if(!Number.isFinite(v))continue;
      if(String(row.source||'').toLowerCase()!=='broker')continue;
      sum+=v;nBroker++;if(v>0)w++;else if(v<0)l++;
    }
    if(nBroker){pnl=sum;src='broker';if(wins+losses===0){wins=w;losses=l}}
  }

  if(pnl==null && src!=='upl_estimate' && recent.length){
    let sum=0,n=0,w=0,l=0;
    for(const t of recent){
      const v=Number(t.pnl);
      if(!Number.isFinite(v))continue;
      if(String(t.pnl_source||'').toLowerCase()!=='broker')continue;
      sum+=v;n++;if(v>0)w++;else if(v<0)l++;
    }
    if(n){pnl=sum;src='broker';if(wins+losses===0){wins=w;losses=l}}
  }

  return {pnl,src,wins,losses,ccy,closed:Number(r.closed_trades)||0,
    cash:Number(r.cash_trades)||0,estimate:estN};
}

function layoutCols(n){
  if(n<=1)return 1;
  if(n===2||n===3)return n;
  if(n===4)return 2;
  if(n<=6)return 3;
  return Math.min(4,Math.ceil(Math.sqrt(n)));
}

function setView(name){
  document.querySelectorAll('.psx-rail-btn[data-view],.nav-item').forEach(b=>{
    const on=b.dataset.view===name;
    b.classList.toggle('active',on);
    b.classList.toggle('on',on);
  });
  document.querySelectorAll('.view').forEach(v=>{
    v.classList.toggle('active',v.dataset.view===name);
  });
  scheduleDeskAutoFit();
  if(name==='desk'){
    lastDeskChartKey='';
    if(lastOverview)renderDesk(lastOverview).catch(()=>{});
  }else if(name==='shadow'){
    if(lastOverview)renderShadowLab(pickDeskFocus(lastOverview.clients||[]));
  }else if(lastOverview){
    // redraw perf when leaving desk so chart sizes correctly in viewport
    try{drawPerfChart(lastOverview.clients)}catch(_){}
  }
}

function shadowLab(client){
  const a=(client&&client.activity)||{};
  const ds=deskSystem(client);
  return (ds.shadow_lab&&typeof ds.shadow_lab==='object')?ds.shadow_lab
    :(a.self_development&&typeof a.self_development==='object'
      ?{paper:a.self_development.shadow_paper||{},hold_shadow:a.self_development.hold_shadow||{},
        structure_shadow:a.self_development.structure_shadow||{},
        shadow_active:!!a.self_development.shadow_active}:{})
}

function _shadowKv(label,val,cls){
  const v=(val==null||val==='')?'—':String(val);
  return `<div class="desk-kv"><span>${esc(label)}</span><strong class="${esc(cls||'')}">${esc(v)}</strong></div>`;
}

function renderShadowLab(client){
  const board=$('shadowBoard');
  const truth=$('shadowTruth');
  const pulseEl=$('shadowPulse');
  if(!board)return;
  if(!client){
    if(truth)stableHtml(truth,'Select an account on TERMINAL — SHADOW follows the focused robot.');
    if(pulseEl)pulseEl.textContent='● —';
    stableHtml(board,'<div class="psx-empty">No focused client</div>');
    return;
  }
  const lab=shadowLab(client);
  const paper=lab.paper||{};
  const hold=lab.hold_shadow||{};
  const struct=lab.structure_shadow||{};
  const trial=lab.trial||{};
  const last=lab.self_dev_last||{};
  const evo=lab.evolution||{};
  const truthInfo=lab.truth||{};
  const pulse=lab.pulse||'IDLE';
  if(pulseEl){
    pulseEl.textContent='● '+pulse;
    pulseEl.className='psx-live shadow-pulse-pill '+pulse;
  }
  if(truth){
    const hundred=truthInfo.hundred_variations===true;
    stableHtml(truth,
      `<strong>Does it run 100 variations?</strong> `
      +`<b>${hundred?'YES':'NO — not today'}</b>. `
      +esc(truthInfo.what_runs||(
        'One shadow neural + paper SL/TP book; ~30 policy edge candidates; '
        +'self-params trials; HOLD opposite replay. Not a 100-parallel sweep.'))
      +` · Focus <b>#${esc(client.id)} ${esc(client.display_name||client.username||'')}</b>`
      +(lab.shadow_active?' · shadow neural <b>ON</b>':' · shadow neural <b>off / booting</b>'));
  }
  const net=paper.paper_net!=null?Number(paper.paper_net):null;
  const netCls=net==null?'neu':(net>=0?'ok':'bad');
  const netTxt=net==null?'—':((net>=0?'+':'')+num(net,2));
  const open=paper.open&&typeof paper.open==='object'?paper.open:null;
  const pending=paper.pending_fill&&typeof paper.pending_fill==='object'?paper.pending_fill:null;
  const recent=Array.isArray(paper.recent)?paper.recent:[];
  const openTxt=open
    ?(`${String(open.direction||'?').toUpperCase()} @ ${num(open.entry,2)} · SL ${open.sl!=null?num(open.sl,2):'—'} · TP ${open.tp!=null?num(open.tp,2):'—'}`)
    :(pending?`pending ${String(pending.direction||'?').toUpperCase()}`:'flat');
  const rowsHtml=recent.length?recent.map(r=>{
    const dir=String(r.direction||'?').toUpperCase();
    const dcls=(dir==='BUY'||dir==='LONG')?'buy':'sell';
    const pnl=r.paper_pnl!=null?Number(r.paper_pnl):null;
    const pcls=pnl==null?'':(pnl>=0?'ok':'bad');
    const pnlTxt=pnl==null?'—':((pnl>=0?'+':'')+num(pnl,2));
    const when=r.at?String(r.at).replace('T',' ').slice(0,16):'—';
    return `<div class="shadow-row">
      <span class="dir ${dcls}">${dir==='BUY'||dir==='LONG'?'▲ BUY':'▼ SELL'}</span>
      <span class="mut">${esc(r.exit_reason||'close')} · ${esc(when)}</span>
      <span class="mut">${esc(num(r.entry,2))}→${esc(num(r.exit,2))}</span>
      <strong class="pnl ${pcls}">${esc(pnlTxt)}</strong>
    </div>`;
  }).join(''):'<div class="psx-empty">No paper closes yet — waiting for shadow fills</div>';
  const top=(struct.top||[]).slice(0,4).map(b=>{
    if(!b||typeof b!=='object')return '';
    return `<div class="shadow-note">${esc(b.key||b.structure||'bucket')} · SL ${esc(b.sl_count||0)} / n ${esc(b.samples||b.n||0)}</div>`;
  }).filter(Boolean).join('')||'<div class="shadow-note">No structure graves yet</div>';
  const advice=struct.last_advice&&typeof struct.last_advice==='object'
    ?`${struct.last_advice.structure||'—'} × ${struct.last_advice.side||struct.last_advice.direction||'—'} → ${struct.last_advice.action||struct.last_advice.advice||'—'}`
    :'—';
  const promoteCls=last.promote?'ok':(last.rollback?'bad':'neu');
  const promoteTxt=last.promote?'PROMOTE':(last.rollback?'ROLLBACK':(last.reason?'HOLD':'—'));
  stableHtml(board,`
    <article class="shadow-card">
      <div class="psx-h"><h2>PAPER BOOK</h2><span class="psx-src">SHADOW £</span></div>
      <div class="body">
        <div class="shadow-kv">
          ${_shadowKv('Paper £',netTxt,netCls)}
          ${_shadowKv('Cash trades',paper.cash_trades!=null?paper.cash_trades:(paper.trades||0))}
          ${_shadowKv('W / L',`${paper.wins||0} / ${paper.losses||0}`)}
          ${_shadowKv('Open',openTxt)}
        </div>
        <div class="shadow-note">SL/TP only — never places LIVE broker orders.</div>
      </div>
    </article>
    <article class="shadow-card">
      <div class="psx-h"><h2>SELF-DEV</h2><span class="psx-src">PROMOTE</span></div>
      <div class="body">
        <div class="shadow-kv">
          ${_shadowKv('Last decision',promoteTxt,promoteCls)}
          ${_shadowKv('Shadow n',last.shadow_neurons!=null?last.shadow_neurons:'—')}
          ${_shadowKv('Paper samples',last.paper_cash_samples!=null?last.paper_cash_samples:'—')}
          ${_shadowKv('Live samples',last.live_samples!=null?last.live_samples:'—')}
        </div>
        <div class="shadow-note">${esc(last.reason||evo.last_reason||'Waiting for aligned £ windows')}</div>
      </div>
    </article>
    <article class="shadow-card">
      <div class="psx-h"><h2>PARAM TRIAL</h2><span class="psx-src">SELF PARAMS</span></div>
      <div class="body">
        <div class="shadow-kv">
          ${_shadowKv('Trial',trial.active?'ACTIVE':'off',trial.active?'ok':'neu')}
          ${_shadowKv('Trial id',trial.trial_id||'—')}
          ${_shadowKv('Horizon',trial.horizon!=null?trial.horizon:'—')}
          ${_shadowKv('Queued events',trial.pending_events!=null?trial.pending_events:0)}
        </div>
        <div class="shadow-note">${esc(trial.draft&&trial.draft.note?trial.draft.note:(trial.active?'Frozen knobs under evaluation':'No active param trial'))}</div>
      </div>
    </article>
    <article class="shadow-card">
      <div class="psx-h"><h2>POLICY SEARCH</h2><span class="psx-src">~30</span></div>
      <div class="body">
        <div class="shadow-kv">
          ${_shadowKv('Attempts',evo.attempts!=null?evo.attempts:0)}
          ${_shadowKv('Candidates',evo.policy_candidates_approx!=null?evo.policy_candidates_approx:30)}
          ${_shadowKv('Mode',(evo.mode||'paper + variants').split(' ')[0])}
          ${_shadowKv('Neural',lab.shadow_active?'ON':'off',lab.shadow_active?'ok':'neu')}
        </div>
        <div class="shadow-note">${esc(evo.last_reason||'Policy evolve waits for enough BUY/SELL labels')}</div>
      </div>
    </article>
    <article class="shadow-card">
      <div class="psx-h"><h2>HOLD SHADOW</h2><span class="psx-src">OPPOSITE</span></div>
      <div class="body">
        <div class="shadow-kv">
          ${_shadowKv('Pending',hold.pending!=null?hold.pending:0)}
          ${_shadowKv('Resolved',hold.resolved!=null?hold.resolved:0)}
          ${_shadowKv('Taught opp',hold.taught_opposite!=null?hold.taught_opposite:0)}
          ${_shadowKv('Opp TP rate',hold.opposite_tp_rate!=null?num(Number(hold.opposite_tp_rate)*100,0)+'%':'—')}
        </div>
        <div class="shadow-note">${esc(hold.strategy_hint||'When LIVE HOLDs a BUY/SELL proposal, shadow replays the opposite path')}</div>
      </div>
    </article>
    <article class="shadow-card">
      <div class="psx-h"><h2>STRUCTURE</h2><span class="psx-src">GRAVES</span></div>
      <div class="body">
        <div class="shadow-kv">
          ${_shadowKv('Buckets',struct.buckets!=null?struct.buckets:0)}
          ${_shadowKv('Snapshots',struct.snapshots!=null?struct.snapshots:0)}
        </div>
        <div class="shadow-note">Last advice: ${esc(advice)}</div>
        ${top}
      </div>
    </article>
    <article class="shadow-card wide">
      <div class="psx-h"><h2>RECENT PAPER CLOSES</h2><span class="psx-src">JOURNAL</span><span class="psx-live">● LIVE</span></div>
      <div class="body"><div class="shadow-rows">${rowsHtml}</div></div>
    </article>
  `);
}

function pickDeskFocus(clients){
  const list=clients||[];
  if(deskFocusId!=null){
    const keep=list.find(c=>String(c.id)===String(deskFocusId));
    if(keep)return keep;
  }
  const open=list.find(c=>{
    const a=c.activity||{};
    const pos=Array.isArray(a.positions)?a.positions:(a.position?[a.position]:[]);
    return pos.length>0;
  });
  if(open)return open;
  const run=list.find(c=>c.robot_running);
  return run||list[0]||null;
}

function deskScoreAction(c){
  const sc=c.scores||{};
  const buy=Number(sc.BUY)||0,hold=Number(sc.HOLD)||0,sell=Number(sc.SELL)||0;
  let act=c.action||'HOLD';
  if(!act||act==='—'){
    if(buy>=hold&&buy>=sell)act='BUY';
    else if(sell>=hold&&sell>=buy)act='SELL';
    else act='HOLD';
  }
  return {buy,hold,sell,act};
}

let deskTfMinutes=15;
let deskWhaleFilter="BOTH";
const deskDemoMode=new URLSearchParams(location.search).get("deskDemo")==="1";

/** Deterministic visual fixture for /admin?deskDemo=1 — never used for live execution. */
function buildDeskDemoFixture(){
  const seed=(i)=>Math.sin(i*12.9898)*43758.5453%1;
  const base=62093.3;
  const candles=[];
  for(let i=0;i<100;i++){
    const wave=Math.sin(i/7.3)*180+Math.cos(i/3.1)*90;
    const o=base+wave+((i%5)-2)*12;
    const c=o+Math.sin(i/2.2)*40-8;
    const h=Math.max(o,c)+Math.abs(Math.sin(i))*35;
    const l=Math.min(o,c)-Math.abs(Math.cos(i))*30;
    const mm=String(i%60).padStart(2,'0');
    const hh=String(Math.floor(i/60)).padStart(2,'0');
    candles.push({at:`2024-11-20T${hh}:${mm}:00Z`,open:+o.toFixed(1),high:+h.toFixed(1),low:+l.toFixed(1),close:+c.toFixed(1)});
  }
  const mid=candles[candles.length-1].close;
  const mk=(epic,mult,chg)=>{
    const cs=candles.map((c,i)=>({...c,open: +(c.open*mult).toFixed(2),high:+(c.high*mult).toFixed(2),
      low:+(c.low*mult).toFixed(2),close:+(c.close*mult).toFixed(2)}));
    return {epic,mid:+(mid*mult).toFixed(2),candles:cs,client_ids:[1],client_names:['DEMO'],donor_client_id:1,_chg:chg};
  };
  const markets=[
    mk('BTCUSD',1,+1.2),mk('ETHUSD',0.052,-0.8),mk('SOLUSD',0.0024,+2.1),
    mk('BNBUSD',0.0095,+0.4),mk('XRPUSD',0.000018,-1.1),mk('XAUUSD',0.042,+0.3),
  ];
  const client={
    id:1,username:'demo',display_name:'DEMO PLATINUM',robot_running:true,armed:true,
    execution_enabled:false,broker:{connected:true},
    scores:{BUY:0.14,HOLD:0.13,SELL:0.73},action:'SELL',
    activity:{
      market:'BTCUSD',market_mid:mid,market_bid:mid-0.5,market_ask:mid+0.5,tick:0.1,
      market_status:'TRADEABLE',tick_count:1284,spread:1.0,entry_can_submit_now:false,
      model_readiness:{ready:true},mtf_momentum:{mode:'ALIGNED',preferred:'SELL',ready:true},
      day_book:{phase:'ACTIVE'},
      economic_news_risk:{headline:'Demo: BTC funding skew widens into US open',risk:'elevated',source:'DEMO',status:'WATCH'},
      intermarket_analysis:{lean:'BEARISH · DXY firm vs risk',bias:'bear'},
      situation_memory:{tip:'Demo fixture — not a live news feed'},
      positions:[{epic:'BTCUSD',direction:'SELL',size:0.25,level:62150,currentLevel:mid,upl:-42.5,has_sl:true,has_tp:true,stopLevel:mid+420,profitLevel:mid-680}],
    },
    results:{
      pnl_currency:'USD',
      recent_trades:[
        {epic:'BTCUSD',direction:'SELL',size:1.2,level:62110,closed:'2024-11-20T12:01:00Z',pnl:-18,pnl_source:'broker'},
        {epic:'ETHUSD',direction:'BUY',size:4.0,level:3240,closed:'2024-11-20T11:55:00Z',pnl:62,pnl_source:'broker'},
        {epic:'SOLUSD',direction:'SELL',size:40,level:148.2,closed:'2024-11-20T11:40:00Z',pnl:-11,pnl_source:'upl_estimate'},
        {epic:'BNBUSD',direction:'BUY',size:2.5,level:590,closed:'2024-11-20T11:22:00Z',pnl:24,pnl_source:'broker'},
      ],
    },
  };
  const view={
    ok:true,epic:'BTCUSD',mid,candles,
    open_levels:[{entry:mid+40,stop:mid+420,profit:mid-680}],
    swing_brackets:{ready:true,entry:mid+40,stop:mid+420,tp1:mid-680},
    overlays:{sl:mid+420,ent:mid+40,tp:mid-680},
  };
  return {overview:{clients:[client],system:{ok:true}},markets,view,client};
}

function deskCandleChange(candles){
  if(!candles||candles.length<2)return null;
  const a=Number(candles[candles.length-1].close??candles[candles.length-1].c);
  const b=Number(candles[0].close??candles[0].c);
  if(!Number.isFinite(a)||!Number.isFinite(b)||!b)return null;
  return ((a-b)/b)*100;
}

function renderDeskTape(markets,focusClient){
  const el=$('deskTape');
  if(!el)return;
  const rows=markets||[];
  if(!rows.length){
    stableHtml(el,'<div class="psx-empty">No live Capitals in fleet — start a robot.</div>');
    return;
  }
  const focusEpic=((focusClient&&focusClient.activity)||{}).market
    ||((focusClient&&focusClient.activity&&focusClient.activity.mandate)||{}).epic
    ||null;
  const html=rows.map(m=>{
    const epic=m.epic||'—';
    const mid=m.mid!=null?num(m.mid,2):'—';
    const ch=deskCandleChange(m.candles);
    const chCls=ch==null?'':(ch>=0?'up':'dn');
    const chTxt=ch==null?'—':((ch>=0?'+':'')+ch.toFixed(2)+'%');
    const names=(m.client_names||[]).slice(0,2).join(', ');
    const active=focusEpic&&String(epic)===String(focusEpic)?' active':'';
    const cid=m.donor_client_id||((m.client_ids||[])[0]);
    return `<button type="button" class="psx-tape-item${active}" data-desk-cid="${esc(cid)}" data-desk-epic="${esc(epic)}">
      <span class="ep">${esc(epic)}</span>
      <span class="md">${esc(mid)}</span>
      <span class="ch ${chCls}">${chCls==='dn'?'▼':(chCls==='up'?'▲':'')} ${esc(chTxt)}</span>
      <span class="cl">${esc(names||('#'+cid))}</span>
    </button>`;
  }).join('');
  if(stableHtml(el,html)){
    el.querySelectorAll('[data-desk-cid]').forEach(btn=>{
      btn.onclick=()=>{
        deskFocusId=Number(btn.dataset.deskCid);
        const sel=$('deskFocus');
        if(sel)sel.value=String(deskFocusId);
        lastDeskChartKey='';
        if(lastOverview)renderDesk(lastOverview).catch(()=>{});
      };
    });
  }
}

/** Prefer live activity.positions brackets over structure-view open_levels. */
function enrichViewOpenLevels(view,client){
  if(!view)return view;
  const a=(client&&client.activity)||{};
  const positions=Array.isArray(a.positions)?a.positions:(a.position?[a.position]:[]);
  if(!positions.length)return view;
  const posLevels=[];
  for(const p of positions){
    if(!p||typeof p!=='object')continue;
    const n=v=>v!=null&&Number(v)>0?Number(v):null;
    const entry=n(p.level),stop=n(p.stopLevel),profit=n(p.profitLevel);
    if(entry==null&&stop==null&&profit==null)continue;
    posLevels.push({
      deal_id:p.dealId||p.deal_id,
      direction:p.direction,
      entry,stop,profit,
    });
  }
  if(!posLevels.length)return view;
  return {...view,open_levels:posLevels};
}

function renderDeskLevels(view,client){
  const el=$('deskLevels');
  if(!el)return;
  const open=(view&&view.open_levels)||[];
  const a=(client&&client.activity)||{};
  const pos=(Array.isArray(a.positions)&&a.positions[0])||a.position||null;
  // Live broker position wins — never hide SL because open_levels.stop is null.
  const lvl=(a,b)=>{
    const x=a!=null&&Number(a)>0?Number(a):null;
    const y=b!=null&&Number(b)>0?Number(b):null;
    return x!=null?x:y;
  };
  const entry=lvl(pos&&pos.level,open[0]&&open[0].entry);
  const stop=lvl(pos&&pos.stopLevel,open[0]&&open[0].stop);
  const profit=lvl(pos&&pos.profitLevel,open[0]&&open[0].profit);
  const mid=view&&view.mid!=null?view.mid:a.market_mid;
  const upl=pos&&pos.upl!=null&&Number.isFinite(Number(pos.upl))?Number(pos.upl):null;
  const ccy=(a.balance&&a.balance.currency)||a.currency||'';
  const bits=[];
  if(stop!=null)bits.push(`<span class="psx-lvl sl">SL ${esc(num(stop,2))}</span>`);
  if(entry!=null)bits.push(`<span class="psx-lvl ent">ENT ${esc(num(entry,2))}</span>`);
  if(profit!=null)bits.push(`<span class="psx-lvl tp">TP ${esc(num(profit,2))}</span>`);
  if(upl!=null)bits.push(`<span class="psx-lvl mid">UPL ${esc(money(upl,ccy))}</span>`);
  if(mid!=null&&Number(mid)>0&&!bits.length)bits.push(`<span class="psx-lvl mid">MID ${esc(num(mid,2))}</span>`);
  stableHtml(el,bits.join('')||'<span class="psx-empty" style="padding:0">No levels</span>');
}

function renderDeskQuote(view,client){
  const el=$('deskQuote');
  const title=$('deskEpicTitle');
  const a=(client&&client.activity)||{};
  const epic=(view&&view.epic)||a.market||((a.mandate||{}).epic)||'MARKET';
  const mid=a.market_mid!=null?a.market_mid:(view&&view.mid);
  if(title)title.textContent=mid!=null?`${epic}  ·  LAST ${num(mid,2)}`:epic;
  if(!el)return;
  const bid=a.market_bid,ask=a.market_ask;
  const spread=(bid!=null&&ask!=null&&Number.isFinite(Number(bid))&&Number.isFinite(Number(ask)))
    ?(Number(ask)-Number(bid)):null;
  const status=a.market_status||'—';
  el.textContent=[
    bid!=null?`Bid ${num(bid,2)}`:'Bid —',
    ask!=null?`Ask ${num(ask,2)}`:'Ask —',
    spread!=null?`Spr ${num(spread,3)}`:'',
    `TF ${deskTfMinutes}M`,
    status,
  ].filter(Boolean).join('  ·  ');
}

function deskTrack(cid,key,val){
  const id=String(cid||'0');
  const prev=deskPrevFields.get(id)||{};
  const now=(val==null||val===''||(typeof val==='number'&&!Number.isFinite(val)))
    ?'—':String(val);
  const was=prev[key];
  const lost=was!=null&&was!=='—'&&now==='—';
  const changed=was!=null&&was!==now;
  prev[key]=now;
  deskPrevFields.set(id,prev);
  return {now,lost,changed,was};
}

function deskKv(cid,label,val,fmt){
  const shown=fmt?fmt(val):val;
  const t=deskTrack(cid,label,shown);
  const cls=t.lost?'lost':(t.changed?'changed':'');
  return `<div class="desk-kv ${cls}"><span>${esc(label)}</span><strong title="${esc(t.was||'')}">${esc(t.now)}</strong></div>`;
}

function renderDeskWeights(client){ /* sink — data folded into engine score */ }
function renderDeskReturns(client){ /* sink */ }
function renderDeskReact(client){ /* sink */ }

function deskSystem(client){
  const a=(client&&client.activity)||{};
  return (a.desk_system&&typeof a.desk_system==='object')?a.desk_system:{};
}

function renderDeskStatusBoard(client){
  const el=$('deskStatusBoard');
  if(!el)return;
  if(!client){
    stableHtml(el,'<div class="psx-status-empty">Select account — board waits for LIVE card</div>');
    return;
  }
  const a=client.activity||{};
  const ds=deskSystem(client);
  const fc=a.flow_consensus||{};
  const fl=ds.flow_learn||fc.learn||{};
  const cards=ds.cards||{};
  const shadow=ds.shadow||{};
  const samples=ds.samples||{};
  const knobs=ds.knobs||{};
  const can=!!(ds.can_open||a.entry_can_submit_now);
  const block=ds.entry_block||a.entry_block_reason||'—';
  const intent=ds.entry_intent||a.entry_intent||a.policy_proposal||'—';
  const closeExec=ds.neural_close_executable!==false&&a.neural_close_executable!==false;
  const exitIntent=ds.neural_exit_intent||a.neural_exit_intent||'—';
  const cons=String((ds.flow_consensus&&ds.flow_consensus.consensus)||fc.consensus||'NEUTRAL').toUpperCase();
  const consCls=cons==='BUY'?'ok':(cons==='SELL'?'bad':'neu');
  const shadowNet=shadow.paper_net!=null?Number(shadow.paper_net):null;
  const shadowTrades=Number(shadow.cash_trades!=null?shadow.cash_trades:(shadow.trades||0))||0;
  const shadowTxt=shadowNet==null?'—':((shadowNet>=0?'+':'')+num(shadowNet,2));
  const lh=ds.learn_horizon||a.learn_horizon||{};
  const edgeN=lh.edge_closes!=null?Number(lh.edge_closes):null;
  // Headline: never flash bare library 0 when cards/closes are live.
  const sampleHead=samples.display_n!=null?samples.display_n
    :(samples.active_n!=null?samples.active_n
      :(samples.library_samples!=null?samples.library_samples
        :(samples.closed_trades!=null?samples.closed_trades:null)));
  const sampleSub=[
    samples.library_samples!=null?('lib '+samples.library_samples):null,
    samples.active_n!=null?('cards '+samples.active_n):null,
    samples.closed_trades!=null?('closes '+samples.closed_trades):null,
    samples.source||null,
  ].filter(Boolean).join(' · ')||'—';
  const flowSub=(()=>{
    if(fc.blocked)return 'BLOCKED';
    if(fl.n!=null&&Number(fl.n)>0){
      return 'learn n='+fl.n+' help '+(fl.helpful||0)+'/'+(fl.harmful||0);
    }
    if(edgeN!=null&&edgeN>0){
      return 'edge £ '+edgeN+(lh.adapted_closes!=null?(' · knobs '+lh.adapted_closes):'');
    }
    return fl.n!=null?('learn n='+fl.n+' help '+(fl.helpful||0)+'/'+(fl.harmful||0)):'—';
  })();
  const tiles=[
    {k:'CAN OPEN',v:can?'YES':'NO',cls:can?'ok':'bad',
      s:can?('Intent '+String(intent).slice(0,18)):String(block).slice(0,48)},
    {k:'NEURAL CLOSE',v:closeExec?'LIVE':'DIAG',cls:closeExec?'ok':'neu',
      s:'Intent '+String(exitIntent).slice(0,24)},
    {k:'M1 FLOW',v:cons,cls:consCls,s:flowSub},
    {k:'STRATEGY CARDS',v:String(cards.n!=null?cards.n:0),cls:(cards.n>0)?'ok':'neu',
      s:(cards.active&&cards.active.length)?cards.active.slice(0,3).join(', '):'none active'},
    {k:'SHADOW £',v:shadowTxt,
      cls:shadowNet==null?'neu':(shadowTrades===0?'neu':(shadowNet>=0?'ok':'bad')),
      s:shadowTrades===0
        ?'no shadow closes yet'
        :('trades '+shadowTrades+' · W'+(shadow.wins||0)+'/L'+(shadow.losses||0))},
    {k:'SAMPLES',v:String(sampleHead!=null?sampleHead:'—'),
      cls:(sampleHead!=null&&Number(sampleHead)>0)?'ok':'neu',s:sampleSub},
    {k:'SELF PARAMS',v:knobs.horizon!=null?('hz '+knobs.horizon):'—',cls:'neu',
      s:'nudge '+(knobs.flow_nudge!=null?Number(knobs.flow_nudge).toFixed(2):'—')
        +' · block '+(knobs.flow_block!=null?(Number(knobs.flow_block)?'on':'off'):'—')
        +' · impulse '+(knobs.flow_impulse!=null?Number(knobs.flow_impulse).toFixed(2):'—')},
  ];
  stableHtml(el,tiles.map(t=>`<article class="psx-status-tile ${esc(t.cls)}">
    <span class="k">${esc(t.k)}</span>
    <strong class="v">${esc(t.v)}</strong>
    <span class="s">${esc(t.s)}</span>
  </article>`).join(''));
}

function renderDeskLearn(client){
  const el=$('deskLearn');
  if(!el)return;
  if(!client){stableHtml(el,'<div class="psx-empty">—</div>');return}
  const ds=deskSystem(client);
  const cards=ds.cards||{};
  const shadow=ds.shadow||{};
  const samples=ds.samples||{};
  const adapt=ds.adaptive||{};
  const knobs=ds.knobs||{};
  const paying=(adapt.paying||[]).slice(0,4);
  const bleeding=(adapt.bleeding||[]).slice(0,4);
  const lh=ds.learn_horizon||{};
  const sampleShow=samples.display_n!=null?samples.display_n
    :(samples.library_samples!=null&&Number(samples.library_samples)>0
      ?samples.library_samples
      :(samples.active_n!=null?samples.active_n
        :(samples.closed_trades!=null?samples.closed_trades:0)));
  // Short labels — long ones mid-broke ("Libra/ry") in the bottom island.
  const rows=[
    deskKv(client.id,'Cards',cards.n!=null?cards.n:0),
    deskKv(client.id,'Samples',sampleShow),
    deskKv(client.id,'Source',samples.source||'—'),
    deskKv(client.id,'Closes',samples.closed_trades!=null?samples.closed_trades
      :((client.results&&client.results.closed_trades)!=null?client.results.closed_trades:'—')),
    deskKv(client.id,'Edge £',lh.edge_closes!=null?lh.edge_closes:'—'),
    deskKv(client.id,'Shadow £',shadow.paper_net,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'W/L',
      (shadow.wins!=null||shadow.losses!=null||shadow.paper_net!=null)
        ?(`${shadow.wins||0}/${shadow.losses||0}`):'—'),
    deskKv(client.id,'Nudge',knobs.flow_nudge,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'Impulse',knobs.flow_impulse,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'Strat Δ',knobs.strategy_gen_nudge,v=>v==null?'—':num(v,2)),
  ];
  const comboBits=[];
  if(paying.length){
    comboBits.push(`<div class="psx-learn-line ok"><span>PAYING</span><b>${esc(paying.map(p=>`${p.key} ${num(p.mean_pnl,2)}`).join(' · '))}</b></div>`);
  }
  if(bleeding.length){
    comboBits.push(`<div class="psx-learn-line bad"><span>BLEEDING</span><b>${esc(bleeding.map(p=>`${p.key} ${num(p.mean_pnl,2)}`).join(' · '))}</b></div>`);
  }
  if(cards.active&&cards.active.length){
    comboBits.push(`<div class="psx-learn-line"><span>CARDS</span><b>${esc(cards.active.slice(0,8).join(' · '))}</b></div>`);
  }
  stableHtml(el,`<div class="psx-learn-kv">${rows.join('')}</div>${comboBits.join('')}`);
}

/** M1 ATR14 from completed candles — never invent from M5 stop_hint (~7.5 lie). */
function deskM1Atr14(view,mtf,ov){
  const mtfUi=(view&&view.mtf)||{};
  const m1tf=((mtf&&mtf.tf)||{}).m1||{};
  const direct=mtfUi.atr14??m1tf.atr14??(ov&&ov.atr14);
  if(direct!=null&&Number.isFinite(Number(direct))&&Number(direct)>0)return Number(direct);
  const closes=_deskCloses(view);
  // Need OHLC highs/lows for true ATR — fall back to close-range proxy only if thin.
  const candles=(view&&view.candles)||[];
  if(candles.length>=16){
    let prevC=null;const trs=[];
    for(const c of candles){
      const h=Number(c.high??c.h),l=Number(c.low??c.l),cl=Number(c.close??c.c);
      if(!Number.isFinite(h)||!Number.isFinite(l))continue;
      if(prevC==null){trs.push(Math.max(0,h-l));}
      else{
        const pc=Number.isFinite(prevC)?prevC:cl;
        trs.push(Math.max(h-l,Math.abs(h-pc),Math.abs(l-pc)));
      }
      if(Number.isFinite(cl))prevC=cl;
    }
    if(trs.length>=14){
      let atr=trs.slice(0,14).reduce((a,b)=>a+b,0)/14;
      for(let i=14;i<trs.length;i++)atr=(atr*13+trs[i])/14;
      return atr>0?atr:null;
    }
  }
  if(closes.length>=16){
    // Last resort: mean |Δclose| × scale — better than painting M5 as ATR14.
    let s=0,n=0;
    for(let i=Math.max(1,closes.length-14);i<closes.length;i++){
      s+=Math.abs(closes[i]-closes[i-1]);n++;
    }
    return n?s/n:null;
  }
  return null;
}

function renderDeskIndicators(client,view){
  const el=$('deskIndicators');
  if(!el)return;
  if(!client){stableHtml(el,'');return}
  const a=client.activity||{};
  const mtf=a.mtf_momentum||{};
  const ov=(view&&view.overlays)||{};
  const mtfUi=(view&&view.mtf)||{};
  // ATR14 = M1 only (LIVE brackets). Never fall back to M5 stop_hint (~7.5 on GOLD).
  const atr=deskM1Atr14(view,mtf,ov);
  const adx=mtfUi.adx??ov.adx??((mtf.tf||{}).m5||{}).adx;
  const kama=mtfUi.kama??((mtf.tf||{}).m5||{}).kama??ov.kama_level??((mtf.tf||{}).m1||{}).kama;
  stableHtml(el,[
    deskKv(client.id,'ATR14 M1',atr,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'ADX',adx,v=>v==null?'—':num(v,1)),
    deskKv(client.id,'KAMA5',kama,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'Spread',a.spread!=null?a.spread:((a.market_ask!=null&&a.market_bid!=null)?(Number(a.market_ask)-Number(a.market_bid)):null),
      v=>v==null?'—':num(v,3)),
  ].join(''));
}

function renderDeskCrowd(client){
  const el=$('deskCrowd');
  if(!el)return;
  if(!client){
    stableHtml(el,'');
    renderDeskLean(null);
    return;
  }
  const {buy,hold,sell}=deskScoreAction(client);
  // AI BUY/HOLD/SELL scores → lean bar (NOT exchange crowd positioning).
  const longP=Math.max(0,buy+hold*0.35);
  const shortP=Math.max(0,sell+hold*0.35);
  const sum=Math.max(0.0001,longP+shortP);
  const lp=Math.round((longP/sum)*1000)/10;
  const sp=Math.round((100-lp)*10)/10;
  stableHtml(el,`
    <div class="psx-crowd-lbl"><span class="b">${lp}% Long</span><span class="s">${sp}% Short</span></div>
    <div class="psx-crowd-bar"><i class="b" style="width:${lp}%"></i><i class="s" style="width:${sp}%"></i></div>
    <div class="psx-lean">Derived from neural BUY/HOLD/SELL — not exchange crowd</div>`);
  renderDeskLean(client,lp,sp);
}

function renderDeskLean(client,lp,sp){
  // Duplicate lean panel removed — crowd bar already shows AI score lean.
  const el=$('deskLean');
  if(el)stableHtml(el,'');
}

/** Signal Watch from OHLC structure + swing RR — never tape-Δ chase after the move. */
function deskOhlcSignal(m,focusAct,isFocus){
  const pref=String((m&&m.preferred)||((m&&m.mtf)||{}).preferred||'').toUpperCase();
  const br=(m&&m.swing_brackets)||{};
  const ready=!!br.ready;
  const rr=br.rr!=null&&Number.isFinite(Number(br.rr))?Number(br.rr):null;
  const bias=String(((m&&m.structure)||{}).bias||((m&&m.structure)||{}).event||'');
  if(isFocus&&focusAct){
    const neural=focusAct.act;
    if((pref==='BUY'||pref==='SELL')&&(neural==='BUY'||neural==='SELL')&&neural!==pref){
      return {act:'HOLD',label:`neural≠ohlc ${pref} →`};
    }
    if(neural==='BUY'||neural==='SELL'){
      if((pref==='BUY'||pref==='SELL')&&!ready){
        return {act:'HOLD',label:`ohlc ${pref} wait RR →`};
      }
      const s=neural==='SELL'?focusAct.sell:focusAct.buy;
      const pct=Math.round(Math.max(0,Math.min(1,s))*100);
      if(ready&&rr!=null)return {act:neural,label:`ohlc RR ${num(rr,2)} ${pct}% →`};
      if(ready)return {act:neural,label:`ohlc swing ${pct}% →`};
      return {act:neural,label:`neural ${pct}% →`};
    }
    return {act:'HOLD',label:bias?`ohlc ${bias} →`:'neural HOLD →'};
  }
  if(pref==='BUY'||pref==='SELL'){
    if(ready&&rr!=null)return {act:pref,label:`ohlc RR ${num(rr,2)} →`};
    if(ready)return {act:pref,label:'ohlc swing →'};
    return {act:'HOLD',label:`ohlc ${pref} wait RR →`};
  }
  return {act:'HOLD',label:bias?`ohlc ${bias} →`:'ohlc — →'};
}

function renderDeskSignal(client,markets){
  const el=$('deskSignal');
  if(!el)return;
  const rows=(markets||[]).slice(0,6);
  const focusAct=client?deskScoreAction(client):null;
  const focusEpic=((client&&client.activity)||{}).market
    ||((((client&&client.activity)||{}).mandate)||{}).epic||'';
  if(!rows.length&&client){
    const epic=focusEpic||'FOCUS';
    const syn={
      epic,
      preferred:(client.activity&&client.activity.mtf_momentum
        &&client.activity.mtf_momentum.preferred)||null,
      mtf:(client.activity&&client.activity.mtf_momentum)||null,
      swing_brackets:null,structure:null,
    };
    const sig=deskOhlcSignal(syn,focusAct,true);
    const side=sig.act==='BUY'?'LONG':(sig.act==='SELL'?'SHORT':'HOLD');
    const cls=sig.act==='BUY'?'long':(sig.act==='SELL'?'short':'hold');
    stableHtml(el,`<div class="psx-sig-row"><strong>${esc(epic)}</strong>
      <span class="agree">${esc(sig.label)}</span>
      <span class="psx-sig-btn ${cls}">${side}</span></div>`);
    return;
  }
  if(!rows.length){stableHtml(el,'<div class="psx-empty">No signals</div>');return}
  stableHtml(el,rows.map((m)=>{
    const epic=m.epic||'—';
    const isFocus=!!(focusAct&&focusEpic
      &&String(epic).toUpperCase()===String(focusEpic).toUpperCase());
    const sig=deskOhlcSignal(m,focusAct,isFocus);
    const side=sig.act==='BUY'?'LONG':(sig.act==='SELL'?'SHORT':'HOLD');
    const cls=sig.act==='BUY'?'long':(sig.act==='SELL'?'short':'hold');
    return `<div class="psx-sig-row"><strong>${esc(epic)}</strong>
      <span class="agree">${esc(sig.label)}</span>
      <span class="psx-sig-btn ${cls}">${side}</span></div>`;
  }).join(''));
}

function renderDeskClosed(client){
  const el=$('deskClosed');
  if(!el)return;
  if(!client){stableHtml(el,'<div class="psx-empty">—</div>');return}
  const results=client.results||{};
  let rows=(results.recent_trades||[]).slice(0,12);
  if(deskWhaleFilter==='BUY')rows=rows.filter(t=>{
    const s=String(t.direction||t.side||'').toUpperCase();
    return s.includes('BUY')||s==='LONG';
  });
  if(deskWhaleFilter==='SELL')rows=rows.filter(t=>{
    const s=String(t.direction||t.side||'').toUpperCase();
    return s.includes('SELL')||s==='SHORT';
  });
  if(!rows.length){
    const err=results.stats_error
      ?(`Stats error · ${results.stats_error}`)
      :'No journaled closes yet · Capital Funds may still show £';
    stableHtml(el,`<div class="psx-empty">${esc(err)}</div>`);
    const srcElEmpty=$('deskClosedSrc');
    if(srcElEmpty){
      srcElEmpty.textContent=results.stats_error?'ERROR':'EMPTY';
      srcElEmpty.classList.add('warn');
    }
    return;
  }
  const ccy=((client.activity||{}).balance&&(client.activity.balance.currency))
    ||results.pnl_currency||'';
  const srcEl=$('deskClosedSrc');
  const anyEst=rows.some(t=>{
    const s=String(t.pnl_source||'').toLowerCase();
    return s==='upl_estimate'||t.pending_cash;
  });
  const anyBroker=rows.some(t=>String(t.pnl_source||'').toLowerCase()==='broker');
  const anyEdge=rows.some(t=>!!t.from_edge_hist);
  if(srcEl){
    if(anyEdge&&!anyEst)srcEl.textContent='EDGE £';
    else if(anyEst&&!anyBroker)srcEl.textContent='≈UPL';
    else if(anyEst)srcEl.textContent='MIXED';
    else srcEl.textContent='CAPITAL';
    srcEl.classList.toggle('warn',!!anyEst);
  }
  stableHtml(el,rows.map(t=>{
    const side=String(t.direction||t.side||'?').toUpperCase();
    const sideCls=(side.includes('BUY')||side==='LONG')?'buy':'sell';
    const src=String(t.pnl_source||'').toLowerCase();
    const raw=t.pnl!=null?t.pnl:(t.profit!=null?t.profit:t.upl_hint);
    const broker=src==='broker';
    const hasNum=raw!=null&&Number.isFinite(Number(raw));
    // Any pending/estimate with a number → ≈£… Blank pending only → gaida.
    const isEst=!broker&&hasNum&&(
      src==='upl_estimate'||src==='pending_broker_pnl'||src==='pending_close_sync'
      ||!!t.pending_cash);
    const blankWait=!hasNum&&!!(t.pending_cash||src==='pending_broker_pnl'
      ||src==='pending_close_sync'||src==='upl_estimate');
    const val=broker&&hasNum?money(raw,t.pnl_currency||ccy)
      :(isEst?'≈'+money(raw,t.pnl_currency||ccy)
        :(blankWait?'gaida Capital £':'—'));
    const when=closeTimeLabel(t);
    const epic=t.epic||t.market||'—';
    const badge=closePnlSrcBadge(
      isEst?'upl_estimate':t.pnl_source,blankWait&&!broker);
    const whenShow=when&&when!=='—'?when:'';
    const pnlCls=broker||isEst?pnlClass(raw):'';
    return `<div class="psx-whale-row">
      <div class="wh-top">
        <span class="tag">TRADE</span>
        <strong class="wh-epic">${esc(epic)}</strong>
        ${whenShow?`<span class="wh-ago">${esc(whenShow)}</span>`:''}
      </div>
      <div class="wh-bot">
        <span class="${sideCls}">${sideCls==='buy'?'▲ BUY':'▼ SELL'}</span>
        <strong class="wh-pnl ${pnlCls}">${esc(val)}</strong>
        <span class="${badge.cls}" title="${esc(sourceLabel(isEst?'upl_estimate':(t.pnl_source||(blankWait?'pending_broker_pnl':''))))}">${esc(badge.text)}</span>
      </div>
    </div>`;
  }).join(''));
}


function renderDeskEngine(client){
  const el=$('deskEngine');
  if(!el)return;
  if(!client){
    stableHtml(el,'<div class="psx-empty">Select account</div>');
    return;
  }
  const a=client.activity||{};
  const {buy,hold,sell,act}=deskScoreAction(client);
  const mtf=a.mtf_momentum||{};
  const day=a.day_book||{};
  const dayPhase=day.phase||(day.daily&&day.daily.phase)||'—';
  const strength=act==='SELL'?sell:(act==='BUY'?buy:hold);
  const pctN=Math.round(Math.max(0,Math.min(1,strength))*100);
  const regimeCls=act==='BUY'?'buy':(act==='SELL'?'sell':'hold');
  const ring=act==='SELL'?'#ef6875':(act==='BUY'?'#31d59a':'#e9bc65');
  const side=act==='SELL'?'SHORT':(act==='BUY'?'LONG':'HOLD');
  const regime=act==='SELL'?'BEAR REGIME':(act==='BUY'?'BULL REGIME':'NEUT REGIME');
  const scorePos=Math.round((1-sell)*50+buy*50);
  stableHtml(el,`
    <div class="psx-engine-top">
      <div class="psx-ring" style="--p:${pctN};--ring:${ring}">
        <div class="psx-ring-core">
          <b style="color:${ring}">${pctN}%</b>
          <span>ALIGN</span>
        </div>
      </div>
      <div>
        <div class="psx-short" style="color:${ring}">${esc(side)}</div>
        <div class="psx-regime ${regimeCls}">${esc(regime)}</div>
        <div class="psx-engine-meta">
          Score = neural alignment (not win-rate)<br>
          MTF <strong>${esc(mtf.mode||'—')}${mtf.preferred?(' · '+esc(mtf.preferred)):''}</strong>
          · Day <strong>${esc(dayPhase)}</strong><br>
          Armed <strong>${esc(client.armed?'YES':'NO')}</strong>
          · Exec <strong>${esc(client.execution_enabled?'ON':'OFF')}</strong>
          · Robot <strong>${esc(client.robot_running?'ON':'OFF')}</strong>
        </div>
      </div>
    </div>
    <div class="psx-engine-score">
      <span class="sell" style="color:var(--r)">BEAR</span>
      <div class="psx-engine-track"><i style="left:${Math.max(4,Math.min(96,scorePos))}%"></i></div>
      <span class="buy" style="color:var(--g)">BULL</span>
    </div>`);
}

function renderDeskCheck(client,view){
  const el=$('deskCheck');
  if(!el)return;
  if(!client){stableHtml(el,'');return}
  const a=client.activity||{};
  const mtf=a.mtf_momentum||{};
  const sc=deskScoreAction(client);
  const pos=(Array.isArray(a.positions)&&a.positions[0])||a.position;
  const hasSl=pos&&(pos.has_sl||(pos.stopLevel!=null&&Number(pos.stopLevel)>0));
  const hasTp=pos&&(pos.has_tp||(pos.profitLevel!=null&&Number(pos.profitLevel)>0));
  const ready=a.model_readiness||{};
  const lean=sc.act==='BUY'?'BULL':(sc.act==='SELL'?'BEAR':'NEUT');
  const mtfLean=mtf.preferred||mtf.mode||'';
  // No lean → NEUT (never invent BULL just because MTF is ready).
  const mtfTag=/sell|bear|down/i.test(String(mtfLean))?'BEAR'
    :(/buy|bull|up/i.test(String(mtfLean))?'BULL':'NEUT');
  const canOpen=!!a.entry_can_submit_now;
  const closeLive=a.neural_close_executable!==false&&!a.neural_close_diagnostic_only;
  const rows=[
    {k:'Trend Structure',v:lean,cls:sc.act==='HOLD'?'neu':(sc.act==='SELL'?'bad':'ok')},
    {k:'Momentum',v:mtfTag,cls:mtfTag==='NEUT'?'neu':(mtfTag==='BEAR'?'bad':'ok')},
    {k:'M1 Flow',v:(()=>{const fc=a.flow_consensus||{};const c=String(fc.consensus||'—').toUpperCase();
      return fc.blocked?('BLOCK '+c):c;})(),
      cls:(()=>{const fc=a.flow_consensus||{};const c=String(fc.consensus||'').toUpperCase();
        return fc.blocked?'bad':(c==='BUY'?'ok':(c==='SELL'?'bad':'neu'));})()},
    {k:'Book Balance',v:a.market_status==='TRADEABLE'?'LIVE':'WAIT',
      cls:a.market_status==='TRADEABLE'?'ok':'neu'},
    {k:'Spread state',v:a.spread!=null?(Number(a.spread)<1?'TIGHT':'WIDE'):'—',cls:'neu'},
    {k:'Model',v:ready.ready?'READY':'WAIT',cls:ready.ready?'ok':'neu'},
    {k:'Can OPEN',v:canOpen?'YES':(a.entry_block_reason||'NO'),cls:canOpen?'ok':'bad'},
    {k:'Neural CLOSE',v:closeLive?('LIVE '+(a.neural_exit_intent||'—')):'DIAG ONLY',
      cls:closeLive?'ok':'neu'},
    {k:'SL Bracket',v:hasSl?'ON':'OFF',cls:hasSl?'ok':(pos?'bad':'neu')},
    {k:'TP Bracket',v:hasTp?'ON':'OFF',cls:hasTp?'ok':(pos?'bad':'neu')},
    {k:'Swing Plan',v:(view&&view.swing_brackets&&view.swing_brackets.ready)?'READY':'—',
      cls:(view&&view.swing_brackets&&view.swing_brackets.ready)?'ok':'neu'},
  ];
  stableHtml(el,rows.map(r=>
    `<div class="psx-score-row"><span>${esc(r.k)}</span><b class="${esc(r.cls)}">${esc(String(r.v))}</b></div>`
  ).join(''));
}

function renderDeskIntel(client){
  const el=$('deskIntel');
  if(!el)return;
  if(!client){stableHtml(el,'<div class="psx-empty">—</div>');return}
  const a=client.activity||{};
  const news=a.economic_news_risk;
  const im=a.intermarket_analysis;
  const sit=a.situation_memory;
  const fl=a.forward_live||{};
  const lh=a.learn_horizon||{};
  const chips=[];
  // Full reason text — CSS wraps; do not hard-truncate GATE/NEWS to ellipsis junk.
  const push=(tag,t,cls,src)=>chips.push({tag,t:String(t),cls,src:src||'NEURAL'});
  if(news&&typeof news==='object'){
    const risk=news.risk||news.level||news.status||news.summary;
    const label=news.headline||news.event||news.next||news.reason||risk;
    const bear=String(risk||'').toLowerCase().includes('high')||String(news.status||'').includes('BLOCK');
    if(label)push(bear?'BEARISH':'NEUTRAL',label,bear?'bear':'neu',news.source||'NEWS');
  }else if(typeof news==='string'&&news){
    push('NEWS',news,'neu','FEED');
  }
  if(im&&typeof im==='object'){
    const lean=im.lean||im.bias||im.regime||im.summary||im.status;
    const cls=String(lean||'').toLowerCase().includes('bull')?'bull'
      :(String(lean||'').toLowerCase().includes('bear')?'bear':'neu');
    if(lean)push(cls==='bull'?'BULLISH':(cls==='bear'?'BEARISH':'NEUTRAL'),
      String(lean),cls,'INTERMARKET');
  }
  if(sit&&typeof sit==='object'){
    const tip=sit.tip||sit.summary||sit.last||sit.note;
    if(tip)push('NEUTRAL',tip,'neu','MEMORY');
  }
  if(fl.ok){
    push('NEUTRAL',`Forward n=${fl.n} · false ${fl.false_signal_rate==null?'—':Math.round(fl.false_signal_rate*100)+'%'}`,
      'neu','FORWARD');
  }
  if(lh.message)push('NEUTRAL',lh.message,'neu','LEARN');
  if(a.entry_block_reason)push('BEARISH',a.entry_block_reason,'bear','GATE');
  if(a.fusion_block_reason)push('BEARISH',a.fusion_block_reason,'bear','FUSION');
  if(a.flow_consensus&&typeof a.flow_consensus==='object'){
    const fc=a.flow_consensus;
    const c=String(fc.consensus||'').toUpperCase();
    if(fc.blocked||fc.decision_effect==='blocked_fight'){
      push('BEARISH',fc.decision_reason||fc.reason||'M1 flow blocked OPEN','bear','FLOW');
    }else if(c==='BUY'||c==='SELL'){
      const fx=fc.decision_effect?` · ${fc.decision_effect}`:'';
      push(c==='BUY'?'BULLISH':'BEARISH',
        `M1 flow ${c} · conf ${fc.confidence==null?'—':Math.round(Number(fc.confidence)*100)+'%'}${fx}`,
        c==='BUY'?'bull':'bear','FLOW');
    }else if(fc.reason){
      push('NEUTRAL',`M1 flow ${fc.reason}`, 'neu','FLOW');
    }
  }
  if(a.error)push('BEARISH',a.error,'bear','ERROR');
  if(!chips.length){
    // Still show structural intel from scores so the column never looks empty/old.
    const sc=deskScoreAction(client);
    push(sc.act==='BUY'?'BULLISH':(sc.act==='SELL'?'BEARISH':'NEUTRAL'),
      `Neural argmax ${sc.act} · edge ${a.directional_advantage!=null?num(a.directional_advantage,3):'—'}`,
      sc.act==='BUY'?'bull':(sc.act==='SELL'?'bear':'neu'),'ENGINE');
    push('NEUTRAL',`MTF ${((a.mtf_momentum||{}).mode)||'—'} · bar ${a.bar_time?String(a.bar_time).slice(11,19):'—'}`,
      'neu','MTF');
  }
  stableHtml(el,chips.map(c=>`<article class="psx-intel-card ${esc(c.cls)}">
    <span class="tag">${esc(c.tag)}</span>
    <div class="hd">${esc(c.t)}</div>
    <div class="meta">${esc(c.src)}</div>
  </article>`).join(''));
}

function renderDeskBook(client,view){
  const el=$('deskBook');
  if(!el)return;
  const a=(client&&client.activity)||{};
  const mid=Number(a.market_mid!=null?a.market_mid:(view&&view.mid));
  const bid=Number(a.market_bid);
  const ask=Number(a.market_ask!=null?a.market_ask:a.market_offer);
  if(!Number.isFinite(mid)||mid<=0){
    stableHtml(el,'<div class="psx-empty">No live quote</div>');
    return;
  }
  const hasBa=Number.isFinite(bid)&&Number.isFinite(ask)&&bid>0&&ask>0;
  const spr=hasBa?Math.max(0,ask-bid):null;
  const fc=a.flow_consensus||{};
  const pillars=Array.isArray(fc.pillars)?fc.pillars:[];
  const providers=Array.isArray(fc.providers)?fc.providers:[];
  const cons=String(fc.consensus||'NEUTRAL').toUpperCase();
  const consCls=cons==='BUY'?'bull':(cons==='SELL'?'bear':'neu');
  const effect=String(fc.decision_effect||'');
  const effectTxt=fc.blocked||effect==='blocked_fight'
    ?('BLOCKED '+(fc.decision_reason||''))
    :(effect?effect.replace(/_/g,' '):'');
  const pillarRows=pillars.length?pillars.map(p=>{
    const v=String((p&&p.vote)||'NEUTRAL').toUpperCase();
    const cls=!p||!p.ok?'off':(v==='BUY'?'bull':(v==='SELL'?'bear':'neu'));
    const tag=!p||!p.ok?'—':v;
    return `<div class="psx-flow-row ${cls}">
      <span class="psx-flow-name">${esc((p&&p.label)||(p&&p.id)||'—')}</span>
      <strong>${esc(tag)}</strong>
    </div>`;
  }).join(''):'<div class="psx-empty">Waiting for M1 multi-source votes</div>';
  const activeProv=providers.filter(p=>!p||p.status!=='inactive').slice(0,5)
    .map(p=>esc((p&&p.label)||(p&&p.id)||'')).filter(Boolean).join(' · ')||'—';
  const conf=fc.confidence!=null&&Number.isFinite(Number(fc.confidence))
    ?Math.round(Number(fc.confidence)*100)+'%':'—';
  // Capital CFD has no exchange depth — BID/ASK real; consensus = fused free M1 feeds.
  stableHtml(el,`
    <div class="psx-quote-box">
      <div class="psx-quote-row ask"><span>ASK</span><strong>${hasBa?esc(num(ask,2)):'—'}</strong></div>
      <div class="psx-spread">MID ${esc(num(mid,2))}${spr==null?'':(' · SPREAD '+esc(num(spr,4)))}</div>
      <div class="psx-quote-row bid"><span>BID</span><strong>${hasBa?esc(num(bid,2)):'—'}</strong></div>
    </div>
    <div class="psx-flow">
      <div class="psx-flow-head">
        <span>M1 CONSENSUS</span>
        <strong class="${consCls}">${esc(cons)}</strong>
        <span class="psx-flow-meta">conf ${esc(conf)}</span>
      </div>
      ${pillarRows}
      ${effectTxt?`<div class="psx-flow-src ${fc.blocked?'bear':''}">Decision: ${esc(effectTxt)}</div>`:''}
      <div class="psx-flow-src">Free sources fused: ${activeProv}</div>
      ${(()=>{
        const L=fc.learn||{};
        if(!L||(!L.n&&L.n!==0))return '';
        const help=L.helpful!=null?L.helpful:'—';
        const harm=L.harmful!=null?L.harmful:'—';
        const nud=L.nudge!=null?Number(L.nudge).toFixed(2):'—';
        const blk=L.block_fights?'on':'off';
        return `<div class="psx-flow-src">Learn n=${esc(String(L.n||0))} · help ${esc(String(help))}/${esc(String(harm))} · nudge ${esc(nud)} · block ${esc(blk)}</div>`;
      })()}
      <div class="psx-flow-note">Affects OPEN + learns from £ · Capital = fill only</div>
    </div>`);
}

function renderDeskWatch(markets,focusClient){
  const el=$('deskWatch');
  if(!el)return;
  const rows=(markets||[]).slice(0,7);
  if(!rows.length){stableHtml(el,'<div class="psx-empty">No markets</div>');return}
  const focusEpic=((focusClient&&focusClient.activity)||{}).market||null;
  stableHtml(el,rows.map(m=>{
    const mid=m.mid!=null?num(m.mid,2):'—';
    const candles=(m.candles||[]).slice(-16);
    const ch=deskCandleChange(m.candles);
    const cls=ch==null?'':(ch>=0?'up':'dn');
    const chTxt=ch==null?'—':((ch>=0?'+':'')+ch.toFixed(2)+'%');
    const closes=candles.map(c=>Number(c.close??c.c)).filter(Number.isFinite);
    const lo=Math.min(...closes,0),hi=Math.max(...closes,1);
    const span=Math.max(1e-9,hi-lo);
    const pts=closes.map((v,i)=>{
      const x=(i/Math.max(1,closes.length-1))*64;
      const y=14-((v-lo)/span)*12;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const stroke=(ch!=null&&ch<0)?'#ef6875':'#31d59a';
    const spark=closes.length>1
      ?`<svg width="64" height="16" viewBox="0 0 64 16"><polyline fill="none" stroke="${stroke}" stroke-width="1.2" points="${pts}"/></svg>`
      :'';
    const active=focusEpic&&String(m.epic)===String(focusEpic);
    return `<button type="button" class="psx-watch-row ${active?'active':''}" data-epic="${esc(m.epic||'')}">
      <strong>${esc(m.epic||'—')}</strong>
      <span class="psx-mini">${spark}</span>
      <span>${esc(mid)}</span>
      <span class="${cls}">${esc(chTxt)}</span>
    </button>`;
  }).join(''));
}

function renderDeskFear(client){
  // Panel removed from UI (proxy F&G was decorative). Keep id sink stable.
  const el=$('deskFear');
  if(el)stableHtml(el,'');
}

function _deskCloses(view){
  return ((view&&view.candles)||[]).map(c=>Number(c.close??c.c)).filter(Number.isFinite);
}
function _deskRsi(closes,period=14){
  if(closes.length<period+1)return null;
  let gains=0,losses=0;
  for(let i=1;i<=period;i++){
    const d=closes[i]-closes[i-1];
    if(d>=0)gains+=d;else losses-=d;
  }
  let ag=gains/period,al=losses/period;
  const out=new Array(closes.length).fill(null);
  out[period]=al===0?100:100-100/(1+ag/al);
  for(let i=period+1;i<closes.length;i++){
    const d=closes[i]-closes[i-1];
    const g=Math.max(0,d),l=Math.max(0,-d);
    ag=(ag*(period-1)+g)/period;
    al=(al*(period-1)+l)/period;
    out[i]=al===0?100:100-100/(1+ag/al);
  }
  return out;
}
function _deskMacdHist(closes){
  if(closes.length<35)return null;
  const ema=(arr,p)=>{
    const k=2/(p+1);let prev=null;const o=[];
    for(let i=0;i<arr.length;i++){
      if(i<p-1){o.push(null);continue;}
      if(prev==null){prev=arr.slice(i-p+1,i+1).reduce((a,b)=>a+b,0)/p;}
      else prev=arr[i]*k+prev*(1-k);
      o.push(prev);
    }
    return o;
  };
  const e12=ema(closes,12),e26=ema(closes,26);
  const macd=closes.map((_,i)=>(e12[i]==null||e26[i]==null)?null:e12[i]-e26[i]);
  // Signal EMA only on real MACD points — null→0 poisoned early hist bars.
  const macdPts=macd.filter(v=>v!=null);
  if(macdPts.length<9)return null;
  const sigPts=(()=>{
    const k=2/(9+1);let prev=null;const o=[];
    for(let i=0;i<macdPts.length;i++){
      if(i<8){o.push(null);continue;}
      if(prev==null)prev=macdPts.slice(i-8,i+1).reduce((a,b)=>a+b,0)/9;
      else prev=macdPts[i]*k+prev*(1-k);
      o.push(prev);
    }
    return o;
  })();
  let j=0;
  return macd.map(v=>{
    if(v==null)return null;
    const s=sigPts[j++];
    return s==null?null:v-s;
  });
}
function renderDeskIndCharts(client,view){
  const el=$('deskIndCharts');
  if(!el)return;
  const closes=_deskCloses(view||deskStructure);
  const rsi=_deskRsi(closes);
  const macd=_deskMacdHist(closes);
  if(!rsi||!macd){
    stableHtml(el,'<div class="psx-empty">INSUFFICIENT DATA for RSI/MACD (need OHLC history)</div>');
    return;
  }
  const rsiNow=rsi.filter(v=>v!=null).at(-1);
  const macdNow=macd.filter(v=>v!=null).at(-1);
  const a=(client&&client.activity)||{};
  const mtf=a.mtf_momentum||{};
  const ov=(view&&view.overlays)||{};
  const mtfUi=(view&&view.mtf)||{};
  const atrHead=deskM1Atr14(view,mtf,ov);
  const adxHead=mtfUi.adx??ov.adx??((mtf.tf||{}).m5||{}).adx;
  stableHtml(el,`
    <div class="psx-ind-head">
      <span>RSI <b>${num(rsiNow,1)}</b></span>
      <span>MACD <b class="${macdNow<0?'sell':'buy'}">${num(macdNow,2)}</b></span>
      <span>ADX <b>${adxHead==null?'—':num(adxHead,1)}</b></span>
      <span>ATR14 M1 <b>${atrHead==null?'—':num(atrHead,2)}</b></span>
    </div>
    <div class="psx-ind-canvas"><canvas id="deskRsiCanvas"></canvas></div>
    <div class="psx-ind-canvas"><canvas id="deskMacdCanvas"></canvas></div>`);
  const paintLine=(canvas,series,color)=>{
    if(!canvas)return;
    const parent=canvas.parentElement;const dpr=window.devicePixelRatio||1;
    const w=parent.clientWidth,h=parent.clientHeight;
    canvas.width=Math.floor(w*dpr);canvas.height=Math.floor(h*dpr);
    canvas.style.width=w+'px';canvas.style.height=h+'px';
    const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.fillStyle='#060a14';ctx.fillRect(0,0,w,h);
    const vals=series.map(v=>v==null?50:v);
    const yOf=v=>4+((100-v)/100)*(h-8);
    ctx.strokeStyle='rgba(40,48,59,.8)';
    [30,70].forEach(lvl=>{ctx.beginPath();ctx.moveTo(0,yOf(lvl));ctx.lineTo(w,yOf(lvl));ctx.stroke();});
    ctx.strokeStyle=color;ctx.lineWidth=1.2;ctx.beginPath();
    vals.forEach((v,i)=>{const x=(i/Math.max(1,vals.length-1))*w;const y=yOf(v);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});
    ctx.stroke();
  };
  const paintHist=(canvas,series)=>{
    if(!canvas)return;
    const parent=canvas.parentElement;const dpr=window.devicePixelRatio||1;
    const w=parent.clientWidth,h=parent.clientHeight;
    canvas.width=Math.floor(w*dpr);canvas.height=Math.floor(h*dpr);
    canvas.style.width=w+'px';canvas.style.height=h+'px';
    const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.fillStyle='#060a14';ctx.fillRect(0,0,w,h);
    const vals=series.map(v=>v==null?0:v);
    const max=Math.max(1,...vals.map(Math.abs));
    const mid=h/2;const slot=w/Math.max(1,vals.length);
    vals.forEach((v,i)=>{
      const bh=(Math.abs(v)/max)*(h/2-2);
      ctx.fillStyle=v>=0?'#31d59a':'#ef6875';
      if(v>=0)ctx.fillRect(i*slot,mid-bh,Math.max(1,slot-.5),bh);
      else ctx.fillRect(i*slot,mid,Math.max(1,slot-.5),bh);
    });
    ctx.strokeStyle='rgba(130,139,157,.5)';ctx.beginPath();ctx.moveTo(0,mid);ctx.lineTo(w,mid);ctx.stroke();
  };
  const paintBoth=()=>{
    paintLine($('deskRsiCanvas'),rsi,'#76a0de');
    paintHist($('deskMacdCanvas'),macd);
  };
  requestAnimationFrame(()=>{paintBoth();requestAnimationFrame(paintBoth);});
}

function renderDeskLiveTrades(openRows){
  const el=$('deskDeals');
  if(!el)return;
  const rows=(openRows||[]).slice(0,10);
  if(!rows.length){
    stableHtml(el,'<div class="psx-empty">No live prints</div>');
    return;
  }
  const head=`<div class="psx-trade-row psx-trade-head"><span>EPIC</span><span>SIDE</span><span>PRICE</span><span>SL</span><span>TP</span></div>`;
  const body=rows.map(t=>{
    const side=String(t.direction||t.side||'?').toUpperCase();
    const cls=side==='BUY'||side==='LONG'?'buy':'sell';
    const px=t.current??t.open??t.level??t.entry??t.price??t.mid;
    const tm=esc(t.epic||'—');
    const slN=t.stopLevel!=null&&Number(t.stopLevel)>0?Number(t.stopLevel):null;
    const tpN=t.profitLevel!=null&&Number(t.profitLevel)>0?Number(t.profitLevel):null;
    const slOn=!!(t.has_sl||slN!=null);
    const tpOn=!!(t.has_tp||tpN!=null);
    return `<div class="psx-trade-row">
      <span title="epic">${tm}</span>
      <span class="${cls}">${esc(side==='LONG'?'BUY':(side==='SHORT'?'SELL':side))}</span>
      <span>${esc(px==null?'—':num(px,2))}</span>
      <span class="${slOn?'sl-on':'sl-off'}" title="Capital stopLevel">${esc(slOn&&slN!=null?num(slN,2):'OFF')}</span>
      <span class="${tpOn?'tp-on':'tp-off'}" title="Capital profitLevel">${esc(tpOn&&tpN!=null?num(tpN,2):'OFF')}</span>
    </div>`;
  }).join('');
  stableHtml(el,head+body);
}

function renderDeskUtc(){
  const el=$('deskUtc');
  if(!el)return;
  const d=new Date();
  el.textContent=d.toISOString().slice(11,19)+' UTC';
}

function renderDeskPulse(client){
  const el=$('deskPulse');
  if(!el)return;
  const age=lastSyncAt?Math.round((Date.now()-lastSyncAt)/1000):null;
  const ticks=client&&client.activity?Number(client.activity.tick_count)||0:0;
  el.textContent=`${age==null?'—':age+'s'} · ${ticks} ticks`;
  el.classList.toggle('hot',age!=null&&age<=3);
  el.classList.toggle('stale',age!=null&&age>12);
}

function resampleDeskCandles(candles,tfMin){
  const rows=(candles||[]).slice().sort((a,b)=>String(a.at||'').localeCompare(String(b.at||'')));
  if(!tfMin||tfMin<=1)return rows;
  const buckets=new Map();
  for(const c of rows){
    const t=Date.parse(c.at||c.time||'');
    if(!Number.isFinite(t))continue;
    const bucket=Math.floor(t/(tfMin*60000))*tfMin*60000;
    const o=Number(c.open??c.o),h=Number(c.high??c.h),l=Number(c.low??c.l),cl=Number(c.close??c.c);
    if(![o,h,l,cl].every(Number.isFinite))continue;
    const prev=buckets.get(bucket);
    if(!prev){
      buckets.set(bucket,{at:new Date(bucket).toISOString(),open:o,high:h,low:l,close:cl});
    }else{
      prev.high=Math.max(prev.high,h);
      prev.low=Math.min(prev.low,l);
      prev.close=cl;
    }
  }
  return [...buckets.entries()].sort((a,b)=>a[0]-b[0]).map(([,v])=>v);
}

function drawDeskChart(view){
  const canvas=$('deskChart');
  if(!canvas||typeof drawOhlcStructure!=='function')return;
  const candles=resampleDeskCandles((view&&view.candles)||[],deskTfMinutes);
  if(!view||!candles.length){
    const ctx=canvas.getContext('2d');
    const w=canvas.clientWidth||960,h=canvas.clientHeight||420;
    const dpr=window.devicePixelRatio||1;
    canvas.width=Math.floor(w*dpr);canvas.height=Math.floor(h*dpr);
    ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.fillStyle='#08090c';
    ctx.fillRect(0,0,w,h);
    ctx.fillStyle='#8b919c';ctx.font='12px Manrope,sans-serif';ctx.textAlign='center';
    ctx.fillText('Waiting for Capital OHLC…',w/2,h/2);
    return;
  }
  const drawn={...view,candles};
  const key=[view.epic,view.mid,candles.length,deskTfMinutes,
    JSON.stringify(view.open_levels||[]),
    (view.swing_brackets&&view.swing_brackets.ready)].join('|');
  if(key===lastDeskChartKey)return;
  lastDeskChartKey=key;
  drawOhlcStructure(canvas,drawn,{labels:true,maxCandles:120});
}

async function loadDeskStructure(clientId){
  if(!clientId){deskStructure=null;return null}
  if(deskStructureInFlight)return deskStructureInFlight;
  deskStructureInFlight=(async()=>{
    try{
      const view=await api(`/api/admin/clients/${clientId}/structure-view?n=120`);
      deskStructure=view;
      return view;
    }catch(e){
      deskStructure={ok:false,reason:e.message,candles:[],open_levels:[]};
      return deskStructure;
    }finally{deskStructureInFlight=null}
  })();
  return deskStructureInFlight;
}

async function renderDesk(data){
  let clients=data.clients||[];
  let demoFx=null;
  if(deskDemoMode){
    demoFx=buildDeskDemoFixture();
    // Visual-only overlay — does not call broker start/stop or learning wipe.
    clients=demoFx.overview.clients;
    data={...data,...demoFx.overview,clients};
  }
  const focus=pickDeskFocus(clients);
  deskFocusId=focus?focus.id:null;
  const sel=$('deskFocus');
  if(sel){
    const html=clients.map(c=>
      `<option value="${esc(c.id)}">#${esc(c.id)} ${esc(c.display_name||c.username)}${c.robot_running?' · ON':''}</option>`
    ).join('')||'<option value="">—</option>';
    const prev=sel.value;
    stableHtml(sel,html);
    if(deskFocusId!=null)sel.value=String(deskFocusId);
    else if(prev)sel.value=prev;
  }
  const open=collectOpenTrades(clients);
  renderDeskLiveTrades(open);
  renderDeskUtc();
  renderDeskStatusBoard(focus);
  renderDeskLearn(focus);
  renderShadowLab(focus);
  renderDeskClosed(focus);
  renderDeskEngine(focus);
  renderDeskCrowd(focus);
  renderDeskIntel(focus);
  renderDeskPulse(focus);
  renderDeskFear(focus);
  let markets=[];
  if(demoFx){
    markets=demoFx.markets;
  }else{
    try{
      const board=await api('/api/admin/markets-ohlc?n=90');
      markets=board.markets||[];
    }catch(_){markets=[]}
  }
  renderDeskTape(markets,focus);
  renderDeskWatch(markets,focus);
  renderDeskSignal(focus,markets);
  let view=null;
  if(demoFx){
    view=demoFx.view;
    deskStructure=view;
  }else if(focus){
    const epic=((focus.activity||{}).market)
      ||(((focus.activity||{}).mandate)||{}).epic;
    view=(markets||[]).find(m=>epic&&String(m.epic)===String(epic)
      &&(m.client_ids||[]).map(String).includes(String(focus.id)))
      ||(markets||[]).find(m=>(m.client_ids||[]).map(String).includes(String(focus.id)))
      ||null;
    if(!view||!(view.candles||[]).length){
      view=await loadDeskStructure(focus.id);
    }else{
      deskStructure=view;
    }
  }
  const titleEl=$('deskChartTitle');
  if(titleEl){
    const ep=(view&&view.epic)||((focus&&focus.activity)||{}).market||'INSTRUMENT';
    titleEl.textContent=`${ep} CHART`;
  }
  const epicTitle=$('deskEpicTitle');
  if(epicTitle){
    const ep=(view&&view.epic)||((focus&&focus.activity)||{}).market||'—';
    epicTitle.textContent=ep;
  }
  view=enrichViewOpenLevels(view,focus);
  if(view)deskStructure=view;
  renderDeskQuote(view,focus);
  renderDeskLevels(view,focus);
  renderDeskIndicators(focus,view);
  renderDeskCheck(focus,view);
  renderDeskBook(focus,view);
  renderDeskIndCharts(focus,view);
  lastDeskChartKey='';
  requestAnimationFrame(()=>{
    deskAutoFit();
    lastDeskChartKey='';
    drawDeskChart(view);
    // Second pass after flex layout settles (canvas often 0×0 on first paint).
    requestAnimationFrame(()=>{lastDeskChartKey='';drawDeskChart(view);});
  });
}

function positionMid(p,activity){
  if(!p)return null;
  const a=activity||{};
  const direct=p.currentLevel??p.current_price??p.mark??p.mid??null;
  if(direct!=null && Number.isFinite(Number(direct)))return Number(direct);
  const bid=p.bid??a.market_bid;
  const ask=p.ask??a.market_ask;
  if(bid!=null && ask!=null && Number.isFinite(Number(bid)) && Number.isFinite(Number(ask))){
    return (Number(bid)+Number(ask))/2;
  }
  const m=a.market_mid;
  if(m!=null && Number.isFinite(Number(m)))return Number(m);
  return null;
}

function collectOpenTrades(clients){
  const rows=[];
  (clients||[]).forEach(c=>{
    const a=c.activity||{},r=c.results||{};
    const ccy=r.pnl_currency||c.account_currency||a.currency||'';
    const positions=Array.isArray(a.positions)&&a.positions.length
      ?a.positions
      :(a.position?[a.position]:[]);
    positions.forEach(p=>{
      if(!p)return;
      rows.push({
        client:c.display_name||c.username||('#'+c.id),
        clientId:c.id,
        epic:p.epic||a.market||'—',
        direction:String(p.direction||'').toUpperCase(),
        size:p.size,
        open:p.level,
        current:positionMid(p,a),
        upl:p.upl,
        currency:p.currency&&p.currency!==ccy?p.currency:ccy,
        stopLevel:p.stopLevel,
        profitLevel:p.profitLevel,
        has_sl:p.has_sl,
        has_tp:p.has_tp,
      });
    });
  });
  return rows;
}

function instrumentIcon(epic){
  const e=String(epic||'').toUpperCase();
  if(e.includes('GOLD')||e.includes('XAU'))return 'gold';
  if(e.includes('NAS')||e.includes('US100')||e.includes('SPX')||e.includes('DAX'))return 'index';
  return 'fx';
}

function formatDir(dir){
  if(dir==='BUY'||dir==='PIRKT'){
    return `<span class="dir buy">${icon('up')} PIRKT</span>`;
  }
  if(dir==='SELL'||dir==='PĀRDOT'||dir==='PARDOT'){
    return `<span class="dir sell">${icon('down')} PĀRDOT</span>`;
  }
  return `<span class="dir">${esc(dir||'—')}</span>`;
}

function renderTradesTable(rows,{empty='Nav atvērtu treidu'}={}){
  if(!rows.length)return `<div class="empty">${esc(empty)}</div>`;
  return `<table class="data"><thead><tr>
    <th>Instruments</th><th>Klients</th><th>Virziens</th><th>Apjoms</th>
    <th>Atvēršanas</th><th>Pašreizējā</th><th>P&amp;L</th>
  </tr></thead><tbody>${rows.map(r=>`<tr>
    <td><div class="inst"><span class="badge">${icon(instrumentIcon(r.epic))}</span>${esc(r.epic)}</div></td>
    <td>${esc(r.client)}</td>
    <td>${formatDir(r.direction)}</td>
    <td>${esc(r.size==null?'—':String(r.size))}</td>
    <td>${esc(num(r.open,5))}</td>
    <td>${esc(r.current==null?'—':num(r.current,5))}</td>
    <td class="${pnlClass(r.upl)}">${esc(money(r.upl,r.currency))}</td>
  </tr>`).join('')}</tbody></table>`;
}

function ringSvg(pctVal){
  const p=Math.max(0,Math.min(100,Number(pctVal)||0));
  const r=18,c=2*Math.PI*r,off=c*(1-p/100);
  return `<div class="ring-wrap" title="${p}%">
    <svg width="44" height="44" viewBox="0 0 44 44" aria-hidden="true">
      <circle cx="22" cy="22" r="${r}" fill="none" stroke="rgba(57,255,136,.15)" stroke-width="3.5"/>
      <circle cx="22" cy="22" r="${r}" fill="none" stroke="#39ff88" stroke-width="3.5"
        stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${off}"/>
    </svg><strong>${p}%</strong></div>`;
}

function fleetPnl(s,clients){
  s=s||{};
  let total=s.working_pnl_all;
  let src=s.working_pnl_source||'none';
  let wins=s.display_wins_all;
  let losses=s.display_losses_all;
  const ccy=s.pnl_currency||'';
  // Clear empty / ≈UPL-as-working placeholders — then prefer live open UPL.
  if(total!=null && (src==='none'||src==='empty'||src==='upl_estimate'))total=null;
  if(src==='open_upl'&&s.open_upl_all!=null&&Number.isFinite(Number(s.open_upl_all))){
    total=Number(s.open_upl_all);
  }
  if(total==null && s.open_upl_all!=null && Number.isFinite(Number(s.open_upl_all))){
    total=Number(s.open_upl_all);src='open_upl';
  }
  if(total==null && s.estimate_pnl_all!=null && Number.isFinite(Number(s.estimate_pnl_all))){
    total=Number(s.estimate_pnl_all);src='upl_estimate';
  }
  if(total==null || wins==null || losses==null){
    let sum=0,n=0,w=0,l=0,estSum=0,estN=0,anyEst=false;
    for(const c of clients||[]){
      const sb=scoreboard(c.results||{});
      const res=c.results||{};
      if(sb.src==='upl_estimate')anyEst=true;
      if(sb.pnl!=null && sb.src==='broker'){sum+=sb.pnl;n++}
      if(sb.src==='broker'){w+=sb.wins;l+=sb.losses}
      if(res.estimate_pnl!=null&&Number.isFinite(Number(res.estimate_pnl))){
        estSum+=Number(res.estimate_pnl);estN++;
      }else if(sb.src==='upl_estimate'&&sb.pnl!=null){
        estSum+=sb.pnl;estN++;
      }
    }
    if(total==null && n){total=sum;src='broker'}
    else if(total==null && estN){total=estSum;src='upl_estimate'}
    else if(total==null && anyEst)src='upl_estimate';
    if(wins==null)wins=w;
    if(losses==null)losses=l;
  }
  const by=(src==='open_upl'
    ?(s.open_upl_by_currency||{})
    :(s.working_pnl_by_currency||s.pnl_by_currency||{}));
  const keys=Object.keys(by);
  let pnlLabel,pnlNum=total;
  if(keys.length>1 && total==null){
    pnlLabel=keys.map(k=>`${num(by[k],2)} ${k}`).join(' · ');
    pnlNum=null;
  }else if(pnlNum!=null){
    pnlLabel=formatPnlDisplay(pnlNum,src,ccy);
  }else if(src==='upl_estimate'||Number(s.pending_cash_trades_all)>0){
    pnlLabel='gaida Capital £';
  }else{
    pnlLabel='—';
  }
  const cash=Number(s.cash_trades_all)||0;
  const closed=Number(s.closed_trades_all)||0;
  const est=Number(s.estimate_trades_all)||0;
  const srcNote=` · ${sourceLabel(src)}`+(cash||est?` · £ ${cash}/${closed}`+(est?` · est ${est}`:''):'');
  return {pnlLabel,pnlNote:srcNote,pnlNum,ccy,closed,wins:wins||0,losses:losses||0,src};
}

function renderSummary(data){
  const s=data.summary||{};
  const clients=data.clients||[];
  const open=collectOpenTrades(clients);
  const running=Number(s.running_robots)||0;
  const total=Number(s.clients)||clients.length||0;
  const robotPct=total?Math.round((running/total)*100):0;
  const {pnlLabel,pnlNote,pnlNum,wins,losses}=fleetPnl(s,clients);
  const uplSum=open.reduce((acc,r)=>acc+(Number.isFinite(Number(r.upl))?Number(r.upl):0),0);
  const delta=Number.isFinite(pnlNum)&&Math.abs(pnlNum)>0.01
    ? ((uplSum/(Math.abs(pnlNum)+Math.abs(uplSum)||1))*100)
    : null;

  stableHtml($('summary'),`
    <article class="stat">
      <span class="label">Klienti</span>
      <b>${total}</b>
      <div class="stat-icon">${icon('users')}</div>
      ${total?`<span class="delta-pill">W/L ${wins}/${losses}</span>`:''}
    </article>
    <article class="stat">
      <span class="label">Aktīvie roboti</span>
      <b>${running}</b>
      ${ringSvg(robotPct)}
    </article>
    <article class="stat">
      <span class="label">Atvērtie treidi</span>
      <b>${open.length}</b>
      <div class="stat-icon">${icon('arrows')}</div>
    </article>
    <article class="stat">
      <span class="label">Kopējais P&amp;L</span>
      <b class="${pnlClass(pnlNum)}">${esc(pnlLabel)}<small class="stat-sub">${esc(pnlNote)}</small></b>
      <div class="stat-icon">${icon('layers')}</div>
      ${delta!=null?`<span class="delta-pill ${delta<0?'neg':''}">${delta>=0?'+':''}${delta.toFixed(2)}%</span>`:''}
    </article>`);

  // Performance header prefers live balances, else honest working P&L.
  const {ccy}=fleetPnl(s,clients);
  let bal=null;
  clients.forEach(c=>{
    const balObj=((c.activity||{}).balance)||{};
    const v=balObj.balance??balObj.available??balObj.deposit??null;
    if(v!=null && Number.isFinite(Number(v)))bal=(bal||0)+Number(v);
  });
  if(bal!=null){
    $('perfValue').textContent=money(bal,ccy||'').replace(/^\+/,'');
  }else if(pnlNum!=null){
    $('perfValue').textContent=money(pnlNum,ccy);
  }else{
    $('perfValue').textContent=pnlLabel;
  }
  const el=$('perfDelta');
  if(Number.isFinite(pnlNum) && Math.abs(pnlNum)>0){
    el.textContent=(pnlNum>=0?'+':'')+num(pnlNum,2)+(ccy?' '+ccy:'');
    el.classList.toggle('neg',pnlNum<0);
  }else{
    el.textContent=open.length?`${open.length} open`:'—';
    el.classList.remove('neg');
  }
}

function buildEquitySeries(clients,range){
  const trades=[];
  (clients||[]).forEach(c=>{
    const r=c.results||{};
    const ccy=r.pnl_currency||c.account_currency||'';
    (r.recent_trades||[]).forEach((t)=>{
      // Capital £ closes only — skip ≈UPL and rows without a real close time.
      if(String(t.pnl_source||'').toLowerCase()!=='broker')return;
      const pnl=Number(t.pnl);
      if(!Number.isFinite(pnl))return;
      const ts=Date.parse(t.closed_at||t.closed||t.time||t.ts||'');
      if(!Number.isFinite(ts))return;
      trades.push({ts,pnl,ccy:t.pnl_currency||ccy});
    });
  });
  trades.sort((a,b)=>a.ts-b.ts);
  const now=Date.now();
  const windowMs=range==='1D'?864e5:range==='1W'?7*864e5:range==='1M'?30*864e5:Infinity;
  const filtered=trades.filter(t=>now-t.ts<=windowMs);
  const use=filtered.length?filtered:trades;
  let cum=0;
  const points=use.map(t=>{cum+=t.pnl;return {t:t.ts,v:cum}});
  // Empty → real empty (never invent timestamps or ≈UPL into the curve).
  return points;
}

function drawPerfChart(clients){
  const canvas=$('perfChart');
  if(!canvas)return;
  const pts=buildEquitySeries(clients,chartRange);
  const perfKey=chartRange+'|'+pts.map(p=>p.t+':'+p.v).join(',');
  const dpr=window.devicePixelRatio||1;
  const cssW=canvas.clientWidth||800;
  const cssH=220;
  const bw=Math.floor(cssW*dpr),bh=Math.floor(cssH*dpr);
  if(perfKey===lastPerfKey&&canvas.width===bw&&canvas.height===bh)return;
  lastPerfKey=perfKey;
  if(canvas.width!==bw||canvas.height!==bh){
    canvas.width=bw;canvas.height=bh;
  }
  const ctx=canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const pad={l:8,r:8,t:16,b:28};
  const w=cssW-pad.l-pad.r,h=cssH-pad.t-pad.b;
  ctx.clearRect(0,0,cssW,cssH);
  if(!pts.length){
    ctx.fillStyle='#8a9a92';
    ctx.font='13px Manrope,sans-serif';
    ctx.textAlign='center';
    ctx.fillText('Nav broker cash closes šajā periodā',cssW/2,cssH/2);
    ctx.textAlign='left';
    return;
  }
  const vals=pts.map(p=>p.v);
  let min=Math.min(...vals),max=Math.max(...vals);
  if(min===max){min-=1;max+=1}
  const x=i=>pad.l+(pts.length===1?w/2:(i/(pts.length-1))*w);
  const y=v=>pad.t+h-((v-min)/(max-min))*h;

  // grid
  ctx.strokeStyle='rgba(57,255,136,.08)';
  ctx.lineWidth=1;
  for(let i=0;i<4;i++){
    const gy=pad.t+(h/3)*i;
    ctx.beginPath();ctx.moveTo(pad.l,gy);ctx.lineTo(pad.l+w,gy);ctx.stroke();
  }

  const grad=ctx.createLinearGradient(0,pad.t,0,pad.t+h);
  grad.addColorStop(0,'rgba(57,255,136,.28)');
  grad.addColorStop(1,'rgba(57,255,136,0)');
  ctx.beginPath();
  pts.forEach((p,i)=>{const X=x(i),Y=y(p.v);i?ctx.lineTo(X,Y):ctx.moveTo(X,Y)});
  ctx.lineTo(x(pts.length-1),pad.t+h);
  ctx.lineTo(x(0),pad.t+h);
  ctx.closePath();
  ctx.fillStyle=grad;
  ctx.fill();

  ctx.beginPath();
  pts.forEach((p,i)=>{const X=x(i),Y=y(p.v);i?ctx.lineTo(X,Y):ctx.moveTo(X,Y)});
  ctx.strokeStyle='#39ff88';
  ctx.lineWidth=2.25;
  ctx.lineJoin='round';
  ctx.lineCap='round';
  ctx.shadowColor='rgba(57,255,136,.55)';
  ctx.shadowBlur=10;
  ctx.stroke();
  ctx.shadowBlur=0;

  // date labels
  ctx.fillStyle='#8a9a92';
  ctx.font='11px Manrope,sans-serif';
  const first=new Date(pts[0].t),last=new Date(pts[pts.length-1].t);
  const fmt=d=>d.toLocaleDateString('lv-LV',{day:'numeric',month:'short'});
  ctx.fillText(fmt(first),pad.l,cssH-8);
  const mid=new Date((pts[0].t+pts[pts.length-1].t)/2);
  const midLabel=fmt(mid);
  ctx.fillText(midLabel,pad.l+w/2-ctx.measureText(midLabel).width/2,cssH-8);
  const lastLabel=fmt(last);
  ctx.fillText(lastLabel,pad.l+w-ctx.measureText(lastLabel).width,cssH-8);
}

function renderTopClients(clients){
  const ranked=[...(clients||[])].map(c=>{
    const r=c.results||{};
    const sb=scoreboard(r);
    const total=sb.wins+sb.losses;
    const wr=total?sb.wins/total:(Number.isFinite(Number(r.win_rate))?Number(r.win_rate):null);
    return {
      name:c.display_name||c.username||('#'+c.id),
      user:c.username||'',
      pnl:sb.pnl==null?0:sb.pnl,
      ccy:sb.ccy||r.pnl_currency||c.account_currency||'',
      wr,
      src:sb.src,
      wl:`${sb.wins}/${sb.losses}`,
    };
  }).sort((a,b)=>b.pnl-a.pnl).slice(0,3);

  if(!ranked.length){
    stableHtml($('topClients'),`<div class="empty">Vēl nav klientu</div>`);
    return;
  }
  stableHtml($('topClients'),ranked.map((c,i)=>`
    <div class="rank">
      <span class="medal m${i+1}">${i+1}</span>
      <div class="who">${esc(c.name)}<small>@${esc(c.user)} · ${esc(sourceLabel(c.src))} · W/L ${esc(c.wl)}</small></div>
      <div class="earn ${pnlClass(c.pnl)}">${esc(money(c.pnl,c.ccy))}
        <small>${c.wr==null?'—':((c.wr<=1?c.wr*100:c.wr).toFixed(1)+'% WR')}</small>
      </div>
    </div>`).join(''));
}

function collectRiskAlerts(clients){
  const alerts=[];
  (clients||[]).forEach(c=>{
    const name=c.display_name||c.username||('#'+c.id);
    const hr=c.hard_risk||{};
    const dd=hr.equity_drawdown_pct??hr.drawdown_pct??hr.current_drawdown_pct;
    if(dd!=null && Number(dd)>=5){
      alerts.push({title:`${name} — augsts drawdown`,detail:'Equity drawdown',metric:Number(dd).toFixed(1)+'%'});
    }
    if(hr.trading_halted){
      alerts.push({title:`${name} — tirdzniecība apturēta`,detail:hr.halt_reason||'Hard risk halt',metric:'HALT'});
    }
    const a=c.activity||{};
    if(a.entry_block_reason){
      alerts.push({title:`${name} — ieeja bloķēta`,detail:String(a.entry_block_reason),metric:'BLOCK'});
    }
    if(a.error||c.last_start_error||a.last_start_error){
      alerts.push({title:`${name} — kļūda`,detail:String(a.error||c.last_start_error||a.last_start_error),metric:'ERR'});
    }
    (a.positions||(a.position?[a.position]:[])).forEach(p=>{
      if(!p)return;
      const epic=String(p.epic||'');
      // Heuristic volatility flag when UPL swings hard vs size
      const upl=Math.abs(Number(p.upl)||0);
      if(upl>50){
        alerts.push({title:`${epic||'INSTR'} — paaugstināta kustība`,detail:name,metric:(upl>=0?'+':'')+upl.toFixed(0)});
      }
    });
  });
  return alerts.slice(0,6);
}

function renderRiskAlerts(clients){
  const alerts=collectRiskAlerts(clients);
  if(!alerts.length){
    stableHtml($('riskAlerts'),`<div class="empty">Nav aktīvu brīdinājumu</div>`);
    return;
  }
  stableHtml($('riskAlerts'),alerts.map(a=>`
    <div class="alert">
      <div><b>${esc(a.title)}</b><span>${esc(a.detail)}</span></div>
      <div class="metric">${esc(a.metric)}</div>
    </div>`).join(''));
}

function renderRiskBoard(clients){
  const board=$('riskBoard');
  if(!(clients||[]).length){
    stableHtml(board,`<div class="empty">Nav klientu</div>`);
    return;
  }
  stableHtml(board,`<p class="muted" style="margin:0 0 12px">LIVE: dienas riska režīmi (DEFEND/DONE/PROTECT) <b>nebloķē</b> OPEN. Hard-risk limitu lauki ir tikai informācija.</p>`+clients.map(c=>{
    const hr=c.hard_risk||{};
    const db=c.day_book||{};
    const name=c.display_name||c.username||('#'+c.id);
    const halted=!!hr.trading_halted;
    return `<article class="card risk-card">
      <div class="card-head">
        <h2>${icon('shield')} ${esc(name)}</h2>
        <span class="state-pill ${halted?'pause':(c.robot_running?'on':'off')}">${halted?'HALT':(c.robot_running?'ON':'OFF')}</span>
      </div>
      <div class="kv"><span>Dienas fāze</span><b>${esc(db.phase||'—')} <small>(info)</small></b></div>
      <div class="kv"><span>Dienas riska režīmi</span><b>OFF — LIVE strādā</b></div>
      <div class="kv"><span>Max risks / treids</span><b>${esc(hr.max_risk_per_trade??'—')}</b></div>
      <div class="kv"><span>Dienas zaud. limits</span><b>${esc(hr.daily_loss_limit??'—')}</b></div>
      <div class="kv"><span>Drawdown limits</span><b>${esc(hr.equity_drawdown_limit??'—')}</b></div>
      <div class="kv"><span>Halt iemesls</span><b>${esc(hr.halt_reason||'—')}</b></div>
      <div class="kv"><span>Entry block</span><b>${esc((c.activity||{}).entry_block_reason||'—')}</b></div>
    </article>`;
  }).join(''));
}

function renderRecentClosed(clients){
  const rows=[];
  (clients||[]).forEach(c=>{
    const r=c.results||{};
    const ccy=r.pnl_currency||c.account_currency||'';
    (r.recent_trades||[]).slice(0,8).forEach(t=>{
      rows.push({
        client:c.display_name||c.username,
        epic:t.epic||'—',
        direction:String(t.direction||'').toUpperCase(),
        size:t.size,
        open:t.entry||t.open_level||t.level,
        current:t.exit||t.close_level,
        upl:t.pnl,
        currency:t.pnl_currency||ccy,
      });
    });
  });
  rows.sort((a,b)=>Math.abs(Number(b.upl)||0)-Math.abs(Number(a.upl)||0));
  stableHtml($('recentClosed'),renderTradesTable(rows.slice(0,20),{empty:'Nav nesen slēgtu treidu'}));
}

function trailFlag(phase){
  const p=String(phase||'flat').toLowerCase();
  const labels={
    flat:'TRAIL —',unknown:'TRAIL ?',manual:'TRAIL manual',watch:'TRAIL watch',
    armed:'TRAIL armed',ratchet:'TRAIL ratchet',be_lock:'TRAIL BE',trail:'TRAILING',
  };
  const label=labels[p]||(`TRAIL ${p}`);
  const on=['armed','ratchet','be_lock','trail'].includes(p);
  return `<span class="flag trail-${esc(p)}${on?'':''}">${esc(label)}</span>`;
}

function renderDeal(p,ccy,activity){
  if(!p){
    return `<div class="deal flat">Nav atvērta pozīcija — robots gaida OPEN</div>`;
  }
  const dir=String(p.direction||'').toUpperCase();
  const sideCls=dir==='BUY'?'buy':(dir==='SELL'?'sell':'');
  const lot=p.size!=null?String(p.size):'—';
  // Capital 0 = Off — never treat 0/null as ON.
  const slOn=!!(p.has_sl||(p.stopLevel!=null&&Number(p.stopLevel)>0));
  const tpOn=!!(p.has_tp||(p.profitLevel!=null&&Number(p.profitLevel)>0));
  const trailOn=activity&&activity.trail_enabled===true;
  const phase=trailOn?(p.trail_phase||(p.enrolled?'armed':'flat')):'flat';
  const uplCcy=p.currency&&p.currency!==ccy?p.currency:ccy;
  const mid=positionMid(p,activity);
  const slTxt=(p.stopLevel!=null&&Number(p.stopLevel)>0)?esc(num(p.stopLevel,2)):'OFF';
  const tpTxt=(p.profitLevel!=null&&Number(p.profitLevel)>0)?esc(num(p.profitLevel,2)):'OFF';
  return `<div class="deal">
    <div class="side ${sideCls}">${esc(dir||'—')}</div>
    <div>
      <div class="lot"><em>LOT</em>${esc(lot)}</div>
      <div class="deal-meta">
        <span>Epic <strong>${esc(p.epic||'—')}</strong></span>
        <span>Entry <strong>${esc(num(p.level,2))}</strong></span>
        <span>Mid <strong>${mid==null?'—':esc(num(mid,2))}</strong></span>
        <span>SL <strong>${slTxt}</strong></span>
        <span>TP <strong>${tpTxt}</strong></span>
        <span>UPL <strong class="${pnlClass(p.upl)}">${esc(money(p.upl,uplCcy))}</strong></span>
      </div>
    </div>
    <div class="flags">
      <span class="flag ${slOn?'on':'off'}">SL ${slOn?'ON':'OFF'}</span>
      <span class="flag ${tpOn?'on':'off'}">TP ${tpOn?'ON':'OFF'}</span>
      ${trailOn?trailFlag(phase):'<span class="flag trail-flat">TRAIL OFF</span>'}
    </div>
  </div>`;
}

function renderCard(c){
  const e=c.evolution||{},a=c.activity||{},r=c.results||{};
  const running=!!c.robot_running;
  const cid=String(c.id);
  let sc=c.scores||{};
  const positions=Array.isArray(a.positions)&&a.positions.length
    ?a.positions
    :(a.position?[a.position]:[]);
  const inTrade=positions.length>0;
  if(inTrade){
    const hasLive=!!(Number(sc.BUY)||Number(sc.HOLD)||Number(sc.SELL));
    const fr0=frozenScoresByClient.get(cid);
    const frEmpty=!fr0||!(Number(fr0.BUY)||Number(fr0.HOLD)||Number(fr0.SELL));
    if(hasLive && (!frozenScoresByClient.has(cid) || frEmpty)){
      frozenScoresByClient.set(cid,{BUY:sc.BUY,HOLD:sc.HOLD,SELL:sc.SELL,action:c.action});
    }
    const fr=frozenScoresByClient.get(cid);
    if(fr && (Number(fr.BUY)||Number(fr.HOLD)||Number(fr.SELL))){
      sc=fr;c={...c,action:fr.action||c.action};
    }
  }else{
    frozenScoresByClient.delete(cid);
  }
  const paused=!!c.operator_paused;
  const pill=running?(paused?'pause':'on'):'off';
  const pillText=running?(paused?'PAUZE':'ON'):'OFF';
  const broker=c.broker?`${c.broker.display_name}`:'nav brokera';
  const ccy=r.pnl_currency||c.account_currency||a.currency||'';
  const day=a.day_book||{};
  const dayPhase=day.phase||(day.daily&&day.daily.phase)||'—';
  const mandateSize=(a.mandate&&a.mandate.size)||'—';
  const sb=scoreboard(r);
  const trades=(r.recent_trades||[]).slice(0,4).map(t=>{
    const tCcy=t.pnl_currency||ccy;
    const s=String(t.pnl_source||'').toLowerCase();
    const raw=t.pnl!=null?t.pnl:(t.profit!=null?t.profit:t.upl_hint);
    const broker=s==='broker';
    const hasNum=raw!=null&&Number.isFinite(Number(raw));
    const isEst=!broker&&hasNum&&(
      s==='upl_estimate'||s==='pending_broker_pnl'||s==='pending_close_sync'||!!t.pending_cash);
    const blankWait=!hasNum&&!!(t.pending_cash||s==='pending_broker_pnl'
      ||s==='pending_close_sync'||s==='upl_estimate');
    const showSrc=broker?'broker':(isEst?'upl_estimate':(blankWait?'pending_broker_pnl':s));
    const cls=hasNum?pnlClass(raw):'';
    const srcTag=broker?' [Capital]':(isEst?' [≈UPL]':(blankWait?' [gaida Capital £]':
      (s?` [${s}]`:'')));
    const right=formatPnlDisplay(hasNum?raw:null,showSrc,tCcy);
    return `<div><span>${esc(t.direction||'?')} ${esc(t.epic||'')}${esc(srcTag)}</span><span class="${cls}">${esc(right)}</span></div>`;
  }).join('')||'<div><span>Nav slēgtu treidu</span></div>';

  const dealsHtml=inTrade
    ?positions.map(p=>renderDeal(p,ccy,a)).join('')
    :renderDeal(null,ccy,a);

  const trailNote=(()=>{
    if(!inTrade)return '';
    if(a.trail_enabled===false||a.trail_enabled==null){
      return `<div class="trail-line">Trail OFF — fiksēts OHLC SL/TP</div>`;
    }
    const live=positions.find(p=>p.trail_status)||positions[0];
    const st=live&&live.trail_status;
    const prot=(a.protection&&a.protection.status)||st;
    const active=['armed','ratchet','be_lock','trail'].includes(String((live&&live.trail_phase)||'').toLowerCase());
    return `<div class="trail-line ${active?'active':''}">${esc(prot||'Trail status nav ziņots')}</div>`;
  })();

  const el=document.createElement('article');
  el.className='strip'+(inTrade?' live':'');
  el.innerHTML=`
    <div class="head">
      <div>
        <h3>#${esc(c.id)} ${esc(c.display_name||c.username)}</h3>
        <div class="who">@${esc(c.username)} · ${esc(c.language||'')} · ${esc(broker)}${ccy?' · '+esc(ccy):''}</div>
      </div>
      <span class="state-pill ${pill}">${pillText}</span>
    </div>
    <div class="deals">${dealsHtml}</div>
    ${trailNote}
    <div class="nerve">
      <div class="buy"><em>BUY</em><b>${pct(sc.BUY)}</b></div>
      <div><em>HOLD</em><b>${pct(sc.HOLD)}</b></div>
      <div class="sell"><em>SELL</em><b>${pct(sc.SELL)}</b></div>
    </div>
    <div class="facts">
      ${(()=>{
        const path=a.entry_path||{};
        const intent=a.entry_intent||path.intent||c.proposal||null;
        const trigger=a.entry_trigger||path.trigger||null;
        const wait=a.entry_wait||path.wait||a.entry_block_reason||null;
        const act=c.action||'—';
        let signal=act;
        if(act==='HOLD'&&intent&&intent!=='HOLD')signal=`INTENT ${intent}`;
        else if(act==='BUY'||act==='SELL')signal=act;
        const trigLabel=trigger||(act==='BUY'||act==='SELL'?'open':'—');
        const waitLabel=(act==='HOLD'&&wait)?String(wait).slice(0,72):'—';
        return `
      <div><span>Intent</span><strong>${esc(intent||'—')}</strong></div>
      <div><span>Trigger</span><strong>${esc(trigLabel)}</strong></div>
      <div><span>Wait</span><strong title="${esc(wait||'')}">${esc(waitLabel)}</strong></div>
      <div><span>Signāls</span><strong>${esc(signal)}</strong></div>`;
      })()}
      <div><span>MTF režīms</span><strong>${esc(((a.mtf_momentum||{}).mode)||'—')}${
        (a.mtf_momentum&&a.mtf_momentum.preferred)?(' · '+esc(a.mtf_momentum.preferred)) : ''
      }${(()=>{
        const m=a.mtf_momentum||{};
        const m1=((m.tf||{}).m1||{}).atr14;
        const m5=m.stop_hint_atr14;
        const bits=[];
        if(m1!=null&&Number.isFinite(Number(m1)))bits.push('M1 ATR '+esc(num(m1,2)));
        if(m5!=null&&Number.isFinite(Number(m5)))bits.push('M5 stop '+esc(num(m5,2)));
        return bits.length?(' · '+bits.join(' · ')):'';
      })()}${
        (a.mtf_momentum&&a.mtf_momentum.shared)?' · shared':''
      }</strong></div>
      ${(()=>{
        const pw=(a.mtf_momentum&&(a.mtf_momentum.neural_period_weights||a.mtf_momentum.period_weights))||null;
        if(!pw||typeof pw!=='object')return '';
        const bits=['m1','m5','m15','m30','m60'].map(k=>{
          const v=Number(pw[k]);
          return Number.isFinite(v)?`${k}:${Math.round(v*100)}`:'';
        }).filter(Boolean);
        return bits.length
          ?`<div><span>TF svari (neironi)</span><strong>${esc(bits.join(' '))}</strong></div>`
          :'';
      })()}
      <div><span>Day phase</span><strong>${esc(dayPhase)}</strong></div>
      <div><span>Mandate lot</span><strong>${esc(mandateSize)}</strong></div>
      <div><span>Tirgus (Capital adopt)</span><strong>${esc(
        a.market||(a.mandate&&a.mandate.epic)||'gaida manuālu darījumu')}</strong></div>
      <div><span>Neironi / soļi</span><strong>${esc(e.neurons??'—')} / ${esc(e.steps??0)}</strong></div>
      <div><span>Paaudze</span><strong>${esc(e.learning_generation??e.generation??0)}</strong></div>
      <div><span>Kopējais P&amp;L</span><strong class="${pnlClass(sb.pnl)}">${
        esc(formatPnlDisplay(sb.pnl,sb.src,sb.ccy||ccy))
      }</strong></div>
      <div><span>W / L · avots</span><strong>${esc(`${sb.wins} / ${sb.losses}`)} · ${esc(sourceLabel(sb.src))}</strong></div>
      ${((r.estimate_trades||0)>0&&sb.src!=='open_upl')?`<div><span>≈UPL closes</span><strong>${esc(r.estimate_trades)} gaida Capital history</strong></div>`:''}
      ${(r.pending_cash_trades)?`<div><span>Gaida peļņa</span><strong>${esc(r.pending_cash_trades)} bez skaitļa</strong></div>`:''}
      ${(()=>{
        const fl=a.forward_live||{};
        if(!fl.ok)return '';
        const tip=(fl.tips&&fl.tips[0])?fl.tips[0]:'';
        return `<div><span>Forward-live</span><strong>${esc(
          `n=${fl.n} · false ${(fl.false_signal_rate==null?'—':Math.round(fl.false_signal_rate*100)+'%')} · E ${fl.expectancy==null?'—':fl.expectancy}`
        )}</strong></div>${tip?`<div><span>Forward tip</span><strong>${esc(tip)}</strong></div>`:''}`;
      })()}
    </div>
    <div class="muted" style="padding:8px 14px;font-size:12px">${esc(e.neural_adapt_reason||e.last_reason||'')}${
      (a.entry_block_reason||a.error||c.last_start_error||a.last_start_error)
        ?(' · '+esc(a.entry_block_reason||a.error||c.last_start_error||a.last_start_error))
        :''
    }</div>
    <div class="trades">${trades}</div>
    <div class="actions">
      <button type="button" class="btn tiny btn-primary" data-act="start" data-id="${c.id}">${icon('play')} Start</button>
      <button type="button" class="btn tiny btn-ghost" data-act="stop" data-id="${c.id}">${icon('stop')} Stop</button>
      <button type="button" class="btn tiny btn-ghost" data-act="resume" data-id="${c.id}">${icon('play')} Resume</button>
      <button type="button" class="btn tiny btn-ghost" data-act="ohlc" data-id="${c.id}"
        data-ohlc-name="${esc(c.display_name||c.username||('#'+c.id))}"
        title="Atvērt OHLC logu šim klientam">${icon('candles')} OHLC</button>
      <button type="button" class="btn tiny btn-ghost" data-act="market" data-id="${c.id}" title="Jebkurš Capital tirgus — ne tikai GOLD">${icon('candles')} Mainīt tirgu</button>
      <button type="button" class="btn tiny btn-ghost" data-act="wipe" data-id="${c.id}">${icon('sync')} Mācīties no jauna</button>
    </div>`;
  el.querySelectorAll('[data-act]').forEach(btn=>{
    btn.onclick=async()=>{
      try{
        if(btn.dataset.act==='ohlc'){
          openClientOhlcWindow(btn.dataset.id, btn.dataset.ohlcName||('#'+btn.dataset.id));
          return;
        }
        if(btn.dataset.act==='wipe'){
          const ok=confirm(
            `Izdzēst mācīšanās vēsturi klientam #${btn.dataset.id}?\n`+
            'Klienta login un brokera API paliek. Capital.com pozīcijas netiek aizvērtas.');
          if(!ok)return;
          const typed=prompt('Ieraksti RESET, lai apstiprinātu:');
          if(typed!=='RESET'){alert('Atcelts.');return}
          await api('/api/admin/reset-learning','POST',{
            confirmation:'RESET',client_id:Number(btn.dataset.id)});
        }else if(btn.dataset.act==='market'){
          const ok=confirm(
            `Mainīt tirgu klientam #${btn.dataset.id}?\n\n`+
            '1) Aizver visas Capital pozīcijas šim kontam.\n'+
            '2) Apstiprini — robots noņem veco mandate.\n'+
            '3) Capital.com atver PIRMO darījumu JAUNAJĀ tirgū (US500, EURUSD, OIL… — jebkurš).\n\n'+
            'Entry / SL / TP tad seko tā tirgus OHLC svecēm. Robots NAV piesaistīts tikai GOLD.');
          if(!ok)return;
          const out=await api(
            `/api/admin/clients/${btn.dataset.id}/prepare-new-market`,'POST');
          alert(out.next_step||out.note||'Gaida jaunu manuālu darījumu.');
        }else{
          await api(`/api/admin/clients/${btn.dataset.id}/${btn.dataset.act}`,'POST');
        }
        await loadOverview();
      }catch(err){alert(err.message)}
    };
  });
  el.dataset.cid=String(c.id);
  el.dataset.contentKey=cardContentKey(c);
  return el;
}

function openClientOhlcWindow(clientId,label){
  const id=Number(clientId);
  if(!Number.isFinite(id)||id<=0)return;
  const w=Math.min(1120,Math.max(760,(window.screen&&screen.availWidth||1200)-48));
  const h=Math.min(780,Math.max(520,(window.screen&&screen.availHeight||800)-80));
  const left=Math.max(0,Math.round(((window.screenX||0)+((window.outerWidth||w)-w)/2)));
  const top=Math.max(0,Math.round(((window.screenY||0)+((window.outerHeight||h)-h)/2)));
  const name=`nta_ohlc_${id}`;
  const features=[
    'popup=yes',`width=${Math.round(w)}`,`height=${Math.round(h)}`,
    `left=${left}`,`top=${top}`,'noopener=yes','noreferrer=yes',
  ].join(',');
  const url=`/admin/ohlc?client=${id}${label?('&name='+encodeURIComponent(label)):''}`;
  const win=window.open(url,name,features);
  if(win)try{win.focus()}catch(_){}
}

function formatSyncClock(ts){
  if(!ts)return '';
  try{
    return new Date(ts).toLocaleTimeString('lv-LV',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  }catch(_){return ''}
}

function updateSystemStatus(data){
  const s=data.summary||{};
  const el=$('systemStatus');
  const running=Number(s.running_robots)||0;
  const open=Number(s.open_trades_all)||collectOpenTrades(data.clients||[]).length;
  const alerts=collectRiskAlerts(data.clients||[]);
  const sync=formatSyncClock(lastSyncAt);
  const syncBit=sync?` · ${sync}`:'';
  if(alerts.some(a=>a.metric==='HALT'||a.metric==='ERR')){
    el.classList.add('warn');
    el.querySelector('span:last-child').textContent='Nepieciešama uzmanība'+syncBit;
  }else{
    el.classList.remove('warn');
    let msg='Sistēma aktīva';
    if(running>0)msg+=` · ${running} roboti`;
    if(open>0)msg+=` · ${open} open`;
    el.querySelector('span:last-child').textContent=msg+syncBit;
  }
}

function desiredPollMs(data){
  const deskOn=!!document.querySelector('.view.active[data-view="desk"]');
  const s=(data&&data.summary)||{};
  const running=Number(s.running_robots)||0;
  const open=Number(s.open_trades_all)||collectOpenTrades((data&&data.clients)||[]).length;
  if(deskOn&&(running>0||open>0))return POLL_DESK_MS;
  return (running>0 || open>0)?POLL_LIVE_MS:POLL_IDLE_MS;
}

function schedulePoll(ms){
  if(currentPollMs===ms&&pollTimer)return;
  currentPollMs=ms;
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(()=>loadOverview().catch(()=>{}),ms);
}

async function loadBrokers(){
  const data=await api('/api/admin/brokers');
  const sel=$('brokerType');sel.innerHTML='';
  (data.brokers||[]).forEach(b=>{
    const o=document.createElement('option');o.value=b.id;
    o.textContent=b.label+(b.status==='slot'?' (slot)':'');
    if(b.status==='slot')o.disabled=true;
    sel.appendChild(o);
  });
}

function cardContentKey(c){
  const a=c.activity||{},e=c.evolution||{},r=c.results||{};
  const pos=Array.isArray(a.positions)?a.positions:(a.position?[a.position]:[]);
  const sb=scoreboard(r);
  const mtf=a.mtf_momentum||{};
  return [
    c.id,c.robot_running?1:0,c.operator_paused?1:0,c.action||'',c.proposal||'',
    a.entry_intent||'',a.entry_trigger||'',String(a.entry_wait||'').slice(0,40),
    pos.map(p=>[p.dealId,p.direction,p.size,p.level,p.stopLevel,p.profitLevel,
      p.has_sl,p.has_tp,p.trail_phase,Number(p.upl).toFixed(2),p.epic].join('/')).join('|'),
    (a.day_book&&a.day_book.phase)||'',
    (a.mandate&&a.mandate.size)||'',a.market||'',
    mtf.mode||'',mtf.preferred||'',mtf.stop_hint_atr14??'',
    e.neurons,e.steps,e.learning_generation??e.generation,
    sb.pnl,sb.wins,sb.losses,sb.src,
    r.pending_broker_pnl,r.pending_trades,
    a.entry_block_reason||a.error||c.last_start_error||'',
    (a.protection&&a.protection.status)||'',
    JSON.stringify(c.scores||{}),
  ].join('§');
}

/** Patch live fields on an existing card — never replace the article (no jerk). */
function softPatchCard(el,c){
  const next=renderCard(c);
  el.className=next.className;
  // Keep action buttons (wired once); swap only changing slots.
  const slots=['.head','.deals','.trail-line','.nerve','.facts','.trades'];
  slots.forEach(sel=>{
    const a=el.querySelector(sel);
    const b=next.querySelector(sel);
    if(a&&b){
      if(a.outerHTML!==b.outerHTML)a.replaceWith(b);
    }else if(!a&&b){
      const actions=el.querySelector('.actions');
      if(actions)actions.before(b);else el.appendChild(b);
    }else if(a&&!b){
      a.remove();
    }
  });
  // Reason / error line is a bare .muted without other class.
  const oldMuted=[...el.children].find(n=>n.classList&&n.classList.contains('muted'));
  const newMuted=[...next.children].find(n=>n.classList&&n.classList.contains('muted'));
  if(oldMuted&&newMuted&&oldMuted.outerHTML!==newMuted.outerHTML){
    oldMuted.replaceWith(newMuted);
  }else if(!oldMuted&&newMuted){
    const actions=el.querySelector('.actions');
    if(actions)actions.before(newMuted);else el.appendChild(newMuted);
  }else if(oldMuted&&!newMuted){
    oldMuted.remove();
  }
  el.dataset.contentKey=cardContentKey(c);
}

async function loadOverview(){
  // Coalesce overlapping polls so a slow /overview cannot stack and freeze UI.
  if(overviewInFlight)return overviewInFlight;
  overviewInFlight=(async()=>{
    const data=await api('/api/admin/overview');
    lastOverview=data;
    lastSyncAt=Date.now();
    renderSummary(data);
    updateSystemStatus(data);
    const open=collectOpenTrades(data.clients||[]);
    stableHtml($('openTradesPreview'),renderTradesTable(open.slice(0,8)));
    stableHtml($('openTradesFull'),renderTradesTable(open));
    renderTopClients(data.clients);
    renderRiskAlerts(data.clients);
    renderRiskBoard(data.clients);
    renderRecentClosed(data.clients);
    drawPerfChart(data.clients);
    // Desk uses overview + markets-ohlc / structure-view (real Capital tape).
    try{await renderDesk(data)}catch(err){console.warn('desk',err)}

    const room=$('controlRoom');
    const clients=data.clients||[];
    const n=clients.length;
    room.dataset.count=String(n);
    room.dataset.dense=n>=4?'1':'0';
    room.style.setProperty('--cols',String(layoutCols(n)));
    const pick=$('brokerClient');const prev=pick.value;
    // Rebuild broker pick options without thrashing if identical.
    const pickHtml=(clients||[]).map(c=>
      `<option value="${esc(c.id)}">#${esc(c.id)} ${esc(c.username)} (${esc(c.display_name)})</option>`
    ).join('');
    stableHtml(pick,pickHtml);
    // Soft card sync — never wipe / replace the whole control-room article.
    const keep=new Map();
    [...room.children].forEach(el=>{
      if(el&&el.dataset&&el.dataset.cid)keep.set(String(el.dataset.cid),el);
    });
    const seen=new Set();
    clients.forEach(c=>{
      const id=String(c.id);
      seen.add(id);
      const key=cardContentKey(c);
      const old=keep.get(id);
      if(old){
        if(old.dataset.contentKey!==key){
          softPatchCard(old,c);
        }
        keep.delete(id);
      }else{
        const card=renderCard(c);
        card.dataset.cid=id;
        card.dataset.contentKey=key;
        room.appendChild(card);
      }
    });
    keep.forEach((el,id)=>{if(!seen.has(id))el.remove();});
    if(prev)pick.value=prev;
    schedulePoll(desiredPollMs(data));
  })();
  try{return await overviewInFlight;}
  finally{overviewInFlight=null}
}

async function enterDash(){
  show(false);
  // Best-effort fullscreen from login click gesture; else arm next click.
  const ok=await enterAdminFullscreen();
  if(!ok)armFullscreenOnNextGesture();
  await loadBrokers();
  await loadOverview();
  schedulePoll(desiredPollMs(lastOverview));
}

document.querySelectorAll('.psx-rail-btn[data-view],.nav-item').forEach(btn=>{
  btn.addEventListener('click',()=>setView(btn.dataset.view));
});
document.querySelectorAll('[data-jump]').forEach(btn=>{
  btn.addEventListener('click',()=>setView(btn.dataset.jump));
});
document.querySelectorAll('.range-tab').forEach(btn=>{
  btn.addEventListener('click',()=>{
    chartRange=btn.dataset.range;
    document.querySelectorAll('.range-tab').forEach(b=>b.classList.toggle('active',b===btn));
    if(lastOverview)drawPerfChart(lastOverview.clients);
  });
});
let deskFitRaf=0;
function scheduleDeskAutoFit(){
  if(deskFitRaf)cancelAnimationFrame(deskFitRaf);
  deskFitRaf=requestAnimationFrame(()=>{
    deskFitRaf=0;
    deskAutoFit();
    termViewsAutoFit();
    lastDeskChartKey='';
    if(deskStructure)drawDeskChart(deskStructure);
    if(lastOverview)drawPerfChart(lastOverview.clients);
  });
}
window.addEventListener('resize',scheduleDeskAutoFit);
document.addEventListener('fullscreenchange',scheduleDeskAutoFit);
const deskFocusEl=$('deskFocus');
if(deskFocusEl){
  deskFocusEl.addEventListener('change',()=>{
    deskFocusId=Number(deskFocusEl.value)||null;
    lastDeskChartKey='';
    if(lastOverview)renderDesk(lastOverview).catch(()=>{});
  });
}
document.querySelectorAll('#deskTfBar .psx-tf-btn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('#deskTfBar .psx-tf-btn').forEach(b=>b.classList.remove('on'));
    btn.classList.add('on');
    deskTfMinutes=Number(btn.dataset.tf)||1;
    lastDeskChartKey='';
    if(deskStructure)drawDeskChart(deskStructure);
    else if(lastOverview)renderDesk(lastOverview).catch(()=>{});
  });
});
const deskUiResetBtn=$('deskUiResetBtn');
if(deskUiResetBtn){
  deskUiResetBtn.addEventListener('click',()=>{
    // UI-only reset — does not wipe neural learning history
    deskWhaleFilter='BOTH';
    deskTfMinutes=15;
    document.querySelectorAll('#deskWhaleFilters .psx-mini').forEach(b=>{
      b.classList.toggle('on',b.dataset.whale==='BOTH');
    });
    document.querySelectorAll('#deskTfBar .psx-tf-btn').forEach(b=>{
      b.classList.toggle('on',b.dataset.tf==='15');
    });
    const search=$('deskSearch');
    if(search)search.value='';
    lastDeskChartKey='';
    if(lastOverview)renderDesk(lastOverview).catch(()=>{});
  });
}
document.querySelectorAll('#deskWhaleFilters .psx-mini').forEach(btn=>{
  btn.addEventListener('click',()=>{
    deskWhaleFilter=btn.dataset.whale||'BOTH';
    document.querySelectorAll('#deskWhaleFilters .psx-mini').forEach(b=>{
      b.classList.toggle('on',b===btn);
    });
    if(lastOverview){
      const focus=pickDeskFocus(lastOverview.clients||[]);
      renderDeskClosed(focus);
    }
  });
});
const deskSearch=$('deskSearch');
if(deskSearch){
  deskSearch.addEventListener('input',()=>{
    const q=deskSearch.value.trim().toLowerCase();
    document.querySelectorAll('#deskTape .psx-tape-item').forEach(el=>{
      const ep=(el.querySelector('.ep')?.textContent||'').toLowerCase();
      el.style.display=!q||ep.includes(q)?'':'none';
    });
  });
}
document.getElementById('psDesk')?.addEventListener('click',ev=>{
  const row=ev.target.closest('.psx-watch-row[data-epic]');
  if(!row||!lastOverview)return;
  // Visual focus only — does not change broker mandate
  document.querySelectorAll('.psx-watch-row').forEach(r=>r.classList.toggle('active',r===row));
});
if(deskDemoMode){
  const badge=$('deskDemoBadge');
  if(badge)badge.classList.remove('hidden');
  document.title='NEURAL Desk · DEMO fixture';
}
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible' && !$('dash').classList.contains('hidden')){
    loadOverview().catch(()=>{});
  }
});

$('loginForm').onsubmit=async ev=>{
  ev.preventDefault();$('loginError').textContent='';
  const data=Object.fromEntries(new FormData(ev.target).entries());
  try{await api('/api/admin/login','POST',data);await enterDash()}
  catch(e){$('loginError').textContent=e.message}
};
$('logoutBtn').onclick=async()=>{
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=null;currentPollMs=null;
  try{await api('/api/admin/logout','POST')}finally{show(true)}
};
$('refreshBtn').onclick=()=>loadOverview().catch(e=>alert(e.message));
const fsBtn=$('fullscreenBtn');
if(fsBtn){
  fsBtn.onclick=async()=>{
    if(document.fullscreenElement){
      try{await document.exitFullscreen()}catch(_){}
    }else{
      await enterAdminFullscreen();
    }
  };
}
$('startAllBtn').onclick=async()=>{
  if(deskDemoMode){alert('DEMO mode — trading commands disabled.');return}
  if(!lastOverview)return;
  const targets=(lastOverview.clients||[]).filter(c=>!c.robot_running && c.broker);
  if(!targets.length){alert('Nav apturētu robotu ar brokeri.');return}
  const btn=$('startAllBtn');btn.disabled=true;
  try{
    for(const c of targets){
      try{await api(`/api/admin/clients/${c.id}/start`,'POST')}
      catch(e){console.warn(c.id,e)}
    }
    await loadOverview();
  }finally{btn.disabled=false}
};
$('resetLearningBtn').onclick=async()=>{
  const ok=confirm(
    'Izdzēst VISU mācīšanās vēsturi (neironi, treidi, signāli, policy)?\n\n'+
    'Klienti un brokera API paliek — nav jāreģistrē no jauna.\n'+
    'Capital.com pozīcijas netiek aizvērtas.\n\n'+
    'Visi roboti tiks apturēti. Pēc tam ieteicams restartēt run_windows.bat.');
  if(!ok)return;
  const typed=prompt('Ieraksti RESET (lielajiem burtiem), lai apstiprinātu:');
  if(typed!=='RESET'){alert('Atcelts — vajag precīzi RESET.');return}
  try{
    const res=await api('/api/admin/reset-learning','POST',{confirmation:'RESET'});
    alert(res.message||'Mācīšanās notīrīta. Klienti saglabāti.');
    await loadOverview();
  }catch(e){alert(e.message)}
};
$('createForm').onsubmit=async ev=>{
  ev.preventDefault();$('createMsg').textContent='';
  const data=Object.fromEntries(new FormData(ev.target).entries());
  try{
    const res=await api('/api/admin/clients','POST',data);
    $('createMsg').textContent=
      `Nosūti klientam → /app · login: ${res.client.username} · parole: ${data.password}`;
    ev.target.reset();await loadOverview();
  }catch(e){$('createMsg').textContent=e.message}
};
$('brokerForm').onsubmit=async ev=>{
  ev.preventDefault();$('brokerMsg').textContent='Pārbauda brokeri…';
  const raw=Object.fromEntries(new FormData(ev.target).entries());
  const client_id=Number(raw.client_id);
  const body={broker_type:raw.broker_type,display_name:raw.display_name,api_key:raw.api_key,
    identifier:raw.identifier,password:raw.password};
  try{
    await api('/api/admin/clients/'+client_id+'/broker','POST',body);
    $('brokerMsg').textContent='Brokeris saglabāts, robots palaists.';
    ev.target.reset();await loadOverview();
  }catch(e){$('brokerMsg').textContent=e.message}
};

(async()=>{
  try{await api('/api/admin/me');await enterDash()}
  catch(_){show(true)}
})();
