'use strict';

const params=new URLSearchParams(location.search);
const clientId=Number(params.get('client')||params.get('id')||0);
const titleEl=document.getElementById('title');
const subEl=document.getElementById('subtitle');
const chipsEl=document.getElementById('chips');
const errEl=document.getElementById('error');
const canvas=document.getElementById('chart');
const chartWrap=document.getElementById('chartWrap');
const liveBadge=document.getElementById('liveBadge');
const liveText=document.getElementById('liveText');
const pageRoot=document.querySelector('.ohlc-page')||document.documentElement;
let pollTimer=null;
let lastView=null;
let candleN=60;
let lastOkAt=0;
let inFlight=false;
let lastPaintKey='';

function viewPaintKey(view,n){
  if(!view)return '';
  const candles=view.candles||[];
  const last=candles[candles.length-1]||{};
  const ol=view.open_levels||[];
  return [
    n,
    view.mid,view.preferred,view.epic,
    candles.length,last.at,last.close,
    (view.mtf||{}).mode,(view.mtf||{}).adx,
    ol.map(p=>[p.entry,p.stop,p.profit].join(':')).join('|'),
  ].join('·');
}

async function api(path){
  const res=await fetch(path,{credentials:'same-origin',cache:'no-store'});
  let body={};try{body=await res.json()}catch(_){}
  if(res.status===401){
    location.href='/admin';
    throw Error('Nav admin sesijas');
  }
  if(!res.ok)throw Error(typeof body.detail==='string'?body.detail:`HTTP ${res.status}`);
  return body;
}

function clock(ts){
  try{
    return new Date(ts||Date.now()).toLocaleTimeString('lv-LV',{
      hour:'2-digit',minute:'2-digit',second:'2-digit'});
  }catch(_){return ''}
}

/** Ensure canvas CSS box matches wrap — % height often collapses to 0. */
function sizeCanvas(){
  if(!canvas||!chartWrap)return;
  const r=chartWrap.getBoundingClientRect();
  const cssW=Math.max(40,Math.floor(r.width)||canvas.clientWidth||640);
  const cssH=Math.max(160,Math.floor(r.height)||canvas.clientHeight||360);
  canvas.style.width=cssW+'px';
  canvas.style.height=cssH+'px';
}

function paint(force){
  if(!lastView)return;
  if(typeof drawOhlcStructure!=='function'){
    if(errEl){
      errEl.hidden=false;
      errEl.textContent='OHLC chart skripts neielādējās — hard refresh (Ctrl+F5).';
    }
    return;
  }
  const key=viewPaintKey(lastView,candleN);
  if(!force&&key&&key===lastPaintKey){
    // Still refresh the LIVE clock; skip full canvas redraw (stops flicker).
    return;
  }
  lastPaintKey=key;
  sizeCanvas();
  drawOhlcStructure(canvas,lastView,{labels:true,maxCandles:candleN});
}

function setLive(ok,detail){
  if(!liveBadge||!liveText)return;
  liveBadge.classList.toggle('stale',!ok);
  liveText.textContent=detail||(ok?'LIVE':'…');
}

function renderChips(view){
  if(!chipsEl)return;
  const bits=[];
  if(view.zones&&view.zones.buy_entry)bits.push(['buy','BUY entry zona']);
  if(view.zones&&view.zones.sell_entry)bits.push(['sell','SELL entry zona']);
  bits.push(['mid','mid']);
  if(view.overlays&&(view.overlays.kama||[]).length)bits.push(['kama','KAMA / EMA']);
  const inTrade=(view.open_levels||[]).length>0;
  if(!inTrade&&view.swing_brackets&&view.swing_brackets.ready){
    bits.push(['sl','swing SL']);
    bits.push(['tp','swing TP']);
  }
  if(inTrade)bits.push(['entry','atvērtais entry/SL/TP']);
  chipsEl.innerHTML=bits.map(([cls,label])=>
    `<span class="ohlc-chip ${cls}"><i></i>${label}</span>`).join('');
}

