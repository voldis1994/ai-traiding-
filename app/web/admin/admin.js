'use strict';
const $=id=>document.getElementById(id);
let pollTimer=null;
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
function setHTML(el,html){
  if(!el)return;
  if(el._html===html)return;
  el._html=html;
  el.innerHTML=html;
}
function setText(el,text){
  if(!el)return;
  const t=String(text??'');
  if(el.textContent!==t)el.textContent=t;
}

async function loadBrokers(){
  const data=await api('/api/admin/brokers');
  const sel=$('brokerType');
  const cur=sel.value;
  const html=(data.brokers||[]).map(b=>{
    const dis=b.status==='slot'?' disabled':'';
    const label=esc(b.label)+(b.status==='slot'?' (slot)':'');
    return `<option value="${esc(b.id)}"${dis}>${label}</option>`;
  }).join('');
  setHTML(sel,html);
  if(cur)sel.value=cur;
}

function pnlSourceLabel(src){
  if(src==='broker')return 'broker £';
  if(src==='upl_estimate')return 'UPL estimate (Capital £ pending)';
  if(src==='none')return 'nav skaitļu';
  return src||'—';
}

function renderSummary(s){
  s=s||{};
  const closed=Number(s.closed_trades_all)||0;
  const cash=Number(s.cash_trades_all)||0;
  const pending=Number(s.pending_cash_trades_all)||0;
  const estimate=Number(s.estimate_trades_all)||0;
  const ccy=s.pnl_currency||'';
  const work=s.working_pnl_all;
  const workSrc=s.working_pnl_source||'none';
  let pnlLabel;
  if(work!=null && Number.isFinite(Number(work))){
    pnlLabel=money(work,ccy);
  }else if(closed>0 && cash===0){
    pnlLabel='—';
  }else{
    pnlLabel='—';
  }
  const note=workSrc==='upl_estimate'
    ?` · est ${estimate}/${closed}`+(pending?` · ${pending} pending`:'')
    :(cash>0?` · broker ${cash}/${closed}`:'');
  const wl=`${s.display_wins_all??0} / ${s.display_losses_all??0}`;
  const wlNote=s.wl_source==='upl_estimate'?' · est':(s.wl_source==='broker'?' · £':'');
  setHTML($('summary'),`
    <div class="fleet-cell"><span>Klienti</span><b>${s.clients||0}</b></div>
    <div class="fleet-cell"><span>Roboti ON</span><b>${s.running_robots||0}</b></div>
    <div class="fleet-cell"><span>Slēgtie</span><b>${closed}</b></div>
    <div class="fleet-cell"><span>W / L</span><b>${esc(wl)}<span class="src" style="display:block;font-size:10px;color:var(--muted);font-family:var(--mono);font-weight:500">${esc(wlNote.trim()||pnlSourceLabel(s.wl_source))}</span></b></div>
    <div class="fleet-cell"><span>P&amp;L (working)</span><b class="${pnlClass(work)}">${esc(pnlLabel)}</b></div>
    <div class="fleet-cell"><span>P&amp;L avots</span><b style="font-size:0.95rem">${esc(pnlSourceLabel(workSrc))}${esc(note)}</b></div>`);
}

function trailFlag(phase){
  const p=String(phase||'flat').toLowerCase();
  const labels={
    flat:'TRAIL —',unknown:'TRAIL ?',manual:'TRAIL manual',watch:'TRAIL watch',
    armed:'TRAIL armed',ratchet:'TRAIL ratchet',be_lock:'TRAIL BE',trail:'TRAILING',
  };
  return `<span class="flag trail-${esc(p)}">${esc(labels[p]||('TRAIL '+p))}</span>`;
}

function renderDeals(positions,ccy){
  if(!positions||!positions.length){
    return `<div class="deal flat">Nav atvērta pozīcija</div>`;
  }
  return positions.map(p=>{
    const dir=String(p.direction||'').toUpperCase();
    const sideCls=dir==='BUY'?'buy':(dir==='SELL'?'sell':'');
    const lot=p.size!=null?String(p.size):'—';
    const tp=p.has_tp || p.profitLevel!=null;
    const sl=p.has_sl || p.stopLevel!=null;
    const phase=p.trail_phase||(p.enrolled?'armed':'flat');
    const uplCcy=p.currency&&p.currency!==ccy?p.currency:ccy;
    const note=p.trail_status
      ?`<div class="trail-note ${['armed','ratchet','be_lock','trail'].includes(String(phase).toLowerCase())?'active':''}">${esc(p.trail_status)}</div>`
      :'';
    return `<div class="deal">
      <div class="side ${sideCls}">${esc(dir||'—')}</div>
      <div>
        <div class="lot"><em>LOT</em>${esc(lot)}</div>
        <div class="deal-lines">
          <span>${esc(p.epic||'—')}</span>
          <span>Entry <strong>${esc(num(p.level,2))}</strong></span>
          <span>SL <strong>${p.stopLevel==null?'—':esc(num(p.stopLevel,2))}</strong></span>
          <span>TP <strong>${p.profitLevel==null?'OFF':esc(num(p.profitLevel,2))}</strong></span>
          <span>UPL <strong class="${pnlClass(p.upl)}">${esc(money(p.upl,uplCcy))}</strong></span>
        </div>
        <div class="flags">
          <span class="flag ${sl?'on':'off'}">SL ${sl?'ON':'OFF'}</span>
          <span class="flag ${tp?'on':'off'}">TP ${tp?'ON':'OFF'}</span>
          ${trailFlag(phase)}
        </div>
        ${note}
      </div>
    </div>`;
  }).join('');
}

