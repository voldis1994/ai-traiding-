import { chromium } from '../perpsignal-desk/node_modules/playwright/index.mjs';
const b=await chromium.launch({headless:true});
const p=await b.newPage({viewport:{width:1290,height:515},deviceScaleFactor:1});
await p.goto('http://127.0.0.1:8765/admin?deskDemo=1',{waitUntil:'networkidle'});
await p.fill('input[name=username]','admin');
await p.fill('input[name=password]','admin123');
await p.click('#loginForm button[type=submit]');
await p.waitForSelector('#dash:not(.hidden)');
if(!p.url().includes('deskDemo')) await p.goto('http://127.0.0.1:8765/admin?deskDemo=1',{waitUntil:'networkidle'});
await p.waitForTimeout(3000);
const info=await p.evaluate(()=>{
  const c=document.getElementById('deskChart');
  const r=c.getBoundingClientRect();
  return {
    badgeHidden: document.getElementById('deskDemoBadge')?.classList.contains('hidden'),
    chart:{w:r.width,h:r.height,cw:c.width,ch:c.height},
    funding: document.getElementById('deskFunding')?.innerText?.slice(0,160),
    closed: document.getElementById('deskClosed')?.innerText?.slice(0,220),
    engine: document.getElementById('deskEngine')?.innerText?.slice(0,140),
    ind: document.getElementById('deskIndCharts')?.innerHTML?.slice(0,200),
    hasRsi: !!document.getElementById('deskRsiCanvas'),
  };
});
console.log(JSON.stringify(info,null,2));
await b.close();
