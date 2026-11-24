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
let deskHeavyInFlight=null;
let deskHeavyDirty=false;
let deskHeavyLastMono=0;
let lastDeskChartKey='';
const deskPrevFields=new Map(); // clientId → {field: previous string}
const deskPrevTicks=new Map();  // clientId → {tick, at}
// Desk near tick rate — Capital WS is live; UI poll must not lag far behind.
// Heavy markets-ohlc / structure-view must NOT block this poll (was 5–33s lag).
const POLL_IDLE_MS=2000;
const POLL_LIVE_MS=400;
const POLL_DESK_MS=120;
// Heavy OHLC board: coalesce + ≥1s gap — was stacking every 120ms until UI stuck.
const DESK_HEAVY_MIN_MS=1000;
// Popup OHLC refresh (only when operator opens a client window — no main-board poll).
const OHLC_REFRESH_MS=250;
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
  // Fit the laptop frame — slightly smaller type so more fits without scroll.
  const fs=clampNum(Math.round(8.5+h/160+w/560),9,12);
  const fsSm=clampNum(fs-1,8,11);
  const fsXs=clampNum(fs-2,7,10);
  const fsMd=clampNum(fs,9,12);
  const fsLg=clampNum(fs+1,10,14);
  const fsXl=clampNum(fs+2,11,16);
  const gap=clampNum(Math.round(2+h/320),2,5);
  const pad=clampNum(gap+1,3,6);
  const headH=clampNum(Math.round(16+fs*0.3),18,24);
  const tapeH=clampNum(Math.round(14+fs*0.2),16,20);
  const statusMin=clampNum(Math.round(32+fs*0.7),36,50);
  let side=clampNum(Math.round(w*0.14),130,220);
  if(w<1200)side=clampNum(Math.round(w*0.13),118,180);
  if(w<980)side=clampNum(Math.round(w*0.12),104,150);
  // fr tracks with soft floors — never demand more height than the viewport.
  const bottomMin=clampNum(Math.round(h*0.22),110,200);
  const topMin=clampNum(Math.round(h*0.20),100,180);
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
  set('--desk-grid-c1','minmax(96px,1fr)');
  set('--desk-grid-c2','minmax(120px,1.15fr)');
  set('--desk-grid-c3','minmax(140px,1.2fr)');
  set('--desk-grid-c4','minmax(140px,1.2fr)');
  set('--desk-grid-r1','minmax('+topMin+'px,1.05fr)');
  set('--desk-grid-r2','minmax('+bottomMin+'px,1.1fr)');
  root.dataset.autofitW=String(Math.round(w));
  root.dataset.autofitH=String(Math.round(h));
  root.dataset.autofitFs=String(fs);
}

