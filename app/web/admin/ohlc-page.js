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
let pollTimer=null;
let lastView=null;
let candleN=60;
let lastOkAt=0;
let inFlight=false;

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

function paint(){
  if(!lastView||typeof drawOhlcStructure!=='function')return;
  drawOhlcStructure(canvas,lastView,{labels:true,maxCandles:candleN});
}

function setLive(ok,detail){
  liveBadge.classList.toggle('stale',!ok);
  liveText.textContent=detail||(ok?'LIVE':'…');
}

function renderChips(view){
  const bits=[];
  if(view.zones&&view.zones.buy_entry)bits.push(['buy','BUY entry zona']);
  if(view.zones&&view.zones.sell_entry)bits.push(['sell','SELL entry zona']);
  bits.push(['mid','mid']);
  if(view.swing_brackets&&view.swing_brackets.ready){
    bits.push(['sl','swing SL']);
    bits.push(['tp','swing TP']);
  }
  if((view.open_levels||[]).length)bits.push(['entry','atvērtais entry/SL/TP']);
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
    lastOkAt=Date.now();
    const name=view.client_name||view.display_name||view.username
      ||params.get('name')||`#${clientId}`;
    const epic=view.epic||'—';
    document.title=`OHLC · ${name} · ${epic}`;
    titleEl.textContent=`${name}`;
    const st=view.structure||{};
    const lastCandle=(view.candles||[])[(view.candles||[]).length-1];
    const candleAt=lastCandle&&lastCandle.at?clock(lastCandle.at):'';
    subEl.textContent=[
      `#${clientId}`,
      epic,
      st.bias?`bias ${st.bias}`:'',
      st.event||'',
      view.preferred?`zona ${view.preferred}`:'abas entry zonas',
      view.mid!=null?`mid ${Number(view.mid).toFixed(2)}`:'',
      candleAt?`pēdējā svece ${candleAt}`:'',
      `${candleN} M1`,
    ].filter(Boolean).join(' · ');
    renderChips(view);
    errEl.hidden=true;
    paint();
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
  pollTimer=setInterval(tick,2000);
}

document.getElementById('btnRefresh').onclick=()=>load().catch(()=>{});
document.getElementById('btnClose').onclick=()=>{
  if(window.opener)window.close();
  else location.href='/admin';
};
document.getElementById('btnFullscreen').onclick=async()=>{
  const root=document.documentElement;
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
  requestAnimationFrame(()=>paint());
};

document.querySelectorAll('.ohlc-range button').forEach(btn=>{
  btn.onclick=()=>{
    document.querySelectorAll('.ohlc-range button').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    candleN=Number(btn.dataset.n)||60;
    paint();
    load().catch(()=>{});
  };
});

if(typeof ResizeObserver!=='undefined'){
  const ro=new ResizeObserver(()=>requestAnimationFrame(()=>paint()));
  ro.observe(chartWrap);
}
window.addEventListener('resize',()=>requestAnimationFrame(()=>paint()));
document.addEventListener('fullscreenchange',()=>requestAnimationFrame(()=>paint()));
document.addEventListener('webkitfullscreenchange',()=>requestAnimationFrame(()=>paint()));
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden)load().catch(()=>{});
});

load().catch(()=>{});
schedulePoll();
window.addEventListener('beforeunload',()=>{if(pollTimer)clearInterval(pollTimer);});
