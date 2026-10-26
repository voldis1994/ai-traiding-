'use strict';
const $=id=>document.getElementById(id);
let pollTimer=null;

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
  $('summary').innerHTML=`
    <div class="stat"><span>Klienti</span><b>${s.clients||0}</b></div>
    <div class="stat"><span>Roboti ON</span><b>${s.running_robots||0}</b></div>
    <div class="stat"><span>Slēgtie treidi</span><b>${s.closed_trades_all||0}</b></div>
    <div class="stat"><span>Realizētais P&amp;L</span><b class="${pnlClass(s.realized_pnl_all)}">${num(s.realized_pnl_all,2)}</b></div>`;
}

function renderCard(c){
  const e=c.evolution||{},a=c.activity||{},r=c.results||{},sc=c.scores||{};
  const running=!!c.robot_running;
  const paused=!!c.operator_paused;
  const pill=running?(paused?'pause':'on'):'off';
  const pillText=running?(paused?'PAUZE':'ON'):'OFF';
  const broker=c.broker?`${c.broker.display_name}`:'nav brokera';
  const trades=(r.recent_trades||[]).slice(0,6).map(t=>{
    const p=t.pnl;const cls=pnlClass(p);
    return `<div><span>${t.direction||'?'} ${t.epic||''}</span><span class="${cls}">${p==null?'—':num(p,2)}</span></div>`;
  }).join('')||'<div><span>Nav slēgtu treidu</span></div>';
  const el=document.createElement('article');
  el.className='card';
  el.innerHTML=`
    <div class="head">
      <div>
        <h3>#${c.id} ${c.display_name||c.username}</h3>
        <div class="muted small">@${c.username} · ${c.language||''} · ${broker}</div>
      </div>
      <span class="pill ${pill}">${pillText}</span>
    </div>
    <div class="scores">
      <div class="score buy"><em>BUY</em><b>${pct(sc.BUY)}</b></div>
      <div class="score"><em>HOLD</em><b>${pct(sc.HOLD)}</b></div>
      <div class="score sell"><em>SELL</em><b>${pct(sc.SELL)}</b></div>
    </div>
    <div class="meta">
      <div><span>Fāze</span><br><strong>${c.phase||'—'}</strong></div>
      <div><span>Signāls</span><br><strong>${c.action||'—'}</strong></div>
      <div><span>Neironi</span><br><strong>${e.neurons??'—'}</strong></div>
      <div><span>Soļi</span><br><strong>${e.steps??0}</strong></div>
      <div><span>Paaudze</span><br><strong>${e.generation??0}</strong></div>
      <div><span>Mācību paraugi</span><br><strong>${e.labeled_samples??0}</strong></div>
      <div><span>Precizitāte</span><br><strong>${pct(e.accuracy_proxy)}</strong></div>
      <div><span>Dzimšanas / prune</span><br><strong>${e.births??0} / ${e.pruned??0}</strong></div>
      <div><span>Tirgus</span><br><strong>${a.market||'—'}</strong></div>
      <div><span>Ticki</span><br><strong>${a.tick_count??0}</strong></div>
      <div><span>P&amp;L</span><br><strong class="${pnlClass(r.realized_pnl)}">${num(r.realized_pnl,2)}</strong></div>
      <div><span>W / L</span><br><strong>${r.wins??0} / ${r.losses??0} (${pct(r.win_rate)})</strong></div>
    </div>
    <div class="muted small">${a.entry_block_reason||a.error||''}</div>
    <div class="trades">${trades}</div>
    <div class="actions">
      <button type="button" class="btn tiny" data-act="start" data-id="${c.id}">Start</button>
      <button type="button" class="btn tiny" data-act="stop" data-id="${c.id}">Stop</button>
      <button type="button" class="btn tiny" data-act="pause" data-id="${c.id}">Pause</button>
      <button type="button" class="btn tiny" data-act="resume" data-id="${c.id}">Resume</button>
    </div>`;
  el.querySelectorAll('[data-act]').forEach(btn=>{
    btn.onclick=async()=>{
      try{
        await api(`/api/admin/clients/${btn.dataset.id}/${btn.dataset.act}`,'POST');
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
  $('clock').textContent=new Date().toLocaleTimeString('lv-LV');
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
