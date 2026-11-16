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
function setHTML(el,html){if(!el)return;if(el._html===html)return;el._html=html;el.innerHTML=html}
function setText(el,text){if(!el)return;const t=String(text??'');if(el.textContent!==t)el.textContent=t}

function sourceLabel(src){
  if(src==='broker')return 'broker £';
  if(src==='upl_estimate')return 'UPL estimate';
  if(src==='working')return 'working';
  if(src==='none'||src==='empty')return 'nav skaitļu';
  return src||'—';
}

/** Always derive a usable P&L + W/L — never leave Admin on lying 0 when estimates exist. */
function scoreboard(r){
  r=r||{};
  const ccy=r.pnl_currency||'';
  const ledger=Array.isArray(r.pnl_ledger)?r.pnl_ledger:[];
  const recent=Array.isArray(r.recent_trades)?r.recent_trades:[];

  let pnl=null,src='none',wins=0,losses=0;

  if(r.working_pnl!=null && Number.isFinite(Number(r.working_pnl))){
    pnl=Number(r.working_pnl);src=r.working_pnl_source||'working';
  }else if((r.cash_trades||0)>0 && r.realized_pnl!=null && Number.isFinite(Number(r.realized_pnl))){
    pnl=Number(r.realized_pnl);src='broker';
  }else if(r.estimate_pnl!=null && Number.isFinite(Number(r.estimate_pnl))){
    pnl=Number(r.estimate_pnl);src='upl_estimate';
  }

  if(r.display_wins!=null || r.display_losses!=null){
    wins=Number(r.display_wins)||0;losses=Number(r.display_losses)||0;
    if(r.wl_source)src=src==='none'?r.wl_source:src;
  }else if(r.estimate_wins!=null || r.estimate_losses!=null){
    wins=Number(r.estimate_wins)||0;losses=Number(r.estimate_losses)||0;
    if(wins+losses>0 && src==='none')src='upl_estimate';
  }else if((r.cash_trades||0)>0){
    wins=Number(r.wins)||0;losses=Number(r.losses)||0;
  }

  // Ledger fallback (all signed closes from API).
  if((pnl==null || wins+losses===0) && ledger.length){
    let sum=0,w=0,l=0;
    for(const row of ledger){
      const v=Number(row.pnl);
      if(!Number.isFinite(v))continue;
      sum+=v;if(v>0)w++;else if(v<0)l++;
    }
    if(pnl==null && ledger.length){pnl=sum;src=ledger.some(x=>x.source==='broker')&& (r.cash_trades||0)>0?'broker':'upl_estimate'}
    if(wins+losses===0){wins=w;losses=l}
  }

  // Recent-trades fallback (partial, but better than 0/0 with visible rows).
  if((pnl==null || wins+losses===0) && recent.length){
    let sum=0,n=0,w=0,l=0;
    for(const t of recent){
      const v=Number(t.pnl);
      if(!Number.isFinite(v))continue;
      const s=String(t.pnl_source||'').toLowerCase();
      if(s==='ticks_only'||s==='price_size'||s==='position_disappeared')continue;
      sum+=v;n++;if(v>0)w++;else if(v<0)l++;
    }
    if(pnl==null && n){pnl=sum;src='upl_estimate'}
    if(wins+losses===0 && n){wins=w;losses=l;if(src==='none')src='upl_estimate'}
  }

  return {pnl,src,wins,losses,ccy,closed:Number(r.closed_trades)||0,
    cash:Number(r.cash_trades)||0,estimate:Number(r.estimate_trades)||0};
}

async function loadBrokers(){
  const data=await api('/api/admin/brokers');
  const sel=$('brokerType');const cur=sel.value;
  setHTML(sel,(data.brokers||[]).map(b=>{
    const dis=b.status==='slot'?' disabled':'';
    return `<option value="${esc(b.id)}"${dis}>${esc(b.label)}${b.status==='slot'?' (slot)':''}</option>`;
  }).join(''));
  if(cur)sel.value=cur;
}

function renderSummary(s,clients){
  s=s||{};
  // Fleet totals: prefer API working; else sum client scoreboards.
  let total=s.working_pnl_all;
  let src=s.working_pnl_source||'none';
  let wins=s.display_wins_all;
  let losses=s.display_losses_all;
  const ccy=s.pnl_currency||'';
  if(total==null || wins==null || losses==null){
    let sum=0,n=0,w=0,l=0,anyEst=false;
    for(const c of clients||[]){
      const sb=scoreboard(c.results||{});
      if(sb.pnl!=null){sum+=sb.pnl;n++}
      w+=sb.wins;l+=sb.losses;
      if(sb.src==='upl_estimate')anyEst=true;
    }
    if(total==null && n){total=sum;src=anyEst?'upl_estimate':src}
    if(wins==null)wins=w;
    if(losses==null)losses=l;
  }
  setHTML($('summary'),`
    <div class="fleet-cell"><span>Klienti</span><b>${s.clients||0}</b></div>
    <div class="fleet-cell"><span>Roboti ON</span><b>${s.running_robots||0}</b></div>
    <div class="fleet-cell"><span>Slēgtie</span><b>${s.closed_trades_all||0}</b></div>
    <div class="fleet-cell"><span>W / L</span><b>${wins||0} / ${losses||0}<span class="sub">${esc(sourceLabel(s.wl_source||src))}</span></b></div>
    <div class="fleet-cell"><span>Kopējais P&amp;L</span><b class="${pnlClass(total)}">${esc(money(total,ccy))}<span class="sub">${esc(sourceLabel(src))}</span></b></div>
    <div class="fleet-cell"><span>Broker £ / est</span><b>${s.cash_trades_all||0} / ${s.estimate_trades_all||0}<span class="sub">cash / UPL est</span></b></div>`);
}

