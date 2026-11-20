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
  if(src==='broker'||src==='upl_estimate')return 'peļņa';
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
  if(name==='desk'){
    lastDeskChartKey='';
    if(lastOverview)renderDesk(lastOverview).catch(()=>{});
  }
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

let deskTfMinutes=1;

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
      <div class="ep">${esc(epic)}</div>
      <div class="md">${esc(mid)}</div>
      <div class="ch ${chCls}">${esc(chTxt)}</div>
      <div class="cl">${esc(names||('#'+cid))}</div>
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

function renderDeskLevels(view,client){
  const el=$('deskLevels');
  if(!el)return;
  const open=(view&&view.open_levels)||[];
  const a=(client&&client.activity)||{};
  const pos=(Array.isArray(a.positions)&&a.positions[0])||a.position||null;
  const entry=open[0]&&open[0].entry!=null?open[0].entry:(pos&&pos.level);
  const stop=open[0]&&open[0].stop!=null?open[0].stop:(pos&&pos.stopLevel);
  const profit=open[0]&&open[0].profit!=null?open[0].profit:(pos&&pos.profitLevel);
  const mid=view&&view.mid!=null?view.mid:a.market_mid;
  const bits=[];
  if(stop!=null&&Number(stop)>0)bits.push(`<span class="psx-lvl sl">SL ${esc(num(stop,2))}</span>`);
  if(entry!=null&&Number(entry)>0)bits.push(`<span class="psx-lvl ent">ENT ${esc(num(entry,2))}</span>`);
  if(profit!=null&&Number(profit)>0){
    bits.push(`<span class="psx-lvl tp">TP1 ${esc(num(profit,2))}</span>`);
    const tp2=Number(profit)+(Number(profit)-Number(entry||mid||profit))*0.5;
    if(Number.isFinite(tp2))bits.push(`<span class="psx-lvl tp">TP2 ${esc(num(tp2,2))}</span>`);
  }
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
function renderDeskLearn(client){ /* sink — learn msg in intel */ }

function renderDeskIndicators(client,view){
  const el=$('deskIndicators');
  if(!el)return;
  if(!client){stableHtml(el,'');return}
  const a=client.activity||{};
  const mtf=a.mtf_momentum||{};
  const ov=(view&&view.overlays)||{};
  const mtfUi=(view&&view.mtf)||{};
  const atr=mtf.stop_hint_atr14??((mtf.tf||{}).m1||{}).atr14??mtfUi.atr14??ov.atr14;
  const adx=mtfUi.adx??ov.adx;
  const kama=mtfUi.kama??ov.kama_level??((mtf.tf||{}).m5||{}).kama;
  stableHtml(el,[
    deskKv(client.id,'ATR14',atr,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'ADX',adx,v=>v==null?'—':num(v,1)),
    deskKv(client.id,'KAMA5',kama,v=>v==null?'—':num(v,2)),
    deskKv(client.id,'Spread',a.spread!=null?a.spread:((a.market_ask!=null&&a.market_bid!=null)?(Number(a.market_ask)-Number(a.market_bid)):null),
      v=>v==null?'—':num(v,3)),
  ].join(''));
}

function renderDeskCrowd(client){
  const el=$('deskCrowd');
  if(!el)return;
  if(!client){stableHtml(el,'');return}
  const {buy,hold,sell}=deskScoreAction(client);
  // Map neural scores → Long/Short crowd like PerpSignal (HOLD folds into both lightly).
  const longP=Math.max(0,buy+hold*0.35);
  const shortP=Math.max(0,sell+hold*0.35);
  const sum=Math.max(0.0001,longP+shortP);
  const lp=Math.round((longP/sum)*1000)/10;
  const sp=Math.round((100-lp)*10)/10;
  const lean=Math.round(Math.abs(lp-sp));
  const leanSide=lp>=sp?'BULLISH LEAN':'BEARISH LEAN';
  stableHtml(el,`
    <div class="psx-crowd-bar"><i class="b" style="width:${lp}%"></i><i class="s" style="width:${sp}%"></i></div>
    <div class="psx-crowd-lbl"><span class="b">${lp}% Long</span><span class="s">${sp}% Short</span></div>
    <div class="psx-lean">${lean}% ${esc(leanSide)}
      <div class="track"><div class="fill" style="width:${Math.min(100,lean+20)}%"></div></div>
    </div>`);
}

function renderDeskSignal(client,markets){
  const el=$('deskSignal');
  if(!el)return;
  const rows=(markets||[]).slice(0,6);
  if(!rows.length&&client){
    const {buy,hold,sell,act}=deskScoreAction(client);
    const strength=act==='SELL'?sell:(act==='BUY'?buy:hold);
    const agree=Math.round(Math.max(0,Math.min(1,strength))*100);
    const side=act==='BUY'?'LONG':(act==='SELL'?'SHORT':'HOLD');
    const cls=act==='BUY'?'long':(act==='SELL'?'short':'hold');
    const epic=((client.activity||{}).market)||(((client.activity||{}).mandate)||{}).epic||'FOCUS';
    stableHtml(el,`<div class="psx-sig-row"><strong>${esc(epic)}</strong>
      <span class="agree">${agree}% agree →</span>
      <span class="psx-sig-btn ${cls}">${side}</span></div>`);
    return;
  }
  if(!rows.length){stableHtml(el,'<div class="psx-empty">No signals</div>');return}
  const focusAct=client?deskScoreAction(client):null;
  stableHtml(el,rows.map((m,i)=>{
    const epic=m.epic||'—';
    let act='HOLD',agree=50;
    if(i===0&&focusAct){
      act=focusAct.act;
      const s=act==='SELL'?focusAct.sell:(act==='BUY'?focusAct.buy:focusAct.hold);
      agree=Math.round(Math.max(0,Math.min(1,s))*100);
    }else{
      const ch=deskCandleChange(m.candles);
      if(ch!=null){
        act=ch>0.05?'BUY':(ch<-0.05?'SELL':'HOLD');
        agree=Math.min(99,Math.round(50+Math.abs(ch)*8));
      }
    }
    const side=act==='BUY'?'LONG':(act==='SELL'?'SHORT':'HOLD');
    const cls=act==='BUY'?'long':(act==='SELL'?'short':'hold');
    return `<div class="psx-sig-row"><strong>${esc(epic)}</strong>
      <span class="agree">${agree}% agree →</span>
      <span class="psx-sig-btn ${cls}">${side}</span></div>`;
  }).join(''));
}

function renderDeskClosed(client){
  const el=$('deskClosed');
  if(!el)return;
  if(!client){stableHtml(el,'<div class="psx-empty">—</div>');return}
  const r=client.results||{};
  const ccy=r.pnl_currency||client.account_currency||'';
  const rows=(r.recent_trades||[]).slice(0,8);
  if(!rows.length){
    stableHtml(el,'<div class="psx-empty">No whale prints yet</div>');
    return;
  }
  stableHtml(el,rows.map(t=>{
    const p=t.pnl!=null?t.pnl:(t.upl_hint!=null?t.upl_hint:null);
    const side=String(t.direction||'?').toUpperCase();
    const sideCls=side==='BUY'||side==='LONG'?'buy':'sell';
    const val=p==null?'—':money(p,t.pnl_currency||ccy);
    const when=(t.closed_at||t.updated_at||t.at||'').toString().slice(11,19)||'—';
    return `<div class="psx-whale-row">
      <span class="tag">WHALE</span>
      <span><span class="${sideCls}">${esc(side)}</span> ${esc(t.epic||'')} · ${esc(when)}</span>
      <strong class="${pnlClass(p)}">${esc(val)}</strong>
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
  const ring=act==='SELL'?'#ef4444':(act==='BUY'?'#22c55e':'#e0b34d');
  const side=act==='SELL'?'SHORT':(act==='BUY'?'LONG':'HOLD');
  const regime=act==='SELL'?'BEAR REGIME':(act==='BUY'?'BULL REGIME':'NEUT REGIME');
  stableHtml(el,`
    <div class="psx-ring" style="--p:${pctN};--ring:${ring}">
      <div class="psx-ring-core">
        <b style="color:${ring}">${pctN}%</b>
        <span>${esc(side)}</span>
      </div>
    </div>
    <div class="psx-regime ${regimeCls}">${esc(regime)}</div>
    <div class="psx-engine-meta">
      MTF <strong>${esc(mtf.mode||'—')}${mtf.preferred?(' · '+esc(mtf.preferred)):''}</strong><br>
      Day <strong>${esc(dayPhase)}</strong> · Armed <strong>${esc(client.armed?'YES':'NO')}</strong>
      · Exec <strong>${esc(client.execution_enabled?'ON':'OFF')}</strong>
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
  const mtfTag=/sell|bear|down/i.test(String(mtfLean))?'BEAR'
    :(/buy|bull|up/i.test(String(mtfLean))?'BULL':(mtf.ready?'BULL':'NEUT'));
  const rows=[
    {k:'Trend Structure',v:lean,cls:sc.act==='HOLD'?'neu':(sc.act==='SELL'?'bad':'ok')},
    {k:'Momentum',v:mtfTag,cls:mtfTag==='NEUT'?'neu':(mtfTag==='BEAR'?'bad':'ok')},
    {k:'Flow',v:(a.day_book&&(a.day_book.phase||''))||lean,cls:'neu'},
    {k:'Book Balance',v:a.market_status==='TRADEABLE'?'LIVE':'WAIT',
      cls:a.market_status==='TRADEABLE'?'ok':'neu'},
    {k:'Liquidity',v:a.spread!=null?(Number(a.spread)<1?'OK':'WIDE'):'—',cls:'neu'},
    {k:'Model',v:ready.ready?'READY':'WAIT',cls:ready.ready?'ok':'neu'},
    {k:'Can OPEN',v:a.entry_can_submit_now?'YES':'NO',cls:a.entry_can_submit_now?'ok':'neu'},
    {k:'SL Bracket',v:hasSl?'ON':'OFF',cls:hasSl?'ok':(pos?'bad':'neu')},
    {k:'TP Bracket',v:hasTp?'ON':'OFF',cls:hasTp?'ok':(pos?'bad':'neu')},
    {k:'Swing Plan',v:(view&&view.swing_brackets&&view.swing_brackets.ready)?'READY':'—',
      cls:(view&&view.swing_brackets&&view.swing_brackets.ready)?'ok':'neu'},
  ];
  stableHtml(el,rows.map(r=>
    `<div class="psx-score-row"><span>${esc(r.k)}</span><b class="${esc(r.cls)}">${esc(String(r.v).slice(0,16))}</b></div>`
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
  const push=(tag,t,cls,src)=>chips.push({tag,t:String(t).slice(0,120),cls,src:src||'NEURAL'});
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
  const tick=Number(a.tick)||(Number.isFinite(mid)?mid*0.00015:1);
  if(!Number.isFinite(mid)||mid<=0){
    stableHtml(el,'<div class="psx-empty">No quote ladder</div>');
    return;
  }
  const spr=(Number.isFinite(bid)&&Number.isFinite(ask))?Math.max(0,ask-bid):tick;
  const levels=9;
  const asks=[],bids=[];
  let askTot=0,bidTot=0;
  for(let i=levels;i>=1;i--){
    const px=mid+spr/2+i*tick;
    const sz=Math.max(0.01,+(0.28+((levels-i+1)*0.09)+(Math.abs(Math.sin(px*3))*0.18)).toFixed(2));
    askTot+=sz;asks.push({px,sz,tot:askTot});
  }
  for(let i=1;i<=levels;i++){
    const px=mid-spr/2-i*tick;
    const sz=Math.max(0.01,+(0.30+(i*0.1)+(Math.abs(Math.cos(px*3))*0.16)).toFixed(2));
    bidTot+=sz;bids.push({px,sz,tot:bidTot});
  }
  const maxSz=Math.max(...asks.map(x=>x.sz),...bids.map(x=>x.sz),0.01);
  const tot=askTot+bidTot||1;
  const bidPct=Math.round((bidTot/tot)*1000)/10;
  const askPct=Math.round((100-bidPct)*10)/10;
  let run=0;
  const askHtml=asks.map(r=>{
    run+=r.sz;
    return `<div class="psx-book-row ask"><i style="width:${Math.round((r.sz/maxSz)*100)}%"></i>
      <span>${esc(num(r.px,2))}</span><span>${esc(num(r.sz,2))}</span>
      <span>${esc(num(r.px*r.sz,0))}</span><span>${esc(num(run,2))}</span></div>`;
  }).join('');
  run=0;
  const bidHtml=bids.map(r=>{
    run+=r.sz;
    return `<div class="psx-book-row bid"><i style="width:${Math.round((r.sz/maxSz)*100)}%"></i>
      <span>${esc(num(r.px,2))}</span><span>${esc(num(r.sz,2))}</span>
      <span>${esc(num(r.px*r.sz,0))}</span><span>${esc(num(run,2))}</span></div>`;
  }).join('');
  stableHtml(el,askHtml+
    `<div class="psx-spread">SPREAD ${esc(num(spr,4))}</div>`+
    bidHtml+
    `<div class="psx-depth"><span class="b" style="width:${bidPct}%"></span><span class="a" style="width:${askPct}%"></span></div>
     <div class="psx-depth-lbl"><span>${bidPct}% BID</span><span>${askPct}% ASK</span></div>`);
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
    const spark=closes.map(v=>{
      const h=Math.max(8,Math.round(((v-lo)/span)*100));
      return `<i style="height:${h}%"></i>`;
    }).join('');
    const active=focusEpic&&String(m.epic)===String(focusEpic);
    return `<div class="psx-watch-row">
      <strong>${esc(m.epic||'—')}${active?' ●':''}</strong>
      <span class="psx-mini ${ch!=null&&ch<0?'dn':''}">${spark||'<i style="height:40%"></i>'}</span>
      <span>${esc(mid)}</span>
      <span class="${cls}">${esc(chTxt)}</span>
    </div>`;
  }).join(''));
}

function renderDeskFear(client){
  const el=$('deskFear');
  if(!el)return;
  if(!client){stableHtml(el,'');return}
  const {buy,hold,sell,act}=deskScoreAction(client);
  const fear=Math.round(Math.max(0,Math.min(100,
    hold*100*0.85+(act==='HOLD'?12:0)-(Math.abs(buy-sell)*42))));
  let label='Neutral';
  if(fear<=20)label='Extreme Fear';
  else if(fear<=40)label='Fear';
  else if(fear<=60)label='Neutral';
  else if(fear<=80)label='Greed';
  else label='Extreme Greed';
  stableHtml(el,`
    <div class="psx-fear-val">${fear} · ${esc(label)}</div>
    <div class="psx-fear-meter"><i style="left:${fear}%"></i></div>`);
}

function renderDeskIndCharts(client){
  const el=$('deskIndCharts');
  if(!el)return;
  if(!client){stableHtml(el,'');return}
  const a=client.activity||{};
  const rb=a.returns_bps||{};
  const keys=['1','5','15','30','60'];
  const vals=keys.map(k=>{
    const n=Number(rb[k]??rb[k+'m']??rb['m'+k]);
    return Number.isFinite(n)?n:0;
  });
  const max=Math.max(1,...vals.map(Math.abs));
  const spark=(arr,bear)=>`<div class="psx-spark ${bear?'bear':''}">${arr.map(v=>{
    const h=Math.max(8,Math.round((Math.abs(v)/max)*100));
    return `<b style="height:${h}%"></b>`;
  }).join('')}</div>`;
  const rsiProxy=Math.round(50+Math.max(-40,Math.min(40,(vals[1]||0)/2)));
  stableHtml(el,`
    <div><div style="font-size:9px;color:#8b919c;margin-bottom:3px;font-weight:800;letter-spacing:.06em">RSI · ${rsiProxy}</div>${spark(vals,rsiProxy<45)}</div>
    <div><div style="font-size:9px;color:#8b919c;margin-bottom:3px;font-weight:800;letter-spacing:.06em">MACD · MTF</div>${spark(vals.slice().reverse(),(vals[0]||0)<0)}</div>
  `);
}

function renderDeskLiveTrades(openRows){
  const el=$('deskDeals');
  if(!el)return;
  const rows=(openRows||[]).slice(0,10);
  if(!rows.length){
    stableHtml(el,'<div class="psx-empty">No live prints</div>');
    return;
  }
  const head=`<div class="psx-trade-row psx-trade-head"><span>TIME</span><span>SIDE</span><span>PRICE</span><span>SIZE</span><span>VALUE</span></div>`;
  const body=rows.map(t=>{
    const side=String(t.direction||t.side||'?').toUpperCase();
    const cls=side==='BUY'||side==='LONG'?'buy':'sell';
    const px=t.current??t.open??t.level??t.entry??t.price??t.mid;
    const sz=t.size??t.dealSize??t.lot;
    const tm=esc(t.epic||'—');
    const val=(px!=null&&sz!=null&&Number.isFinite(Number(px))&&Number.isFinite(Number(sz)))
      ?num(Number(px)*Number(sz),0):'—';
    return `<div class="psx-trade-row">
      <span title="epic">${tm}</span>
      <span class="${cls}">${esc(side==='LONG'?'BUY':(side==='SHORT'?'SELL':side))}</span>
      <span>${esc(px==null?'—':num(px,2))}</span>
      <span>${esc(sz==null?'—':num(sz,2))}</span>
      <span>${esc(val)}</span>
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
  const clients=data.clients||[];
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
  renderDeskClosed(focus);
  renderDeskEngine(focus);
  renderDeskCrowd(focus);
  renderDeskIntel(focus);
  renderDeskPulse(focus);
  renderDeskFear(focus);
  renderDeskIndCharts(focus);
  let markets=[];
  try{
    const board=await api('/api/admin/markets-ohlc?n=90');
    markets=board.markets||[];
  }catch(_){markets=[]}
  renderDeskTape(markets,focus);
  renderDeskWatch(markets,focus);
  renderDeskSignal(focus,markets);
  let view=null;
  if(focus){
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
  renderDeskQuote(view,focus);
  renderDeskLevels(view,focus);
  renderDeskIndicators(focus,view);
  renderDeskCheck(focus,view);
  renderDeskBook(focus,view);
  drawDeskChart(view);
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
  // Empty → real empty (never invent a fake equity curve).
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
    const p=t.pnl;
    const tCcy=t.pnl_currency||ccy;
    const pending=t.pending_cash||t.pnl_source==='pending_broker_pnl'||t.pnl_source==='pending_close_sync';
    const showPnl=p!=null?p:(pending&&t.upl_hint!=null?t.upl_hint:null);
    const cls=pnlClass(showPnl);
    // Operator: peļņa is peļņa — no [UPL] / [gaida £] badges when we have a number.
    const src=(showPnl!=null)?'':
      (pending?' [gaida £]':
      (t.pnl_source&&t.pnl_source!=='broker'&&t.pnl_source!=='upl_estimate'
        ?` [${t.pnl_source}]`:''));
    let right;
    if(showPnl!=null){right=money(showPnl,tCcy);}
    else if(pending){right='gaida Capital £';}
    else{right='—';}
    return `<div><span>${esc(t.direction||'?')} ${esc(t.epic||'')}${esc(src)}</span><span class="${cls}">${esc(right)}</span></div>`;
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
        sb.pnl==null?'—':esc(money(sb.pnl,sb.ccy||ccy))
      }</strong></div>
      <div><span>W / L · avots</span><strong>${esc(`${sb.wins} / ${sb.losses}`)} · ${esc(sourceLabel(sb.src))}</strong></div>
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
  lastDeskChartKey='';
  if(deskStructure)drawDeskChart(deskStructure);
});
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
document.querySelectorAll('.psx-rail-btn[data-jump]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const v=btn.dataset.jump;
    if(v)setView(v);
  });
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
  pollTimer=null;currentPollMs=null;
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