/** Non-desk modules: fill the shell frame — no scrollbars. */
function termViewsAutoFit(){
  const shell=$('termShell')||document.querySelector('.term');
  if(!shell)return;
  const h=window.innerHeight;
  const w=window.innerWidth;
  const fs=clampNum(Math.round(7.5+h/150+w/1000),8,11);
  const gap=clampNum(Math.round(2+h/400),2,4);
  const pad=clampNum(gap,2,5);
  shell.style.setProperty('--term-fit-fs',fs+'px');
  shell.style.setProperty('--term-fit-gap',gap+'px');
  shell.style.setProperty('--term-fit-pad',pad+'px');
  document.querySelectorAll('.term-main>.view:not([data-view="desk"]) .term-view').forEach(el=>{
    el.style.minHeight='0';
    el.style.height='100%';
    el.style.overflow='hidden';
  });
  document.querySelectorAll('.term-main>.view .fill-scroll').forEach(el=>{
    el.style.overflow='hidden';
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
/** Account cash currency for open floating P&L — never GOLD quote USD / pnl_currency invent. */
function accountCashCcy(c){
  const a=(c&&c.activity)||{};
  const bal=(a.balance&&typeof a.balance==='object')?a.balance:{};
  const raw=(c&&c.account_currency)||a.currency||bal.currency||'GBP';
  const ccy=String(raw||'GBP').trim().toUpperCase();
  // Capital tags GOLD positions with quote USD — that is NOT account cash.
  if(ccy==='USD'&&String((c&&c.account_currency)||'').toUpperCase()==='GBP')return 'GBP';
  return ccy||'GBP';
}
function esc(s){
  return String(s??'').replace(/[&<>"']/g,c=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function icon(name){return `<svg class="ico" aria-hidden="true"><use href="#i-${esc(name)}"></use></svg>`}

function isCapitalFundsSrc(src){
  // V5 T38: blank / empty is NOT Capital Funds £.
  const s=String(src||'').trim().toLowerCase();
  if(!s)return false;
  return s==='broker'||s==='funds'||s==='capital'||s==='live_funds_mirror'
    ||s==='cash_ledger'||s==='capital_funds'||s==='verified_funds'||s==='cash';
}
/** CLIENT CLOSES = Capital Funds £ only — never UPL/pending guesses (looked “random”). */
function isDeskListableClose(src){
  return isCapitalFundsSrc(src);
}
function sourceLabel(src){
  if(isCapitalFundsSrc(src))return 'Capital £';
  const s=String(src||'').trim().toLowerCase();
  if(!s||s==='none'||s==='empty'||s==='blank'||s==='unverified')return 'nav Capital £';
  if(s==='upl_estimate'||s.startsWith('pending'))return 'sync Funds £';
  return 'nav Capital £';
}
function closePnlSrcBadge(src,_pending){
  // Desk badges: CAPITAL for Funds £; SYNC while journal waits; never blank→CAPITAL.
  if(isCapitalFundsSrc(src))return {cls:'psx-pnl-src ok',text:'CAPITAL'};
  const s=String(src||'').toLowerCase();
  if(s==='upl_estimate'||s.startsWith('pending'))return {cls:'psx-pnl-src est',text:'SYNC'};
  return {cls:'psx-pnl-src est',text:'NAV'};
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
  // Zero-poison broker £0 while closes/estimates exist is NOT Capital truth.
  if(Math.abs(Number(r.working_pnl))<1e-9){
    const closed=Number(r.closed_trades)||0;
    const est=Number(r.estimate_trades)||0;
    const pending=Number(r.pending_cash_trades)||0;
    if(closed>0||est>0||pending>0)return false;
  }
  return true;
}

/** Format P&L for Desk — Capital Funds £ only. Never ≈UPL / gaida / pts. */
function formatPnlDisplay(pnl,src,ccy){
  if(pnl!=null&&Number.isFinite(Number(pnl))&&isCapitalFundsSrc(src)){
    return money(Number(pnl),ccy);
  }
  return '—';
}

/** Capital Funds £ only — never ≈UPL, pending, open UPL, or pts. */
function scoreboard(r){
  r=r||{};
  const ccy=r.pnl_currency||'';
  const ledger=Array.isArray(r.pnl_ledger)?r.pnl_ledger:[];
  const recent=Array.isArray(r.recent_trades)?r.recent_trades:[];
  let pnl=null,src='none',wins=0,losses=0;
  const wsrc=String(r.working_pnl_source||'').toLowerCase();

  if(hasRealWorkingPnl(r) && wsrc==='broker'){
    pnl=Number(r.working_pnl);src='broker';
  }else if(r.realized_pnl_24h!=null && Number.isFinite(Number(r.realized_pnl_24h))
      && (Number(r.cash_trades_24h)||0)>0){
    pnl=Number(r.realized_pnl_24h);src='broker';
  }else if((r.cash_trades||0)>0 && r.realized_pnl!=null && Number.isFinite(Number(r.realized_pnl))){
    pnl=Number(r.realized_pnl);src='broker';
  }

  if(pnl==null && ledger.length){
    let sum=0,w=0,l=0,nBroker=0;
    for(const row of ledger){
      const v=Number(row.pnl);
      if(!Number.isFinite(v))continue;
      if(String(row.source||'').toLowerCase()!=='broker')continue;
      sum+=v;nBroker++;if(v>0)w++;else if(v<0)l++;
    }
    if(nBroker){pnl=sum;src='broker';wins=w;losses=l}
  }
  if(pnl==null && recent.length){
    let sum=0,n=0,w=0,l=0;
    for(const t of recent){
      const v=Number(t.pnl);
      if(!Number.isFinite(v))continue;
      if(!isCapitalFundsSrc(t.pnl_source))continue;
      sum+=v;n++;if(v>0)w++;else if(v<0)l++;
    }
    if(n){pnl=sum;src='broker';wins=w;losses=l}
  }

  if(src==='broker'){
    if(r.display_wins!=null || r.display_losses!=null){
      wins=Number(r.display_wins)||0;losses=Number(r.display_losses)||0;
    }else if(!(wins+losses)){
      wins=Number(r.wins)||0;losses=Number(r.losses)||0;
    }
  }

  return {pnl,src,wins,losses,ccy,closed:Number(r.closed_trades)||0,
    cash:Number(r.cash_trades)||0,estimate:0};
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
  const lab=(ds.shadow_lab&&typeof ds.shadow_lab==='object')?ds.shadow_lab:null;
  if(lab)return lab;
  // Never drop mistake_audit — self_development fallback used to wipe 24h W/L.
  const sd=(a.self_development&&typeof a.self_development==='object')
    ?a.self_development:{};
  return {
    paper:sd.shadow_paper||{},
    hold_shadow:sd.hold_shadow||{},
    structure_shadow:sd.structure_shadow||{},
    shadow_active:!!sd.shadow_active,
    mistake_audit:sd.mistake_audit||{},
    pulse:sd.shadow_active?'WATCHING':'BOOTING',
  };
}

/** 24h W/L for SHADOW — journal/audit first; CLIENT CLOSES results as last resort. */
function auditWindow24(audit,client){
  const w=(audit&&audit.window_24h&&typeof audit.window_24h==='object')
    ?audit.window_24h:{};
  if((Number(w.n)||0)>0)return w;
  const r=(client&&client.results)||{};
  const n=Number(r.cash_trades!=null?r.cash_trades:r.closed_trades)||0;
  if(n<=0)return w;
  const wins=Number(r.display_wins!=null?r.display_wins:r.wins)||0;
  const losses=Number(r.display_losses!=null?r.display_losses:r.losses)||0;
  const net=r.realized_pnl!=null?Number(r.realized_pnl):null;
  return {
    hours:24,n,wins,losses,scratch:0,net,
    useful_n:wins,hurt_n:losses,reads:'wins+losses',
    from_journal:true,from_results:true,
  };
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
  const audit=lab.mistake_audit||{};
  const auditLast=audit.last||{};
  const pendingFix=audit.pending_fix||{};
  const pulse=lab.pulse||'IDLE';
  if(pulseEl){
    pulseEl.textContent='● '+pulse;
    pulseEl.className='psx-live shadow-pulse-pill '+pulse;
  }
  // SHADOW view = CMD only (no duplicate side panels).
  if(truth)stableHtml(truth,'');
  const win24=auditWindow24(audit,client);
  const winN=Number(win24.n)||0;
  const winW=Number(win24.wins)||0;
  const winL=Number(win24.losses)||0;
  const winNet=win24.net!=null?Number(win24.net):null;
  const winNetTxt=winNet==null?'—'
    :((winNet>=0?'+':'')+num(winNet,2));
  const promoteNeed=auditLast.promote_need!=null
    ?auditLast.promote_need
    :(audit.promote_n!=null?audit.promote_n:1);
  const gateTxt=(auditLast.promote_count!=null)
    ?(`${auditLast.promote_count}/${promoteNeed}`)
    :(`0/${promoteNeed}`);
  const fixSteps=Array.isArray(pendingFix.steps)?pendingFix.steps
    :(Array.isArray(auditLast.fix_steps)?auditLast.fix_steps:[]);
  const fixWaiting=fixSteps.some(s=>s&&s.status==='wait');
  const fixState=pendingFix.committed?'COMMITTED'
    :(fixWaiting?'DIAGNOSING'
      :(pendingFix.fingerprint?'PENDING'
        :(fixSteps.length?'DIAGNOSING':'IDLE')));
  const logLines=Array.isArray(audit.learning_log)?audit.learning_log:[];
  const cmdHtml=logLines.length?logLines.map(L=>{
    if(!L||typeof L!=='object')return '';
    const lv=String(L.level||'info');
    return `<div class="shadow-cmd-line ${esc(lv)}">${esc(String(L.text||''))}</div>`;
  }).join(''):'<div class="shadow-cmd-line wait">waiting for Capital / paper closes — nothing audited yet</div>';
  const cmdMeta=(
    `<div class="shadow-cmd-meta">`
    +`<span><b>#${esc(client.id)}</b> ${esc(client.display_name||client.username||'')}</span>`
    +`<span><b>24h</b> ${esc(winN?(`${winW}/${winL}`):'0/0')} · ${esc(winNetTxt)}</span>`
    +`<span><b>gate</b> ${esc(gateTxt)}</span>`
    +`<span><b>fix</b> ${esc(fixState)}</span>`
    +`<span><b>ready</b> ${esc(audit.ready_count!=null?audit.ready_count:0)}</span>`
    +`<span><b>fp</b> ${esc(pendingFix.fingerprint?String(pendingFix.fingerprint).slice(0,8):'—')}</span>`
    +`</div>`
  );
  stableHtml(board,`
    <article class="shadow-card cmdlog cmdlog-only">
      <div class="psx-h"><h2>SHADOW CMD LOG</h2><span class="psx-src">CLOSE→MAP→WRITE</span><span class="psx-live">● LIVE</span></div>
      <div class="body">
        ${cmdMeta}
        <div class="shadow-cmd" id="shadowCmdLog">${cmdHtml}</div>
      </div>
    </article>
  `);
}

function clientOpenPositions(c){
  const a=(c&&c.activity)||{};
  return Array.isArray(a.positions)?a.positions:(a.position?[a.position]:[]);
}

function pickDeskFocus(clients){
  const list=clients||[];
  const withOpen=list.filter(c=>clientOpenPositions(c).length>0);
  // Live open book ALWAYS beats sticky focus — otherwise ATR/chart stay on
  // US100 while OPEN POSITIONS show GOLD (ATR looks "debesīs" for gold).
  if(withOpen.length===1)return withOpen[0];
  if(deskFocusId!=null){
    const keep=list.find(c=>String(c.id)===String(deskFocusId));
    if(keep){
      if(clientOpenPositions(keep).length||!withOpen.length)return keep;
      return withOpen[0];
    }
  }
  if(withOpen.length)return withOpen[0];
  const run=list.find(c=>c.robot_running);
  return run||list[0]||null;
}

/** Epic for Desk ATR/chart — open deal epic wins over mandate after switch. */
function deskFocusEpic(client,view){
  const pos=clientOpenPositions(client);
  for(const p of pos){
    const ep=p&&p.epic;
    if(ep)return String(ep);
  }
  const a=(client&&client.activity)||{};
  return String(
    (view&&view.epic)||a.market||((a.mandate)||{}).epic||''
  )||null;
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
  const mkClient=(id,name,user,scores,action,pos)=>{
    const open=pos?[{epic:pos.epic||'GOLD',direction:pos.dir||'SELL',size:pos.size||0.1,
      level:pos.entry||2634.3,currentLevel:pos.mid||2635.8,upl:pos.upl||-0.3,
      has_sl:true,has_tp:true,stopLevel:pos.sl||2640.2,profitLevel:pos.tp||2629.2}]:[];
    return {
      id,username:user,display_name:name,robot_running:true,armed:true,language:'lv',
      account_currency:'GBP',execution_enabled:false,
      broker:{connected:true,display_name:name},
      scores,action,
      evolution:{neurons:120+id*7,steps:40+id*3,learning_generation:id},
      activity:{
        market:pos?pos.epic:'GOLD',market_mid:pos?pos.mid:2635.8,tick:0.1,
        market_status:'TRADEABLE',tick_count:400+id*10,spread:0.5,
        entry_can_submit_now:!pos,entry_intent:action,entry_wait:pos?null:'Neural HOLD dominates',
        model_readiness:{ready:true},
        mtf_momentum:{mode:action==='HOLD'?'HOLD':'ALIGNED',preferred:action,
          ready:true,neural_period_weights:{m1:0.2,m5:0.2,m15:0.2,m30:0.2,m60:0.2}},
        day_book:{phase:'HUNT'},mandate:{size:0.1+id*0.05,epic:pos?pos.epic:'GOLD'},
        positions:open,position:open[0]||null,
      },
      results:{
        pnl_currency:'GBP',wins:6,losses:0,cash_trades:6,closed_trades:6,
        realized_pnl:0.5*id,pnl_source:'broker',
        recent_trades:[{epic:'GOLD',direction:'BUY',size:0.1,pnl:0.36,pnl_source:'broker',
          entry:2630,exit:2634,closed:'2024-11-20T12:01:00Z'}],
      },
    };
  };
  const client=mkClient(1,'voldis','voldis',{BUY:0.01,HOLD:0.98,SELL:0.01},'HOLD',null);
  client.display_name='DEMO PLATINUM';
  client.username='demo';
  client.scores={BUY:0.14,HOLD:0.13,SELL:0.73};client.action='SELL';
  client.activity.market='BTCUSD';
  client.activity.market_mid=mid;client.activity.market_bid=mid-0.5;client.activity.market_ask=mid+0.5;
  client.activity.spread=1.0;client.activity.tick_count=1284;
  client.activity.economic_news_risk={headline:'Demo: BTC funding skew widens into US open',risk:'elevated',source:'DEMO',status:'WATCH'};
  client.activity.intermarket_analysis={lean:'BEARISH · DXY firm vs risk',bias:'bear'};
  client.activity.situation_memory={tip:'Demo fixture — not a live news feed'};
  client.activity.positions=[{epic:'BTCUSD',direction:'SELL',size:0.25,level:62150,currentLevel:mid,upl:-42.5,
    has_sl:true,has_tp:true,stopLevel:mid+420,profitLevel:mid-680}];
  client.activity.position=client.activity.positions[0];
  client.results.pnl_currency='USD';
  client.results.recent_trades=[
    {epic:'BTCUSD',direction:'SELL',size:1.2,level:62110,closed:'2024-11-20T12:01:00Z',pnl:-18,pnl_source:'broker'},
    {epic:'ETHUSD',direction:'BUY',size:4.0,level:3240,closed:'2024-11-20T11:55:00Z',pnl:62,pnl_source:'broker'},
    {epic:'SOLUSD',direction:'SELL',size:40,level:148.2,closed:'2024-11-20T11:40:00Z',pnl:-11,pnl_source:'upl_estimate'},
    {epic:'BNBUSD',direction:'BUY',size:2.5,level:590,closed:'2024-11-20T11:22:00Z',pnl:24,pnl_source:'broker'},
  ];
  const fleet=[
    client,
    mkClient(2,'guntis','guntis',{BUY:0.29,HOLD:0.58,SELL:0.13},'HOLD',
      {epic:'GOLD',dir:'SELL',size:0.1,entry:2634.31,mid:2635.87,sl:2640.2,tp:2629.2,upl:-0.32}),
    mkClient(3,'dimitrij','dimitrij',{BUY:0.55,HOLD:0.30,SELL:0.15},'BUY',null),
    mkClient(4,'andris','andris',{BUY:0.22,HOLD:0.61,SELL:0.17},'HOLD',null),
  ];
  const view={
    ok:true,epic:'BTCUSD',mid,candles,
    open_levels:[{entry:mid+40,stop:mid+420,profit:mid-680}],
    swing_brackets:{ready:true,entry:mid+40,stop:mid+420,tp1:mid-680},
    overlays:{sl:mid+420,ent:mid+40,tp:mid-680},
  };
  return {overview:{clients:fleet,system:{ok:true}},markets,view,client};
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
  const ccy=accountCashCcy(client);
  const bits=[];
  if(stop!=null)bits.push(`<span class="psx-lvl sl">SL ${esc(num(stop,2))}</span>`);
  if(entry!=null)bits.push(`<span class="psx-lvl ent">ENT ${esc(num(entry,2))}</span>`);
  if(profit!=null)bits.push(`<span class="psx-lvl tp">TP ${esc(num(profit,2))}</span>`);
  // Open floating cash in account currency — never label as inventēts UPL/USD.
  if(upl!=null)bits.push(`<span class="psx-lvl mid">Open ${esc(money(upl,ccy))}</span>`);
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
  const block=humanEntryBlock(ds.entry_block||a.entry_block_reason)||'—';
  const intent=ds.entry_intent||a.entry_intent||a.policy_proposal||'—';
  const closeExec=ds.neural_close_executable!==false&&a.neural_close_executable!==false;
  const exitIntent=ds.neural_exit_intent||a.neural_exit_intent||'—';
  const cons=String((ds.flow_consensus&&ds.flow_consensus.consensus)||fc.consensus||'NEUTRAL').toUpperCase();
  const consCls=cons==='BUY'?'ok':(cons==='SELL'?'bad':'neu');
  // Paper cash £ only — never price pts on Desk hero.
  const shadowCash=Number(shadow.cash_trades||0)||0;
  let shadowNet=shadowCash>0&&shadow.paper_net!=null?Number(shadow.paper_net):null;
  const shadowTrades=shadowCash;
  const results=client.results||{};
  const liveClosed=Number(results.closed_trades!=null?results.closed_trades
    :(samples.closed_trades!=null?samples.closed_trades:0))||0;
  const liveCash=Number(results.cash_trades!=null?results.cash_trades:0)||0;
  const liveReal24=results.realized_pnl_24h!=null?Number(results.realized_pnl_24h):null;
  const liveReal=(liveCash>0&&liveReal24!=null&&Number.isFinite(liveReal24))
    ?liveReal24
    :(liveCash>0&&results.realized_pnl!=null?Number(results.realized_pnl):null);
  const liveLosses=Number(results.display_losses!=null?results.display_losses
    :(results.losses!=null?results.losses:0))||0;
  const liveWins=Number(results.display_wins!=null?results.display_wins
    :(results.wins!=null?results.wins:0))||0;
  // Paper empty → show Capital Funds 24h £ (broker only), never audit invent.
  let shadowHero='paper';
  const auditLabFull=shadowLab(client);
  if((!shadowTrades || shadowNet==null || !Number.isFinite(shadowNet) ||
      (shadowTrades===0 && Math.abs(shadowNet)<1e-9))){
    if(liveReal!=null && Number.isFinite(liveReal) && liveCash>0){
      shadowNet=liveReal;
      shadowHero='live24h';
    }
  }
  const shadowTxt=shadowNet==null?'—'
    :((shadowNet>=0?'+':'')+num(shadowNet,2));
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
  // SHADOW £ = paper book only. LIVE Capital losses live in CLIENT CLOSES —
  // never imply "system at 0" when paper is flat but LIVE has minuses.
  // Prefer live desk_system payload; fall back to shadowLab() mistake_audit.
  const auditLab=(ds.shadow_lab&&ds.shadow_lab.mistake_audit)
    ||(auditLabFull.mistake_audit)||{};
  const auditLast=auditLab.last||{};
  const auditHurt=Number(auditLab.hurt_n)||0;
  const auditPnl=auditLast.pnl!=null?Number(auditLast.pnl):null;
  const shadowSub=(()=>{
    if(shadowHero==='live24h'){
      return 'Capital 24h · W'+liveWins+'/L'+liveLosses
        +' · paper independent'
        +(auditHurt?(' · audit hurts '+auditHurt):'');
    }
    let paperBit=shadowTrades===0
      ?(auditHurt>0
        ?('paper empty · audit hurts '+auditHurt
          +(auditPnl!=null&&Number.isFinite(auditPnl)
            ?(' · last '+(auditPnl>=0?'+':'')+num(auditPnl,2))
            :''))
        :'paper 0 closes')
      :('paper £ '+shadowTrades+' · W'+(shadow.wins||0)+'/L'+(shadow.losses||0));
    if(shadow.audit_backfill)paperBit+=' · from audit';
    if(liveCash>0){
      const liveBit='Capital £ '+liveCash
        +(liveLosses||liveWins?(' · W'+liveWins+'/L'+liveLosses):'')
        +(liveReal!=null&&Number.isFinite(liveReal)
          ?(' · '+(liveReal>=0?'+':'')+num(liveReal,2))
          :'');
      return paperBit+' · '+liveBit;
    }
    return paperBit+(shadowTrades===0&&!auditHurt?' · waits paper SL/TP':'');
  })();
  const tiles=[
    {k:'CAN OPEN',v:can?'YES':'NO',cls:can?'ok':'bad',
      s:can?('Intent '+String(intent).slice(0,18)):String(block).slice(0,48)},
    {k:'NEURAL CLOSE',v:closeExec?'LIVE':'DIAG',cls:closeExec?'ok':'neu',
      s:'Intent '+String(exitIntent).slice(0,24)},
    {k:'M1 FLOW',v:cons,cls:consCls,s:flowSub},
    {k:'STRATEGY CARDS',v:String(cards.n!=null?cards.n:0),cls:(cards.n>0)?'ok':'neu',
      s:(cards.active&&cards.active.length)?cards.active.slice(0,3).join(', '):'none active'},
    {k:shadowHero==='live24h'?'LIVE 24h £':'SHADOW £',v:shadowTxt,
      cls:shadowNet==null?'neu':(shadowNet>=0?'ok':'bad'),
      s:shadowSub},
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
    deskKv(client.id,'Closes',samples.closed_trades!=null?samples.closed_trades
      :((client.results&&client.results.closed_trades)!=null?client.results.closed_trades:'—')),
    deskKv(client.id,'Edge £',lh.edge_closes!=null?lh.edge_closes:'—'),
    deskKv(client.id,'Shadow £',
      (Number(shadow.cash_trades||0)>0?shadow.paper_net:null),
      v=>v==null?'—':num(v,2)),
    deskKv(client.id,'W/L',
      (Number(shadow.cash_trades||0)>0
        ?(`${shadow.wins||0}/${shadow.losses||0}`):'—')),
  ];
  const comboBits=[];
  if(paying.length){
    comboBits.push(`<div class="psx-learn-line ok"><span>PAY</span><b>${esc(paying.slice(0,2).map(p=>`${p.key} ${num(p.mean_pnl,2)}`).join(' · '))}</b></div>`);
  }
  if(bleeding.length){
    comboBits.push(`<div class="psx-learn-line bad"><span>BLEED</span><b>${esc(bleeding.slice(0,2).map(p=>`${p.key} ${num(p.mean_pnl,2)}`).join(' · '))}</b></div>`);
  }
  stableHtml(el,`<div class="psx-learn-kv">${rows.join('')}</div>${comboBits.join('')}`);
}

/** True-range list from OHLC candles (for Wilder ATR + spike clip). */
function deskTrueRanges(candles){
  const trs=[];
  let prevC=null;
  for(const c of candles||[]){
    const h=Number(c.high??c.h),l=Number(c.low??c.l),cl=Number(c.close??c.c);
    if(!Number.isFinite(h)||!Number.isFinite(l))continue;
    if(prevC==null)trs.push(Math.max(0,h-l));
    else{
      const pc=Number.isFinite(prevC)?prevC:cl;
      trs.push(Math.max(h-l,Math.abs(h-pc),Math.abs(l-pc)));
    }
    if(Number.isFinite(cl))prevC=cl;
  }
  return trs;
}

/** Clip sky ATR: Wilder must stay within 6× median TR (gap/spike bars). */
function deskClipAtr(atr,trs){
  if(atr==null||!Number.isFinite(atr)||atr<=0)return null;
  if(!trs||trs.length<8)return atr;
  const sorted=trs.slice(-30).filter(v=>v>0).sort((a,b)=>a-b);
  if(sorted.length<5)return atr;
  const mid=sorted[Math.floor(sorted.length/2)];
  if(!(mid>0))return atr;
  const cap=mid*6;
  return atr>cap?cap:atr;
}

/** M1 ATR14 from completed candles — never invent from M5 stop_hint (~7.5 lie). */
function deskM1Atr14(view,mtf,ov,expectedEpic){
  // Refuse ATR from a different epic than the live book / focus (GOLD vs US100).
  const viewEpic=view&&view.epic?String(view.epic):null;
  if(expectedEpic&&viewEpic&&viewEpic!==String(expectedEpic)){
    // Fall through to candle recompute only if candles match expected — else null.
    const candleEpic=(view.candles&&view.candles[0]&&view.candles[0].epic)||null;
    if(candleEpic&&String(candleEpic)!==String(expectedEpic))return null;
  }
  const mtfUi=(view&&view.mtf)||{};
  const m1tf=((mtf&&mtf.tf)||{}).m1||{};
  // Never take stop_hint / atr14_m5 / M5 atr as Desk ATR14.
  const direct=mtfUi.atr14??m1tf.atr14??(ov&&ov.atr14);
  const candles=(view&&view.candles)||[];
  const trs=deskTrueRanges(candles);
  if(direct!=null&&Number.isFinite(Number(direct))&&Number(direct)>0){
    return deskClipAtr(Number(direct),trs);
  }
  const closes=_deskCloses(view);
  if(trs.length>=14){
    let atr=trs.slice(0,14).reduce((a,b)=>a+b,0)/14;
    for(let i=14;i<trs.length;i++)atr=(atr*13+trs[i])/14;
    return deskClipAtr(atr>0?atr:null,trs);
  }
  if(closes.length>=16){
    // Last resort: mean |Δclose| — better than painting M5 as ATR14.
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
  const epic=deskFocusEpic(client,view);
  // ATR14 = M1 only for the live epic. Never M5 stop_hint; never other-epic ATR.
  const atr=deskM1Atr14(view,mtf,ov,epic);
  const adx=mtfUi.adx??ov.adx??((mtf.tf||{}).m5||{}).adx;
  const kama=mtfUi.kama??((mtf.tf||{}).m5||{}).kama??ov.kama_level??((mtf.tf||{}).m1||{}).kama;
  const atrLabel=epic?`ATR M1 · ${epic}`:'ATR M1';
  stableHtml(el,[
    deskKv(client.id,atrLabel,atr,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'ADX',adx,v=>v==null?'—':num(v,1)),
    deskKv(client.id,'KAMA',kama,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'SPR',a.spread!=null?a.spread:((a.market_ask!=null&&a.market_bid!=null)?(Number(a.market_ask)-Number(a.market_bid)):null),
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
  const rows=(markets||[]).slice(0,4);
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
  // Funds £ first; journal SYNC rows fill NESEN while mirror upgrades to broker.
  let rows=(results.recent_trades||[]).filter(t=>{
    if(!t||typeof t!=='object')return false;
    if(t.pending_cash||t.from_edge_hist)return false;
    if(!isDeskListableClose(t.pnl_source))return false;
    const v=t.pnl!=null?Number(t.pnl):NaN;
    return Number.isFinite(v)&&Math.abs(v)>=1e-12;
  }).slice(0,6);
  if(deskWhaleFilter==='BUY')rows=rows.filter(t=>{
    const s=String(t.direction||t.side||'').toUpperCase();
    return s.includes('BUY')||s==='LONG';
  });
  if(deskWhaleFilter==='SELL')rows=rows.filter(t=>{
    const s=String(t.direction||t.side||'').toUpperCase();
    return s.includes('SELL')||s==='SHORT';
  });
  const closedN=Number(results.closed_trades||0)||0;
  const cashN=Number(results.cash_trades||0)||0;
  if(!rows.length){
    const err=results.stats_error
      ?(`Stats · ${results.stats_error}`)
      :(cashN?'Nav filtra match':'Gaida Funds £');
    stableHtml(el,`<div class="psx-empty">${esc(err)}</div>`);
    const srcElEmpty=$('deskClosedSrc');
    if(srcElEmpty){
      srcElEmpty.textContent=results.stats_error?'ERROR':(cashN?`${cashN} CAPITAL`:'EMPTY');
      srcElEmpty.classList.remove('warn');
    }
    return;
  }
  const ccy=((client.activity||{}).balance&&(client.activity.balance.currency))
    ||results.pnl_currency||'';
  const syncN=rows.filter(t=>!isCapitalFundsSrc(t.pnl_source)).length;
  const srcEl=$('deskClosedSrc');
  if(srcEl){
    if(cashN){
      srcEl.textContent=`${Math.min(rows.length,cashN)}/${cashN} · CAPITAL £`;
      srcEl.classList.remove('warn');
    }else if(syncN){
      srcEl.textContent=`${syncN} · SYNC`;
      srcEl.classList.add('warn');
    }else{
      srcEl.textContent='CAPITAL £';
      srcEl.classList.remove('warn');
    }
  }
  const tip=syncN
    ?'SYNC ≠ phone Funds £ — pēc CLOSE Capital nostāda īsto £ (var atšķirties no Open).'
    :'Phone Funds £ = CAPITAL. Open ir tikai floating pirms CLOSE.';
  stableHtml(el,rows.map(t=>{
    const side=String(t.direction||t.side||'?').toUpperCase();
    const sideCls=(side.includes('BUY')||side==='LONG')?'buy':'sell';
    const raw=Number(t.pnl);
    const val=money(raw,t.pnl_currency||ccy);
    const when=closeTimeLabel(t);
    const epic=t.epic||t.market||'—';
    const src=t.pnl_source||'broker';
    const badge=closePnlSrcBadge(src,false);
    const whenShow=when&&when!=='—'?when:'';
    const pnlCls=pnlClass(raw);
    const capOrphan=String(t.deal_id||'').startsWith('cap:');
    const tag=isCapitalFundsSrc(src)?(capOrphan?'FUNDS':'TRADE'):'SYNC';
    return `<div class="psx-whale-row">
      <div class="wh-top">
        <span class="tag">${tag}</span>
        <strong class="wh-epic">${esc(epic)}</strong>
        ${whenShow?`<span class="wh-ago">${esc(whenShow)}</span>`:''}
      </div>
      <div class="wh-bot">
        <span class="${sideCls}">${sideCls==='buy'?'▲ BUY':'▼ SELL'}</span>
        <strong class="wh-pnl ${pnlCls}">${esc(val)}</strong>
        <span class="${badge.cls}" title="${esc(sourceLabel(src))}">${esc(badge.text)}</span>
      </div>
    </div>`;
  }).join('')+`<div class="psx-intel-tip">${esc(tip)}</div>`);
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
          MTF <strong>${esc(mtf.mode||'—')}${mtf.preferred?(' · '+esc(mtf.preferred)):''}</strong>
          · Day <strong>${esc(dayPhase)}</strong>
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
    {k:'Trend',v:lean,cls:sc.act==='HOLD'?'neu':(sc.act==='SELL'?'bad':'ok')},
    {k:'Momentum',v:mtfTag,cls:mtfTag==='NEUT'?'neu':(mtfTag==='BEAR'?'bad':'ok')},
    {k:'M1 Flow',v:(()=>{const fc=a.flow_consensus||{};const c=String(fc.consensus||'—').toUpperCase();
      return fc.blocked?('BLOCK '+c):c;})(),
      cls:(()=>{const fc=a.flow_consensus||{};const c=String(fc.consensus||'').toUpperCase();
        return fc.blocked?'bad':(c==='BUY'?'ok':(c==='SELL'?'bad':'neu'));})()},
    {k:'Model',v:ready.ready?'READY':(ready.cold_start_open?'COLD':'WAIT'),
      cls:ready.ready?'ok':(ready.cold_start_open?'neu':'neu')},
    {k:'OPEN',v:canOpen?'YES':'NO',cls:canOpen?'ok':'bad'},
    {k:'CLOSE',v:closeLive?'LIVE':'DIAG',cls:closeLive?'ok':'neu'},
    {k:'SL/TP',v:(hasSl?'SL':'—')+'/'+(hasTp?'TP':'—'),
      cls:(hasSl&&hasTp)?'ok':(pos?'bad':'neu')},
    {k:'Swing',v:(view&&view.swing_brackets&&view.swing_brackets.ready)?'READY':'—',
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
  const push=(tag,t,cls,src)=>{
    const line=shortIntelLine(t,src||tag);
    if(!line)return;
    chips.push({tag,t:line,cls,src:src||'NEURAL'});
  };
  if(news&&typeof news==='object'){
    const st=String(news.status||'');
    const risk=news.risk||news.level||st||news.summary;
    const label=news.headline||news.event||news.next||news.reason||risk||st;
    const softBoot=st==='UNMAPPED_INSTRUMENT'||st==='CALENDAR_WAITING'
      ||st==='CALENDAR_FEED_DEGRADED'||st==='UNCONFIGURED';
    const bear=!softBoot&&(st==='NEWS_WINDOW'||st==='HIGH_IMPACT'
      ||st==='CALENDAR_UNAVAILABLE'
      ||String(risk||'').toLowerCase().includes('high')
      ||String(st).includes('BLOCK')
      ||news.entry_allowed===false);
    if(label)push(bear?'BEARISH':(softBoot?'CONTEXT':'NEUTRAL'),
      String(label),
      bear?'bear':'neu',news.source||'NEWS');
  }else if(typeof news==='string'&&news){
    push('NEWS',news,'neu','FEED');
  }
  if(im&&typeof im==='object'){
    const st=String(im.status||'');
    const lean=im.lean||im.bias||im.regime||im.summary||st;
    const peers=Array.isArray(im.peers)?im.peers:[];
    const readyN=peers.filter(p=>p&&p.ready).length;
    const reason=im.reason?String(im.reason):'';
    const softWait=st==='WAITING'||st==='UNAVAILABLE'||st==='BOOTING';
    const cls=softWait?'neu'
      :(st==='CONTRADICTION'||String(lean||'').toLowerCase().includes('bear')?'bear'
      :(String(lean||'').toLowerCase().includes('bull')?'bull':'neu'));
    const line=[(readyN||peers.length)?(`peers ${readyN}/${peers.length}`):null,
      reason||lean||st].filter(Boolean).join(' · ');
    if(line)push(cls==='bull'?'BULLISH':(cls==='bear'?'BEARISH'
      :(softWait?'CONTEXT':'NEUTRAL')),
      line,cls,'INTERMARKET');
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
  if(a.entry_block_reason){
    const hb=humanEntryBlock(a.entry_block_reason);
    if(hb)push('BEARISH',hb,'bear','GATE');
  }
  if(a.fusion_block_reason)push('BEARISH',a.fusion_block_reason,'bear','FUSION');
  if(a.flow_consensus&&typeof a.flow_consensus==='object'){
    const fc=a.flow_consensus;
    const c=String(fc.consensus||'').toUpperCase();
    if(fc.blocked||fc.decision_effect==='blocked_fight'){
      push('BEARISH',fc.decision_reason||fc.reason||'M1 flow blocked OPEN','bear','FLOW');
    }else if(c==='BUY'||c==='SELL'){
      push(c==='BUY'?'BULLISH':'BEARISH',
        `M1 flow ${c} · ${fc.confidence==null?'—':Math.round(Number(fc.confidence)*100)+'%'}`,
        c==='BUY'?'bull':'bear','FLOW');
    }else if(fc.reason){
      push('NEUTRAL',`M1 flow ${fc.reason}`, 'neu','FLOW');
    }
  }
  if(a.error)push('BEARISH',a.error,'bear','ERROR');
  if(!chips.length){
    const sc=deskScoreAction(client);
    push(sc.act==='BUY'?'BULLISH':(sc.act==='SELL'?'BEARISH':'NEUTRAL'),
      `Neural ${sc.act}`,
      sc.act==='BUY'?'bull':(sc.act==='SELL'?'bear':'neu'),'ENGINE');
    push('NEUTRAL',`MTF ${((a.mtf_momentum||{}).mode)||'—'}`,
      'neu','MTF');
  }
  const tip='Open/SYNC var atšķirties no phone Funds £ — Funds nostājas pēc CLOSE.';
  stableHtml(el,chips.map(c=>`<article class="psx-intel-card ${esc(c.cls)}">
    <span class="tag">${esc(c.tag)}</span>
    <div class="hd">${esc(c.t)}</div>
    <div class="meta">${esc(c.src)}</div>
  </article>`).join('')
    +`<div class="psx-intel-tip" title="${esc(tip)}">${esc(tip)}</div>`);
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
  const pillarRows=pillars.length?pillars.slice(0,3).map(p=>{
    const v=String((p&&p.vote)||'NEUTRAL').toUpperCase();
    const cls=!p||!p.ok?'off':(v==='BUY'?'bull':(v==='SELL'?'bear':'neu'));
    const tag=!p||!p.ok?'—':v;
    return `<div class="psx-flow-row ${cls}">
      <span class="psx-flow-name">${esc((p&&p.label)||(p&&p.id)||'—')}</span>
      <strong>${esc(tag)}</strong>
    </div>`;
  }).join(''):'<div class="psx-empty">Waiting for M1 votes</div>';
  const conf=fc.confidence!=null&&Number.isFinite(Number(fc.confidence))
    ?Math.round(Number(fc.confidence)*100)+'%':'—';
  // Capital CFD has no exchange depth — BID/ASK real; consensus = fused free M1 feeds.
  stableHtml(el,`
    <div class="psx-quote-box">
      <div class="psx-quote-row ask"><span>ASK</span><strong>${hasBa?esc(num(ask,2)):'—'}</strong></div>
      <div class="psx-spread">MID ${esc(num(mid,2))}${spr==null?'':(' · SPR '+esc(num(spr,3)))}</div>
      <div class="psx-quote-row bid"><span>BID</span><strong>${hasBa?esc(num(bid,2)):'—'}</strong></div>
    </div>
    <div class="psx-flow">
      <div class="psx-flow-head">
        <span>M1 CONSENSUS</span>
        <strong class="${consCls}">${esc(cons)}</strong>
        <span class="psx-flow-meta">${esc(conf)}</span>
      </div>
      ${pillarRows}
      ${effectTxt?`<div class="psx-flow-src ${fc.blocked?'bear':''}">${esc(effectTxt.slice(0,48))}</div>`:''}
      <div class="psx-flow-note">Affects OPEN + learns from £ · Capital = fill only · Free sources fused</div>
    </div>`);
}

function renderDeskWatch(markets,focusClient){
  const el=$('deskWatch');
  if(!el)return;
  // Frame-fit: epic + mid + Δ only (no spark) — max 4 rows.
  const rows=(markets||[]).slice(0,4);
  if(!rows.length){stableHtml(el,'<div class="psx-empty">No markets</div>');return}
  const focusEpic=((focusClient&&focusClient.activity)||{}).market||null;
  stableHtml(el,rows.map(m=>{
    const mid=m.mid!=null?num(m.mid,2):'—';
    const ch=deskCandleChange(m.candles);
    const cls=ch==null?'':(ch>=0?'up':'dn');
    const chTxt=ch==null?'—':((ch>=0?'+':'')+ch.toFixed(2)+'%');
    const active=focusEpic&&String(m.epic)===String(focusEpic);
    return `<button type="button" class="psx-watch-row ${active?'active':''}" data-epic="${esc(m.epic||'')}">
      <strong>${esc(m.epic||'—')}</strong>
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
  const epicHead=deskFocusEpic(client,view);
  const atrHead=deskM1Atr14(view,mtf,ov,epicHead);
  const adxHead=mtfUi.adx??ov.adx??((mtf.tf||{}).m5||{}).adx;
  stableHtml(el,`
    <div class="psx-ind-head">
      <span><i>RSI</i> <b>${num(rsiNow,1)}</b></span>
      <span><i>MACD</i> <b class="${macdNow<0?'sell':'buy'}">${num(macdNow,2)}</b></span>
      <span><i>ADX</i> <b>${adxHead==null?'—':num(adxHead,1)}</b></span>
      <span title="${esc(epicHead?('M1 '+epicHead):'M1')}"><i>ATR</i> <b>${atrHead==null?'—':num(atrHead,2)}</b></span>
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
    ctx.fillStyle='#050a08';ctx.fillRect(0,0,w,h);
    const vals=series.map(v=>v==null?50:v);
    const yOf=v=>4+((100-v)/100)*(h-8);
    ctx.strokeStyle='rgba(30,52,40,.85)';
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
    ctx.fillStyle='#050a08';ctx.fillRect(0,0,w,h);
    const vals=series.map(v=>v==null?0:v);
    const max=Math.max(1,...vals.map(Math.abs));
    const mid=h/2;const slot=w/Math.max(1,vals.length);
    vals.forEach((v,i)=>{
      const bh=(Math.abs(v)/max)*(h/2-2);
      ctx.fillStyle=v>=0?'#39ff88':'#ef6875';
      if(v>=0)ctx.fillRect(i*slot,mid-bh,Math.max(1,slot-.5),bh);
      else ctx.fillRect(i*slot,mid,Math.max(1,slot-.5),bh);
    });
    ctx.strokeStyle='rgba(128,145,136,.45)';ctx.beginPath();ctx.moveTo(0,mid);ctx.lineTo(w,mid);ctx.stroke();
  };
  const paintBoth=()=>{
    paintLine($('deskRsiCanvas'),rsi,'#39ff88');
    paintHist($('deskMacdCanvas'),macd);
  };
  requestAnimationFrame(()=>{paintBoth();requestAnimationFrame(paintBoth);});
}

function renderDeskLiveTrades(openRows){
  const el=$('deskDeals');
  if(!el)return;
  const rows=(openRows||[]).slice(0,4);
  if(!rows.length){
    stableHtml(el,'<div class="psx-empty">No live prints</div>');
    return;
  }
  const head=`<div class="psx-trade-row psx-trade-head"><span>EPIC</span><span>SIDE</span><span>MID</span><span>SL/TP</span></div>`;
  const body=rows.map(t=>{
    const side=String(t.direction||t.side||'?').toUpperCase();
    const cls=side==='BUY'||side==='LONG'?'buy':'sell';
    const px=t.current??t.open??t.level??t.entry??t.price??t.mid;
    const tm=esc(t.epic||'—');
    const slN=t.stopLevel!=null&&Number(t.stopLevel)>0?Number(t.stopLevel):null;
    const tpN=t.profitLevel!=null&&Number(t.profitLevel)>0?Number(t.profitLevel):null;
    const slOn=!!(t.has_sl||slN!=null);
    const tpOn=!!(t.has_tp||tpN!=null);
    const bracket=(slOn||tpOn)
      ?(`${slOn?'SL':'—'}·${tpOn?'TP':'—'}`)
      :'—';
    const tip=[
      slN!=null?('SL '+num(slN,2)):null,
      tpN!=null?('TP '+num(tpN,2)):null,
    ].filter(Boolean).join(' · ')||'SL/TP';
    return `<div class="psx-trade-row">
      <span title="epic">${tm}</span>
      <span class="${cls}">${esc(side==='LONG'?'BUY':(side==='SHORT'?'SELL':side))}</span>
      <span>${esc(px==null?'—':num(px,1))}</span>
      <span class="${slOn?'sl-on':'sl-off'}" title="${esc(tip)}">${esc(bracket)}</span>
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

/** Paint Desk from overview activity first — never block poll on OHLC HTTP. */
function renderDeskLight(data,focus){
  const open=collectOpenTrades(data.clients||[]);
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
  // Quote / signal / book from live activity — ask/bid must not wait for candles.
  renderDeskQuote(deskStructure,focus);
  renderDeskSignal(focus,[]);
  renderDeskBook(focus,deskStructure);
  renderDeskLevels(deskStructure,focus);
  renderDeskIndicators(focus,deskStructure);
  renderDeskCheck(focus,deskStructure);
  if(deskStructure&&(deskStructure.candles||[]).length){
    renderDeskIndCharts(focus,deskStructure);
    requestAnimationFrame(()=>{
      deskAutoFit();
      lastDeskChartKey='';
      drawDeskChart(deskStructure);
    });
  }
}

async function renderDeskHeavy(focus,demoFx){
  // Coalesce: one heavy at a time; mark dirty so the next run covers skipped polls.
  if(deskHeavyInFlight){
    deskHeavyDirty=true;
    return deskHeavyInFlight;
  }
  const now=Date.now();
  if(deskHeavyLastMono && (now-deskHeavyLastMono)<DESK_HEAVY_MIN_MS){
    deskHeavyDirty=true;
    return null;
  }
  deskHeavyLastMono=now;
  deskHeavyInFlight=(async()=>{
    let markets=[];
    let view=null;
    try{
      if(demoFx){
        markets=demoFx.markets||[];
        view=demoFx.view||null;
      }else{
        try{
          const board=await api('/api/admin/markets-ohlc?n=90');
          markets=board.markets||[];
        }catch(_){markets=[]}
        if(focus){
          // Open deal epic first — never paint US100 ATR while GOLD is live.
          const epic=deskFocusEpic(focus,null)
            ||((focus.activity||{}).market)
            ||(((focus.activity||{}).mandate)||{}).epic;
          view=(markets||[]).find(m=>epic&&String(m.epic)===String(epic)
            &&(m.client_ids||[]).map(String).includes(String(focus.id)))
            ||(markets||[]).find(m=>epic&&String(m.epic)===String(epic))
            ||(markets||[]).find(m=>(m.client_ids||[]).map(String).includes(String(focus.id)))
            ||null;
          if(!view||!(view.candles||[]).length
              ||(epic&&view.epic&&String(view.epic)!==String(epic))){
            view=await loadDeskStructure(focus.id);
          }
        }
      }
      renderDeskTape(markets,focus);
      renderDeskWatch(markets,focus);
      renderDeskSignal(focus,markets);
      view=enrichViewOpenLevels(view,focus);
      if(view)deskStructure=view;
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
      renderDeskQuote(view,focus);
      renderDeskLevels(view,focus);
      renderDeskIndicators(focus,view);
      renderDeskCheck(focus,view);
      renderDeskBook(focus,view);
      renderDeskIndCharts(focus,view);
      requestAnimationFrame(()=>{
        deskAutoFit();
        drawDeskChart(view);
      });
    }finally{
      deskHeavyInFlight=null;
      if(deskHeavyDirty){
        deskHeavyDirty=false;
        // One catch-up pass — still respects DESK_HEAVY_MIN_MS via lastMono.
        const again=focus;
        const fx=demoFx;
        setTimeout(()=>{
          renderDeskHeavy(again,fx).catch(err=>console.warn('desk-heavy',err));
        }, Math.max(50, DESK_HEAVY_MIN_MS-(Date.now()-deskHeavyLastMono)));
      }
    }
  })();
  return deskHeavyInFlight;
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
  // Light paint returns immediately so overview poll stays ≤250ms cadence.
  renderDeskLight(data,focus);
  // Heavy OHLC board/chart — background; never hold overviewInFlight.
  renderDeskHeavy(focus,demoFx).catch(err=>console.warn('desk-heavy',err));
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
    const a=c.activity||{};
    // Never r.pnl_currency here — closed-row USD tags used to paint Open … USD.
    const ccy=accountCashCcy(c);
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
        // Always account cash currency. Capital tags GOLD with quote USD —
        // that is NOT the Funds/UPL settle currency (GBP account → GBP).
        currency:ccy||'GBP',
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
    <th>Atvēršanas</th><th>Pašreizējā</th><th>Open</th>
  </tr></thead><tbody>${rows.map(r=>`<tr>
    <td><div class="inst"><span class="badge">${icon(instrumentIcon(r.epic))}</span>${esc(r.epic)}</div></td>
    <td>${esc(r.client)}</td>
    <td>${formatDir(r.direction)}</td>
    <td>${esc(r.size==null?'—':String(r.size))}</td>
    <td>${esc(num(r.open,5))}</td>
    <td>${esc(r.current==null?'—':num(r.current,5))}</td>
    <td class="${pnlClass(r.upl)}">${esc(money(r.upl,r.currency||'GBP'))}</td>
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
  // Capital Funds £ only — never open UPL / ≈estimate / gaida.
  if(total!=null && src!=='broker')total=null;
  if(total==null || wins==null || losses==null){
    let sum=0,n=0,w=0,l=0;
    for(const c of clients||[]){
      const sb=scoreboard(c.results||{});
      if(sb.pnl!=null && sb.src==='broker'){sum+=sb.pnl;n++}
      if(sb.src==='broker'){w+=sb.wins;l+=sb.losses}
    }
    if(total==null && n){total=sum;src='broker'}
    if(wins==null)wins=w;
    if(losses==null)losses=l;
  }
  const by=s.working_pnl_by_currency||s.pnl_by_currency||{};
  const keys=Object.keys(by);
  let pnlLabel,pnlNum=total;
  if(keys.length>1 && total==null){
    pnlLabel=keys.map(k=>`${num(by[k],2)} ${k}`).join(' · ');
    pnlNum=null;
  }else if(pnlNum!=null && src==='broker'){
    pnlLabel=formatPnlDisplay(pnlNum,'broker',ccy);
  }else{
    pnlLabel='—';
    pnlNum=null;
    src='none';
  }
  const cash=Number(s.cash_trades_all)||0;
  const closed=Number(s.closed_trades_all)||0;
  const srcNote=` · Capital £`+(cash?` · ${cash}/${closed||cash}`:'');
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
      <b class="${pnlClass(pnlNum)}">${esc(pnlNum==null?'gaida Funds £':pnlLabel)}<small class="stat-sub">${esc(
        pnlNum==null?' · nav Capital closes':' · Capital £')}</small></b>
      <div class="stat-icon">${icon('layers')}</div>
      ${delta!=null?`<span class="delta-pill ${delta<0?'neg':''}">${delta>=0?'+':''}${delta.toFixed(2)}%</span>`:''}
    </article>`);

  // PERFORMANCE = focused client equity (never sum all 4 accounts into one number).
  const {ccy}=fleetPnl(s,clients);
  const focusId=$('deskFocus')&&$('deskFocus').value;
  const focus=clients.find(c=>String(c.id)===String(focusId))||clients[0]||null;
  const focusBal=(()=>{
    if(!focus)return null;
    const balObj=((focus.activity||{}).balance)||{};
    const v=balObj.balance??balObj.available??balObj.deposit??null;
    return (v!=null&&Number.isFinite(Number(v)))?Number(v):null;
  })();
  const focusCcy=(focus&&(focus.account_currency||(focus.results||{}).pnl_currency))||ccy||'GBP';
  const focusName=focus?(focus.display_name||focus.username||('#'+focus.id)):'—';
  if(focusBal!=null){
    $('perfValue').textContent=money(focusBal,focusCcy).replace(/^\+/,'');
  }else if(pnlNum!=null){
    $('perfValue').textContent=money(pnlNum,focusCcy);
  }else{
    $('perfValue').textContent='—';
  }
  const el=$('perfDelta');
  el.textContent=focus?`${focusName} · equity`:(open.length?`${open.length} open`:'—');
  el.classList.remove('neg');
  // Per-client equity strip — each account separate (no fleet sum).
  const strip=$('perfClients');
  if(strip){
    const bits=(clients||[]).map(c=>{
      const balObj=((c.activity||{}).balance)||{};
      const v=balObj.balance??balObj.available??balObj.deposit??null;
      const ccyC=c.account_currency||((c.results||{}).pnl_currency)||'GBP';
      const nm=c.display_name||c.username||('#'+c.id);
      const on=focus&&String(c.id)===String(focus.id)?' on':'';
      const val=(v!=null&&Number.isFinite(Number(v)))?money(Number(v),ccyC).replace(/^\+/,''):'—';
      return `<button type="button" class="perf-client${on}" data-perf-id="${c.id}" title="${esc(nm)}">
        <span>${esc(nm)}</span><strong>${esc(val)}</strong></button>`;
    }).join('');
    stableHtml(strip,bits||'<span class="muted">Nav klientu</span>');
    strip.querySelectorAll('[data-perf-id]').forEach(btn=>{
      btn.onclick=()=>{
        const sel=$('deskFocus');
        if(sel){sel.value=btn.dataset.perfId;sel.dispatchEvent(new Event('change',{bubbles:true}))}
      };
    });
  }
}

function parseCloseTs(raw){
  if(raw==null||raw==='')return NaN;
  let s=String(raw).trim();
  if(!s)return NaN;
  // Capital often returns "2026-11-23T21:15:00" without Z — treat as UTC.
  if(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)&&!/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)){
    s=s.replace(/ /,'T')+'Z';
  }
  const ts=Date.parse(s);
  return Number.isFinite(ts)?ts:NaN;
}

function buildEquitySeries(clients,range){
  const trades=[];
  const focusId=$('deskFocus')&&$('deskFocus').value;
  const pool=(clients||[]).filter(c=>!focusId||String(c.id)===String(focusId));
  const useClients=pool.length?pool:(clients||[]);
  useClients.forEach(c=>{
    const r=c.results||{};
    const ccy=r.pnl_currency||c.account_currency||'GBP';
    (r.recent_trades||[]).forEach((t)=>{
      // Chart = Funds £ only (not SYNC estimates) — Kopējais stays honest.
      if(!isCapitalFundsSrc(t.pnl_source))return;
      const pnl=Number(t.pnl);
      if(!Number.isFinite(pnl)||Math.abs(pnl)<1e-12)return;
      const ts=parseCloseTs(t.closed_at||t.closed||t.time||t.ts||'');
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
  // Match CSS #perfChart height — never paint 220px over client chips.
  const cssH=Math.min(100,canvas.clientHeight||100);
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
    ctx.fillText('Nav Capital Funds £ closes šajā periodā',cssW/2,cssH/2-8);
    ctx.font='11px Manrope,sans-serif';
    ctx.fillText('Gaida /api/v1/history/transactions — ne UPL',cssW/2,cssH/2+12);
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
      pnl:sb.pnl,  // keep null — never invent £0 for clients without cash
      ccy:sb.ccy||r.pnl_currency||c.account_currency||'',
      wr,
      src:sb.src,
      wl:`${sb.wins}/${sb.losses}`,
    };
  }).sort((a,b)=>(Number(b.pnl)||-1e99)-(Number(a.pnl)||-1e99)).slice(0,3);

  if(!ranked.length){
    stableHtml($('topClients'),`<div class="empty">Vēl nav klientu</div>`);
    return;
  }
  stableHtml($('topClients'),ranked.map((c,i)=>`
    <div class="rank">
      <span class="medal m${i+1}">${i+1}</span>
      <div class="who">${esc(c.name)}<small>@${esc(c.user)} · ${esc(sourceLabel(c.src))} · W/L ${esc(c.wl)}</small></div>
      <div class="earn ${c.pnl==null?'neu':pnlClass(c.pnl)}">${esc(
        c.pnl==null?formatPnlDisplay(null,c.src,c.ccy):money(c.pnl,c.ccy))}
        <small>${c.wr==null?'—':((c.wr<=1?c.wr*100:c.wr).toFixed(1)+'% WR')}</small>
      </div>
    </div>`).join(''));
}

function humanEntryBlock(raw){
  let s=String(raw||'').trim();
  if(!s)return '';
  // Strip legacy "UPL -0.15" / deal UUID / dmu noise from older engines.
  s=s.replace(/\bUPL\b\s*[+\-]?\d+(?:\.\d+)?/gi,'')
     .replace(/\bUPL\b\s*USD\b/gi,'')
     .replace(/\b(?:deal|dmu)\s+[0-9a-f-]{8,}/gi,'')
     .replace(/\bemitting block for manual fusion vote\b/gi,'')
     .replace(/\s*[—\-·]+\s*/g,' — ')
     .replace(/^(?:\s*[—\-·]\s*)+|(?:\s*[—\-·]\s*)+$/g,'')
     .replace(/\s{2,}/g,' ')
     .trim();
  const low=s.toLowerCase();
  if(low.includes('no adaptive')||low.includes('no adoptive')||
     low.includes('adoptive size')||low.includes('adaptive size')||
     low.includes('manual trade')){
    return 'Nav lot — manuāls OPEN Capital';
  }
  if(low.includes('existing open')||low.includes('open position')){
    if(low.includes('fusion')||low.includes('neural')){
      return 'Open ir — gaida neironu';
    }
    return 'Open ir — gaida CLOSE';
  }
  if(low.includes('fusion')||low.includes('neural')){
    return 'Gaida neironu';
  }
  if(low.includes('waiting')){
    return 'Gaida · '+s.slice(0,48);
  }
  return s.slice(0,72);
}

/** Desk MARKET INTEL — compress engine prose to short chips. */
function shortIntelLine(raw,src){
  let s=String(raw||'').trim();
  if(!s)return '';
  const low=s.toLowerCase();
  const srcU=String(src||'').toUpperCase();
  if(low.includes('no matching high-impact')||low.includes('safety window')){
    return 'Nav HI news logā';
  }
  if(low.includes('no fresh aligned')||low.includes('peer history')){
    const m=s.match(/peers\s+(\d+\/\d+)/i);
    return m?`Peers ${m[1]} · gaida`:'Peers gaida';
  }
  if(srcU==='FORWARD'||/^forward\s*n=/i.test(s)){
    const n=(s.match(/n\s*=\s*(\d+)/i)||[])[1];
    const f=(s.match(/false\s+(\d+)%/i)||[])[1];
    return `Fwd n=${n||'—'} · false ${f!=null?f+'%':'—'}`;
  }
  if(srcU==='LEARN'||low.includes('edge floor')){
    const floor=(s.match(/edge floor\s+([\d.]+)/i)||[])[1];
    const moved=(s.match(/(\d+)\s*\/\s*(\d+)/)||[]);
    if(floor&&moved[1])return `Edge ${floor} · ${moved[1]}/${moved[2]} £`;
    if(floor)return `Edge ${floor}`;
    return s.slice(0,40);
  }
  if(srcU==='FLOW'||low.startsWith('m1 flow')){
    if(low.includes('no_directional'))return 'M1 flow · nav virziena';
    return s.replace(/^m1 flow\s*/i,'M1 · ').replace(/_/g,' ').slice(0,42);
  }
  if(srcU==='FUSION'||low.includes('fusion')){
    if(low.includes('peer'))return 'Fusion · peers gaida';
    return 'Fusion · gaida';
  }
  if(srcU==='GATE'||srcU==='NEWS'||srcU==='INTERMARKET'||srcU==='MEMORY'){
    return humanEntryBlock(s)||s.slice(0,48);
  }
  // Strip noisy status prefixes like "CLEAR · " / "UNAVAILABLE · "
  s=s.replace(/^(CLEAR|UNAVAILABLE|WAITING|BOOTING|NEUTRAL|READY)\s*[·\-—]+\s*/i,'');
  return s.slice(0,48);
}

function collectRiskAlerts(clients){
  const alerts=[];
  (clients||[]).forEach(c=>{
    const name=c.display_name||c.username||('#'+c.id);
    const hr=c.hard_risk||{};
    const dd=hr.equity_drawdown_pct??hr.drawdown_pct??hr.current_drawdown_pct;
    if(dd!=null && Number(dd)>=5){
      alerts.push({title:`${name} — liels kritums`,detail:'Konta equity drawdown',metric:Number(dd).toFixed(1)+'%'});
    }
    if(hr.trading_halted){
      alerts.push({title:`${name} — apturēts`,detail:humanEntryBlock(hr.halt_reason)||'Hard-risk limīts',metric:'HALT'});
    }
    const a=c.activity||{};
    if(a.entry_block_reason){
      alerts.push({
        title:`${name} — pagaidām bez jauna OPEN`,
        detail:humanEntryBlock(a.entry_block_reason),
        metric:'WAIT'});
    }
    if(a.error||c.last_start_error||a.last_start_error){
      alerts.push({title:`${name} — kļūda`,detail:String(a.error||c.last_start_error||a.last_start_error).slice(0,140),metric:'ERR'});
    }
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
      <div class="kv"><span>Kāpēc nav jauna OPEN</span><b>${esc(
        humanEntryBlock((c.activity||{}).entry_block_reason)||'—')}</b></div>
    </article>`;
  }).join(''));
}

function renderRecentClosed(clients){
  const rows=[];
  (clients||[]).forEach(c=>{
    const r=c.results||{};
    const ccy=r.pnl_currency||c.account_currency||'';
    (r.recent_trades||[]).filter(t=>t&&isDeskListableClose(t.pnl_source)).slice(0,8).forEach(t=>{
      const pnl=Number(t.pnl);
      if(!Number.isFinite(pnl)||Math.abs(pnl)<1e-12)return;
      rows.push({
        client:c.display_name||c.username,
        epic:t.epic||'—',
        direction:String(t.direction||'').toUpperCase(),
        size:t.size,
        open:t.entry||t.open_level||t.level,
        current:t.exit||t.close_level,
        upl:pnl,
        currency:t.pnl_currency||ccy,
      });
    });
  });
  rows.sort((a,b)=>Math.abs(Number(b.upl)||0)-Math.abs(Number(a.upl)||0));
  stableHtml($('recentClosed'),renderTradesTable(rows.slice(0,8),{empty:'Nav nesen slēgtu treidu'}));
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
  // Never p.currency (GOLD quote USD). Never invent UPL label.
  let uplCcy=String(ccy||'GBP').trim().toUpperCase()||'GBP';
  if(uplCcy==='USD'){
    // GOLD quote leak — Capital GBP accounts settle open cash in GBP.
    const acct=String((activity&&(activity.currency||(activity.balance&&activity.balance.currency)))||'').trim().toUpperCase();
    if(acct==='GBP'||!acct)uplCcy='GBP';
  }
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
        <span>Open <strong class="${pnlClass(p.upl)}">${esc(money(p.upl,uplCcy))}</strong></span>
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
  // Closed Capital £ rows may keep their own pnl_currency; open cards use account only.
  const ccy=accountCashCcy(c);
  const day=a.day_book||{};
  const dayPhase=day.phase||(day.daily&&day.daily.phase)||'—';
  const mandateSize=(a.mandate&&a.mandate.size)||'—';
  const sb=scoreboard(r);
  const trades=(r.recent_trades||[]).filter(t=>{
    if(!t||t.pending_cash||t.from_edge_hist)return false;
    if(!isDeskListableClose(t.pnl_source))return false;
    return t.pnl!=null&&Number.isFinite(Number(t.pnl))&&Math.abs(Number(t.pnl))>=1e-12;
  }).slice(0,4).map(t=>{
    const tCcy=t.pnl_currency||ccy;
    const raw=Number(t.pnl);
    const cls=pnlClass(raw);
    const tag=isCapitalFundsSrc(t.pnl_source)?'Capital':'sync';
    const right=isCapitalFundsSrc(t.pnl_source)
      ?formatPnlDisplay(raw,'broker',tCcy)
      :money(raw,tCcy);
    return `<div><span>${esc(t.direction||'?')} ${esc(t.epic||'')} [${tag}]</span><span class="${cls}">${esc(right)}</span></div>`;
  }).join('')||'<div><span>Nav Capital £ closes</span></div>';

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
        const waitLabel=(act==='HOLD'&&wait)?String(wait).slice(0,42):'—';
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
      <div><span>W / L · Capital £</span><strong>${esc(`${sb.wins} / ${sb.losses}`)}</strong></div>
      ${(()=>{
        const fl=a.forward_live||{};
        if(!fl.ok)return '';
        const tip=(fl.tips&&fl.tips[0])?fl.tips[0]:'';
        return `<div><span>Forward-live</span><strong>${esc(
          `n=${fl.n} · false ${(fl.false_signal_rate==null?'—':Math.round(fl.false_signal_rate*100)+'%')} · E ${fl.expectancy==null?'—':fl.expectancy}`
        )}</strong></div>${tip?`<div><span>Forward tip</span><strong>${esc(tip)}</strong></div>`:''}`;
      })()}
    </div>
    <div class="muted" style="padding:4px 8px;font-size:9px">${esc(e.neural_adapt_reason||e.last_reason||'')}${
      (a.entry_block_reason||a.error||c.last_start_error||a.last_start_error)
        ?(' · '+esc(humanEntryBlock(a.entry_block_reason)||a.error||c.last_start_error||a.last_start_error))
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
  // Visible proof the Windows process reloaded #241+ (not a stale uvicorn).
  const rev=s.desk_truth_rev!=null?Number(s.desk_truth_rev):null;
  const revBit=(rev&&Number.isFinite(rev))?` · truth #${rev}`:' · truth ?';
  if(alerts.some(a=>a.metric==='HALT'||a.metric==='ERR')){
    el.classList.add('warn');
    el.querySelector('span:last-child').textContent='Nepieciešama uzmanība'+syncBit+revBit;
  }else{
    el.classList.remove('warn');
    let msg='Sistēma aktīva';
    if(running>0)msg+=` · ${running} roboti`;
    if(open>0)msg+=` · ${open} open`;
    el.querySelector('span:last-child').textContent=msg+syncBit+revBit;
  }
}

function desiredPollMs(data){
  const deskOn=!!document.querySelector('.view.active[data-view="desk"]');
  const s=(data&&data.summary)||{};
  const running=Number(s.running_robots)||0;
  const open=Number(s.open_trades_all)||collectOpenTrades((data&&data.clients)||[]).length;
  // Desk tab always fast — operator is watching live tape / SHADOW CMD.
  if(deskOn)return POLL_DESK_MS;
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
  const mid=a.market_mid!=null?Number(a.market_mid).toFixed(2):'';
  return [
    c.id,c.robot_running?1:0,c.operator_paused?1:0,c.action||'',c.proposal||'',
    a.entry_intent||'',a.entry_trigger||'',String(a.entry_wait||'').slice(0,40),
    pos.map(p=>[p.dealId,p.direction,p.size,p.level,p.stopLevel,p.profitLevel,
      p.has_sl,p.has_tp,p.trail_phase,Number(p.upl).toFixed(2),p.epic].join('/')).join('|'),
    (a.day_book&&a.day_book.phase)||'',
    (a.mandate&&a.mandate.size)||'',a.market||'',mid,a.tick_count||0,
    mtf.mode||'',mtf.preferred||'',mtf.stop_hint_atr14??'',
    e.neurons,e.steps,e.learning_generation??e.generation,
    sb.pnl,sb.wins,sb.losses,sb.src,
    r.pending_broker_pnl,r.pending_trades,r.cash_trades,r.realized_pnl,
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

let truthForceBooted=false;
async function loadOverview(){
  // Coalesce overlapping polls so a slow /overview cannot stack and freeze UI.
  if(overviewInFlight)return overviewInFlight;
  overviewInFlight=(async()=>{
    // First paint: kick Funds mirror in background — never block the live poll.
    if(!truthForceBooted){
      truthForceBooted=true;
      api('/api/admin/refresh-truth','POST').catch(()=>{});
    }
    let data=await api('/api/admin/overview');
    if(deskDemoMode){
      const fx=buildDeskDemoFixture();
      data={...data,...fx.overview,clients:fx.overview.clients};
    }
    lastOverview=data;
    lastSyncAt=Date.now();
    renderSummary(data);
    updateSystemStatus(data);
    const open=collectOpenTrades(data.clients||[]);
    stableHtml($('openTradesPreview'),renderTradesTable(open.slice(0,4)));
    stableHtml($('openTradesFull'),renderTradesTable(open.slice(0,8)));
    renderTopClients(data.clients);
    renderRiskAlerts(data.clients);
    renderRiskBoard(data.clients);
    renderRecentClosed(data.clients);
    drawPerfChart(data.clients);
    // Light Desk paint only — heavy markets-ohlc runs in background.
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
  // Auth succeeded — NEVER re-show the login gate if later loads fail.
  show(false);
  try{
    // Best-effort fullscreen from login click gesture; else arm next click.
    const ok=await enterAdminFullscreen();
    if(!ok)armFullscreenOnNextGesture();
  }catch(_){}
  try{await loadBrokers()}catch(e){console.warn('loadBrokers',e)}
  try{await loadOverview()}catch(e){console.warn('loadOverview',e)}
  try{schedulePoll(desiredPollMs(lastOverview))}catch(_){}
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
  document.title='VS-System Desk · DEMO fixture';
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
  const host=(location.hostname||'').toLowerCase();
  const isLocal=host==='127.0.0.1'||host==='localhost'||host==='[::1]'||host.startsWith('127.');
  // Prefill from ?username=&password= (and strip secrets from the address bar).
  try{
    const q=new URLSearchParams(location.search||'');
    const u=q.get('username');const p=q.get('password');
    const form=$('loginForm');
    if(form&&u){const el=form.querySelector('[name=username]');if(el)el.value=u}
    if(form&&p){const el=form.querySelector('[name=password]');if(el)el.value=p}
    // Local Windows Desk: always seed default operator creds so Ienākt works
    // even if auto-boot races / fails.
    if(isLocal&&form){
      const ue=form.querySelector('[name=username]');
      const pe=form.querySelector('[name=password]');
      if(ue&&!ue.value)ue.value='admin';
      if(pe&&!pe.value)pe.value='admin123';
    }
    if(u||p){
      const clean=location.pathname+(location.hash||'');
      history.replaceState({},'',clean);
    }
  }catch(_){}
  // Local Desk: open UI first, then authenticate — never bounce back to login.
  if(isLocal){
    show(false);
    const attempts=[
      async()=>{await api('/api/admin/me')},
      async()=>{await api('/api/admin/local-boot','POST')},
      async()=>{await api('/api/admin/login','POST',{username:'admin',password:'admin123'})},
    ];
    let authed=false;
    for(const step of attempts){
      try{await step();authed=true;break}catch(e){console.warn('local-open',e)}
    }
    await enterDash();
    if(!authed){
      const err=$('loginError');
      if(err)err.textContent='Local boot failed — typed admin/admin123 then Ienākt';
    }
    return;
  }
  try{await api('/api/admin/me');await enterDash();return}catch(_){}
  // Query / form creds fallback.
  try{
    const form=$('loginForm');
    if(form){
      const u=(form.querySelector('[name=username]')||{}).value||'';
      const p=(form.querySelector('[name=password]')||{}).value||'';
      if(u&&p){
        await api('/api/admin/login','POST',{username:u,password:p});
        await enterDash();
        return;
      }
    }
  }catch(e){
    const err=$('loginError');if(err)err.textContent=e.message||String(e);
  }
  show(true);
})();