function trailFlag(phase){
  const p=String(phase||'flat').toLowerCase();
  const labels={flat:'TRAIL —',unknown:'TRAIL ?',manual:'TRAIL manual',watch:'TRAIL watch',
    armed:'TRAIL armed',ratchet:'TRAIL ratchet',be_lock:'TRAIL BE',trail:'TRAILING'};
  return `<span class="flag trail-${esc(p)}">${esc(labels[p]||('TRAIL '+p))}</span>`;
}

function renderDeals(positions,ccy){
  if(!positions||!positions.length)return `<div class="deal flat">Nav atvērta pozīcija</div>`;
  return positions.map(p=>{
    const dir=String(p.direction||'').toUpperCase();
    const sideCls=dir==='BUY'?'buy':(dir==='SELL'?'sell':'');
    const tp=p.has_tp||p.profitLevel!=null;
    const sl=p.has_sl||p.stopLevel!=null;
    const phase=p.trail_phase||(p.enrolled?'armed':'flat');
    const uplCcy=p.currency&&p.currency!==ccy?p.currency:ccy;
    return `<div class="deal">
      <div class="side ${sideCls}">${esc(dir||'—')}</div>
      <div>
        <div class="lot"><em>LOT</em>${esc(p.size!=null?String(p.size):'—')}</div>
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
        ${p.trail_status?`<div class="trail-note">${esc(p.trail_status)}</div>`:''}
      </div>
    </div>`;
  }).join('');
}

function balanceText(a,ccy){
  const b=a&&a.balance;
  if(!b||typeof b!=='object')return '—';
  const bal=b.balance??b.available??b.deposit;
  const pl=b.profitLoss??b.profit_loss;
  if(bal==null && pl==null)return '—';
  const parts=[];
  if(bal!=null)parts.push(money(bal,ccy||b.currency));
  if(pl!=null)parts.push('P/L '+money(pl,ccy||b.currency));
  return parts.join(' · ')||'—';
}