function clientShell(id){
  const el=document.createElement('article');
  el.className='client';
  el.dataset.clientId=String(id);
  el.innerHTML=`
    <div class="who">
      <h3 data-k="title"></h3>
      <div class="meta" data-k="who"></div>
      <span class="state-pill" data-k="pill"></span>
    </div>
    <div class="deals" data-k="deals"></div>
    <div>
      <div class="nerve">
        <div class="buy"><em>BUY</em><b data-k="buy">0%</b></div>
        <div><em>HOLD</em><b data-k="hold">0%</b></div>
        <div class="sell"><em>SELL</em><b data-k="sell">0%</b></div>
      </div>
      <div class="facts">
        <div><span>Signāls</span><strong data-k="action">—</strong></div>
        <div><span>Day</span><strong data-k="day">—</strong></div>
        <div><span>Mandate lot</span><strong data-k="mandate">—</strong></div>
        <div><span>Tirgus</span><strong data-k="market">—</strong></div>
      </div>
    </div>
    <div class="money-block">
      <div><span style="font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)">P&amp;L working</span>
        <div class="big" data-k="pnl">—</div>
        <div class="src" data-k="pnlSrc"></div>
      </div>
      <div style="margin-top:10px"><span style="font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)">W / L</span>
        <div class="big" data-k="wl">0 / 0</div>
        <div class="src" data-k="wlSrc"></div>
      </div>
      <div class="trades" data-k="trades"></div>
    </div>
    <div class="actions">
      <button type="button" class="btn tiny" data-act="start">Start</button>
      <button type="button" class="btn tiny ghost" data-act="stop">Stop</button>
      <button type="button" class="btn tiny ghost" data-act="resume">Resume</button>
      <button type="button" class="btn tiny ghost" data-act="wipe">Reset learn</button>
    </div>
    <div class="note" data-k="note"></div>`;
  el.querySelectorAll('[data-act]').forEach(btn=>{
    btn.onclick=async()=>{
      const cid=el.dataset.clientId;
      try{
        if(btn.dataset.act==='wipe'){
          const ok=confirm(`Izdzēst mācīšanās vēsturi klientam #${cid}?`);
          if(!ok)return;
          const typed=prompt('Ieraksti RESET:');
          if(typed!=='RESET'){alert('Atcelts.');return}
          await api('/api/admin/reset-learning','POST',{confirmation:'RESET',client_id:Number(cid)});
        }else{
          await api(`/api/admin/clients/${cid}/${btn.dataset.act}`,'POST');
        }
        await loadOverview();
      }catch(err){alert(err.message)}
    };
  });
  return el;
}

