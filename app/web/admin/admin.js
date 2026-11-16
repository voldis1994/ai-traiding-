'use strict';
const $=id=>document.getElementById(id);
let pollTimer=null;
// While a LIVE deal is open, freeze displayed BUY/HOLD/SELL per client so a
// lagging backend cannot make the Admin card dance mid-trade.
const frozenScoresByClient=new Map();

async function api(path,method='GET',payload){
  const opts={method,credentials:'same-origin',headers:{'Content-Type':'application/json'}};
  if(payload!==undefined)opts.body=JSON.stringify(payload);
  const res=await fetch(path,opts);let body={};try{body=await res.json()}catch(_){}
  if(!res.ok)throw Error(typeof body.detail==='string'?body.detail:`HTTP ${res.status}`);
  return body;
}
function show(login){$('login').classList.toggle('hidden',!login);$('dash').classList.toggle('hidden',login)}
function pct(v){return Math.round((Number(v)||0)*100)+'%'}
function num(v,d=2){const n=Number(v);return Number.isFinite(n)?n.toFixed(d):'—'}
function pnlClass(v){const n=Number(v);if(!Number.isFinite(n)||n===0)return '';return n>0?'pnl-pos':'pnl-neg'}
function money(v,ccy){
  if(v==null||v===''||!Number.isFinite(Number(v)))return '—';
  return num(v,2)+(ccy?' '+ccy:'');
}
function esc(s){
  return String(s??'').replace(/[&<>"']/g,c=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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

function renderSummary(s){
  s=s||{};
  let pnlLabel;
  const by=s.pnl_by_currency||{};
  const keys=Object.keys(by);
  const closed=Number(s.closed_trades_all)||0;
  const cash=Number(s.cash_trades_all)||0;
  const pending=Number(s.pending_cash_trades_all)||0;
  const proxy=Number(s.proxy_trades_all)||0;
  if(keys.length>1){
    pnlLabel=keys.map(k=>`${num(by[k],2)} ${k}`).join(' · ');
  }else if(s.realized_pnl_all!=null && s.pnl_currency){
    pnlLabel=money(s.realized_pnl_all,s.pnl_currency);
  }else if(keys.length===1){
    pnlLabel=money(by[keys[0]],keys[0]);
  }else if(closed>0 && cash===0){
    pnlLabel='— · bez broker £';
  }else{
    pnlLabel='—';
  }
  let pnlNote='';
  if(closed>0 && cash===0){
    pnlNote=` · ${closed} bez £`+(pending?` (${pending} pend)`:'')+(proxy?` · ${proxy} ticks`:'');
  }else if(pending>0){
    pnlNote=` · ${pending} pending £`;
  }else if(cash>0 && cash<closed){
    pnlNote=` · £ ${cash}/${closed}`;
  }
  $('summary').innerHTML=`
    <div class="fleet-cell"><span>Klienti</span><b>${s.clients||0}</b></div>
    <div class="fleet-cell"><span>Roboti ON</span><b>${s.running_robots||0}</b></div>
    <div class="fleet-cell"><span>Slēgtie</span><b>${closed}</b></div>
    <div class="fleet-cell"><span>Realizētais P&amp;L</span><b class="${pnlClass(s.realized_pnl_all)}">${esc(pnlLabel)}${esc(pnlNote)}</b></div>`;
}

function trailFlag(phase){
  const p=String(phase||'flat').toLowerCase();
  const labels={
    flat:'TRAIL —',
    unknown:'TRAIL ?',
    manual:'TRAIL manual',
    watch:'TRAIL watch',
    armed:'TRAIL armed',
    ratchet:'TRAIL ratchet',
    be_lock:'TRAIL BE',
    trail:'TRAILING',
  };
  const label=labels[p]||(`TRAIL ${p}`);
  const on=['armed','ratchet','be_lock','trail'].includes(p);
  return `<span class="flag trail-${esc(p)}${on?'':''}">${esc(label)}</span>`;
}

function renderDeal(p,ccy){
  if(!p){
    return `<div class="deal flat">Nav atvērta pozīcija — robots gaida OPEN</div>`;
  }
  const dir=String(p.direction||'').toUpperCase();
  const sideCls=dir==='BUY'?'buy':(dir==='SELL'?'sell':'');
  const lot=p.size!=null?String(p.size):'—';
  const tp=p.has_tp || p.profitLevel!=null;
  const sl=p.has_sl || p.stopLevel!=null;
  const phase=p.trail_phase||(p.enrolled?'armed':'flat');
  const uplCcy=p.currency&&p.currency!==ccy?p.currency:ccy;
  return `<div class="deal">
    <div class="side ${sideCls}">${esc(dir||'—')}</div>
    <div>
      <div class="lot"><em>LOT</em>${esc(lot)}</div>
      <div class="deal-meta">
        <span>Epic <strong>${esc(p.epic||'—')}</strong></span>
        <span>Entry <strong>${esc(num(p.level,2))}</strong></span>
        <span>SL <strong>${p.stopLevel==null?'—':esc(num(p.stopLevel,2))}</strong></span>
        <span>TP <strong>${p.profitLevel==null?'OFF':esc(num(p.profitLevel,2))}</strong></span>
        <span>UPL <strong class="${pnlClass(p.upl)}">${esc(money(p.upl,uplCcy))}</strong></span>
      </div>
    </div>
    <div class="flags">
      <span class="flag ${sl?'on':'off'}">SL ${sl?'ON':'OFF'}</span>
      <span class="flag ${tp?'on':'off'}">TP ${tp?'ON':'OFF'}</span>
      ${trailFlag(phase)}
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
  const trades=(r.recent_trades||[]).slice(0,6).map(t=>{
    const p=t.pnl;const cls=pnlClass(p);
    const tCcy=t.pnl_currency||ccy;
    const src=t.pnl_source&&t.pnl_source!=='broker'?` [${t.pnl_source}]`:'';
    return `<div><span>${esc(t.direction||'?')} ${esc(t.epic||'')}${esc(src)}</span><span class="${cls}">${p==null?'—':esc(money(p,tCcy))}</span></div>`;
  }).join('')||'<div><span>Nav slēgtu treidu</span></div>';

  const dealsHtml=inTrade
    ?positions.map(p=>renderDeal(p,ccy)).join('')
    :renderDeal(null,ccy);

  const trailNote=(()=>{
    const live=positions.find(p=>p.trail_status)||positions[0];
    const st=live&&live.trail_status;
    const prot=(a.protection&&a.protection.status)||st;
    if(!inTrade)return '';
    const active=['armed','ratchet','be_lock','trail'].includes(String((live&&live.trail_phase)||'').toLowerCase());
    return `<div class="trail-line ${active?'active':''}">${esc(prot||'Trail status nav ziņots')}</div>`;
  })();

  const cashLine=
    r.pnl_status==='closed_without_broker_cash'?'⛔ bez broker £':
    r.pnl_status==='broker_cash_pending'?`⏳ pending ${r.pending_cash_trades||0}`:
    r.pnl_status==='broker_cash_ok'?'✓ broker £':
    (r.pnl_status||'—');

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
      <div><span>Signāls</span><strong>${esc(c.action||'—')}</strong></div>
      <div><span>Day phase</span><strong>${esc(dayPhase)}</strong></div>
      <div><span>Mandate lot</span><strong>${esc(mandateSize)}</strong></div>
      <div><span>Tirgus</span><strong>${esc(a.market||'—')}</strong></div>
      <div><span>Neironi / soļi</span><strong>${esc(e.neurons??'—')} / ${esc(e.steps??0)}</strong></div>
      <div><span>Paaudze</span><strong>${esc(e.learning_generation??e.generation??0)}</strong></div>
      <div><span>P&amp;L realizēts</span><strong class="${pnlClass(r.realized_pnl)}">${
        (r.closed_trades>0 && (r.cash_trades||0)===0)
          ? ('— · bez £'+(r.pending_cash_trades?` (${r.pending_cash_trades})`:''))
          : esc(money(r.realized_pnl,ccy))
      }</strong></div>
      <div><span>Broker £</span><strong>${esc(cashLine)}</strong></div>
    </div>
    <div class="muted" style="padding:8px 14px;font-size:11px">${esc(e.neural_adapt_reason||e.last_reason||'')}${
      (a.entry_block_reason||a.error||c.last_start_error||a.last_start_error)
        ?(' · '+esc(a.entry_block_reason||a.error||c.last_start_error||a.last_start_error))
        :''
    }</div>
    <div class="trades">${trades}</div>
    <div class="actions">
      <button type="button" class="btn tiny" data-act="start" data-id="${c.id}">Start</button>
      <button type="button" class="btn tiny ghost" data-act="stop" data-id="${c.id}">Stop</button>
      <button type="button" class="btn tiny ghost" data-act="resume" data-id="${c.id}">Resume</button>
      <button type="button" class="btn tiny ghost" data-act="wipe" data-id="${c.id}">Mācīties no jauna</button>
    </div>`;
  el.querySelectorAll('[data-act]').forEach(btn=>{
    btn.onclick=async()=>{
      try{
        if(btn.dataset.act==='wipe'){
          const ok=confirm(
            `Izdzēst mācīšanās vēsturi klientam #${btn.dataset.id}?\n`+
            'Klienta login un brokera API paliek. Capital.com pozīcijas netiek aizvērtas.');
          if(!ok)return;
          const typed=prompt('Ieraksti RESET, lai apstiprinātu:');
          if(typed!=='RESET'){alert('Atcelts.');return}
          await api('/api/admin/reset-learning','POST',{
            confirmation:'RESET',client_id:Number(btn.dataset.id)});
        }else{
          await api(`/api/admin/clients/${btn.dataset.id}/${btn.dataset.act}`,'POST');
        }
        await loadOverview();
      }catch(err){alert(err.message)}
    };
  });
  return el;
}

async function loadOverview(){
  const data=await api('/api/admin/overview');
  renderSummary(data.summary);
  const room=$('controlRoom');room.innerHTML='';
  const pick=$('brokerClient');const prev=pick.value;pick.innerHTML='';
  (data.clients||[]).forEach(c=>{
    room.appendChild(renderCard(c));
    const opt=document.createElement('option');opt.value=c.id;
    opt.textContent=`#${c.id} ${c.username} (${c.display_name})`;pick.appendChild(opt);
  });
  if(prev)pick.value=prev;
  $('clock').textContent=new Date().toLocaleTimeString('lv-LV',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

async function enterDash(){
  show(false);
  await loadBrokers();
  await loadOverview();
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(()=>loadOverview().catch(()=>{}),5000);
}

$('loginForm').onsubmit=async ev=>{
  ev.preventDefault();$('loginError').textContent='';
  const data=Object.fromEntries(new FormData(ev.target).entries());
  try{await api('/api/admin/login','POST',data);await enterDash()}
  catch(e){$('loginError').textContent=e.message}
};
$('logoutBtn').onclick=async()=>{
  if(pollTimer)clearInterval(pollTimer);
  try{await api('/api/admin/logout','POST')}finally{show(true)}
};
$('refreshBtn').onclick=()=>loadOverview().catch(e=>alert(e.message));
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