function clientShell(id){
  const el=document.createElement('article');
  el.className='card';
  el.dataset.clientId=String(id);
  el.innerHTML=`
    <div class="card-head">
      <div>
        <h3 data-k="title"></h3>
        <div class="who" data-k="who"></div>
      </div>
      <span class="state-pill" data-k="pill"></span>
    </div>
    <div class="card-body">
      <section data-k="deals"></section>
      <section>
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
          <div><span>Konta balance</span><strong data-k="balance">—</strong></div>
          <div><span>Neironi</span><strong data-k="neurons">—</strong></div>
        </div>
      </section>
      <section class="scoreboard">
        <div class="label">Kopējais P&amp;L</div>
        <div class="big" data-k="pnl">—</div>
        <div class="src" data-k="pnlSrc"></div>
        <div class="row">
          <div class="label">W / L</div>
          <div class="big" data-k="wl">0 / 0</div>
          <div class="src" data-k="wlSrc"></div>
        </div>
        <div class="trades" data-k="trades"></div>
      </section>
    </div>
    <div class="card-foot">
      <div class="note" data-k="note"></div>
      <div class="actions">
        <button type="button" class="btn tiny" data-act="start">Start</button>
        <button type="button" class="btn tiny ghost" data-act="stop">Stop</button>
        <button type="button" class="btn tiny ghost" data-act="resume">Resume</button>
        <button type="button" class="btn tiny ghost" data-act="wipe">Reset learn</button>
      </div>
    </div>`;
  el.querySelectorAll('[data-act]').forEach(btn=>{
    btn.onclick=async()=>{
      const cid=el.dataset.clientId;
      try{
        if(btn.dataset.act==='wipe'){
          if(!confirm(`Reset learn #${cid}?`))return;
          if(prompt('Ieraksti RESET:')!=='RESET'){alert('Atcelts');return}
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
  const positions=Array.isArray(a.positions)&&a.positions.length?a.positions:(a.position?[a.position]:[]);
  const inTrade=positions.length>0;
  if(inTrade){
    const hasLive=!!(Number(sc.BUY)||Number(sc.HOLD)||Number(sc.SELL));
    const fr0=frozenScoresByClient.get(cid);
    const frEmpty=!fr0||!(Number(fr0.BUY)||Number(fr0.HOLD)||Number(fr0.SELL));
    if(hasLive&&(!frozenScoresByClient.has(cid)||frEmpty)){
      frozenScoresByClient.set(cid,{BUY:sc.BUY,HOLD:sc.HOLD,SELL:sc.SELL,action:c.action});
    }
    const fr=frozenScoresByClient.get(cid);
    if(fr&&(Number(fr.BUY)||Number(fr.HOLD)||Number(fr.SELL))){
      sc=fr;c={...c,action:fr.action||c.action};
    }
  }else frozenScoresByClient.delete(cid);

  el.classList.toggle('live',inTrade);
  const paused=!!c.operator_paused;
  const pill=running?(paused?'pause':'on'):'off';
  const pillText=running?(paused?'PAUZE':'ON'):'OFF';
  const broker=c.broker?`${c.broker.display_name}`:'nav brokera';
  const ccy=r.pnl_currency||c.account_currency||a.currency||'';
  const day=a.day_book||{};
  const dayPhase=day.phase||(day.daily&&day.daily.phase)||'—';
  const sb=scoreboard(r);
  const trades=(r.recent_trades||[]).slice(0,10).map(t=>{
    const p=t.pnl;const cls=pnlClass(p);
    const tCcy=t.pnl_currency||ccy;
    const src=t.pnl_source&&t.pnl_source!=='broker'?` [${t.pnl_source}]`:'';
    return `<div><span>${esc(t.direction||'?')} ${esc(t.epic||'')}${esc(src)}</span><span class="${cls}">${p==null?'—':esc(money(p,tCcy))}</span></div>`;
  }).join('')||'<div><span>Nav slēgtu treidu</span></div>';
  const note=[e.neural_adapt_reason||e.last_reason,a.entry_block_reason||a.error||c.last_start_error||a.last_start_error].filter(Boolean).join(' · ');

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
  setText(el.querySelector('[data-k="mandate"]'),(a.mandate&&a.mandate.size)||'—');
  setText(el.querySelector('[data-k="market"]'),a.market||'—');
  setText(el.querySelector('[data-k="balance"]'),balanceText(a,ccy));
  setText(el.querySelector('[data-k="neurons"]'),`${e.neurons??'—'} / ${e.steps??0}`);
  const pnlEl=el.querySelector('[data-k="pnl"]');
  pnlEl.className=`big ${pnlClass(sb.pnl)}`;
  setText(pnlEl,sb.pnl==null?'—':money(sb.pnl,sb.ccy||ccy));
  setText(el.querySelector('[data-k="pnlSrc"]'),
    `${sourceLabel(sb.src)} · cash ${sb.cash}/${sb.closed}`+(sb.estimate?` · est ${sb.estimate}`:''));
  setText(el.querySelector('[data-k="wl"]'),`${sb.wins} / ${sb.losses}`);
  setText(el.querySelector('[data-k="wlSrc"]'),sourceLabel(sb.src==='broker'||sb.src==='upl_estimate'?sb.src:(r.wl_source||sb.src)));
  setHTML(el.querySelector('[data-k="trades"]'),trades);
  setText(el.querySelector('[data-k="note"]'),note||'');
}

async function loadOverview(){
  const data=await api('/api/admin/overview');
  const clients=data.clients||[];
  renderSummary(data.summary,clients);
  const room=$('controlRoom');
  const seen=new Set();
  const pick=$('brokerClient');const prev=pick.value;
  const opts=[];
  clients.forEach(c=>{
    const id=String(c.id);seen.add(id);
    let el=room.querySelector(`[data-client-id="${CSS.escape(id)}"]`);
    if(!el){el=clientShell(id);room.appendChild(el)}
    patchClient(el,c);
    opts.push(`<option value="${esc(id)}">#${esc(id)} ${esc(c.username)} (${esc(c.display_name)})</option>`);
  });
  [...room.querySelectorAll('[data-client-id]')].forEach(el=>{
    if(!seen.has(el.dataset.clientId))el.remove();
  });
  setHTML(pick,opts.join(''));
  if(prev)pick.value=prev;
  setText($('clock'),new Date().toLocaleTimeString('lv-LV',{hour:'2-digit',minute:'2-digit',second:'2-digit'}));
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
  if(!confirm('Izdzēst VISU mācīšanās vēsturi?'))return;
  if(prompt('Ieraksti RESET:')!=='RESET'){alert('Atcelts');return}
  try{alert((await api('/api/admin/reset-learning','POST',{confirmation:'RESET'})).message||'OK');await loadOverview()}
  catch(e){alert(e.message)}
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
  try{
    await api('/api/admin/clients/'+Number(raw.client_id)+'/broker','POST',{
      broker_type:raw.broker_type,display_name:raw.display_name,api_key:raw.api_key,
      identifier:raw.identifier,password:raw.password});
    $('brokerMsg').textContent='OK';ev.target.reset();await loadOverview();
  }catch(e){$('brokerMsg').textContent=e.message}
};

(async()=>{
  try{await api('/api/admin/me');await enterDash()}
  catch(_){show(true)}
})();
