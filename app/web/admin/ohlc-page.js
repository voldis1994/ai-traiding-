'use strict';

const params=new URLSearchParams(location.search);
const clientId=Number(params.get('client')||params.get('id')||0);
const titleEl=document.getElementById('title');
const subEl=document.getElementById('subtitle');
const legendEl=document.getElementById('legend');
const errEl=document.getElementById('error');
const canvas=document.getElementById('chart');
let pollTimer=null;
let lastView=null;

async function api(path){
  const res=await fetch(path,{credentials:'same-origin'});
  let body={};try{body=await res.json()}catch(_){}
  if(res.status===401){
    location.href='/admin';
    throw Error('Nav admin sesijas');
  }
  if(!res.ok)throw Error(typeof body.detail==='string'?body.detail:`HTTP ${res.status}`);
  return body;
}

function paint(){
  if(lastView&&typeof drawOhlcStructure==='function')drawOhlcStructure(canvas,lastView);
}

async function load(){
  if(!clientId){
    errEl.hidden=false;
    errEl.textContent='Trūkst klienta ID (?client=…).';
    return;
  }
  try{
    await api('/api/admin/me');
    const view=await api(`/api/admin/clients/${clientId}/structure-view?n=180`);
    lastView=view||{};
    const name=view.client_name||view.display_name||view.username||`#${clientId}`;
    const epic=view.epic||'—';
    document.title=`OHLC · ${name} · ${epic}`;
    titleEl.textContent=`${name}`;
    const st=view.structure||{};
    subEl.textContent=[
      `#${clientId}`,
      epic,
      st.bias?`bias ${st.bias}`:'',
      st.event||'',
      view.preferred?`zona ${view.preferred}`:'abas entry zonas',
      view.mid!=null?`mid ${Number(view.mid).toFixed(2)}`:'',
    ].filter(Boolean).join(' · ');
    const bits=[];
    if(view.zones&&view.zones.buy_entry)bits.push('zaļa = BUY entry zona');
    if(view.zones&&view.zones.sell_entry)bits.push('sarkana = SELL entry zona');
    bits.push('dzeltena = mid');
    if(view.swing_brackets&&view.swing_brackets.ready)bits.push('punktētas = swing SL/TP');
    if((view.open_levels||[]).length)bits.push('baltas = atvērtā darījuma līmeņi');
    legendEl.textContent=bits.join(' · ');
    errEl.hidden=true;
    paint();
  }catch(err){
    errEl.hidden=false;
    errEl.textContent=String(err.message||err);
  }
}

document.getElementById('btnRefresh').onclick=()=>load().catch(()=>{});
document.getElementById('btnClose').onclick=()=>{
  if(window.opener)window.close();
  else location.href='/admin';
};
window.addEventListener('resize',()=>paint());
load().catch(()=>{});
pollTimer=setInterval(()=>load().catch(()=>{}),8000);
window.addEventListener('beforeunload',()=>{if(pollTimer)clearInterval(pollTimer);});