function patchClient(el,c){
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
  el.classList.toggle('live',inTrade);
  const paused=!!c.operator_paused;
  const pill=running?(paused?'pause':'on'):'off';
  const pillText=running?(paused?'PAUZE':'ON'):'OFF';
  const broker=c.broker?`${c.broker.display_name}`:'nav brokera';
  const ccy=r.pnl_currency||c.account_currency||a.currency||'';
  const day=a.day_book||{};
  const dayPhase=day.phase||(day.daily&&day.daily.phase)||'—';
  const mandateSize=(a.mandate&&a.mandate.size)||'—';
  const work=r.working_pnl;
  const workSrc=r.working_pnl_source||'none';
  const dw=r.display_wins??r.wins??0;
  const dl=r.display_losses??r.losses??0;
  const wlSrc=r.wl_source||'none';

  const trades=(r.recent_trades||[]).slice(0,8).map(t=>{
    const p=t.pnl;const cls=pnlClass(p);
    const tCcy=t.pnl_currency||ccy;
    const src=t.pnl_source&&t.pnl_source!=='broker'?` [${t.pnl_source}]`:'';
    return `<div><span>${esc(t.direction||'?')} ${esc(t.epic||'')}${esc(src)}</span><span class="${cls}">${p==null?'—':esc(money(p,tCcy))}</span></div>`;
  }).join('')||'<div><span>Nav slēgtu treidu</span></div>';

  const note=[e.neural_adapt_reason||e.last_reason,
    a.entry_block_reason||a.error||c.last_start_error||a.last_start_error
  ].filter(Boolean).join(' · ');

  setText(el.querySelector('[data-k="title"]'),`#${c.id} ${c.display_name||c.username||''}`);
  setText(el.querySelector('[data-k="who"]'),`@${c.username||''} · ${c.language||''} · ${broker}${ccy?' · '+ccy:''}`);
  const pillEl=el.querySelector('[data-k="pill"]');
  pillEl.className=`state-pill ${pill}`;
  setText(pillEl,pillText);
  setHTML(el.querySelector('[data-k="deals"]'),renderDeals(positions,ccy));
  setText(el.querySelector('[data-k="buy"]'),pct(sc.BUY));
  setText(el.querySelector('[data-k="hold"]'),pct(sc.HOLD));
  setText(el.querySelector('[data-k="sell"]'),pct(sc.SELL));
  setText(el.querySelector('[data-k="action"]'),c.action||'—');
  setText(el.querySelector('[data-k="day"]'),dayPhase);
  setText(el.querySelector('[data-k="mandate"]'),mandateSize);
  setText(el.querySelector('[data-k="market"]'),a.market||'—');
  const pnlEl=el.querySelector('[data-k="pnl"]');
  pnlEl.className=`big ${pnlClass(work)}`;
  setText(pnlEl,work==null?'—':money(work,ccy));
  setText(el.querySelector('[data-k="pnlSrc"]'),pnlSourceLabel(workSrc)+(
    r.cash_trades!=null?` · cash ${r.cash_trades||0}/${r.closed_trades||0}`:''));
  setText(el.querySelector('[data-k="wl"]'),`${dw} / ${dl}`);
  setText(el.querySelector('[data-k="wlSrc"]'),pnlSourceLabel(wlSrc));
  setHTML(el.querySelector('[data-k="trades"]'),trades);
  setText(el.querySelector('[data-k="note"]'),note||'');
}

async function loadOverview(){
  const data=await api('/api/admin/overview');
  renderSummary(data.summary);
  const room=$('controlRoom');
  const clients=data.clients||[];
  const seen=new Set();
  const pick=$('brokerClient');
  const prev=pick.value;
  const opts=[];
  clients.forEach(c=>{
    const id=String(c.id);
    seen.add(id);
    let el=room.querySelector(`[data-client-id="${id}"]`);
    if(!el){
      el=clientShell(id);
      room.appendChild(el);
    }
    patchClient(el,c);
    opts.push(`<option value="${esc(id)}">#${esc(id)} ${esc(c.username)} (${esc(c.display_name)})</option>`);
  });
  [...room.querySelectorAll('[data-client-id]')].forEach(el=>{
    if(!seen.has(el.dataset.clientId))el.remove();
  });
  setHTML(pick,opts.join(''));
  if(prev)pick.value=prev;
  setText($('clock'),new Date().toLocaleTimeString('lv-LV',{
    hour:'2-digit',minute:'2-digit',second:'2-digit'}));
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
  const ok=confirm('Izdzēst VISU mācīšanās vēsturi? Klienti paliek.');
  if(!ok)return;
  const typed=prompt('Ieraksti RESET:');
  if(typed!=='RESET'){alert('Atcelts.');return}
  try{
    const res=await api('/api/admin/reset-learning','POST',{confirmation:'RESET'});
    alert(res.message||'Notīrīts.');
    await loadOverview();
  }catch(e){alert(e.message)}
};
$('createForm').onsubmit=async ev=>{
  ev.preventDefault();$('createMsg').textContent='';
  const data=Object.fromEntries(new FormData(ev.target).entries());
  try{
    const res=await api('/api/admin/clients','POST',data);
    $('createMsg').textContent=`/app · ${res.client.username} · ${data.password}`;
    ev.target.reset();await loadOverview();
  }catch(e){$('createMsg').textContent=e.message}
};
$('brokerForm').onsubmit=async ev=>{
  ev.preventDefault();$('brokerMsg').textContent='…';
  const raw=Object.fromEntries(new FormData(ev.target).entries());
  const client_id=Number(raw.client_id);
  const body={broker_type:raw.broker_type,display_name:raw.display_name,api_key:raw.api_key,
    identifier:raw.identifier,password:raw.password};
  try{
    await api('/api/admin/clients/'+client_id+'/broker','POST',body);
    $('brokerMsg').textContent='OK — robots palaists.';
    ev.target.reset();await loadOverview();
  }catch(e){$('brokerMsg').textContent=e.message}
};

(async()=>{
  try{await api('/api/admin/me');await enterDash()}
  catch(_){show(true)}
})();
