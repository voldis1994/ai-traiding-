'use strict';
const $=id=>document.getElementById(id);
let lang=localStorage.getItem('nta_client_lang')||'lv';
let socket=null;let pollTimer=null;let currentClient=null;

function t(key){return (window.NTA_I18N[lang]||window.NTA_I18N.lv)[key]||key}
function applyI18n(){
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    const key=el.getAttribute('data-i18n');el.textContent=t(key);
  });
  document.documentElement.lang=lang;
}
async function api(path,method='GET',payload){
  const opts={method,credentials:'same-origin',headers:{'Content-Type':'application/json'}};
  if(payload!==undefined)opts.body=JSON.stringify(payload);
  const res=await fetch(path,opts);let body={};try{body=await res.json()}catch(_){}
  if(!res.ok)throw Error(typeof body.detail==='string'?body.detail:`HTTP ${res.status}`);
  return body;
}
function show(id){['login','shell'].forEach(x=>$(x).classList.toggle('hidden',x!==id))}
function pct(x){return Math.round(Math.max(0,Math.min(1,Number(x)||0))*100)}

function renderGuide(phase){
  $('guide').classList.remove('hidden');$('robot').classList.add('hidden');
  let msg=t('waiting');
  if(phase==='setup')msg=t('setup');
  if(phase==='starting')msg=t('starting');
  $('guideStatus').textContent=msg;
}
function renderRobot(view){
  $('guide').classList.add('hidden');$('robot').classList.remove('hidden');
  const scores=view.scores||{};
  const b=pct(scores.BUY),h=pct(scores.HOLD),s=pct(scores.SELL);
  $('scoreBuy').textContent=b+'%';$('scoreHold').textContent=h+'%';$('scoreSell').textContent=s+'%';
  $('barBuy').style.width=b+'%';$('barHold').style.width=h+'%';$('barSell').style.width=s+'%';
  const evo=view.evolution||{};
  const coef=Number(evo.generation||0)+Math.min(0.99,(Number(evo.labeled_samples||0)/100));
  $('evoCoef').textContent=coef.toFixed(2);
  $('evoGen').textContent=String(evo.generation??0);
  $('evoSamples').textContent=String(evo.labeled_samples??0);
  $('marketEpic').textContent=view.mandate?.epic||'—';
  $('marketSize').textContent=view.mandate?.size||'—';
  const pos=view.position;
  $('positionLine').textContent=pos?`${pos.direction||''} ${pos.size||''}`.trim():t('none');
  const paused=!!view.operator_paused || !view.execution_enabled;
  $('livePill').textContent=paused?t('paused'):t('live');
  $('runState').textContent=paused?t('paused'):t('live');
  const block=view.entry_block_reason||'';
  const canGo=!!view.entry_can_submit_now;
  $('entryBlock').textContent=canGo?t('entryReady'):(block||t('entryWaiting'));
  $('robotError').textContent=view.error||'';
}

function applyStatus(payload){
  if(!payload)return;
  if(payload.client){
    currentClient=payload.client;
    $('clientName').textContent=payload.client.display_name||payload.client.username||'—';
    if(payload.client.language && payload.client.language!==lang){
      lang=payload.client.language;localStorage.setItem('nta_client_lang',lang);
      $('langSelect').value=lang;applyI18n();
    }
  }
  const phase=payload.phase||(payload.view?.phase)||'guide';
  if(phase==='robot' && payload.view)renderRobot(payload.view);
  else renderGuide(phase);
}

function startLive(){
  stopLive();
  const proto=location.protocol==='https:'?'wss':'ws';
  socket=new WebSocket(`${proto}://${location.host}/ws/client`);
  socket.onmessage=e=>{try{applyStatus(JSON.parse(e.data))}catch(_){}};
  socket.onclose=()=>{pollTimer=setInterval(async()=>{
    try{applyStatus(await api('/api/client/status'))}catch(err){
      if(String(err.message).includes('login')||String(err.message).includes('401'))showLogin();
    }
  },3000)};
}
function stopLive(){if(socket){try{socket.close()}catch(_){}}socket=null;clearInterval(pollTimer)}

function showLogin(){stopLive();show('login');applyI18n()}
async function showApp(){
  show('shell');applyI18n();
  try{applyStatus(await api('/api/client/status'));startLive()}
  catch(e){showLogin()}
}

$('loginLang').value=lang;
$('langSelect').value=lang;
applyI18n();

$('loginLang').addEventListener('change',()=>{lang=$('loginLang').value;localStorage.setItem('nta_client_lang',lang);applyI18n()});
$('langSelect').addEventListener('change',async()=>{
  lang=$('langSelect').value;localStorage.setItem('nta_client_lang',lang);applyI18n();
  try{await api('/api/client/language','POST',{language:lang})}catch(_){}
});

$('loginForm').addEventListener('submit',async ev=>{
  ev.preventDefault();
  $('loginError').textContent='';
  const data=Object.fromEntries(new FormData(ev.target).entries());
  lang=data.language||lang;localStorage.setItem('nta_client_lang',lang);applyI18n();
  try{
    const res=await api('/api/client/login','POST',{username:data.username,password:data.password});
    if(res.client){await api('/api/client/language','POST',{language:lang}).catch(()=>{});}
    await showApp();
  }catch(e){$('loginError').textContent=e.message}
});

$('logoutBtn').onclick=async()=>{try{await api('/api/client/logout','POST')}finally{showLogin()}};
$('pauseBtn').onclick=async()=>{try{const r=await api('/api/client/pause','POST');if(r.view)renderRobot(r.view)}catch(e){$('robotError').textContent=e.message}};
$('resumeBtn').onclick=async()=>{try{const r=await api('/api/client/resume','POST');if(r.view)renderRobot(r.view)}catch(e){$('robotError').textContent=e.message}};

(async()=>{
  try{await api('/api/client/me');await showApp()}
  catch(_){showLogin()}
})();
