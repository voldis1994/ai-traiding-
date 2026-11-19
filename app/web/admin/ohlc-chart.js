'use strict';
/** Shared OHLC + entry-zone canvas for Admin mini + popup.
 *  Draws the LIVE info stack: swings, entry zones, KAMA/EMA, ATR, ADX legend,
 *  swing SL/TP. Popup passes {labels:true, maxCandles} for readable denser bars.
 */

function _ohlcFinite(v){
  const n=Number(v);
  return Number.isFinite(n)?n:null;
}

/** Keep Y-scale on the tape — drop unset broker 0 / wild outliers (e.g. poz TP 0). */
function _ohlcNearTape(v,anchor,span){
  const n=_ohlcFinite(v);
  if(n==null)return null;
  // Broker often sends unset SL/TP as 0 — never a real GOLD/FX level on this chart.
  if(!(n>0))return null;
  if(anchor==null||!Number.isFinite(anchor))return n;
  const band=Math.max(
    (Number.isFinite(span)&&span>0?span:Math.abs(anchor)*0.002)*10,
    Math.abs(anchor)*0.015,
    0.5);
  if(Math.abs(n-anchor)>band)return null;
  return n;
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
  const slicedOff=candles.length>maxN?candles.length-maxN:0;
  if(slicedOff)candles=candles.slice(-maxN);
  if(!candles.length){
    ctx.fillStyle='#8a9a92';ctx.font='13px Manrope,sans-serif';ctx.textAlign='center';
    ctx.fillText('Gaida OHLC sveces…',w/2,h/2);
    return;
  }

  // Base range = candles only so bars fill the viewport; overlays expand only if near.
  let candleLow=Math.min(...candles.map(c=>+c.low));
  let candleHigh=Math.max(...candles.map(c=>+c.high));
  const mid=_ohlcFinite(view.mid);
  const anchor=mid!=null?mid:(candleLow+candleHigh)/2;
  const candleSpan=Math.max(candleHigh-candleLow,Math.abs(anchor)*0.0004,0.01);
  let low=candleLow;
  let high=candleHigh;
  const expand=(raw)=>{
    const v=_ohlcNearTape(raw,anchor,candleSpan);
    if(v==null)return;
    low=Math.min(low,v);high=Math.max(high,v);
  };
  const zones=view.zones||{};
  for(const z of [zones.support,zones.resistance,zones.buy_entry,zones.sell_entry]){
    if(!z)continue;
    expand(z.lo);expand(z.hi);
  }
  const br=view.swing_brackets||{};
  for(const k of ['stop_level','profit_level','swing_hi','swing_lo','kama']){
    expand(br[k]);
  }
  for(const p of (view.open_levels||[])){
    expand(p.entry);expand(p.stop);expand(p.profit);
  }
  const ov=view.overlays||{};
  // Align overlay series to the same candle window we display.
  const sliceSeries=(arr)=>{
    if(!Array.isArray(arr)||!arr.length)return [];
    if(arr.length<=candles.length)return arr.slice();
    return arr.slice(-candles.length);
  };
  const kamaSeries=sliceSeries(ov.kama);
  const ema5Series=sliceSeries(ov.ema5);
  const ema13Series=sliceSeries(ov.ema13);
  for(const series of [kamaSeries,ema5Series,ema13Series]){
    for(const v of series)expand(v);
  }
  expand(mid);
  const atr=_ohlcFinite(ov.atr14)||_ohlcFinite((view.mtf||{}).atr14);
  if(atr!=null&&atr>0&&mid!=null){
    expand(mid-atr);expand(mid+atr);
  }

  // Tight pad so candle bodies use most of the canvas height.
  const pad=(high-low||Math.abs(high)*.001||.01)*(showLabels?.06:.10);
  low-=pad;high+=pad;
  const yPadTop=showLabels?28:16;
  const yPadBot=showLabels?36:28;
  const leftPad=showLabels?18:12;
  const rightPad=showLabels?78:56;
  const chartW=Math.max(40,w-leftPad-rightPad);
  const y=p=>yPadTop+(high-p)/(high-low)*(h-yPadTop-yPadBot);

  // Soft grid (popup).
  if(showLabels){
    ctx.strokeStyle='rgba(232,240,236,.06)';
    ctx.lineWidth=1;
    for(let k=0;k<=4;k++){
      const yy=yPadTop+(h-yPadTop-yPadBot)*k/4;
      ctx.beginPath();ctx.moveTo(leftPad,yy);ctx.lineTo(leftPad+chartW,yy);ctx.stroke();
    }
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
    ctx.fillRect(leftPad,y1,chartW,Math.max(showLabels?3:2,y2-y1));
    if(stroke){
      ctx.strokeStyle=stroke;ctx.setLineDash([]);ctx.lineWidth=showLabels?1.25:1;
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

  if(atr!=null&&mid!=null){
    ctx.fillStyle='rgba(120,160,200,.08)';
    ctx.fillRect(leftPad,y(mid+atr),chartW,Math.max(2,y(mid-atr)-y(mid+atr)));
  }

  const spacing=chartW/candles.length;
  const polyline=(series,color,width)=>{
    if(!Array.isArray(series)||!series.length)return;
    const offset=Math.max(0,candles.length-series.length);
    ctx.strokeStyle=color;ctx.lineWidth=width||1.25;ctx.setLineDash([]);
    ctx.beginPath();
    let started=false;
    for(let i=0;i<series.length;i++){
      const v=+series[i];
      if(!Number.isFinite(v))continue;
      const x=leftPad+(offset+i+.5)*spacing;
      if(!started){ctx.moveTo(x,y(v));started=true;}
      else ctx.lineTo(x,y(v));
    }
    if(started)ctx.stroke();
  };
  polyline(ema13Series,'rgba(140,160,180,.55)',1);
  polyline(ema5Series,'rgba(100,180,220,.7)',1.1);
  polyline(kamaSeries,'rgba(240,193,75,.95)',1.6);

  // Candles — fat bodies; Y already fitted to tape so height reads large.
  const bodyW=Math.max(showLabels?3.5:2.5,Math.min(showLabels?18:14,spacing*(showLabels?.82:.70)));
  candles.forEach((c,i)=>{
    const x=leftPad+(i+.5)*spacing;
    const rising=+c.close>=+c.open;
    ctx.strokeStyle=rising?'#39ff88':'#ff5c7a';
    ctx.fillStyle=ctx.strokeStyle;
    ctx.lineWidth=Math.max(1.25,Math.min(2.5,bodyW*.4));
    ctx.beginPath();ctx.moveTo(x,y(+c.high));ctx.lineTo(x,y(+c.low));ctx.stroke();
    const y1=y(+c.open),y2=y(+c.close);
    ctx.fillRect(x-bodyW/2,Math.min(y1,y2),bodyW,Math.max(2,Math.abs(y1-y2)));
  });

  // Swing pivot markers — remap i when display window is a slice.
  const mark=(list,color)=>{
    if(!Array.isArray(list))return;
    ctx.fillStyle=color;
    for(const s of list){
      if(!_ohlcFinite(s.price))continue;
      let idx=_ohlcFinite(s.i);
      if(idx!=null)idx=idx-slicedOff;
      const x=(idx!=null&&idx>=0&&idx<candles.length)
        ?leftPad+(idx+.5)*spacing
        :null;
      if(x==null)continue;
      const yy=y(+s.price);
      ctx.beginPath();ctx.arc(x,yy,2.5,0,Math.PI*2);ctx.fill();
    }
  };
  const st=view.structure||{};
  mark(st.swing_highs,'rgba(255,92,122,.85)');
  mark(st.swing_lows,'rgba(57,255,136,.85)');

  const labels=[];
  const hline=(price,color,dash,label)=>{
    const p=_ohlcNearTape(price,anchor,candleSpan);
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
      labels.push({y:y(midZ),color:'#39ff88',label:'BUY zona',price:midZ});
    }
    if(zones.sell_entry&&(!pref||pref==='SELL')){
      const z=zones.sell_entry;
      const midZ=((_ohlcFinite(z.lo)||0)+(_ohlcFinite(z.hi)||_ohlcFinite(z.lo)||0))/2;
      labels.push({y:y(midZ),color:'#ff5c7a',label:'SELL zona',price:midZ});
    }
  }

  const digits=Math.abs(high)<50?2:(Math.abs(high)<500?1:0);
  ctx.fillStyle='#8a9a92';
  ctx.font=(showLabels?'11px':'10px')+' Manrope,sans-serif';
  ctx.textAlign='left';
  for(let k=0;k<=4;k++){
    const price=high-(high-low)*k/4;
    const yy=yPadTop+(h-yPadTop-yPadBot)*k/4;
    ctx.fillText(price.toFixed(digits),w-rightPad+8,yy+4);
  }

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

  // Footer: time window (popup) + MTF stack summary.
  const mtf=view.mtf||{};
  const parts=[];
  if(showLabels){
    const last=candles[candles.length-1];
    const first=candles[0];
    const fmt=iso=>{
      if(!iso)return '';
      try{
        return new Date(iso).toLocaleTimeString('lv-LV',{hour:'2-digit',minute:'2-digit'});
      }catch(_){return ''}
    };
    parts.push(`${fmt(first.at)}–${fmt(last.at)}`);
    parts.push(`${candles.length} M1`);
  }
  if(mtf.mode||ov.mode)parts.push(`MTF ${mtf.mode||ov.mode}`);
  if(_ohlcFinite(mtf.adx||ov.adx)!=null)parts.push(`ADX ${Number(mtf.adx||ov.adx).toFixed(0)}`);
  if(_ohlcFinite(mtf.atr14||ov.atr14)!=null)parts.push(`ATR ${Number(mtf.atr14||ov.atr14).toFixed(2)}`);
  if(kamaSeries.length||ov.kama_level!=null)parts.push('KAMA');
  if(view.info_stack)parts.push(String(view.info_stack));
  if(parts.length){
    ctx.fillStyle='rgba(180,200,190,.85)';
    ctx.font='9px Manrope,sans-serif';
    ctx.textAlign='left';
    ctx.fillText(parts.join(' · '),leftPad,h-10);
  }
}

window.drawOhlcStructure=drawOhlcStructure;
