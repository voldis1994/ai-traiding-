'use strict';
/** Shared OHLC + entry-zone canvas renderer for Admin mini + popup windows. */

function _ohlcFinite(v){
  const n=Number(v);
  return Number.isFinite(n)?n:null;
}

function drawOhlcStructure(canvas,view,opts){
  if(!canvas||!view)return;
  opts=opts||{};
  const showLabels=!!opts.labels;
  const dpr=Math.min(2.5,window.devicePixelRatio||1);
  const rect=canvas.getBoundingClientRect();
  if(rect.width<20||rect.height<20)return;
  const w=rect.width,h=rect.height;
  canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle='rgba(8,12,14,.94)';
  ctx.fillRect(0,0,w,h);

  let candles=(view.candles||[]).filter(c=>
    ['open','high','low','close'].every(k=>Number.isFinite(Number(c[k]))));
  const maxN=Math.max(20,Number(opts.maxCandles)||candles.length);
  if(candles.length>maxN)candles=candles.slice(-maxN);
  if(!candles.length){
    ctx.fillStyle='#8a9a92';ctx.font='13px Manrope,sans-serif';ctx.textAlign='center';
    ctx.fillText('Gaida OHLC sveces…',w/2,h/2);
    return;
  }

  let low=Math.min(...candles.map(c=>+c.low));
  let high=Math.max(...candles.map(c=>+c.high));
  const zones=view.zones||{};
  for(const z of [zones.support,zones.resistance,zones.buy_entry,zones.sell_entry]){
    if(!z)continue;
    const a=_ohlcFinite(z.lo),b=_ohlcFinite(z.hi);
    if(a!=null)low=Math.min(low,a);
    if(b!=null)high=Math.max(high,b);
  }
  const br=view.swing_brackets||{};
  for(const k of ['stop_level','profit_level','swing_hi','swing_lo']){
    const v=_ohlcFinite(br[k]);
    if(v!=null){low=Math.min(low,v);high=Math.max(high,v);}
  }
  for(const p of (view.open_levels||[])){
    for(const k of ['entry','stop','profit']){
      const v=_ohlcFinite(p[k]);
      if(v!=null){low=Math.min(low,v);high=Math.max(high,v);}
    }
  }
  const mid=_ohlcFinite(view.mid);
  if(mid!=null){low=Math.min(low,mid);high=Math.max(high,mid);}

  const pad=(high-low||Math.abs(high)*.001||.01)*.12;
  low-=pad;high+=pad;
  const yPadTop=showLabels?28:16;
  const yPadBot=showLabels?36:20;
  const leftPad=showLabels?18:12;
  const rightPad=showLabels?78:54;
  const chartW=Math.max(40,w-leftPad-rightPad);
  const y=p=>yPadTop+(high-p)/(high-low)*(h-yPadTop-yPadBot);

  // Soft grid.
  ctx.strokeStyle='rgba(232,240,236,.06)';
  ctx.lineWidth=1;
  for(let k=0;k<=4;k++){
    const yy=yPadTop+(h-yPadTop-yPadBot)*k/4;
    ctx.beginPath();ctx.moveTo(leftPad,yy);ctx.lineTo(leftPad+chartW,yy);ctx.stroke();
  }

  const band=(z,fill,stroke)=>{
    if(!z)return;
    const a=_ohlcFinite(z.lo);
    const b=_ohlcFinite(z.hi);
    if(a==null)return;
    const hi=b!=null?Math.max(a,b):a;
    const lo=b!=null?Math.min(a,b):a;
    const y1=y(hi),y2=y(lo);
    ctx.fillStyle=fill;
    ctx.fillRect(leftPad,y1,chartW,Math.max(3,y2-y1));
    if(stroke){
      ctx.strokeStyle=stroke;ctx.setLineDash([]);ctx.lineWidth=1.25;
      ctx.beginPath();ctx.moveTo(leftPad,y(a));ctx.lineTo(leftPad+chartW,y(a));ctx.stroke();
      if(b!=null&&b!==a){
        ctx.beginPath();ctx.moveTo(leftPad,y(b));ctx.lineTo(leftPad+chartW,y(b));ctx.stroke();
      }
    }
  };

  band(zones.resistance,'rgba(255,92,122,.12)','rgba(255,92,122,.45)');
  band(zones.support,'rgba(57,255,136,.10)','rgba(57,255,136,.4)');
  const pref=view.preferred;
  if(!pref||pref==='BUY')band(zones.buy_entry,'rgba(57,255,136,.28)',null);
  if(!pref||pref==='SELL')band(zones.sell_entry,'rgba(255,92,122,.26)',null);

  // Candles — keep readable body width even with many bars.
  const spacing=chartW/candles.length;
  const bodyW=Math.max(2.5,Math.min(14,spacing*.72));
  candles.forEach((c,i)=>{
    const x=leftPad+(i+.5)*spacing;
    const rising=+c.close>=+c.open;
    ctx.strokeStyle=rising?'#39ff88':'#ff5c7a';
    ctx.fillStyle=ctx.strokeStyle;
    ctx.lineWidth=Math.max(1,Math.min(2,bodyW*.35));
    ctx.beginPath();ctx.moveTo(x,y(+c.high));ctx.lineTo(x,y(+c.low));ctx.stroke();
    const y1=y(+c.open),y2=y(+c.close);
    ctx.fillRect(x-bodyW/2,Math.min(y1,y2),bodyW,Math.max(1.5,Math.abs(y1-y2)));
  });

  const labels=[];
  const hline=(price,color,dash,label)=>{
    const p=_ohlcFinite(price);
    if(p==null)return;
    ctx.strokeStyle=color;ctx.lineWidth=showLabels?1.6:1.25;
    ctx.setLineDash(dash||[]);
    const yy=y(p);
    ctx.beginPath();ctx.moveTo(leftPad,yy);ctx.lineTo(leftPad+chartW,yy);ctx.stroke();
    ctx.setLineDash([]);
    if(showLabels&&label)labels.push({y:yy,color,label,price:p});
  };

  if(br&&br.ready){
    hline(br.stop_level,'#ff5c7a',[5,4],'swing SL');
    hline(br.profit_level,'#39ff88',[5,4],'swing TP');
  }
  hline(mid,'#f0c14b',[],'mid');
  for(const p of (view.open_levels||[])){
    hline(p.entry,'#eef6f1',[],'entry');
    hline(p.stop,'#ff5c7a',[5,3],'poz. SL');
    hline(p.profit,'#39ff88',[5,3],'poz. TP');
  }
  if(showLabels){
    if(zones.buy_entry&&(!pref||pref==='BUY')){
      const z=zones.buy_entry;
      const midZ=((_ohlcFinite(z.lo)||0)+(_ohlcFinite(z.hi)||_ohlcFinite(z.lo)||0))/2;
      labels.push({y:y(midZ),color:'#39ff88',label:'BUY zona',price:midZ,band:true});
    }
    if(zones.sell_entry&&(!pref||pref==='SELL')){
      const z=zones.sell_entry;
      const midZ=((_ohlcFinite(z.lo)||0)+(_ohlcFinite(z.hi)||_ohlcFinite(z.lo)||0))/2;
      labels.push({y:y(midZ),color:'#ff5c7a',label:'SELL zona',price:midZ,band:true});
    }
  }

  // Price axis.
  ctx.fillStyle='#8a9a92';
  ctx.font=(showLabels?'11px':'10px')+' Manrope,sans-serif';
  ctx.textAlign='left';
  const digits=Math.abs(high)<50?2:(Math.abs(high)<500?1:0);
  for(let k=0;k<=4;k++){
    const price=high-(high-low)*k/4;
    const yy=yPadTop+(h-yPadTop-yPadBot)*k/4;
    ctx.fillText(price.toFixed(digits),w-rightPad+8,yy+4);
  }

  // In-chart labels (avoid stacking).
  if(showLabels&&labels.length){
    labels.sort((a,b)=>a.y-b.y);
    let lastY=-999;
    ctx.font='600 11px Manrope,sans-serif';
    for(const L of labels){
      let yy=L.y;
      if(yy-lastY<14)yy=lastY+14;
      lastY=yy;
      const text=`${L.label} ${L.price.toFixed(digits)}`;
      const tw=ctx.measureText(text).width+10;
      const tx=leftPad+10;
      ctx.fillStyle='rgba(8,12,14,.82)';
      ctx.fillRect(tx-4,yy-11,tw,16);
      ctx.fillStyle=L.color;
      ctx.textAlign='left';
      ctx.fillText(text,tx,yy+1);
    }
  }

  // Time hint under last candle.
  if(showLabels){
    const last=candles[candles.length-1];
    const first=candles[0];
    const fmt=iso=>{
      if(!iso)return '';
      try{
        return new Date(iso).toLocaleTimeString('lv-LV',{hour:'2-digit',minute:'2-digit'});
      }catch(_){return ''}
    };
    ctx.fillStyle='#6f8078';ctx.font='10px Manrope,sans-serif';ctx.textAlign='left';
    ctx.fillText(fmt(first.at),leftPad,h-10);
    ctx.textAlign='right';
    ctx.fillText(fmt(last.at)+' · '+candles.length+' M1',leftPad+chartW,h-10);
  }
}

window.drawOhlcStructure=drawOhlcStructure;
