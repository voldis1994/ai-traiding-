'use strict';
/** Shared OHLC + entry-zone canvas renderer for Admin mini + popup windows. */

function drawOhlcStructure(canvas,view){
  if(!canvas||!view)return;
  const dpr=Math.min(2,window.devicePixelRatio||1);
  const rect=canvas.getBoundingClientRect();
  if(rect.width<20||rect.height<20)return;
  const w=rect.width,h=rect.height;
  canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle='rgba(8,12,14,.92)';
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
  const yPadTop=18,yPadBot=22;
  const y=p=>yPadTop+(high-p)/(high-low)*(h-yPadTop-yPadBot);
  const leftPad=14,rightPad=56;
  const chartW=Math.max(40,w-leftPad-rightPad);
  const spacing=chartW/candles.length;
  const band=(z,fill,stroke)=>{
    if(!z)return;
    const a=Number.isFinite(+z.lo)?+z.lo:null;
    const b=Number.isFinite(+z.hi)?+z.hi:a;
    if(a==null)return;
    const y1=y(Math.max(a,b)),y2=y(Math.min(a,b));
    ctx.fillStyle=fill;
    ctx.fillRect(leftPad,y1,chartW,Math.max(2,y2-y1));
    if(stroke){
      ctx.strokeStyle=stroke;ctx.setLineDash([]);ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(leftPad,y(a));ctx.lineTo(leftPad+chartW,y(a));ctx.stroke();
      if(b!=null&&b!==a){
        ctx.beginPath();ctx.moveTo(leftPad,y(b));ctx.lineTo(leftPad+chartW,y(b));ctx.stroke();
      }
    }
  };
  band(zones.resistance,'rgba(255,92,122,.14)','rgba(255,92,122,.55)');
  band(zones.support,'rgba(57,255,136,.12)','rgba(57,255,136,.5)');
  const pref=view.preferred;
  if(!pref||pref==='BUY')band(zones.buy_entry,'rgba(57,255,136,.22)',null);
  if(!pref||pref==='SELL')band(zones.sell_entry,'rgba(255,92,122,.20)',null);
  candles.forEach((c,i)=>{
    const x=leftPad+(i+.5)*spacing;
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
    ctx.beginPath();ctx.moveTo(leftPad,y(+price));ctx.lineTo(leftPad+chartW,y(+price));ctx.stroke();
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
    ctx.fillText(price.toFixed(Math.abs(high)<20?2:1),w-rightPad+6,yPadTop+(h-yPadTop-yPadBot)*k/4);
  }
}

window.drawOhlcStructure=drawOhlcStructure;
