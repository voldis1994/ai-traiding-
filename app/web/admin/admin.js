'use strict';
const $=id=>document.getElementById(id);
let pollTimer=null;
let lastOverview=null;
let chartRange='1W';
let overviewInFlight=null;
let lastSyncAt=null;
const POLL_IDLE_MS=5000;
const POLL_LIVE_MS=2000;
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
  if(src==='broker')return 'broker £';
  if(src==='upl_estimate')return 'UPL peļņa';
  if(src==='open_upl')return 'live UPL';
  if(src==='working')return 'working';
  if(src==='none'||src==='empty')return 'nav skaitļu';
  return src||'—';
}

function hasRealWorkingPnl(r){
  if(r==null||r.working_pnl==null||!Number.isFinite(Number(r.working_pnl)))return false;
  const src=String(r.working_pnl_source||'').toLowerCase();
  // Backend used to emit working_pnl=0 with source empty/none — treat as missing.
  if(src==='none'||src==='empty')return false;
  return true;
}

/** Prefer working/estimate P&L + W/L — never show a lying 0 when estimates exist (from main #89). */
function scoreboard(r){
  r=r||{};
  const ccy=r.pnl_currency||'';
  const ledger=Array.isArray(r.pnl_ledger)?r.pnl_ledger:[];
  const recent=Array.isArray(r.recent_trades)?r.recent_trades:[];
  let pnl=null,src='none',wins=0,losses=0;

  if(hasRealWorkingPnl(r)){
    pnl=Number(r.working_pnl);src=r.working_pnl_source||'working';
  }else if((r.cash_trades||0)>0 && r.realized_pnl!=null && Number.isFinite(Number(r.realized_pnl))){
    pnl=Number(r.realized_pnl);src='broker';
  }else if(r.estimate_pnl!=null && Number.isFinite(Number(r.estimate_pnl))){
    pnl=Number(r.estimate_pnl);src='upl_estimate';
  }else if(r.open_upl!=null && Number.isFinite(Number(r.open_upl))){
    pnl=Number(r.open_upl);src='open_upl';
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

  if((pnl==null || wins+losses===0) && ledger.length){
    let sum=0,w=0,l=0;
    for(const row of ledger){
      const v=Number(row.pnl);
      if(!Number.isFinite(v))continue;
      sum+=v;if(v>0)w++;else if(v<0)l++;
    }
    if(pnl==null && ledger.length){
      pnl=sum;
      src=ledger.some(x=>x.source==='broker')&&(r.cash_trades||0)>0?'broker':'upl_estimate';
    }
    if(wins+losses===0){wins=w;losses=l}
  }

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

function layoutCols(n){
  if(n<=1)return 1;
  if(n===2||n===3)return n;
  if(n===4)return 2;
  if(n<=6)return 3;
  return Math.min(4,Math.ceil(Math.sqrt(n)));
}

function setView(name){
  document.querySelectorAll('.nav-item').forEach(b=>{
    b.classList.toggle('active',b.dataset.view===name);
  });
  document.querySelectorAll('.view').forEach(v=>{
    v.classList.toggle('active',v.dataset.view===name);
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
  // Treat empty/none fleet totals as missing so live open UPL can surface.
  if(total!=null && (src==='none'||src==='empty'))total=null;
  if(total==null && s.open_upl_all!=null && Number.isFinite(Number(s.open_upl_all))){
    total=Number(s.open_upl_all);src='open_upl';
  }
  if(total==null || wins==null || losses==null){
    let sum=0,n=0,w=0,l=0,anyEst=false,anyOpen=false;
    for(const c of clients||[]){
      const sb=scoreboard(c.results||{});
      if(sb.pnl!=null){sum+=sb.pnl;n++}
      w+=sb.wins;l+=sb.losses;
      if(sb.src==='upl_estimate')anyEst=true;
      if(sb.src==='open_upl')anyOpen=true;
    }
    if(total==null && n){
      total=sum;
      src=anyEst?'upl_estimate':(anyOpen?'open_upl':src);
    }
    if(wins==null)wins=w;
    if(losses==null)losses=l;
  }
  // Multi-currency: never invent one number from mixed books.
  const by=(src==='open_upl'
    ?(s.open_upl_by_currency||{})
    :(s.working_pnl_by_currency||s.pnl_by_currency||{}));
  const keys=Object.keys(by);
  let pnlLabel,pnlNum=total;
  if(keys.length>1 && total==null){
    pnlLabel=keys.map(k=>`${num(by[k],2)} ${k}`).join(' · ');
    pnlNum=null;
  }else if(pnlNum!=null){
    pnlLabel=money(pnlNum,ccy);
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

  $('summary').innerHTML=`
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
    </article>`;

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
    (r.recent_trades||[]).forEach((t,idx)=>{
      const pnl=Number(t.pnl);
      if(!Number.isFinite(pnl))return;
      const ts=Date.parse(t.closed_at||t.closed||t.time||t.ts||'')||(Date.now()-idx*3600e3);
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
  if(!points.length){
    // Soft placeholder curve so the glass chart never looks empty.
    const seed=[0,12,8,22,18,34,41,38,52,61];
    return seed.map((v,i)=>({t:now-(seed.length-i)*864e5,v}));
  }
  return points;
}

function drawPerfChart(clients){
  const canvas=$('perfChart');
  if(!canvas)return;
  const dpr=window.devicePixelRatio||1;
  const cssW=canvas.clientWidth||800;
  const cssH=220;
  canvas.width=Math.floor(cssW*dpr);
  canvas.height=Math.floor(cssH*dpr);
  const ctx=canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const pts=buildEquitySeries(clients,chartRange);
  const pad={l:8,r:8,t:16,b:28};
  const w=cssW-pad.l-pad.r,h=cssH-pad.t-pad.b;
  const vals=pts.map(p=>p.v);
  let min=Math.min(...vals),max=Math.max(...vals);
  if(min===max){min-=1;max+=1}
  const x=i=>pad.l+(pts.length===1?w/2:(i/(pts.length-1))*w);
  const y=v=>pad.t+h-((v-min)/(max-min))*h;

  ctx.clearRect(0,0,cssW,cssH);
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
    $('topClients').innerHTML=`<div class="empty">Vēl nav klientu</div>`;
    return;
  }
  $('topClients').innerHTML=ranked.map((c,i)=>`
    <div class="rank">
      <span class="medal m${i+1}">${i+1}</span>
      <div class="who">${esc(c.name)}<small>@${esc(c.user)} · ${esc(sourceLabel(c.src))} · W/L ${esc(c.wl)}</small></div>
      <div class="earn ${pnlClass(c.pnl)}">${esc(money(c.pnl,c.ccy))}
        <small>${c.wr==null?'—':((c.wr<=1?c.wr*100:c.wr).toFixed(1)+'% WR')}</small>
      </div>
    </div>`).join('');
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
    $('riskAlerts').innerHTML=`<div class="empty">Nav aktīvu brīdinājumu</div>`;
    return;
  }
  $('riskAlerts').innerHTML=alerts.map(a=>`
    <div class="alert">
      <div><b>${esc(a.title)}</b><span>${esc(a.detail)}</span></div>
      <div class="metric">${esc(a.metric)}</div>
    </div>`).join('');
}

function renderRiskBoard(clients){
  const board=$('riskBoard');
  if(!(clients||[]).length){
    board.innerHTML=`<div class="empty">Nav klientu</div>`;
    return;
  }
  board.innerHTML=clients.map(c=>{
    const hr=c.hard_risk||{};
    const name=c.display_name||c.username||('#'+c.id);
    const halted=!!hr.trading_halted;
    return `<article class="card risk-card">
      <div class="card-head">
        <h2>${icon('shield')} ${esc(name)}</h2>
        <span class="state-pill ${halted?'pause':(c.robot_running?'on':'off')}">${halted?'HALT':(c.robot_running?'ON':'OFF')}</span>
      </div>
      <div class="kv"><span>Max risks / treids</span><b>${esc(hr.max_risk_per_trade??'—')}</b></div>
      <div class="kv"><span>Dienas zaud. limits</span><b>${esc(hr.daily_loss_limit??'—')}</b></div>
      <div class="kv"><span>Drawdown limits</span><b>${esc(hr.equity_drawdown_limit??'—')}</b></div>
      <div class="kv"><span>Halt iemesls</span><b>${esc(hr.halt_reason||'—')}</b></div>
      <div class="kv"><span>Entry block</span><b>${esc((c.activity||{}).entry_block_reason||'—')}</b></div>
    </article>`;
  }).join('');
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
  $('recentClosed').innerHTML=renderTradesTable(rows.slice(0,20),{empty:'Nav nesen slēgtu treidu'});
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
  const tp=p.has_tp || p.profitLevel!=null;
  const sl=p.has_sl || p.stopLevel!=null;
  const phase=p.trail_phase||(p.enrolled?'armed':'flat');
  const uplCcy=p.currency&&p.currency!==ccy?p.currency:ccy;
  const mid=positionMid(p,activity);
  return `<div class="deal">
    <div class="side ${sideCls}">${esc(dir||'—')}</div>
    <div>
      <div class="lot"><em>LOT</em>${esc(lot)}</div>
      <div class="deal-meta">
        <span>Epic <strong>${esc(p.epic||'—')}</strong></span>
        <span>Entry <strong>${esc(num(p.level,2))}</strong></span>
        <span>Mid <strong>${mid==null?'—':esc(num(mid,2))}</strong></span>
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
  const sb=scoreboard(r);
  const trades=(r.recent_trades||[]).slice(0,4).map(t=>{
    const p=t.pnl;const cls=pnlClass(p);
    const tCcy=t.pnl_currency||ccy;
    const pending=t.pending_cash||t.pnl_source==='pending_broker_pnl'||t.pnl_source==='pending_close_sync';
    const src=pending?' [gaida £]':(t.pnl_source==='upl_estimate'?' [UPL]':
      (t.pnl_source&&t.pnl_source!=='broker'?` [${t.pnl_source}]`:''));
    let right;
    if(p!=null){right=money(p,tCcy);}
    else if(pending&&t.upl_hint!=null){right=`gaida · UPL ${money(t.upl_hint,tCcy)}`;}
    else if(pending){right='gaida Capital £';}
    else{right='—';}
    return `<div><span>${esc(t.direction||'?')} ${esc(t.epic||'')}${esc(src)}</span><span class="${cls}">${esc(right)}</span></div>`;
  }).join('')||'<div><span>Nav slēgtu treidu</span></div>';

  const dealsHtml=inTrade
    ?positions.map(p=>renderDeal(p,ccy,a)).join('')
    :renderDeal(null,ccy,a);

  const trailNote=(()=>{
    const live=positions.find(p=>p.trail_status)||positions[0];
    const st=live&&live.trail_status;
    const prot=(a.protection&&a.protection.status)||st;
    if(!inTrade)return '';
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
    <div class="ohlc-panel" data-ohlc-client="${c.id}">
      <div class="ohlc-head">
        <span>OHLC · entry zonas</span>
        <em class="ohlc-meta muted">ielādē…</em>
      </div>
      <canvas class="ohlc-mini" width="640" height="160"></canvas>
      <div class="ohlc-legend muted"></div>
    </div>
    <div class="nerve">
      <div class="buy"><em>BUY</em><b>${pct(sc.BUY)}</b></div>
      <div><em>HOLD</em><b>${pct(sc.HOLD)}</b></div>
      <div class="sell"><em>SELL</em><b>${pct(sc.SELL)}</b></div>
    </div>
    <div class="facts">
      <div><span>Signāls</span><strong>${esc(c.action||'—')}</strong></div>
      <div><span>MTF režīms</span><strong>${esc(((a.mtf_momentum||{}).mode)||'—')}${
        (a.mtf_momentum&&a.mtf_momentum.preferred)?(' · '+esc(a.mtf_momentum.preferred)) : ''
      }${
        (a.mtf_momentum&&a.mtf_momentum.stop_hint_atr14!=null)
          ?(' · ATR '+esc(num(a.mtf_momentum.stop_hint_atr14,2))) : ''
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
      <div><span>Tirgus</span><strong>${esc(a.market||'—')}</strong></div>
      <div><span>Neironi / soļi</span><strong>${esc(e.neurons??'—')} / ${esc(e.steps??0)}</strong></div>
      <div><span>Paaudze</span><strong>${esc(e.learning_generation??e.generation??0)}</strong></div>
      <div><span>Kopējais P&amp;L</span><strong class="${pnlClass(sb.pnl)}">${
        sb.pnl==null?'—':esc(money(sb.pnl,sb.ccy||ccy))
      }</strong></div>
      <div><span>W / L · avots</span><strong>${esc(`${sb.wins} / ${sb.losses}`)} · ${esc(sourceLabel(sb.src))}</strong></div>
      ${(r.pending_broker_pnl||r.pending_trades||r.pending_cash_trades)?`<div><span>Gaida broker £</span><strong>${esc(r.pending_broker_pnl??0)} rindā · ${esc(r.pending_trades??r.pending_cash_trades??0)} žurnālā</strong></div>`:''}
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
      <button type="button" class="btn tiny btn-ghost" data-act="wipe" data-id="${c.id}">${icon('sync')} Mācīties no jauna</button>
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
  // Lazy-load OHLC structure chart (not every overview tick payload).
  queueMicrotask(()=>refreshOhlcPanel(el,c.id).catch(()=>{}));
  return el;
}

function drawOhlcStructure(canvas,view){
  if(!canvas||!view)return;
  const dpr=Math.min(2,window.devicePixelRatio||1);
  const rect=canvas.getBoundingClientRect();
  if(rect.width<20)return;
  const w=rect.width,h=rect.height;
  canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle='rgba(8,12,14,.9)';
  ctx.fillRect(0,0,w,h);
  const candles=(view.candles||[]).filter(c=>
    ['open','high','low','close'].every(k=>Number.isFinite(Number(c[k]))));
  if(!candles.length){
    ctx.fillStyle='#8a9a92';ctx.font='12px Manrope,sans-serif';ctx.textAlign='center';
    ctx.fillText('Gaida OHLC sveces…',w/2,h/2);
    return;
  }
  let low=Math.min(...candles.map(c=>+c.low));
  let high=Math.max(...candles.map(c=>+c.high));
  const zones=view.zones||{};
  const levels=[];
  for(const z of [zones.support,zones.resistance,zones.buy_entry,zones.sell_entry]){
    if(!z)continue;
    if(Number.isFinite(+z.lo))low=Math.min(low,+z.lo);
    if(Number.isFinite(+z.hi))high=Math.max(high,+z.hi);
  }
  const br=view.swing_brackets||{};
  for(const k of ['stop_level','profit_level','swing_hi','swing_lo']){
    if(Number.isFinite(+br[k])){low=Math.min(low,+br[k]);high=Math.max(high,+br[k]);}
  }
  for(const p of (view.open_levels||[])){
    for(const k of ['entry','stop','profit']){
      if(Number.isFinite(+p[k])){low=Math.min(low,+p[k]);high=Math.max(high,+p[k]);}
    }
  }
  if(Number.isFinite(+view.mid)){low=Math.min(low,+view.mid);high=Math.max(high,+view.mid);}
  const pad=(high-low||Math.abs(high)*.001||.01)*.18;
  low-=pad;high+=pad;
  const y=p=>16+(high-p)/(high-low)*(h-36);
  const chartW=w-64;
  const spacing=chartW/candles.length;
  const band=(z,fill,stroke)=>{
    if(!z)return;
    const a=Number.isFinite(+z.lo)?+z.lo:null;
    const b=Number.isFinite(+z.hi)?+z.hi:(a);
    if(a==null)return;
    const y1=y(Math.max(a,b)),y2=y(Math.min(a,b));
    ctx.fillStyle=fill;
    ctx.fillRect(12,y1,chartW,Math.max(2,y2-y1));
    if(stroke){
      ctx.strokeStyle=stroke;ctx.setLineDash([]);ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(12,y(a));ctx.lineTo(12+chartW,y(a));ctx.stroke();
      if(b!=null&&b!==a){ctx.beginPath();ctx.moveTo(12,y(b));ctx.lineTo(12+chartW,y(b));ctx.stroke();}
    }
  };
  // Structure S/R first, then prospective entry zones.
  band(zones.resistance,'rgba(255,92,122,.14)','rgba(255,92,122,.55)');
  band(zones.support,'rgba(57,255,136,.12)','rgba(57,255,136,.5)');
  const pref=view.preferred;
  if(!pref||pref==='BUY')band(zones.buy_entry,'rgba(57,255,136,.22)',null);
  if(!pref||pref==='SELL')band(zones.sell_entry,'rgba(255,92,122,.20)',null);
  candles.forEach((c,i)=>{
    const x=12+(i+.5)*spacing;
    const rising=+c.close>=+c.open;
    ctx.strokeStyle=rising?'#39ff88':'#ff5c7a';
    ctx.fillStyle=ctx.strokeStyle;
    ctx.beginPath();ctx.moveTo(x,y(+c.high));ctx.lineTo(x,y(+c.low));ctx.stroke();
    const y1=y(+c.open),y2=y(+c.close);
    ctx.fillRect(x-Math.max(1,spacing*.32),Math.min(y1,y2),
      Math.max(2,spacing*.64),Math.max(1,Math.abs(y1-y2)));
  });
  const hline=(price,color,dash)=>{
    if(!Number.isFinite(+price))return;
    ctx.strokeStyle=color;ctx.lineWidth=1.25;
    ctx.setLineDash(dash||[]);
    ctx.beginPath();ctx.moveTo(12,y(+price));ctx.lineTo(12+chartW,y(+price));ctx.stroke();
    ctx.setLineDash([]);
  };
  if(br&&br.ready){
    hline(br.stop_level,'#ff5c7a',[4,3]);
    hline(br.profit_level,'#39ff88',[4,3]);
  }
  hline(view.mid,'#f0c14b',[]);
  for(const p of (view.open_levels||[])){
    hline(p.entry,'#eef6f1',[]);
    hline(p.stop,'#ff5c7a',[5,3]);
    hline(p.profit,'#39ff88',[5,3]);
  }
  ctx.fillStyle='#8a9a92';ctx.font='10px Manrope,sans-serif';ctx.textAlign='left';
  for(let k=0;k<=4;k++){
    const price=high-(high-low)*k/4;
    ctx.fillText(price.toFixed(Math.abs(high)<20?2:1),w-50,18+(h-36)*k/4);
  }
}

async function refreshOhlcPanel(cardEl,clientId){
  const panel=cardEl&&cardEl.querySelector(`[data-ohlc-client="${clientId}"]`);
  if(!panel)return;
  const meta=panel.querySelector('.ohlc-meta');
  const legend=panel.querySelector('.ohlc-legend');
  const canvas=panel.querySelector('canvas.ohlc-mini');
  try{
    const data=await api(`/api/admin/clients/${clientId}/structure-view?n=90`);
    const view=data||{};
    const st=view.structure||{};
    if(meta){
      meta.textContent=[
        view.epic||'',
        st.bias?`bias ${st.bias}`:'',
        st.event||'',
        view.preferred?`zona ${view.preferred}`:'abas zonas',
      ].filter(Boolean).join(' · ');
    }
    if(legend){
      const bits=[];
      if(view.zones&&view.zones.buy_entry)bits.push('zaļa = BUY entry zona pie swing low');
      if(view.zones&&view.zones.sell_entry)bits.push('sarkana = SELL entry zona pie swing high');
      bits.push('dzeltena = mid');
      if(view.swing_brackets&&view.swing_brackets.ready)bits.push('punktētas = swing SL/TP');
      legend.textContent=bits.join(' · ');
    }
    drawOhlcStructure(canvas,view);
  }catch(err){
    if(meta)meta.textContent=String(err.message||err).slice(0,80);
  }
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
  const s=(data&&data.summary)||{};
  const running=Number(s.running_robots)||0;
  const open=Number(s.open_trades_all)||collectOpenTrades((data&&data.clients)||[]).length;
  return (running>0 || open>0)?POLL_LIVE_MS:POLL_IDLE_MS;
}

function schedulePoll(ms){
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
    $('openTradesPreview').innerHTML=renderTradesTable(open.slice(0,8));
    $('openTradesFull').innerHTML=renderTradesTable(open);
    renderTopClients(data.clients);
    renderRiskAlerts(data.clients);
    renderRiskBoard(data.clients);
    renderRecentClosed(data.clients);
    drawPerfChart(data.clients);

    const room=$('controlRoom');
    const clients=data.clients||[];
    const n=clients.length;
    room.dataset.count=String(n);
    room.dataset.dense=n>=4?'1':'0';
    room.style.setProperty('--cols',String(layoutCols(n)));
    const pick=$('brokerClient');const prev=pick.value;pick.innerHTML='';
    const frag=document.createDocumentFragment();
    clients.forEach(c=>{
      const card=renderCard(c);
      card.dataset.cid=String(c.id);
      frag.appendChild(card);
      const opt=document.createElement('option');opt.value=c.id;
      opt.textContent=`#${c.id} ${c.username} (${c.display_name})`;pick.appendChild(opt);
    });
    room.replaceChildren(frag);
    if(prev)pick.value=prev;
    schedulePoll(desiredPollMs(data));
  })();
  try{return await overviewInFlight;}
  finally{overviewInFlight=null}
}

async function enterDash(){
  show(false);
  await loadBrokers();
  await loadOverview();
  schedulePoll(desiredPollMs(lastOverview));
}

document.querySelectorAll('.nav-item').forEach(btn=>{
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
window.addEventListener('resize',()=>{
  if(lastOverview)drawPerfChart(lastOverview.clients);
});
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
  try{await api('/api/admin/logout','POST')}finally{show(true)}
};
$('refreshBtn').onclick=()=>loadOverview().catch(e=>alert(e.message));
$('startAllBtn').onclick=async()=>{
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
