/* Offline-capable browser control center. All data originates from local FastAPI,
   which in turn uses the authenticated Capital.com connection. */
'use strict';
const $ = id => document.getElementById(id);
const escapeHtml = input => String(input ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let liveSocket, refreshTimer, current = null, visible = 'dashboard', stopped = false;
const money = (x, currency='') => (x == null || !Number.isFinite(Number(x)) ? '—' : Number(x).toLocaleString('en-GB',{minimumFractionDigits:2,maximumFractionDigits:2}) + (currency ? ' '+currency : ''));
const small = x => (x == null || !Number.isFinite(Number(x)) ? '—' : Number(x).toLocaleString('en-GB',{maximumFractionDigits:6}));
async function api(path, method='GET', payload) {
  const opts={method,credentials:'same-origin',headers:{'Content-Type':'application/json'}};
  if(payload!==undefined)opts.body=JSON.stringify(payload);
  const result=await fetch(path,opts);let body={};try{body=await result.json()}catch(_){}
  if(!result.ok)throw Error(body.detail || `HTTP ${result.status}`);
  return body;
}
function setText(id,value){const el=$(id);if(el)el.textContent=String(value??'—')}
function setHtml(id,value){const el=$(id);if(el)el.innerHTML=value}
function displayDashboard(){ $('login').classList.add('hidden');$('dashboard').classList.remove('hidden'); }
function displayLogin(){ $('dashboard').classList.add('hidden');$('login').classList.remove('hidden');if(liveSocket)liveSocket.close();clearInterval(refreshTimer); }
$('loginForm').addEventListener('submit',async event=>{
  event.preventDefault();const btn=$('loginButton');btn.disabled=true;setText('loginError','Connecting to Capital.com LIVE...');
  const data=Object.fromEntries(new FormData(event.target).entries());
  try{await api('/api/connect','POST',data);event.target.reset();displayDashboard();await updateStatus();startStream();}
  catch(e){setText('loginError',e.message)}finally{btn.disabled=false}
});
async function updateStatus(){try{const value=await api('/api/status');render(value);return true}catch(e){if(e.message.includes('Login required'))displayLogin();return false}}
function startStream(){clearInterval(refreshTimer);const proto=location.protocol==='https:'?'wss':'ws';
  liveSocket=new WebSocket(`${proto}://${location.host}/ws/live`);
  liveSocket.onmessage=e=>{try{render(JSON.parse(e.data))}catch(err){console.warn(err)}};
  liveSocket.onclose=()=>{if(!$('dashboard').classList.contains('hidden')){setText('connection','◌ RETRYING CONNECTION');refreshTimer=setInterval(updateStatus,3000);setTimeout(()=>{if(!$('dashboard').classList.contains('hidden'))startStream()},5000)}};
  liveSocket.onerror=()=>{try{liveSocket.close()}catch(_){}};
}
function showTab(name){visible=name;document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x.dataset.tab===name));document.querySelectorAll('[data-section]').forEach(x=>x.classList.toggle('active',x.dataset.section===name));if(current)render(current)}
document.querySelectorAll('[data-tab]').forEach(tab=>tab.addEventListener('click',()=>showTab(tab.dataset.tab)));
document.querySelectorAll('[data-jump]').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.jump)));
async function control(path,message,confirmAction=false){if(confirmAction&&!confirm(message))return;try{await api(path,'POST');await updateStatus()}catch(e){alert(e.message)}}
$('pause').onclick=()=>control('/api/pause','');
$('resume').onclick=()=>control('/api/resume','');
$('newMarket').onclick=async()=>{
 if(!confirm('Switch instrument? You must first close every open broker position. The next FIRST trade must be opened manually in Capital.com. Training history is preserved.'))return;
 try{await api('/api/prepare-new-market','POST',{});await updateStatus();alert('Ready. Open the first trade manually in your newly selected market.');}
 catch(e){alert('Market switch denied: '+e.message)}
};
$('emergency').onclick=()=>control('/api/emergency','Aizvērt esošo pozīciju un apturēt automātisko tirdzniecību?',true);
$('logout').onclick=async()=>{try{await api('/api/logout','POST')}finally{displayLogin()}};
function neuronDots(arr){return (arr||[]).map(n=>`<i title="Activation ${escapeHtml(n)}" class="neuron ${n>.02?'pos':n<-.02?'neg':''}"></i>`).join('')}
function entries(arr,count){return (arr||[]).slice(0,count).map(e=>`<div class="event"><span style="color:${e.kind==='error'?'#fa6690':e.kind==='code'?'#e889ff':'#8ed8fa'}">${escapeHtml(e.kind.toUpperCase())}</span> · ${escapeHtml(e.message)}<time>${escapeHtml(e.time)}</time></div>`).join('') || '<div class="muted">No events yet</div>'}
function table(columns,rows){return `<table><thead><tr>${columns.map(c=>`<th>${escapeHtml(c[0])}</th>`).join('')}</tr></thead><tbody>${(rows||[]).map(row=>`<tr>${columns.map(c=>`<td>${escapeHtml(typeof c[1]==='function'?c[1](row):row[c[1]])}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${columns.length}">No records yet</td></tr>`}</tbody></table>`}
function drawCandles(canvasId,candles){
  const canvas=$(canvasId);if(!canvas)return;const dpr=Math.min(2,window.devicePixelRatio||1), rect=canvas.getBoundingClientRect();if(rect.width<10)return;
  const w=rect.width,h=rect.height;canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
  ctx.strokeStyle='#233257';ctx.lineWidth=1;for(let k=0;k<=5;k++){let y=18+(h-45)*k/5;ctx.beginPath();ctx.moveTo(14,y);ctx.lineTo(w-72,y);ctx.stroke()}
  const clean=(candles||[]).filter(c=>['open','high','low','close'].every(k=>Number.isFinite(Number(c[k])))).slice(-80);
  if(!clean.length){ctx.fillStyle='#8495bb';ctx.font='14px Segoe UI';ctx.textAlign='center';ctx.fillText('Awaiting real Capital.com candle data',w/2,h/2);return}
  let low=Math.min(...clean.map(c=>Number(c.low))),high=Math.max(...clean.map(c=>Number(c.high)));const pad=(high-low||Math.abs(high)*.001||.01)*.18;low-=pad;high+=pad;
  const y=p=>20+(high-p)/(high-low)*(h-54);const chartW=w-92;const spacing=chartW/clean.length;
  clean.forEach((c,i)=>{let x=15+(i+.5)*spacing;const rising=+c.close>=+c.open;ctx.strokeStyle=rising?'#28dbb1':'#fe6294';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.moveTo(x,y(+c.high));ctx.lineTo(x,y(+c.low));ctx.stroke();let y1=y(+c.open),y2=y(+c.close);ctx.fillRect(x-Math.max(1,spacing*.34),Math.min(y1,y2),Math.max(2,spacing*.68),Math.max(1,Math.abs(y1-y2))) });
  ctx.fillStyle='#8a9cc5';ctx.font='10px Segoe UI';ctx.textAlign='left';for(let k=0;k<=5;k++){const price=high-(high-low)*k/5;ctx.fillText(price.toFixed(Math.abs(high)<5?5:2),w-73,23+(h-54)*k/5)}
  ctx.fillStyle='#7190b6';ctx.fillText(String(clean[0].at||'').slice(0,16),15,h-10);ctx.textAlign='right';ctx.fillText(String(clean[clean.length-1].at||'').slice(0,16),w-70,h-10);
}
function render(data){
  if(!data?.state?.connected)return;current=data;
  const state=data.state;const signal=state.signal||{};const scores=signal.scores||{BUY:0,SELL:0,HOLD:0};const position=state.positions?.[0];const quote=state.market||{};const neural=state.neural||{};const mandate=state.mandate;
  const bal=state.account?.balance||{},curr=state.account?.currency||'';
  setText('balance',money(bal.balance,curr));setText('equity',money(bal.available,curr));setText('pnl',money(position?.upl,curr));
  setText('pnlSub',position?'Open position':'No open position');
  setText('confidence',signal.confidence!=null?(signal.confidence*100).toFixed(1)+'%':'—');setText('signalBrief',signal.action?signal.action+' · uncalibrated score':'Waiting for prices');
  setText('connection',state.error?'◌ MARKET CONNECTION ISSUE':'● CAPITAL.COM CONNECTED');
  setText('accountId',state.account?.accountId||'—');setText('execute',state.execution_enabled?'AUTOMATIC LIVE':'PAUSED');setText('robotState',state.armed?'RUNNING':'PAUSED');setText('ticks',state.tick_count);
  setText('epic',mandate?.epic||(position?'ADOPTING OPEN POSITION…':'WAITING FOR MOBILE TRADE'));
  setText('size',mandate?'ADOPTED SIZE: '+mandate.size:(position?'Broker position seen — waiting for epic/size adoption':'Instrument and lot size will be adopted automatically'));
  setText('marketStatus',quote.status||'—');
  setText('mid',small(quote.mid));setText('bidask',`BID ${small(quote.bid)}  /  ASK ${small(quote.ask)}`);setText('chartEpic',quote.epic||'—');
  const action=signal.action||'HOLD';setText('decision',action);$('decision').className='signal-value '+action.toLowerCase();
  setHtml('scores',Object.entries(scores).map(([key,value])=>`<div class="scorebar"><header><span>${key}</span><span>${(value*100).toFixed(1)}%</span></header><div class="track"><span style="width:${Math.max(0,Math.min(100,value*100))}%"></span></div></div>`).join(''));
  setText('regime',state.market_regime||'—');setText('accuracy',state.labeled_samples?`${(state.accuracy_proxy*100).toFixed(1)}% / ${state.labeled_samples} labels`:'—');
  setText('neurons',neural.neurons||'—');setText('births',`${neural.births??0} / ${neural.pruned??0}`);setText('loss',neural.loss==null?'—':neural.loss.toFixed(5));
  setHtml('neuronGrid',neuronDots(neural.activations));setHtml('neuronGridLarge',neuronDots(neural.activations));
  setText('generation',state.code_generation||0);setText('strategy',`Evolved policy research ${state.code_generation||0} · ${data.code?.strategy||'edge'} · LIVE uses neural vote`);
  setText('positionBadge',position?'● OPEN':'○ CLOSED');
  const protect=state.adaptive_protection||{};
  setText('protectionStatus',protect.status||'Waiting for robot-opened position');
  setText('intelProtection',`Adaptive SL/TP: ${protect.status||'Waiting'} · Historical samples: ${protect.evidence?.samples??protect.candidate_samples??'—'} · Changes are shown only after broker verification`);
  setHtml('position',position?`<b>${escapeHtml(position.epic)} ${escapeHtml(position.direction)}</b><div class="kv"><span>Size</span><b>${escapeHtml(position.size)}</b></div><div class="kv"><span>Entry</span><b>${small(position.level)}</b></div><div class="kv"><span>Stop-loss</span><b>${small(position.stopLevel)}</b></div><div class="kv"><span>Take-profit</span><b>${small(position.profitLevel)}</b></div><div class="kv"><span>Open P&L</span><b>${money(position.upl,curr)}</b></div><div class="muted">Deal ID: ${escapeHtml(position.dealId)}</div>`:'<span class="muted">No open position. AI evaluates on the NEXT completed M1 candle after the first manual deal closes.</span>');
  setText('bid',small(quote.bid));setText('ask',small(quote.ask));setText('spread',small(quote.spread));setText('quoteTime',quote.time||'—');
  const context=state.market_context||{};
  setText('contextReady',context.ready?'READY · '+(context.bar_time||''):'WAIT · '+(context.reason||'not loaded'));
  setText('contextReturns',context.ready?[1,5,15,60].map(m=>`${m}m: ${context.returns_bps?.[m]??'—'}bp`).join(' · '):'Waiting for complete candles');
  setHtml('events',entries(data.events,10));setHtml('recentTrades',entries((data.events||[]).filter(e=>e.kind==='trade'||e.kind==='order'),7));
  setHtml('tradeDetails',table([['Deal ID','dealId'],['Epic','epic'],['Direction','direction'],['Size','size'],['Stop','stopLevel'],['Profit','profitLevel'],['Unrealized P&L','upl']],state.positions||[]));
  setHtml('pendingDetails',state.unresolved_orders?.length?table([['ID','id'],['Kind','kind'],['Status','status'],['Reference','reference'],['Time','time']],state.unresolved_orders):'<div class="muted">No unresolved broker operations.</div>');
  setHtml('neuralDetails',`<div class="metric-grid"><div class="metric"><span>ACTIVE NEURONS</span><strong>${neural.neurons??0}</strong></div><div class="metric"><span>TRAINING STEPS</span><strong>${neural.steps??0}</strong></div><div class="metric"><span>RECONSTRUCTION ERROR EMA</span><strong>${small(neural.smoothed_error)}</strong></div><div class="metric"><span>GRADIENT NORM</span><strong>${small(neural.gradient_norm)}</strong></div><div class="metric"><span>NEURAL INPUTS</span><strong>${neural.input_dimension??'—'}</strong></div><div class="metric"><span>EXTERNAL NEURAL LEARNING</span><strong>${neural.external_features_learned?(neural.external_feed_ready?'READY':'WAITING DATA'):'NOT YET INITIALIZED'}</strong></div></div><div class="muted">${neural.external_features_learned?'Neural fusion enabled. '+String(neural.external_trained_labels??0)+' delayed external-feature supervised updates.':'Waiting for aligned Capital.com reference markets AND verified news; legacy OHLC model still runs.'}</div>`);
  setHtml('evoDetails',`<div class="kv"><span>Generation</span><b>${state.code_generation||0}</b></div><div class="kv"><span>Shadow-evolved code variant (not LIVE gate)</span><b>${escapeHtml(data.code?.strategy||'—')}</b></div><div class="kv"><span>Evolution attempts</span><b>${data.code?.attempts??0}</b></div><div class="kv"><span>Next evaluation</span><b>${Math.ceil(Math.max(20,state.labeled_samples+1)/20)*20} labeled samples</b></div><div class="kv"><span>Last evolution result</span><b>${escapeHtml(data.code?.last_reason||'—')}</b></div><div class="kv"><span>SHA-256</span><b>${escapeHtml(data.code?.sha256?.slice(0,24)||'—')}…</b></div>`);
  setText('codeSource',data.code?.source||'—');setHtml('codeEvents',entries((data.events||[]).filter(e=>e.kind==='code'),40));
  setHtml('signalHistory',table([['Time','time'],['Epic','epic'],['Action','action'],['Price','price'],['Step','model_step'],['Next-label','label']],data.signals||[]));
  setHtml('fullTrades',table([['Deal','deal_id'],['Epic','epic'],['Side','direction'],['Size','size'],['Entry','entry'],['Exit','exit'],['P&L','pnl'],['Opened','opened'],['Closed','closed']],data.trades||[]));
  setHtml('fullEvents',entries(data.events,80));setText('footerState',state.error?state.error:'CAPITAL.COM LIVE · NEURAL ENGINE');
  if(visible==='dashboard')drawCandles('candleChart',data.candles);
  if(visible==='analysis'){renderIntelligence(data);drawCandles('analysisChart',data.candles);drawSignalMarkers(data.candles,data.signals||[])}
}
window.addEventListener('resize',()=>{if(current){drawCandles('candleChart',current.candles);drawCandles('analysisChart',current.candles)}});
setInterval(()=>setText('clock',new Date().toLocaleTimeString('lv-LV')),1000);
updateStatus().then(ok=>{if(ok) {displayDashboard();startStream()}});

// Market Intelligence is read-only: it never submits orders or modifies broker protection.
function renderIntelligence(data){
 const st=data.state||{},sg=st.signal||{},pos=st.positions?.[0],market=st.market||{},candles=(data.candles||[]).filter(c=>Number.isFinite(Number(c.close))),signals=data.signals||[];
 const act=['BUY','SELL','HOLD'].includes(sg.action)?sg.action:'HOLD';const score=Number(sg.confidence);
 setText('intelAction',act);$('intelAction').className='intel-action '+act.toLowerCase();setText('intelConfidence',Number.isFinite(score)?`${(score*100).toFixed(1)}% model score · uncalibrated`:'No model confidence');
 setText('intelBroker',pos?`${pos.direction} ${pos.size}`:'NO POSITION');setText('intelBrokerState',`${pos?.epic||st.mandate?.epic||'Waiting for instrument'} · ${st.execution_enabled?'AUTO LIVE':'PAUSED'}`);
 setText('intelGeneration',st.code_generation??0);setText('intelSamples',`${st.labeled_samples??0} labeled samples · ${st.neural?.steps??0} training steps`);setText('intelEpic',market.epic||st.mandate?.epic||'—');
 const shadow=signals.find(x=>['BUY','SELL','HOLD'].includes(x.action));setText('intelShadow',shadow?shadow.action:'AWAITING');setText('intelShadowDetail',shadow?`Latest logged model signal · ${shadow.time||'—'}`:'No logged model signals');
 const format=x=>x==null?'—':small(x);const valid=Number.isFinite(Number(market.mid));
 const news=st.economic_news_risk||{},cross=st.intermarket_analysis||{};
 const peerLines=(cross.peers||[]).map(p=>'<div class="kv"><span>'+escapeHtml(p.name||p.epic||'Peer')+'</span><b>'+escapeHtml(p.ready?(p.stable?'STABLE ':'OBSERVED ')+String(p.correlation??'—'):'NO DATA')+'</b></div>').join('');
 const fusionMode=st.neural?.external_features_learned?(st.neural?.external_feed_ready?'18 INPUTS · LEARNING':'18 INPUTS · FEED WAIT'):'10 INPUTS · AWAITING UPGRADE';
 const externalRows='<div class="kv"><span>Neural intermarket + news training</span><b>'+escapeHtml(fusionMode)+'</b></div><div class="kv"><span>Forex Factory news guard</span><b>'+escapeHtml(news.status||'WAITING')+'</b></div><div class="muted">'+escapeHtml(news.reason||'Waiting for economic calendar')+'</div><div class="kv"><span>Intermarket analysis</span><b>'+escapeHtml(cross.status||'WAITING')+'</b></div>'+peerLines+'<div class="muted">'+escapeHtml(cross.reason||'Waiting for broker peer prices')+'</div>';
 const ctx=st.market_context||{};const contextRows=ctx.ready?'<div class="kv"><span>Completed bar (UTC)</span><b>'+escapeHtml(ctx.bar_time||'—')+'</b></div>'+[1,5,15,60].map(m=>'<div class="kv"><span>'+m+' minute return</span><b>'+escapeHtml(String(ctx.returns_bps?.[m]??'—'))+' bp</b></div>').join('')+'<div class="kv"><span>60-minute regime</span><b>'+escapeHtml(ctx.trend||'—')+'</b></div>':'<div class="muted">OHLC WAIT: '+escapeHtml(ctx.reason||'Not loaded')+'</div>'; 
 setHtml('intelBrief',`${contextRows}${externalRows}<div class="kv"><span>Decision</span><b>${escapeHtml(act)}</b></div><div class="kv"><span>Market regime</span><b>${escapeHtml(st.market_regime||'—')}</b></div><div class="kv"><span>Bid / Ask</span><b>${format(market.bid)} / ${format(market.ask)}</b></div><div class="kv"><span>Spread</span><b>${format(market.spread)}</b></div><div class="kv"><span>Data status</span><b>${escapeHtml(market.status||'—')}</b></div><div class="kv"><span>Model confidence</span><b>${Number.isFinite(score)?(score*100).toFixed(1)+'%':'—'}</b></div><div class="kv"><span>Neural decision time (UTC)</span><b>${escapeHtml(st.neural_decision_bar||'Waiting for completed M1 candle')}</b></div><div class="kv"><span>Decision cadence</span><b>ONE per completed M1 candle · M5/M15/H1 context</b></div><div class="kv"><span>Entry confidence / margin threshold</span><b>NONE — model class winner decides</b></div><div class="kv"><span>Observed directional lead</span><b>${Number.isFinite(st.directional_advantage)?(st.directional_advantage*100).toFixed(1)+' pp':'—'}</b></div><div class="kv"><span>Strategy proposal</span><b>${escapeHtml(st.policy_proposal||'HOLD')}</b></div><div class="kv"><span>Neural / news decision</span><b>${escapeHtml(st.policy_decision||'HOLD')}</b></div><div class="kv"><span>Broker execution ready</span><b>${st.entry_eligible?'YES':'NO'}</b></div><div class="kv"><span>BUY/SELL signal validated</span><b>${st.entry_signal_validated?'YES':'NO'}</b></div><div class="kv"><span>Order submission permitted now</span><b>${st.entry_can_submit_now?'YES':'NO'}</b></div><div class="muted">Neural BUY / SELL / HOLD is evaluated on each confirmed M1 candle. No 60% confidence, 8.1 pp, extra directional holdout or correlation veto after the neural decision. Operational broker and high-impact news protections remain.</div><div class="muted">${escapeHtml(st.entry_block_reason||'—')} · ${escapeHtml(st.entry_policy_reason||'No current evidence')} · Raw neural BUY/SELL is not an executed trade.</div>`);
 setHtml('intelRisk',`<div class="kv"><span>Position</span><b>${pos?escapeHtml(pos.direction+' '+pos.size):'No open position'}</b></div><div class="kv"><span>Entry</span><b>${format(pos?.level)}</b></div><div class="kv"><span>Stop loss</span><b>${format(pos?.stopLevel)}</b></div><div class="kv"><span>Take profit</span><b>${format(pos?.profitLevel)}</b></div><div class="kv"><span>Unrealized P&L</span><b>${money(pos?.upl,st.account?.currency||'')}</b></div><div class="muted">Only broker-reported levels are shown. No speculative SL/TP is sent from this panel.</div>`);
 const highs=candles.slice(-40).map(c=>Number(c.high)).filter(Number.isFinite),lows=candles.slice(-40).map(c=>Number(c.low)).filter(Number.isFinite);
 setHtml('intelZones',highs.length&&lows.length?`<div class="kv"><span>Observed resistance (40 candles)</span><b>${small(Math.max(...highs))}</b></div><div class="kv"><span>Observed support (40 candles)</span><b>${small(Math.min(...lows))}</b></div><div class="kv"><span>Current midpoint</span><b>${valid?small(market.mid):'—'}</b></div><div class="muted">Observed extremes, not validated entry zones or guaranteed support/resistance.</div>`:'<div class="muted">Insufficient real candles for observed zones.</div>');
 setHtml('shadowLab',signals.length?signals.slice(0,12).map(x=>`<div class="event"><b>${escapeHtml(x.action)}</b> · ${escapeHtml(x.epic)} @ ${small(x.price)}<time>${escapeHtml(x.time)} · virtual observation only</time></div>`).join(''):'<div class="muted">No recorded decisions yet. Shadow simulation has not been activated.</div>');
 setHtml('intelEvents',entries(data.events,14));
}
function drawSignalMarkers(candles,signals){
 const canvas=$('analysisChart');if(!canvas||!signals?.length)return;
 const clean=(candles||[]).filter(c=>['open','high','low','close'].every(k=>Number.isFinite(Number(c[k])))).slice(-80);if(!clean.length)return;
 const ctx=canvas.getContext('2d'),w=canvas.getBoundingClientRect().width,h=canvas.getBoundingClientRect().height;
 let low=Math.min(...clean.map(c=>+c.low)),high=Math.max(...clean.map(c=>+c.high));const pad=(high-low||Math.abs(high)*.001||.01)*.18;low-=pad;high+=pad;
 const spacing=(w-92)/clean.length;let plotted=0;
 for(const sig of signals.slice(0,70).reverse()){
   const t=String(sig.time||'').slice(0,16);const idx=clean.findIndex(c=>String(c.at||'').slice(0,16)===t);if(idx<0)continue;
   const action=String(sig.action||'');if(!['BUY','SELL','HOLD'].includes(action))continue;
   const x=15+(idx+.5)*spacing,y=20+(high-Number(clean[idx].close))/(high-low)*(h-54);if(!Number.isFinite(y))continue;
   ctx.fillStyle=action==='BUY'?'#38efbd':action==='SELL'?'#ff699c':'#ffce74';ctx.beginPath();ctx.arc(x,Math.max(13,y-11),3.5,0,Math.PI*2);ctx.fill();plotted++;
 }
}
$('fullscreenBtn')?.addEventListener('click',()=>{if(!document.fullscreenElement)document.documentElement.requestFullscreen?.();else document.exitFullscreen?.()});