async function load(){
  if(!clientId){
    errEl.hidden=false;
    errEl.textContent='Trūkst klienta ID (?client=…).';
    setLive(false,'nav klienta');
    return;
  }
  if(inFlight)return;
  inFlight=true;
  try{
    await api('/api/admin/me');
    const bust=Date.now();
    // Fetch a bit more than display window so range toggles stay snappy.
    const fetchN=Math.max(candleN+20,120);
    const view=await api(
      `/api/admin/clients/${clientId}/structure-view?n=${fetchN}&_=${bust}`);
    lastView=view||{};
    // API may return 200 with ok:false (no epic / no candles).
    if(view&&view.ok===false){
      const name=view.client_name||view.display_name||view.username
        ||params.get('name')||`#${clientId}`;
      titleEl.textContent=name;
      subEl.textContent=[`#${clientId}`,view.reason||'nav OHLC'].filter(Boolean).join(' · ');
      renderChips(view);
      errEl.hidden=false;
      errEl.textContent=view.hint||view.reason||'OHLC nav pieejams šim klientam';
      paint();
      setLive(false,view.reason||'nav datu');
      return;
    }
    lastOkAt=Date.now();
    const name=view.client_name||view.display_name||view.username
      ||params.get('name')||`#${clientId}`;
    const epic=view.epic||'—';
    document.title=`OHLC · ${name} · ${epic}`;
    titleEl.textContent=`${name}`;
    const st=view.structure||{};
    const lastCandle=(view.candles||[])[(view.candles||[]).length-1];
    const candleAt=lastCandle&&lastCandle.at?clock(lastCandle.at):'';
    const mtf=view.mtf||{};
    const path=view.entry_path||{};
    // Structure vs decision are separate — do not mash as one claim.
    const structBits=[
      st.bias?`bias ${st.bias}`:'',
      st.event||'',
    ].filter(Boolean).join(' ');
    const decisionBits=[
      view.shared_side?`shared ${view.shared_side}`:(view.preferred?`zona ${view.preferred}`:'zonas abas'),
      mtf.mode?`MTF ${mtf.mode}`:'',
      path.intent?`intent ${path.intent}`:'',
      path.trigger?`trigger ${path.trigger}`:(path.wait?`wait ${String(path.wait).slice(0,40)}`:''),
    ].filter(Boolean).join(' · ');
    subEl.textContent=[
      `#${clientId}`,
      epic,
      structBits?`struktūra: ${structBits}`:'',
      decisionBits?`lēmums: ${decisionBits}`:'',
      view.mid!=null?`mid ${Number(view.mid).toFixed(2)}`:'',
      candleAt?`pēdējā svece ${candleAt}`:'',
      `${candleN} M1`,
    ].filter(Boolean).join(' · ');
    renderChips(view);
    errEl.hidden=true;
    paint(false);
    setLive(true,`LIVE · ${clock(lastOkAt)}`);
  }catch(err){
    errEl.hidden=false;
    errEl.textContent=String(err.message||err);
    setLive(false,'offline');
  }finally{
    inFlight=false;
  }
}

function schedulePoll(){
  if(pollTimer)clearInterval(pollTimer);
  const tick=()=>{
    if(document.hidden)return;
    load().catch(()=>{});
  };
  // Popup only (~2s) — no main-board hammer; paint() skips when unchanged.
  pollTimer=setInterval(tick,2000);
}

document.getElementById('btnRefresh').onclick=()=>load().catch(()=>{});
document.getElementById('btnClose').onclick=()=>{
  if(window.opener)window.close();
  else location.href='/admin';
};
document.getElementById('btnFullscreen').onclick=async()=>{
  // Fullscreen the page root so .ohlc-page:fullscreen CSS applies.
  const root=pageRoot;
  try{
    if(!document.fullscreenElement&&!document.webkitFullscreenElement){
      if(root.requestFullscreen)await root.requestFullscreen();
      else if(root.webkitRequestFullscreen)root.webkitRequestFullscreen();
    }else if(document.exitFullscreen){
      await document.exitFullscreen();
    }else if(document.webkitExitFullscreen){
      document.webkitExitFullscreen();
    }
  }catch(_){}
  requestAnimationFrame(()=>paint(true));
};

document.querySelectorAll('.ohlc-range button').forEach(btn=>{
  btn.onclick=()=>{
    document.querySelectorAll('.ohlc-range button').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    candleN=Number(btn.dataset.n)||60;
    paint(true);
    load().catch(()=>{});
  };
});

if(typeof ResizeObserver!=='undefined'&&chartWrap){
  const ro=new ResizeObserver(()=>requestAnimationFrame(()=>paint(true)));
  ro.observe(chartWrap);
}
window.addEventListener('resize',()=>requestAnimationFrame(()=>paint(true)));
document.addEventListener('fullscreenchange',()=>requestAnimationFrame(()=>paint(true)));
document.addEventListener('webkitfullscreenchange',()=>requestAnimationFrame(()=>paint(true)));
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden)load().catch(()=>{});
});

load().catch(()=>{});
schedulePoll();
window.addEventListener('beforeunload',()=>{if(pollTimer)clearInterval(pollTimer);});
