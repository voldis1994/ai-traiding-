'use strict';
const $=id=>document.getElementById(id);
async function api(path,method='GET',payload){
  const opts={method,credentials:'same-origin',headers:{'Content-Type':'application/json'}};
  if(payload!==undefined)opts.body=JSON.stringify(payload);
  const res=await fetch(path,opts);let body={};try{body=await res.json()}catch(_){}
  if(!res.ok)throw Error(typeof body.detail==='string'?body.detail:`HTTP ${res.status}`);
  return body;
}
function show(login){$('login').classList.toggle('hidden',!login);$('dash').classList.toggle('hidden',login)}

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
async function loadClients(){
  const data=await api('/api/admin/clients');
  const rows=$('clientRows');const pick=$('brokerClient');
  rows.innerHTML='';pick.innerHTML='';
  (data.clients||[]).forEach(c=>{
    const opt=document.createElement('option');opt.value=c.id;
    opt.textContent=`#${c.id} ${c.username} (${c.display_name})`;pick.appendChild(opt);
    const tr=document.createElement('tr');
    const broker=c.broker?`${c.broker.display_name} · ${c.broker.broker_type}`:'—';
    tr.innerHTML=`<td>${c.id}</td><td>${c.username}</td><td>${c.display_name}</td><td>${c.language}</td>
      <td>${broker}</td><td>${c.robot_running?'ON':'off'}</td><td>${c.phase||'—'}</td>
      <td><button type="button" class="btn tiny" data-start="${c.id}">Start</button>
      <button type="button" class="btn tiny" data-stop="${c.id}">Stop</button></td>`;
    rows.appendChild(tr);
  });
  rows.querySelectorAll('[data-start]').forEach(btn=>btn.onclick=async()=>{
    try{await api('/api/admin/clients/'+btn.dataset.start+'/start','POST');await loadClients()}
    catch(e){alert(e.message)}
  });
  rows.querySelectorAll('[data-stop]').forEach(btn=>btn.onclick=async()=>{
    try{await api('/api/admin/clients/'+btn.dataset.stop+'/stop','POST');await loadClients()}
    catch(e){alert(e.message)}
  });
}
async function enterDash(){show(false);await loadBrokers();await loadClients()}

$('loginForm').onsubmit=async ev=>{
  ev.preventDefault();$('loginError').textContent='';
  const data=Object.fromEntries(new FormData(ev.target).entries());
  try{await api('/api/admin/login','POST',data);await enterDash()}
  catch(e){$('loginError').textContent=e.message}
};
$('logoutBtn').onclick=async()=>{try{await api('/api/admin/logout','POST')}finally{show(true)}};
$('refreshBtn').onclick=()=>loadClients().catch(e=>alert(e.message));
$('createForm').onsubmit=async ev=>{
  ev.preventDefault();$('createMsg').textContent='';
  const data=Object.fromEntries(new FormData(ev.target).entries());
  try{
    const res=await api('/api/admin/clients','POST',data);
    $('createMsg').textContent=`Izveidots #${res.client.id} · login ${res.client.username}`;
    ev.target.reset();await loadClients();
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
    ev.target.reset();await loadClients();
  }catch(e){$('brokerMsg').textContent=e.message}
};

(async()=>{
  try{await api('/api/admin/me');await enterDash()}
  catch(_){show(true)}
})();
